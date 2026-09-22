import hashlib
import json
import os
import socket
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest

from linguistic_oj import auth_config
from linguistic_oj.bounded_executor_state import BoundedExecutorState
from linguistic_oj.challenge import build_challenge
from linguistic_oj.executor_state import RecoveryRequired, executor_lock
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.providers import (
    DeterministicMockProvider,
    GenerationSettings,
    ModelGeneration,
    ModelIdentity,
    ProviderContractError,
)
from linguistic_oj.responses import TaskType
from linguistic_oj.runner import JobDeadline, run_challenge
from linguistic_oj.sample_scheduler import (
    PreparedJob,
    SampleScheduler,
    ScheduledProvider,
    prepare_job,
)
from linguistic_oj.submission_jobs import MockRequestPreflight

ROOT = Path(__file__).parents[1]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    if os.name == 'nt':
        monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)
    path = tmp_path / 'fifty.jsonl'
    with path.open('w', encoding='utf-8') as file:
        for index in range(50):
            file.write(json.dumps({'id': f'sample-{index}', 'language': 'English',
                'treebank': 'RequestFixture', 'text': f'{index:02d}', 'tasks_available': ['upos'],
                'answers': {'segmentation': list(f'{index:02d}'),
                            'upos': ['NOUN', 'VERB'] if index % 2 == 0
                            else ['ADJ', 'NOUN']}}) + '\n')
    artifacts = build_challenge(path, language='English', treebank='RequestFixture', task='upos',
                                count=50, seed=2026, version='request-level-fixture-v1')
    config = json.loads((ROOT / 'config/mvp_evaluation_v2.json').read_text())
    public = artifacts.public.model_dump(mode='json')
    for key in config['catalog']:
        config['catalog'][key] = public[key]
    config['evaluation_identity'].update(challenge_id=artifacts.public.challenge_id,
        dataset_sha256=artifacts.public.dataset_sha256,
        selection_sha256=artifacts.public.selection_sha256,
        model_identity={'model': 'fixture', 'revision': 'a' * 40,
                        'runtime': 'fixture', 'runtime_version': '1'})
    config['leaderboard_partition']['expected_sha256'] = canonical_sha256(
        config['evaluation_identity'])
    state_dir = tmp_path / 'requests'
    state_dir.mkdir(mode=0o700)
    return SimpleNamespace(artifacts=artifacts, config=config, state_dir=state_dir)


def raw_answer(prompt, tokens):
    number = int(''.join(tokens))
    if prompt.startswith('C') and number == 7:
        return '{bad-json'
    tags = (['X', 'X'] if prompt.startswith('B') else
            ['NOUN', 'VERB'] if number % 2 == 0 else ['ADJ', 'NOUN'])
    return json.dumps({'tags': tags})


class SerialFixture:
    def generate(self, request, *, timeout_seconds=None):
        return ModelGeneration(raw_answer(request.student_prompt, request.model_input.tokens))


