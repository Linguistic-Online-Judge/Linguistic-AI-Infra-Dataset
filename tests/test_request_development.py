import asyncio
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from linguistic_oj import auth_config, qwen_development, request_development
from linguistic_oj.bounded_executor_state import BoundedExecutorState
from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.executor_state import ExecutorBusy, RecoveryRequired, executor_lock
from linguistic_oj.submission_jobs import QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS, InMemoryJobQueue
from linguistic_oj.submission_store import SubmissionStore
from scripts.check_request_development import exercise, prepare_fixture
from scripts.prepare_request_development import prepare

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def permissions(monkeypatch):
    if os.name == 'nt':
        monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)


@pytest.fixture
def profile_fixture(tmp_path):
    return prepare_fixture(ROOT, tmp_path, max_jobs=4)


def test_profile_preserves_all_frozen_fields_and_instance_marker(profile_fixture):
    args, marker, profile, _ = profile_fixture
    sources = load_challenge_contract_registry(args.root, args.registry).contracts
    marker_before = (args.state_dir / 'instance.json').read_bytes()
    for key, source in sources.items():
        expected = json.loads(source.snapshot_json)
        expected['limits']['worker_model_concurrency'] = 32
        assert json.loads(profile.contracts[key].snapshot_json) == expected
        assert profile.contracts[key].evaluation_identity == source.evaluation_identity
    binding = request_development.initialize_state(args.state_dir, profile)
    child = args.state_dir / request_development.STATE_CHILD
    with executor_lock(child):
        state = BoundedExecutorState(child, binding)
        assert state.max_inflight == 32
        with pytest.raises(ExecutorBusy), executor_lock(child):
            pass
    with pytest.raises(FileExistsError):
        request_development.initialize_state(args.state_dir, profile)
    assert (args.state_dir / 'instance.json').read_bytes() == marker_before
    assert binding == request_development.state_binding(profile, marker)
    with pytest.raises(ValueError):
        request_development.state_binding(profile, {**marker, 'instance': 'b' * 32})
    with executor_lock(child):
        state.begin('fixture-interrupted')
    with executor_lock(child), pytest.raises(RecoveryRequired):
        BoundedExecutorState(child, binding).require_clean()
    with pytest.raises(RecoveryRequired):
        request_development.require_legacy_safe(args.state_dir)


@pytest.mark.parametrize('change', ['deadline', 'generation', 'source', 'missing', 'boolean'])
def test_profile_rejects_silent_policy_or_identity_changes(profile_fixture, change):
    args, _, _, _ = profile_fixture
    sources = load_challenge_contract_registry(args.root, args.registry).contracts
    document = json.loads(args.execution_profile.read_text())
    key = next(iter(document['contracts']))
    entry = document['contracts'][key]
    if change == 'deadline':
        entry['execution_contract']['job_policy']['job_deadline_seconds'] += 1
    elif change == 'generation':
        entry['execution_contract']['evaluation_identity']['generation_settings']['seed'] += 1
    elif change == 'source':
        entry['source_contract_sha256'] = 'e' * 64
    elif change == 'missing':
        del document['contracts'][key]
    else:
        document['request_slots'] = True
    args.execution_profile.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        request_development.load_profile(args.execution_profile, sources)


def test_transition_refuses_old_queued_work_and_does_not_rewrite_it(profile_fixture, tmp_path):
    args, _, profile, _ = profile_fixture
    store = SubmissionStore(tmp_path / 'transition.db')
    sources = load_challenge_contract_registry(args.root, args.registry).contracts
    contract = next(iter(sources.values()))
    user = store.register_user(auth_subject='fixture', public_handle='Fixture')
    job = store.create_submission(user=user, idempotency_key='old', student_prompt='old prompt',
                                   contract=contract).submission
    with pytest.raises(ValueError, match='drain'):
        request_development.require_compatible_outstanding(store, profile.contracts)
    request_development.require_compatible_outstanding(store, sources)
    assert store.owner_result(job.submission_id, user.user_id).status.value == 'queued'
    assert store.count_results() == 0


def test_offline_templates_keep_unsafe_restart_codes_and_do_not_activate(tmp_path):
    settings = json.loads((ROOT / 'config/request_development.example.json').read_text())
    output = tmp_path / 'prepared'
    report = prepare(ROOT, settings, output)
    assert report['request_slots'] == 32 and report['max_active_jobs'] == 4
    assert report['contracts'] == 70 and not report['services_started']
    unit = (output / 'linguistic-oj-request-development.service').read_text()
    assert 'RestartPreventExitStatus=75 78' in unit and 'TimeoutStopSec=infinity' in unit
    assert 'SendSIGKILL=no' in unit and '--execution-profile' in unit
    assert '--initialize' not in unit and '--initialize' not in (
        output / 'initialize-command.txt').read_text()
    with pytest.raises(FileExistsError):
        prepare(ROOT, settings, output)


