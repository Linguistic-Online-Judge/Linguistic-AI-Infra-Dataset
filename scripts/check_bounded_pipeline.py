"""Opt-in bounded executor/real-service acceptance with an explicitly fake local HTTP model.

No Qwen requests, model/service changes, live scores, SMTP or public endpoints.
The resource helpers create ownership-marked isolated PostgreSQL/Redis namespaces.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import socket
import sys
import time
import uuid
from collections import Counter
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Lock, Thread


def resources_module(root):
    spec = importlib.util.spec_from_file_location(
        'owned_bounded_resources', root / 'scripts/check_qwen_pipeline.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_contract(root, directory, capacity):
    from linguistic_oj.challenge import build_challenge
    from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256

    path = directory / 'handwritten-fifty.jsonl'
    with path.open('x', encoding='utf-8') as file:
        for index in range(50):
            file.write(json.dumps({'id': f'fixture-{index}', 'language': 'English',
                'treebank': 'BoundedFixture', 'text': f'{index:02d}', 'tasks_available': ['upos'],
                'answers': {'segmentation': list(f'{index:02d}'), 'upos': ['X', 'X']}}) + '\n')
    artifacts = build_challenge(path, language='English', treebank='BoundedFixture',
        task='upos', count=50, seed=2026, version='bounded-' + uuid.uuid4().hex)
    config = json.loads((root / 'config/mvp_evaluation_v2.json').read_text(encoding='utf-8'))
    public = artifacts.public.model_dump(mode='json')
    for key in config['catalog']:
        config['catalog'][key] = public[key]
    config['evaluation_identity'].update(challenge_id=artifacts.public.challenge_id,
        dataset_sha256=artifacts.public.dataset_sha256,
        selection_sha256=artifacts.public.selection_sha256,
        model_identity={'model': 'bounded-http-fixture', 'revision': 'a' * 40,
                        'runtime': 'fixture', 'runtime_version': '1'})
    config['limits']['worker_model_concurrency'] = capacity
    config['leaderboard_partition']['expected_sha256'] = canonical_sha256(
        config['evaluation_identity'])
    return artifacts, EvaluationContract.from_mapping(config)


def exercise_fixture(root, directory, store, queue_factory, *, capacity, lifecycle=None,
                     request_level=False):
    """Cookie-authenticated API + worker + real local HTTP; caller selects storage adapters."""
    from fastapi.testclient import TestClient

    from linguistic_oj.api import create_app
    from linguistic_oj.auth import AuthService, install_auth_routes
    from linguistic_oj.bounded_dispatch import run_bounded
    from linguistic_oj.bounded_executor_state import BoundedExecutorState, BoundedGuardedProvider
    from linguistic_oj.executor_state import executor_lock
    from linguistic_oj.providers import GenerationSettings, ModelIdentity
    from linguistic_oj.runner import _prepare_samples
    from linguistic_oj.sample_cache import VerifiedSelectionCache
    from linguistic_oj.sample_scheduler import ScheduledProvider
    from linguistic_oj.submission_jobs import (
        MockRequestPreflight,
        OutboxDispatcher,
        _SubmissionWorkerCore,
    )

    if capacity not in (2, 4):
        raise ValueError('fixture capacity must be 2 or 4')
    lifecycle = {} if lifecycle is None else lifecycle
    lifecycle['worker_stopped'] = True
    lifecycle['requests_reconciled'] = True
    artifacts, contract = fixture_contract(root, directory, capacity)
    prepared = _prepare_samples(artifacts)
    queue = queue_factory(contract)
    names = ('alice', 'bob', 'carol', 'dana')
    prompts = (' Alice A\r\ne\u0301 ', 'Alice B 中文\n', 'Bob العربية', 'Carol\tC', 'Dana\nD')
    owners = dict(zip(prompts, ('alice', 'alice', 'bob', 'carol', 'dana'), strict=True))
    labels = dict(zip(prompts, (['X', 'X'], ['X', 'NOUN'], ['NOUN', 'NOUN'],
                               ['X', 'NOUN'], ['X', 'X']), strict=True))
    records, faults, active_owners = [], [], Counter()
    bindings, submission_by_prompt = [], {}
    mutex, first_batch = Lock(), Barrier(capacity)
    active = peak = 0

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal active, peak
            counted = False
            try:
                raw = self.rfile.read(int(self.headers['Content-Length']))
                payload = json.loads(raw)
                envelope = json.loads(payload['messages'][1]['content'])
                prompt = envelope['student_prompt']
                assert self.path == '/v1/chat/completions'
                assert payload['model'] == 'bounded-http-fixture' and payload['stream'] is False
                with mutex:
                    active += 1
                    active_owners[owners[prompt]] += 1
                    counted = True
                    peak = max(peak, active)
                    records.append((prompt, envelope['input']))
                    ordinal = len(records)
                    if not request_level:
                        assert active_owners[owners[prompt]] == 1
                if request_level:
                    digest = hashlib.sha256(raw).hexdigest()
                    pending_records = state.snapshot()['pending']
                    matching = [record['request_context'] for record in pending_records.values()
                        if record.get('request_context', {}).get('request_sha256') == digest]
                    assert len(matching) == 1
                    context = matching[0]
                    position = context['sample_position']
                    assert context['submission_sha256'] == hashlib.sha256(
                        submission_by_prompt[prompt].encode()).hexdigest()
                    assert envelope['input'] == prepared[position - 1].model_input.model_dump(
                        mode='json')
                    with mutex:
                        bindings.append((prompt, position))
                    owner_jobs = store.submissions_for_owner(users[owners[prompt]], limit=20)
                    assert sum(job.status.value == 'running' for job in owner_jobs) == 1
                if ordinal <= capacity:
                    first_batch.wait(timeout=30)
                body = json.dumps({'choices': [{'message': {'content': json.dumps(
                    {'tags': labels[prompt]})}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 6}}).encode()
                with mutex:
                    active -= 1
                    active_owners[owners[prompt]] -= 1
                    counted = False
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except BaseException as error:
                faults.append(type(error).__name__)
                self.close_connection = True
            finally:
                if counted:
                    with mutex:
                        active -= 1
                        active_owners[owners[prompt]] -= 1

    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    server.daemon_threads = True
    model_thread = Thread(target=server.serve_forever, daemon=True)
    model_thread.start()
    stop = Event()
    worker_thread = None
    state = None
    state_dir = directory / 'state'
    state_dir.mkdir(mode=0o700)
    results = []
    try:
        with executor_lock(state_dir, create=True):
            state = BoundedExecutorState.initialize(state_dir, 'd' * 64, max_inflight=capacity)
            lifecycle['requests_reconciled'] = False
            cache = VerifiedSelectionCache()
            lanes = []
            for _ in range(capacity):
                provider_class = ScheduledProvider if request_level else BoundedGuardedProvider
                provider = provider_class(executor_state=state,
                    challenge_id=contract.challenge_id,
                    base_url=f'http://127.0.0.1:{server.server_port}/v1',
                    identity=ModelIdentity(**contract.evaluation_identity['model_identity']),
                    settings=GenerationSettings(**contract.evaluation_identity['generation_settings']),
                    timeout_seconds=contract.provider_request_timeout_seconds,
                    max_response_body_bytes=contract.provider_response_body_bytes)
                if request_level:
                    lanes.append({contract.challenge_id: provider})
                else:
                    worker = _SubmissionWorkerCore(store=store, queue=queue, contract=contract,
                        artifacts=artifacts, provider=provider,
                        lease_seconds=contract.job_deadline_seconds,
                        request_preflight=MockRequestPreflight(contract),
                        require_termination_confirmation=True, selection_cache=cache,
                        claim_guard=state.claim_guard)
                    lanes.append({contract.challenge_id: worker})
            if request_level:
                from linguistic_oj.request_submission_executor import RequestSubmissionExecutor

                executor = RequestSubmissionExecutor(store=store,
                    queues={contract.challenge_id: queue},
                    contracts={contract.challenge_id: contract},
                    artifacts={contract.challenge_id: artifacts},
                    preflights={contract.challenge_id: MockRequestPreflight(contract)}, slots=lanes,
                    state=state, model_capacity=capacity, max_jobs=capacity, stop=stop,
                    selection_cache=cache)
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                origin = f'http://127.0.0.1:{reserved.getsockname()[1]}'
                auth = AuthService(store, public_origin=origin, development=True,
                    development_cookie_name='loj_bounded_fixture', mailer=lambda *args: None)
                credentials = {name: secrets.token_urlsafe(24) for name in names}
                for name in names:
                    auth.provision_development_account(f'{name}@example.test', credentials[name],
                                                       name.title())
                app = create_app(store=store, dispatcher=OutboxDispatcher(store, queue, contract),
                    contract=contract, authenticate=auth.authenticate, environment='test',
                    allow_draft_submissions=True,
                    public_challenges={contract.challenge_id: artifacts.public})
                install_auth_routes(app, auth)
                headers = {'Origin': origin, 'X-LOJ-CSRF': '1'}
                with ExitStack() as clients:
                    sessions = {name: clients.enter_context(TestClient(app, base_url=origin,
                        headers=headers, client=('127.0.0.1', 50001 + index)))
                        for index, name in enumerate(names)}
                    users = {}
                    for name, client in sessions.items():
                        response = client.post('/v1/auth/login', json={
                            'email': f'{name}@example.test', 'password': credentials[name]})
                        assert response.status_code == 200
                        users[name] = response.json()['user']['user_id']
                        assert client.cookies.get(auth.cookie_name)
                    submissions = []
                    for index, prompt in enumerate(prompts):
                        name = owners[prompt]
                        client = sessions[name]
                        selected = {'X-LOJ-Expected-User': users[name],
                                    'Idempotency-Key': f'bounded-{index}'}
                        payload = {'challenge_id': contract.challenge_id, 'student_prompt': prompt}
                        response = client.post('/v1/submissions', headers=selected, json=payload)
                        assert response.status_code == 202
                        sid = response.json()['submission_id']
                        submission_by_prompt[prompt] = sid
                        replay = client.post('/v1/submissions', headers=selected, json=payload)
                        assert replay.status_code == 202 and replay.json()['submission_id'] == sid
                        assert client.get(f'/v1/submissions/{sid}/result').status_code == 409
                        other = sessions['bob' if name != 'bob' else 'carol']
                        assert other.get(f'/v1/submissions/{sid}/result').status_code == 404
                        submissions.append((sid, prompt))

                    def execute():
                        try:
                            if request_level:
                                while not stop.is_set():
                                    observation = executor.run_round()
                                    if not observation['claims']:
                                        stop.wait(.05)
                            else:
                                run_bounded(lanes, state, stop, model_capacity=capacity)
                        except BaseException as error:
                            faults.append(type(error).__name__)

                    worker_thread = Thread(target=execute)
                    lifecycle['worker_stopped'] = False
                    worker_thread.start()
                    pending = list(submissions)
                    deadline = time.monotonic() + 180
                    while pending and not faults and time.monotonic() < deadline:
                        for sid, prompt in list(pending):
                            response = sessions[owners[prompt]].get(f'/v1/submissions/{sid}/result')
                            if response.status_code == 409:
                                continue
                            assert response.status_code == 200
                            result = response.json()
                            assert result['outcome'] == 'succeeded'
                            assert result['samples_total'] == result['samples_valid'] == 50
                            assert result['score'] == labels[prompt].count('X') / 2
                            assert result['student_prompt_sha256'] == hashlib.sha256(
                                prompt.encode()).hexdigest()
                            saved = sessions[owners[prompt]].get(
                                f'/v1/submissions/{sid}/prompt').json()
                            assert saved['student_prompt'] == prompt
                            results.append({'samples': 50, 'score': result['score'],
                                'prompt_sha256': result['student_prompt_sha256']})
                            pending.remove((sid, prompt))
                        if pending:
                            time.sleep(.05)
                    assert not pending and not faults
                    stop.set()
                    worker_thread.join(30)
                    assert not worker_thread.is_alive()
                    state.require_clean()
                    assert peak == capacity and len(records) == 250
                    if hasattr(queue, '_client'):
                        assert queue._client.xpending(
                            queue.stream_name, queue._group_name)['pending'] == 0
                        assert queue._client.xlen(queue.stream_name) == 0
                        assert queue._client.hlen(queue._active_key) == 0
                        assert queue._client.hlen(queue._receipt_key) == 0
                    else:
                        assert len(queue) == 0 and not queue._inflight
                    assert Counter(prompt for prompt, _ in records) == dict.fromkeys(prompts, 50)
                    if request_level:
                        assert Counter(bindings) == {(prompt, position): 1
                            for prompt in prompts for position in range(1, 51)}
                    else:
                        baseline = [value for prompt, value in records if prompt == prompts[0]]
                        assert all([value for key, value in records if key == prompt] == baseline
                                   for prompt in prompts)
                    with store._connect() as connection:
                        counts = {table: connection.execute(
                            f'SELECT count(*) FROM {table}').fetchone()[0]
                                  for table in ('submissions', 'results', 'submission_outbox')}
                    assert counts == dict.fromkeys(counts, 5)
        return {'schema_version': 'bounded-services-fixture-v1', 'passed': True,
            'request_level': request_level,
            'capacity': capacity, 'users': 4, 'submissions': 5, 'model_fixture_calls': 250,
            'peak_model_fixture_requests': peak, 'results': results,
            'cookie_auth': True, 'idempotency_verified': True, 'owner_isolation_verified': True,
            'counts': counts, 'queue_drained': True, 'pending_requests': {},
            'real_qwen_requests': 0,
            'transport': 'inprocess-API-and-real-loopback-model-HTTP',
            'classroom_capacity_verified': False}
    finally:
        stop.set()
        if worker_thread is not None:
            worker_thread.join(150)
            if worker_thread.is_alive():
                raise RuntimeError('fixture worker is still active; preserve resources')
        lifecycle['worker_stopped'] = True
        if state is not None:
            try:
                lifecycle['requests_reconciled'] = not bool(state.snapshot()['pending'])
            except Exception:
                lifecycle['requests_reconciled'] = False
        server.shutdown()
        server.server_close()
        model_thread.join(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'output', 'postgres-socket', 'redis-socket'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--postgres-port', type=int, default=5433)
    parser.add_argument('--postgres-user', required=True)
    parser.add_argument('--postgres-database', required=True)
    parser.add_argument('--capacity', type=int, choices=(2, 4), required=True)
    parser.add_argument('--request-level', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    owned = resources_module(root)
    owned.validate_output(root, args.output)
    sys.path.insert(0, str(root / 'src'))
    os.umask(0o077)
    directory = args.output.parent / ('bounded-fixture-' + uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    cleanup = {}
    lifecycle = {'worker_stopped': True}
    isolated = {}
    parameters = owned.postgres_parameters(argparse.Namespace(postgres_url_file=None,
        postgres_socket=args.postgres_socket, postgres_port=args.postgres_port,
        postgres_user=args.postgres_user, postgres_database=args.postgres_database))
    empty_passfile = directory / 'empty.pgpass'
    with empty_passfile.open('xb'):
        pass
    empty_passfile.chmod(0o600)
    parameters['passfile'] = str(empty_passfile)
    report = {'passed': False, 'cleanup': cleanup, 'real_qwen_requests': 0}
    try:
        with ExitStack() as resources:
            postgres = owned.OwnedPostgres(parameters, cleanup)
            isolated['postgres_schema'] = postgres.schema

            def guarded_close(name, resource):
                if (not lifecycle['worker_stopped']
                        or not lifecycle.get('requests_reconciled', True)):
                    cleanup[name] = {'confirmed': False, 'preserved_for_reconciliation': True}
                    return
                resource.close()

            resources.callback(guarded_close, 'postgres', postgres)
            store = postgres.create()

            def queue_factory(contract):
                redis = owned.OwnedRedis(args.redis_socket, 15, contract, cleanup)
                isolated['redis_keys'] = redis.keys
                resources.callback(guarded_close, 'redis', redis)
                return redis.create(contract)

            report = exercise_fixture(root, directory, store, queue_factory,
                                      capacity=args.capacity, lifecycle=lifecycle,
                                      request_level=args.request_level)
            report['storage'] = 'PostgreSQL-and-Redis'
    finally:
        report['cleanup'] = cleanup
        report['isolated_resources'] = isolated
        with args.output.open('x', encoding='utf-8') as file:
            json.dump(report, file, indent=2)
        args.output.chmod(0o600)
    assert report['passed'] and all(item['confirmed'] for item in cleanup.values())
    print(json.dumps({'passed': True, 'report': str(args.output), 'cleanup': cleanup}))


if __name__ == '__main__':
    main()
