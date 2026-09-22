import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from linguistic_oj import auth_config, providers
from linguistic_oj.bounded_executor_state import BoundedExecutorState
from linguistic_oj.executor_state import RecoveryRequired, executor_lock
from linguistic_oj.providers import (
    GenerationSettings,
    ModelGeneration,
    ModelIdentity,
    ProviderContractError,
    ProviderTransportError,
)
from linguistic_oj.request_submission_executor import RequestSubmissionExecutor
from linguistic_oj.sample_scheduler import ScheduledProvider
from linguistic_oj.submission_jobs import (
    QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS,
    InMemoryJobQueue,
    JobMessage,
    MockRequestPreflight,
    OutboxDispatcher,
)
from linguistic_oj.submission_store import SubmissionStore
from scripts.check_bounded_pipeline import fixture_contract

ROOT = Path(__file__).parents[1]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    if os.name == 'nt':
        monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)
    artifacts, contract = fixture_contract(ROOT, tmp_path, 2)
    store = SubmissionStore(tmp_path / 'jobs.db')

    class Queue(InMemoryJobQueue):
        delivery = None

        def receive(self):
            delivery = super().receive()
            if delivery is not None:
                self.delivery = delivery
            return delivery

    queue = Queue(contract.contract_snapshot_sha256,
                  visibility_timeout_seconds=contract.job_deadline_seconds
                  + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS)
    dispatcher = OutboxDispatcher(store, queue, contract)
    state_dir = tmp_path / 'state'
    state_dir.mkdir(mode=0o700)
    calls = []

    def generate(self, request, **kwargs):
        calls.append(request)
        return ModelGeneration(json.dumps({'tags': ['X', 'X']}))

    monkeypatch.setattr(providers.OpenAICompatibleProvider, 'generate', generate)

    def submit(name, prompt='A'):
        user = store.user_by_subject(name)
        if user is None:
            user = store.register_user(auth_subject=name, public_handle=name)
        submission = store.create_submission(user=user, idempotency_key=str(time.monotonic_ns()),
            student_prompt=prompt, contract=contract).submission
        dispatcher.dispatch_pending()
        return submission

    with executor_lock(state_dir, create=True):
        state = BoundedExecutorState.initialize(state_dir, 'e' * 64, max_inflight=2)
        slots = [{contract.challenge_id: ScheduledProvider(executor_state=state,
            challenge_id=contract.challenge_id, base_url='http://127.0.0.1:8001/v1',
            identity=ModelIdentity(**contract.evaluation_identity['model_identity']),
            settings=GenerationSettings(**contract.evaluation_identity['generation_settings']),
            timeout_seconds=contract.provider_request_timeout_seconds,
            max_response_body_bytes=contract.provider_response_body_bytes)} for _ in range(2)]
        executor = RequestSubmissionExecutor(store=store, queues={contract.challenge_id: queue},
            contracts={contract.challenge_id: contract},
            artifacts={contract.challenge_id: artifacts},
            preflights={contract.challenge_id: MockRequestPreflight(contract)}, slots=slots,
            state=state, model_capacity=2, max_jobs=4, model_healthy=lambda: True,
            history_limit=3)
        yield store, queue, contract, state, executor, submit, calls


def background(executor, *, continuous=False):
    results, errors = [], []

    def run():
        try:
            results.append(executor.run() if continuous else executor.run_round())
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    return thread, results, errors


def test_success_publishes_each_owner_result_and_empty_round_never_replays(setup):
    store, queue, _, state, executor, submit, calls = setup
    first = submit('alice', '  A\r\ne\u0301 ')
    deferred = submit('alice', 'second prompt')
    other = submit('bob', 'different prompt')
    report = executor.run_round()
    assert report['claims'] == 2 and len(calls) == 100
    assert set(report['publications']) == {first.submission_id, other.submission_id}
    assert store.owner_result(first.submission_id, other.user_id) is None
    assert store.submission_for_owner(
        deferred.submission_id, first.user_id).status.value == 'queued'
    second = executor.run_round()
    assert second['claims'] == 1 and len(calls) == 150
    assert executor.run_round()['claims'] == 0 and len(calls) == 150
    for job in (first, deferred, other):
        result = store.owner_result(job.submission_id, job.user_id)
        assert result.status.value == 'succeeded'
        assert result.result['samples_total'] == 50 and result.result['score'] == 1
    assert len(queue) == 0 and store.count_results() == 3
    state.require_clean()