@contextmanager
def model_server(fixture, before_reply=None, first_batch=None):
    records, faults = [], []
    mutex = Lock()
    active = peak = 0
    barrier = Barrier(first_batch) if first_batch else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal active, peak
            counted = False
            try:
                body = self.rfile.read(int(self.headers['Content-Length']))
                payload = json.loads(body)
                envelope = json.loads(payload['messages'][1]['content'])
                prompt, tokens = envelope['student_prompt'], envelope['input']['tokens']
                digest = hashlib.sha256(body).hexdigest()
                # Read the durable file under the shared lock; Windows cannot replace an open
                # file while an unrelated reader denies delete sharing. Linux is deployment target.
                pending = fixture.ledger.snapshot()['pending']
                matched = [record['request_context'] for record in pending.values()
                           if record.get('request_context', {}).get('request_sha256') == digest]
                assert len(matched) == 1  # Every fixture prompt/input is distinct.
                context = matched[0]
                with mutex:
                    active += 1
                    peak = max(peak, active)
                    counted = True
                    records.append((prompt, tuple(tokens), context, body))
                    ordinal = len(records)
                if barrier and ordinal <= first_batch:
                    barrier.wait(timeout=15)
                if before_reply:
                    before_reply(prompt, tokens, context, self)
                response = json.dumps({'id': 'wrong-owner-id-must-be-ignored', 'choices': [
                    {'message': {'content': raw_answer(prompt, tokens)}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 6}}).encode()
                with mutex:
                    active -= 1
                    counted = False
                self.send_response(200)
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as error:
                faults.append(error)
                self.close_connection = True
            finally:
                if counted:
                    with mutex:
                        active -= 1

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f'http://127.0.0.1:{server.server_port}/v1', records=records,
                              faults=faults, peak=lambda: peak)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def setup(fixture, server, *, capacity=4, stop=None, per_job_limit=None):
    config = json.loads(json.dumps(fixture.config))
    config['limits']['worker_model_concurrency'] = capacity
    contract = EvaluationContract.from_mapping(config)
    state = BoundedExecutorState.initialize(fixture.state_dir, 'b' * 64, max_inflight=capacity)
    fixture.ledger = state
    slots = [{contract.challenge_id: ScheduledProvider(executor_state=state,
        challenge_id=contract.challenge_id, base_url=server.url,
        identity=ModelIdentity(**contract.evaluation_identity['model_identity']),
        settings=GenerationSettings(**contract.evaluation_identity['generation_settings']),
        timeout_seconds=contract.provider_request_timeout_seconds,
        max_response_body_bytes=contract.provider_response_body_bytes)} for _ in range(capacity)]
    scheduler = SampleScheduler(slots, state, model_capacity=capacity, stop=stop,
                                 per_job_limit=per_job_limit)

    def job(key, prompt, *, owner=None, deadline=None):
        return prepare_job(submission_id=key, owner_id=owner or key, contract=contract,
            artifacts=fixture.artifacts, student_prompt=prompt,
            deadline=deadline or JobDeadline(datetime.now(UTC) + timedelta(minutes=3)),
            request_preflight=MockRequestPreflight(contract))

    return scheduler, state, job


def background(scheduler):
    result, errors = [], []

    def execute():
        try:
            result.append(scheduler.run())
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=execute)
    thread.start()
    return thread, result, errors


def test_full_50_out_of_order_matches_serial_scoring_and_preserves_prompts(fixture):
    slow, release = Event(), Event()

    def before(prompt, tokens, context, handler):
        if prompt.startswith('A') and context['sample_position'] == 1:
            slow.set()
            assert release.wait(15)

    with model_server(fixture, before, first_batch=4) as server, executor_lock(
        fixture.state_dir, create=True
    ):
        scheduler, state, prepare = setup(fixture, server)
        prompts = ('A  中文\r\ne\u0301 ', 'B العربية\n', 'C\tformat-error')
        jobs = [prepare(str(index), prompt) for index, prompt in enumerate(prompts)]
        baseline = {job.submission_id: run_challenge(fixture.artifacts, SerialFixture(),
                    student_prompt=job.requests[0].student_prompt) for job in jobs}
        for job in jobs:
            scheduler.add_job(job)
        thread, result, errors = background(scheduler)
        try:
            assert slow.wait(10)
            until = time.monotonic() + 10
            while scheduler.snapshot()['0']['samples_completed'] == 0 and time.monotonic() < until:
                time.sleep(.01)
            partial = scheduler.snapshot()['0']
            assert partial['samples_completed'] > 0 and partial['result'] is None
        finally:
            release.set()
        thread.join(60)
        assert not thread.is_alive() and not errors and not server.faults
        assert server.peak() == 4 and len(server.records) == 150
        for job in jobs:
            outcome = result[0][job.submission_id]
            assert outcome['status'] == 'succeeded' and outcome['samples_completed'] == 50
            assert outcome['result'] == baseline[job.submission_id]
            observed = [item for item in server.records
                        if item[0] == job.requests[0].student_prompt]
            assert len(observed) == 50
            assert {item[2]['sample_position'] for item in observed} == set(range(1, 51))
            for _, _, context, _ in observed:
                sample = job.samples[context['sample_position'] - 1]
                assert context['submission_sha256'] == hashlib.sha256(
                    job.submission_id.encode()).hexdigest()
                assert context['sample_id_sha256'] == hashlib.sha256(
                    sample.manifest_sample.sample_id.encode()).hexdigest()
        assert result[0]['2']['result'].samples_invalid == 1
        state.require_clean()
        with pytest.raises(RuntimeError):
            scheduler.run()


