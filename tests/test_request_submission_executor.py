import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from linguistic_oj import auth_config, providers
from linguistic_oj.bounded_executor_state import BoundedExecutorState
from linguistic_oj.executor_state import RecoveryRequired, executor_lock
from linguistic_oj.providers import (
    GenerationSettings,
    ModelGeneration,
    ModelIdentity,
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
            state=state, model_capacity=2, max_jobs=4)
        yield store, queue, contract, state, executor, submit, calls


def background(executor):
    results, errors = [], []

    def run():
        try:
            results.append(executor.run_round())
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