def test_completed_short_job_is_published_before_slow_job_finishes(setup, monkeypatch):
    store, _, _, state, executor, submit, calls = setup
    fast = submit('alice', 'fast')
    slow = submit('bob', 'slow')
    entered, release = Event(), Event()

    def generate(self, request, **kwargs):
        calls.append(request)
        if request.student_prompt == 'slow' and not entered.is_set():
            entered.set()
            assert release.wait(15)
        return ModelGeneration('{"tags":["X","X"]}')

    monkeypatch.setattr(providers.OpenAICompatibleProvider, 'generate', generate)
    thread, _, errors = background(executor)
    try:
        assert entered.wait(10)
        until = time.monotonic() + 10
        while store.count_results() == 0 and time.monotonic() < until:
            time.sleep(.01)
        assert store.owner_result(fast.submission_id, fast.user_id).status.value == 'succeeded'
        assert store.owner_result(slow.submission_id, slow.user_id).status.value == 'running'
    finally:
        release.set()
    thread.join(30)
    assert not thread.is_alive() and not errors and store.count_results() == 2
    state.require_clean()


def test_lost_lease_stops_remaining_samples_and_old_receipt_cannot_ack_new_delivery(
    setup, monkeypatch,
):
    store, queue, _, state, executor, submit, calls = setup
    job = submit('alice')
    entered, release = Event(), Event()
    mutex = Lock()

    def generate(self, request, **kwargs):
        with mutex:
            calls.append(request)
            if len(calls) == 2:
                entered.set()
        assert release.wait(15)
        return ModelGeneration('{"tags":["X","X"]}')

    monkeypatch.setattr(providers.OpenAICompatibleProvider, 'generate', generate)
    thread, results, errors = background(executor)
    try:
        assert entered.wait(10)
        old_delivery = queue.delivery
        assert queue.nack(old_delivery)
        new_delivery = queue.receive()
        with store._connect() as connection:
            connection.execute('UPDATE submissions SET lease_token = ? WHERE id = ?',
                               ('replacement-token', job.submission_id))
            connection.commit()
    finally:
        release.set()
    thread.join(30)
    assert not thread.is_alive() and not errors
    assert len(calls) == 2 and store.count_results() == 0
    assert results[0]['publications'][job.submission_id] == 'stale_claim'
    assert store.submission_for_owner(job.submission_id, job.user_id).status.value == 'running'
    assert queue.ack(new_delivery)  # The old receipt did not delete this delivery.
    state.require_clean()


def test_lease_change_between_observation_and_commit_cannot_publish(setup, monkeypatch):
    store, _, _, state, executor, submit, calls = setup
    job = submit('alice')
    complete = store.complete_success

    def changed(claim, **kwargs):
        with store._connect() as connection:
            connection.execute('UPDATE submissions SET lease_token = ? WHERE id = ?',
                               ('new-owner', claim.submission_id))
            connection.commit()
        return complete(claim, **kwargs)

    monkeypatch.setattr(store, 'complete_success', changed)
    report = executor.run_round()
    assert len(calls) == 50 and store.count_results() == 0
    assert report['publications'][job.submission_id] == 'stale_at_publication'
    state.require_clean()


def test_expired_queued_submission_is_failed_without_a_model_call(setup):
    store, queue, _, state, executor, submit, calls = setup
    job = submit('alice')
    with store._connect() as connection:
        connection.execute('UPDATE submissions SET deadline_at = ? WHERE id = ?',
                           ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                            job.submission_id))
        connection.commit()
    assert executor.run_round()['claims'] == 0
    result = store.owner_result(job.submission_id, job.user_id)
    assert result.status.value == 'failed' and result.failure['code'] == 'JOB_DEADLINE'
    assert result.result is None and not calls and len(queue) == 0
    state.require_clean()