def test_one_job_borrows_idle_slots_and_late_job_gets_next_free_slot(fixture):
    initial, newcomer, release_one, release_all = Event(), Event(), Event(), Event()
    entered = []
    lock = Lock()

    def before(prompt, tokens, context, handler):
        if prompt == 'A' and context['sample_position'] <= 4:
            with lock:
                entered.append(context['sample_position'])
                if len(entered) == 4:
                    initial.set()
            if context['sample_position'] == 1:
                assert release_one.wait(15)
            else:
                assert release_all.wait(15)
        if prompt == 'B':
            newcomer.set()

    with model_server(fixture, before) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, state, prepare = setup(fixture, server)
        scheduler.add_job(prepare('a', 'A'))
        later = prepare('b', 'B')
        thread, result, errors = background(scheduler)
        try:
            assert initial.wait(10)
            assert server.peak() == 4
            scheduler.add_job(later)
            release_one.set()
            assert newcomer.wait(10)
            assert scheduler.dispatch_order[4] == ('b', 1)
        finally:
            release_one.set()
            release_all.set()
        thread.join(60)
        assert not thread.is_alive() and not errors and not server.faults
        assert all(value['status'] == 'succeeded' for value in result[0].values())
        assert len(server.records) == 100
        state.require_clean()


def test_normal_stop_rejects_new_jobs_but_finishes_admitted_full_job(fixture):
    entered, release, stop = Event(), Event(), Event()

    def before(prompt, tokens, context, handler):
        if context['sample_position'] == 1:
            entered.set()
            assert release.wait(15)

    with model_server(fixture, before) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, state, prepare = setup(fixture, server, capacity=2, stop=stop)
        scheduler.add_job(prepare('a', 'A'))
        later = prepare('b', 'B')
        thread, result, errors = background(scheduler)
        try:
            assert entered.wait(10)
            stop.set()
            with pytest.raises(RuntimeError):
                scheduler.add_job(later)
        finally:
            release.set()
        thread.join(60)
        assert not thread.is_alive() and not errors and not server.faults
        assert result[0]['a']['samples_completed'] == 50 and len(server.records) == 50
        state.require_clean()


def test_serial_per_job_control_uses_same_request_path_with_one_inflight(fixture):
    with model_server(fixture) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, state, prepare = setup(fixture, server, capacity=4, per_job_limit=1)
        scheduler.add_job(prepare('a', 'A'))
        result = scheduler.run()
        assert result['a']['status'] == 'succeeded'
        assert server.peak() == 1 and len(server.records) == 50
        state.require_clean()


def test_uncertain_disconnect_stops_all_new_requests_and_never_publishes_partial_scores(fixture):
    other_entered, release = Event(), Event()

    def before(prompt, tokens, context, handler):
        if prompt == 'A':
            assert other_entered.wait(10)
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
        else:
            other_entered.set()
            assert release.wait(15)

    with model_server(fixture, before, first_batch=2) as server, executor_lock(
        fixture.state_dir, create=True
    ):
        scheduler, state, prepare = setup(fixture, server, capacity=2)
        scheduler.add_job(prepare('a', 'A'))
        scheduler.add_job(prepare('b', 'B'))
        thread, result, errors = background(scheduler)
        try:
            assert other_entered.wait(10)
            until = time.monotonic() + 10
            while not state.snapshot()['blocked'] and time.monotonic() < until:
                time.sleep(.01)
            assert state.snapshot()['blocked'] and thread.is_alive()
        finally:
            release.set()
        thread.join(30)
        assert not thread.is_alive() and not errors and len(server.records) == 2
        assert all(value['result'] is None for value in result[0].values())
        assert all(value['status'] == 'failed' for value in result[0].values())
        with pytest.raises(RecoveryRequired):
            BoundedExecutorState(fixture.state_dir, 'b' * 64).require_dispatchable()
        assert len(state.snapshot()['pending']) == 2