def test_cancelled_async_wait_does_not_abandon_a_live_request_thread():
    async def check():
        entered, release = Event(), Event()

        def work():
            entered.set()
            assert release.wait(5)

        task = asyncio.create_task(asyncio.to_thread(work))
        await asyncio.to_thread(entered.wait, 5)
        drain = asyncio.create_task(qwen_development._drain_task(task))
        await asyncio.sleep(.01)
        drain.cancel()
        await asyncio.sleep(.02)
        assert not drain.done() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await drain
        assert task.done()

    asyncio.run(check())


def test_runtime_unexpected_exit_is_not_reported_as_clean_stop():
    class Resources:
        closed = False

        def close(self):
            self.closed = True

    resources = Resources()
    stop = Event()
    executor = SimpleNamespace(run=lambda: {}, availability=lambda *args: True)
    runtime = request_development.RequestDevelopmentRuntime(executor, None, resources, stop)
    with pytest.raises(RuntimeError, match='unexpectedly'):
        runtime.run()
    runtime.close()
    assert resources.closed


def test_wrong_live_capacity_fails_before_generation_and_releases_every_resource(
    profile_fixture, tmp_path, monkeypatch,
):
    from linguistic_oj import providers, qwen_runtime
    from linguistic_oj.qwen_runtime import QwenRuntimeAttestationError
    from scripts.check_request_development import FixtureTokenizer

    args, marker, profile, _ = profile_fixture
    store = SubmissionStore(tmp_path / 'wrong-capacity.db')
    request_development.initialize_state(args.state_dir, profile)
    evidence = json.loads(args.launch_evidence.read_text())
    evidence['max_num_seqs'] = 1
    args.launch_evidence.write_text(json.dumps(evidence))
    monkeypatch.setattr(qwen_runtime, 'load_huggingface_tokenizer', lambda path: FixtureTokenizer())
    monkeypatch.setattr(qwen_development, 'prepare_store', lambda *a, **k: (store, marker))
    monkeypatch.setattr(providers.OpenAICompatibleProvider, 'served_model_ids',
                        lambda self: pytest.fail('capacity mismatch must precede metadata HTTP'))
    queues = []

    class Queue(InMemoryJobQueue):
        closed = False

        def __init__(self, **kwargs):
            super().__init__(kwargs['routing_key'])
            queues.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(qwen_development, 'RedisJobQueue', Queue)
    with pytest.raises(QwenRuntimeAttestationError, match='concurrency'):
        qwen_development.build_qwen_development(args)
    assert len(queues) == 22 and all(queue.closed for queue in queues)
    with executor_lock(args.state_dir / request_development.STATE_CHILD):
        pass
    assert store.count_submissions() == 0


@pytest.mark.parametrize('uncertain', [False, True])
def test_workbench_failure_closes_admission_and_requests_supervised_shutdown(
    profile_fixture, tmp_path, monkeypatch, uncertain,
):
    from contextlib import ExitStack

    from fastapi.testclient import TestClient

    args, marker, _, _ = profile_fixture
    store = SubmissionStore(tmp_path / 'lifecycle.db')
    monkeypatch.setattr(qwen_development, 'prepare_store', lambda *a, **k: (store, marker))
    queues = []

    class Queue(InMemoryJobQueue):
        closed = False

        def __init__(self, **kwargs):
            super().__init__(kwargs['routing_key'])
            queues.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(qwen_development, 'RedisJobQueue', Queue)

    def failed():
        raise RecoveryRequired() if uncertain else RuntimeError('fixture storage outage')

    stop, shutdown = Event(), Event()
    executor = SimpleNamespace(run=failed, availability=lambda *args: False)
    runtime = request_development.RequestDevelopmentRuntime(executor,
        SimpleNamespace(dispatch_faulted=uncertain), ExitStack(), stop)
    monkeypatch.setattr(qwen_development, 'build_runtime', lambda **kwargs: runtime)
    app = qwen_development.build_qwen_development(args)
    app.state.request_executor_shutdown = shutdown.set
    with TestClient(app, base_url='http://127.0.0.1:8090') as client:
        assert shutdown.wait(5)
        assert client.get('/health/ready').status_code == 503
        assert all(not item['runtime_available'] for item in client.get('/v1/challenges').json())
    assert app.state.executor_exit_code == (75 if uncertain else 70)
    assert all(queue.closed for queue in queues) and not runtime._running.is_set()


@pytest.mark.parametrize('max_jobs', [4, 8, 16])
def test_actual_private_workbench_with_mixed_full_jobs(tmp_path, max_jobs):
    store = SubmissionStore(tmp_path / 'fixture.db')

    class Queue(InMemoryJobQueue):
        def health_check(self):
            pass

        def close(self):
            pass

    def queue_factory(contract):
        return Queue(contract.contract_snapshot_sha256,
            visibility_timeout_seconds=contract.job_deadline_seconds
            + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS)

    report = exercise(ROOT, tmp_path, store, queue_factory, max_jobs=max_jobs)
    assert report['passed'] and report['real_qwen_calls'] == 0
    assert report['fixture_model_calls'] == 800 and store.count_results() == 16