def test_database_deadline_fence_rejects_a_late_aggregate(setup, monkeypatch):
    import linguistic_oj.submission_store as store_module

    store, _, _, state, executor, submit, calls = setup
    job = submit('alice')
    complete = store.complete_success

    def expired(claim, **kwargs):
        database_time = datetime.fromisoformat(claim.deadline_at) + timedelta(seconds=1)
        monkeypatch.setattr(store_module, '_utc_now', lambda: database_time)
        return complete(claim, **kwargs)

    monkeypatch.setattr(store, 'complete_success', expired)
    report = executor.run_round()
    assert report['publications'][job.submission_id] == 'stale_at_publication'
    result = store.owner_result(job.submission_id, job.user_id)
    assert result.status.value == 'failed' and result.failure['code'] == 'JOB_DEADLINE'
    assert len(calls) == 50 and store.count_results() == 0
    state.require_clean()


@pytest.mark.parametrize('confirmed', [True, False])
def test_retry_waits_for_peer_requests_and_only_uses_existing_confirmed_policy(
    setup, monkeypatch, confirmed,
):
    store, queue, _, state, executor, submit, calls = setup
    job = submit('alice')
    peer, failed, release = Event(), Event(), Event()
    mutex = Lock()

    def generate(self, request, **kwargs):
        with mutex:
            calls.append(request)
            index = len(calls)
        if index == 1:
            assert peer.wait(10)
            failed.set()
            raise ProviderTransportError('fixture failure', termination_confirmed=confirmed)
        if index == 2:
            peer.set()
            assert release.wait(15)
        return ModelGeneration('{"tags":["X","X"]}')

    monkeypatch.setattr(providers.OpenAICompatibleProvider, 'generate', generate)
    thread, results, errors = background(executor)
    try:
        assert failed.wait(10)
        assert store.submission_for_owner(job.submission_id, job.user_id).status.value == 'running'
        assert len(queue) == 0 and store.count_results() == 0
    finally:
        release.set()
    thread.join(30)
    assert not thread.is_alive() and len(calls) == 2
    if confirmed:
        assert not errors and results[0]['publications'][job.submission_id] == 'requeued'
        assert len(queue) == 1
        with store._connect() as connection:
            old_deadline = connection.execute('SELECT deadline_at FROM submissions WHERE id = ?',
                                             (job.submission_id,)).fetchone()[0]
        executor.run_round()
        assert len(calls) == 52 and store.count_results() == 1
        with store._connect() as connection:
            row = connection.execute(
                'SELECT attempt_number, deadline_at FROM submissions WHERE id = ?',
                (job.submission_id,)).fetchone()
        assert tuple(row) == (2, old_deadline)
        state.require_clean()
    else:
        assert isinstance(errors[0], RecoveryRequired)
        assert store.submission_for_owner(job.submission_id, job.user_id).status.value == 'failed'
        assert store.count_results() == 0 and len(queue) == 0
        with pytest.raises(RecoveryRequired):
            executor.run_round()
        assert len(calls) == 2


def test_committed_result_survives_ack_failure_and_redelivery_does_not_regenerate(
    setup, monkeypatch,
):
    store, queue, contract, state, executor, submit, calls = setup
    job = submit('alice')
    acknowledge = queue.ack
    failed = []

    def ack(delivery):
        if not failed:
            failed.append(True)
            raise OSError('fixture ack connection failure')
        return acknowledge(delivery)

    monkeypatch.setattr(queue, 'ack', ack)
    with pytest.raises(OSError):
        executor.run_round()
    assert store.count_results() == 1 and len(calls) == 50
    state.require_clean()
    queue.publish(JobMessage(job.submission_id, contract.evaluation_identity_sha256,
                             contract.contract_snapshot_sha256))
    assert executor.run_round()['claims'] == 0
    assert store.count_results() == 1 and len(calls) == 50