def test_expired_job_has_no_calls_or_zero_score_and_does_not_block_other_job(fixture):
    now = [datetime.now(UTC)]
    deadline = JobDeadline(now[0] + timedelta(seconds=1), clock=lambda: now[0])
    with model_server(fixture) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, state, prepare = setup(fixture, server, capacity=2)
        expired = prepare('expired', 'A', deadline=deadline)
        scheduler.add_job(expired)
        scheduler.add_job(prepare('live', 'B'))
        now[0] += timedelta(seconds=2)
        result = scheduler.run()
        assert result['expired']['error'] == 'JOB_DEADLINE'
        assert result['expired']['result'] is None and result['expired']['samples_dispatched'] == 0
        assert not server.faults, server.faults
        assert result['live']['status'] == 'succeeded', result
        assert Counter(prompt for prompt, *_ in server.records) == {'B': 50}
        state.require_clean()


def test_admission_rejects_duplicate_ids_same_owner_and_failed_preflight(fixture):
    with model_server(fixture) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, _, prepare = setup(fixture, server, capacity=2)
        first = prepare('a', 'A', owner='owner')
        scheduler.add_job(first)
        with pytest.raises(ValueError, match='duplicate'):
            scheduler.add_job(first)
        with pytest.raises(ValueError, match='owner'):
            scheduler.add_job(prepare('b', 'B', owner='owner'))
        with pytest.raises(ValueError):
            prepare_job(submission_id='bad', owner_id='bad', contract=first.contract,
                artifacts=fixture.artifacts, student_prompt='A', deadline=first.deadline,
                request_preflight=lambda requests: (_ for _ in ()).throw(ValueError('budget')))
        assert server.records == []


def test_correlation_context_is_durable_and_wrong_body_hash_cannot_send(fixture):
    with model_server(fixture) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, state, prepare = setup(fixture, server, capacity=2)
        job = prepare('a', 'A')
        provider = scheduler._slots[0][job.contract.challenge_id]
        context = {'submission_sha256': 'a' * 64, 'sample_id_sha256': 'b' * 64,
                   'sample_position': 1, 'request_sha256': 'c' * 64}
        with pytest.raises(ProviderContractError):
            provider.generate_sample(job.requests[0], context=context, timeout_seconds=5)
        assert not state.snapshot()['pending'] and not server.records
        with pytest.raises(ValueError):
            state.begin('fixture', request_context={**context, 'sample_position': True})
        operation = state.begin('fixture', request_context=context)
        context['sample_position'] = 2
        pending = state.snapshot()['pending']
        assert pending[operation]['request_context']['sample_position'] == 1
        proof = fixture.state_dir.parent / 'proof.json'
        proof.write_text(json.dumps({'binding_sha256': 'b' * 64,
            'pending_request_ids': sorted(pending), 'all_prior_requests_terminated': True,
            'confirmed_by': 'fixture', 'evidence_reference': 'intent-only fixture, no HTTP sent'}))
        proof.chmod(0o600)
        restarted = BoundedExecutorState(fixture.state_dir, 'b' * 64)
        restarted.recover_all(canonical_sha256(pending), proof)
        restarted.require_clean()
        audit = json.loads(next((fixture.state_dir / 'recoveries').glob('*.json')).read_text())
        assert audit['pending'] == pending


def test_factory_cannot_be_bypassed_and_response_binding_mismatch_fails_closed(
    fixture, monkeypatch,
):
    with pytest.raises(TypeError):
        PreparedJob('a', 'owner', None, None, (), (), None)
    with model_server(fixture) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, state, prepare = setup(fixture, server, capacity=2)
        scheduler.add_job(prepare('a', 'A'))
        evaluate = scheduler._evaluate

        def wrong_key(*args):
            key, position, outcome = evaluate(*args)
            return 'another-submission', position, outcome

        monkeypatch.setattr(scheduler, '_evaluate', wrong_key)
        result = scheduler.run()
        assert result['a']['result'] is None and result['a']['error'] == 'EXECUTION_FAILED'
        assert len(server.records) <= 2
        state.require_clean()


def test_deadline_expiring_in_flight_prevents_aggregate(fixture):
    now = [datetime.now(UTC)]
    deadline = JobDeadline(now[0] + timedelta(seconds=5), clock=lambda: now[0])

    def before(*args):
        now[0] += timedelta(seconds=10)

    with model_server(fixture, before) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, state, prepare = setup(fixture, server, capacity=1)
        scheduler.add_job(prepare('a', 'A', deadline=deadline))
        result = scheduler.run()
        assert len(server.records) == 1
        assert result['a']['result'] is None and result['a']['error'] == 'JOB_DEADLINE'
        state.require_clean()