def test_invalid_result_contract_fails_without_publishing_a_grade(setup, monkeypatch):
    store, _, contract, state, executor, submit, calls = setup
    job = submit('alice')

    def invalid(*args, **kwargs):
        raise ValueError('fixture result contract mismatch')

    monkeypatch.setattr(type(contract), 'owner_result', invalid)
    report = executor.run_round()
    result = store.owner_result(job.submission_id, job.user_id)
    assert report['publications'][job.submission_id] == 'failed_result_contract'
    assert result.status.value == 'failed'
    assert result.failure['code'] == 'RUNTIME_MISCONFIGURATION'
    assert result.result is None and store.count_results() == 0 and len(calls) == 50
    state.require_clean()


def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition did not become true'
        time.sleep(.01)


def test_many_empty_routes_do_not_add_one_backoff_per_route(setup, monkeypatch):
    import linguistic_oj.request_submission_executor as module

    store, _, contract, state, executor, submit, calls = setup
    submitted = submit('alice')
    route = executor._routes[contract.challenge_id]
    visited = []

    def empty(index):
        visited.append(index)
        return None, None

    executor._routes = {f'empty-{index}': SimpleNamespace(
        _receive_and_claim_attempt=lambda index=index: empty(index)) for index in range(70)}
    executor._routes[contract.challenge_id] = route
    # With an unchanged admission clock, a per-route cooldown would never reach the real queue.
    monkeypatch.setattr(module, 'monotonic', lambda: 0.)
    preflight = route._request_preflight

    def stop_after_claim(requests):
        executor._stop.set()
        preflight(requests)

    route._request_preflight = stop_after_claim
    thread, reports, errors = background(executor, continuous=True)
    thread.join(20)
    if thread.is_alive():
        executor._stop.set()
        thread.join(10)
        pytest.fail('empty routes delayed the runnable queue')
    assert not errors and visited == list(range(70))
    assert reports[0]['counts']['queue_observations'] == 71
    assert len(calls) == 50
    result = store.owner_result(submitted.submission_id, submitted.user_id)
    assert result.status.value == 'succeeded'
    state.require_clean()


def test_continuous_late_jobs_publish_and_retire_while_slow_peer_is_inflight(setup, monkeypatch):
    store, _, _, state, executor, submit, calls = setup
    slow = submit('alice', 'slow')
    entered, release = Event(), Event()
    mutex = Lock()

    def generate(self, request, **kwargs):
        with mutex:
            calls.append(request)
            block = request.student_prompt == 'slow' and not entered.is_set()
            if block:
                entered.set()
        if block:
            assert release.wait(60)
        return ModelGeneration('{"tags":["X","X"]}')

    monkeypatch.setattr(providers.OpenAICompatibleProvider, 'generate', generate)
    assert not executor.availability()
    thread, reports, errors = background(executor, continuous=True)
    try:
        assert entered.wait(10)
        assert executor.availability()
        with pytest.raises(RuntimeError, match='already running'):
            executor.run_round()
        # More lifetime jobs than max_jobs; every late result appears before the slow job ends.
        for index in range(6):
            job = submit(f'late{index}', f'  late {index}\r\n中文 e\u0301 ')
            until(lambda job=job: store.owner_result(job.submission_id, job.user_id).status.value
                  == 'succeeded')
            assert store.owner_result(slow.submission_id, slow.user_id).status.value == 'running'
        assert store.count_results() == 6
    finally:
        executor._stop.set()
        release.set()
        thread.join(30)
    assert not thread.is_alive() and not errors
    report = reports[0]
    assert report['counts']['claims'] == report['counts']['succeeded'] == 7
    assert report['peak_jobs'] <= executor._max_jobs and report['retained_jobs'] == 0
    assert len(report['recent_publications']) == report['retained_dispatches'] == 3
    assert len(calls) == 350 and store.count_results() == 7
    assert not executor.availability()
    with pytest.raises(RuntimeError, match='cannot be replayed'):
        executor.run()
    state.require_clean()