def test_explicit_abort_drains_sent_calls_without_finishing_unsent_samples(fixture):
    entered, release = Event(), Event()
    barrier = Barrier(2, action=entered.set)

    def before(*args):
        barrier.wait(timeout=10)
        assert release.wait(15)

    with model_server(fixture, before) as server, executor_lock(fixture.state_dir, create=True):
        scheduler, state, prepare = setup(fixture, server, capacity=2)
        scheduler.add_job(prepare('a', 'A'))
        thread, result, errors = background(scheduler)
        try:
            assert entered.wait(10)
            scheduler.abort()
        finally:
            release.set()
        thread.join(30)
        assert not thread.is_alive() and not errors and len(server.records) == 2
        assert result[0]['a']['result'] is None
        assert result[0]['a']['error'] == 'EXECUTOR_BLOCKED'
        state.require_clean()


@pytest.mark.parametrize('task', list(TaskType))
def test_original_aggregation_is_preserved_for_all_task_types(fixture, task):
    data = fixture.state_dir.parent / 'five-tasks.jsonl'
    with data.open('w', encoding='utf-8') as file:
        for index in range(50):
            file.write(json.dumps({'id': str(index), 'language': 'English', 'treebank': 'AllTasks',
                'text': 'AB', 'tasks_available': [kind.value for kind in TaskType],
                'answers': {'segmentation': ['A', 'B'], 'upos': ['X', 'X'],
                    'xpos': ['MOCK', 'MOCK'],
                    'dependency': [[1, 'A', 0, 'ROOT', 'root'], [2, 'B', 1, 'A', 'dep']],
                    'transliteration': ['A', 'B']}}) + '\n')
    artifacts = build_challenge(data, language='English', treebank='AllTasks', task=task,
                                count=50, seed=2026, version='all-tasks-fixture')
    config = json.loads(json.dumps(fixture.config))
    public = artifacts.public.model_dump(mode='json')
    for key in config['catalog']:
        config['catalog'][key] = public[key]
    config['evaluation_identity'].update(challenge_id=artifacts.public.challenge_id,
        dataset_sha256=artifacts.public.dataset_sha256,
        selection_sha256=artifacts.public.selection_sha256,
        task=task.value, response_schema_version=artifacts.public.response_schema_version)
    config['limits']['worker_model_concurrency'] = 4
    config['leaderboard_partition']['expected_sha256'] = canonical_sha256(
        config['evaluation_identity'])
    contract = EvaluationContract.from_mapping(config)
    mock = DeterministicMockProvider()

    class MockScheduled(ScheduledProvider):
        def _generate_guarded(self, request, *, timeout_seconds=None, request_context=None):
            operation = self.executor_state.begin(self.challenge_id,
                                                  request_context=request_context)
            result = mock.generate(request)
            self.executor_state.finish(operation)
            return result

    with executor_lock(fixture.state_dir, create=True):
        state = BoundedExecutorState.initialize(fixture.state_dir, 'b' * 64, max_inflight=4)
        providers = [{contract.challenge_id: MockScheduled(executor_state=state,
            challenge_id=contract.challenge_id, base_url='http://127.0.0.1:8001/v1',
            identity=ModelIdentity(**contract.evaluation_identity['model_identity']),
            settings=GenerationSettings(**contract.evaluation_identity['generation_settings']),
            timeout_seconds=contract.provider_request_timeout_seconds,
            max_response_body_bytes=contract.provider_response_body_bytes)} for _ in range(4)]
        scheduler = SampleScheduler(providers, state, model_capacity=4)
        scheduler.add_job(prepare_job(submission_id='fixture', owner_id='owner', contract=contract,
            artifacts=artifacts, student_prompt='Return JSON.',
            deadline=JobDeadline(datetime.now(UTC) + timedelta(minutes=3)),
            request_preflight=MockRequestPreflight(contract)))
        result = scheduler.run()['fixture']
        assert result['status'] == 'succeeded' and result['samples_completed'] == 50
        assert result['result'] == run_challenge(artifacts, mock, student_prompt='Return JSON.')
        state.require_clean()