def test_continuous_health_pauses_claims_and_samples_then_recovers_without_replay(
    setup, monkeypatch,
):
    store, _, _, state, executor, submit, calls = setup
    healthy, entered, release = Event(), Event(), Event()
    monkeypatch.setattr(executor.availability, '_model_healthy', healthy.is_set)
    first = submit('alice', 'first')
    mutex = Lock()

    def generate(self, request, **kwargs):
        with mutex:
            calls.append(request)
            if len(calls) == 2:
                entered.set()
        assert release.wait(30)
        return ModelGeneration('{"tags":["X","X"]}')

    monkeypatch.setattr(providers.OpenAICompatibleProvider, 'generate', generate)
    thread, reports, errors = background(executor, continuous=True)
    try:
        time.sleep(.2)
        assert not calls and not executor.availability()
        assert store.owner_result(first.submission_id, first.user_id).status.value == 'queued'
        healthy.set()
        assert entered.wait(10)
        healthy.clear()
        second = submit('bob', 'second')
        assert not executor.availability()
        release.set()
        until(lambda: not state.snapshot()['pending'])
        time.sleep(.2)
        assert len(calls) == 2 and store.count_results() == 0
        assert store.owner_result(second.submission_id, second.user_id).status.value == 'queued'
        healthy.set()
        until(lambda: store.count_results() == 2)
    finally:
        healthy.set()
        release.set()
        executor._stop.set()
        thread.join(30)
    assert not thread.is_alive() and not errors
    assert len(calls) == 100 and reports[0]['counts']['succeeded'] == 2
    state.require_clean()


def test_continuous_stop_during_preflight_drains_claim_and_leaves_later_job_queued(
    setup, monkeypatch,
):
    store, _, contract, state, executor, submit, calls = setup
    first = submit('alice')
    queued = submit('bob')
    route = executor._routes[contract.challenge_id]
    original = route._request_preflight

    def preflight(requests):
        executor._stop.set()
        original(requests)

    monkeypatch.setattr(route, '_request_preflight', preflight)
    report = executor.run()
    assert report['counts']['claims'] == 1 and len(calls) == 50
    assert store.owner_result(first.submission_id, first.user_id).status.value == 'succeeded'
    assert store.owner_result(queued.submission_id, queued.user_id).status.value == 'queued'
    assert not executor.availability() and not executor.availability.dispatch_ready()
    state.require_clean()


@pytest.mark.parametrize('unknown', [True, False])
def test_continuous_fault_blocks_admission_until_after_peer_drain(setup, monkeypatch, unknown):
    store, _, _, state, executor, submit, calls = setup
    submit('alice')
    peer, fail, release = Event(), Event(), Event()
    mutex = Lock()

    def generate(self, request, **kwargs):
        with mutex:
            calls.append(request)
            ordinal = len(calls)
        if ordinal == 1:
            assert peer.wait(10)
            fail.set()
            if not unknown:
                raise ProviderContractError('invalid provider result')
            raise ProviderTransportError('unknown termination', termination_confirmed=False)
        peer.set()
        assert release.wait(20)
        return ModelGeneration('{"tags":["X","X"]}')

    monkeypatch.setattr(providers.OpenAICompatibleProvider, 'generate', generate)
    thread, _, errors = background(executor, continuous=True)
    try:
        assert fail.wait(10)
        until(lambda: state.dispatch_faulted or executor._fault_signal.is_set())
        assert not executor.availability()
        late = submit('bob')
        time.sleep(.2)
        assert thread.is_alive() and len(calls) == 2
        assert store.owner_result(late.submission_id, late.user_id).status.value == 'queued'
    finally:
        release.set()
        executor._stop.set()
        thread.join(30)
    assert not thread.is_alive()
    if unknown:
        assert isinstance(errors[0], RecoveryRequired)
    else:
        assert not errors
        state.require_clean()
    assert not executor.availability() and store.count_results() == 0


def test_continuous_ack_failure_closes_health_and_preserves_committed_score(setup, monkeypatch):
    store, queue, _, state, executor, submit, calls = setup
    job = submit('alice')

    def ack(delivery):
        raise OSError('fixture ack failure')

    monkeypatch.setattr(queue, 'ack', ack)
    with pytest.raises(OSError, match='ack failure'):
        executor.run()
    assert not executor.availability()
    assert store.owner_result(job.submission_id, job.user_id).status.value == 'succeeded'
    assert len(calls) == 50 and store.count_results() == 1
    state.require_clean()
