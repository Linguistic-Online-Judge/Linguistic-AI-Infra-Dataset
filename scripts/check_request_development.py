"""Private-workbench integration fixture: real HTTP, explicit fake tokenizer/model, full50.

The caller supplies owned SQLite or PostgreSQL/Redis adapters. No school model request.
"""

import hashlib
import json
import time
from collections import Counter
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from linguistic_oj import qwen_development, qwen_runtime, request_development
from linguistic_oj.challenge import LANGUAGE_CODES, build_challenge, write_challenge
from linguistic_oj.mvp_contract import canonical_sha256
from linguistic_oj.qwen_runtime import TokenizerIdentity
from linguistic_oj.responses import TaskType
from linguistic_oj.runtime_availability import LocalModelProbe


class FixtureTokenizer:
    chat_template = 'EXPLICIT TOKENIZER FIXTURE: {{ messages }}'

    def encode(self, text, *, add_special_tokens):
        return list(range(len(text)))

    def apply_chat_template(self, conversation, **kwargs):
        return list(range(100))


def prepare_fixture(root, directory, *, max_jobs):
    source, data, state = (directory / name for name in ('source', 'data', 'state'))
    for path in (source / 'config/contracts', data / 'Standard_Dataset/by_language', state):
        path.mkdir(parents=True, mode=0o700)
    snapshot = directory / 'tokenizer'
    snapshot.mkdir(mode=0o700)
    (snapshot / 'tokenizer_config.json').write_text(json.dumps(
        {'chat_template': FixtureTokenizer.chat_template}), encoding='utf-8')
    (snapshot / 'tokenizer.json').write_text('{}', encoding='utf-8')
    original = json.loads((root / 'config/mvp_evaluation_v2.json').read_text())
    token_identity = TokenizerIdentity.from_snapshot(snapshot,
        repository='Qwen/Qwen3.5-9B',
        revision=original['evaluation_identity']['model_identity']['revision'])
    entries, routes = [], {}
    for language in LANGUAGE_CODES:
        dataset = data / 'Standard_Dataset/by_language' / f'{language}_fixture.jsonl'
        rows = [{'id': f'{language}-{i}', 'language': language, 'treebank': 'RequestDevFixture',
            'text': f'{i:02d}', 'tasks_available': [task.value for task in TaskType],
            'answers': {'segmentation': list(f'{i:02d}'), 'upos': ['X', 'X'],
                'xpos': ['MOCK', 'MOCK'], 'transliteration': list(f'{i:02d}'),
                'dependency': [[1, f'{i:02d}'[0], 0, 'ROOT', 'root'],
                               [2, f'{i:02d}'[1], 1, f'{i:02d}'[0], 'dep']]}}
            for i in range(50)]
        dataset.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        tasks = list(TaskType) if language == 'English' else [TaskType.UPOS]
        for task in tasks:
            artifacts = build_challenge(dataset, language=language, treebank='RequestDevFixture',
                task=task.value, count=50, seed=2026, version='request-dev-fixture-v1')
            write_challenge(artifacts, public_dir=source / 'challenges/public',
                            private_dir=data / 'runtime/private/challenges')
            public = artifacts.public.model_dump(mode='json')
            value = json.loads(json.dumps(original))
            for key in value['catalog']:
                value['catalog'][key] = public[key]
            for key in ('challenge_id', 'dataset_sha256', 'selection_sha256', 'task',
                        'response_schema_version', 'scorer_version', 'aggregation_version'):
                value['evaluation_identity'][key] = public[key]
            value['evaluation_identity']['tokenizer_identity'] = token_identity.to_dict()
            value['leaderboard_partition']['expected_sha256'] = canonical_sha256(
                value['evaluation_identity'])
            key = artifacts.public.challenge_id
            path = f'config/contracts/{key}.json'
            (source / path).write_text(json.dumps(value), encoding='utf-8')
            entries.append({'public_descriptor_path': f'challenges/public/{key}.json',
                            'evaluation_contract_path': path})
            routes[language, task.value] = key
    registry_path = Path('config/registry.json')
    (source / registry_path).write_text(json.dumps(
        {'schema_version': 'challenge-contract-registry-v1', 'entries': entries}), encoding='utf-8')
    registry, _ = qwen_development.load_development_catalog(source, data, registry_path)
    instance = 'a' * 32
    marker = {'kind': qwen_development.INSTANCE_KIND, 'instance': instance,
        'owner': 'explicit-fixture', 'database': 'loj_dev18_' + instance[:16],
        'contracts': {key: c.contract_snapshot_sha256 for key, c in registry.contracts.items()}}
    path = state / 'instance.json'
    path.write_text(json.dumps(marker), encoding='utf-8')
    path.chmod(0o600)
    profile_path = directory / 'profile.json'
    request_development.write_profile(profile_path, registry.contracts, max_jobs=max_jobs)
    profile = request_development.load_profile(profile_path, registry.contracts)
    evidence = directory / 'launch.json'
    evidence.write_text(json.dumps({'schema_version': 'linguistic-oj-vllm-launch-v1',
        'model_snapshot_path': str(snapshot.resolve()), 'max_model_len': 4096, 'max_num_seqs': 32,
        'runtime_version': original['evaluation_identity']['model_identity']['runtime_version'],
        'language_model_only': True}), encoding='utf-8')
    evidence.chmod(0o600)
    args = SimpleNamespace(root=source, data_root=data, state_dir=state, registry=registry_path,
        postgres_socket=directory, postgres_port=5433, redis_socket=directory / 'fake-redis',
        initialize=False, tokenizer_snapshot=snapshot, launch_evidence=evidence,
        port=8090, execution_profile=profile_path)
    return args, marker, profile, routes


def exercise(root, directory, store, queue_factory, *, max_jobs=4, lifecycle=None):
    lifecycle = {} if lifecycle is None else lifecycle
    lifecycle.update(worker_stopped=True, requests_reconciled=True)
    args, marker, profile, routes = prepare_fixture(root, directory, max_jobs=max_jobs)
    healthy, first, release = Event(), Event(), Event()
    healthy.set()
    first_batch = Barrier(32)
    mutex = Lock()
    records, correlations, errors, active, peak = [], [], [], 0, 0
    app = None

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({'data': [{'id': 'Qwen/Qwen3.5-9B'}]}).encode()
            self.send_response(200 if healthy.is_set() else 503)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            nonlocal active, peak
            counted = False
            try:
                body = self.rfile.read(int(self.headers['Content-Length']))
                envelope = json.loads(json.loads(body)['messages'][1]['content'])
                prompt, task, model_input = (envelope[key] for key in
                                              ('student_prompt', 'task', 'input'))
                pending = app.state.request_execution.state.snapshot()['pending']
                digest = hashlib.sha256(body).hexdigest()
                matches = [r['request_context'] for r in pending.values()
                           if r['request_context']['request_sha256'] == digest]
                assert len(matches) == 1
                with mutex:
                    active += 1
                    counted = True
                    peak = max(peak, active)
                    records.append((prompt, task, envelope['language']))
                    correlations.append((matches[0]['submission_sha256'],
                                         matches[0]['sample_position']))
                    ordinal = len(records)
                if ordinal == 1:
                    first.set()
                if ordinal <= 32:
                    first_batch.wait(30)
                if ordinal == 1:
                    assert release.wait(120)
                if task == 'segmentation':
                    answer = {'tokens': list(model_input['text'])}
                elif task in ('upos', 'xpos'):
                    tag = 'NOUN' if 'wrong' in prompt else 'X' if task == 'upos' else 'MOCK'
                    answer = {'tags': [tag] * len(model_input['tokens'])}
                elif task == 'transliteration':
                    answer = {'transliterations': model_input['tokens']}
                else:
                    answer = {'arcs': [{'token_id': token['token_id'],
                        'head_id': token['token_id'] - 1,
                        'deprel': 'root' if token['token_id'] == 1 else 'dep'}
                        for token in model_input['tokens']]}
                payload = json.dumps({'choices': [{'message': {'content': json.dumps(answer)},
                                      'finish_reason': 'stop'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except BaseException as error:
                errors.append(type(error).__name__)
                self.close_connection = True
            finally:
                if counted:
                    with mutex:
                        active -= 1

    class Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True

    server = Server(('127.0.0.1', 0), Model)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    queues, results, bindings = [], [], []

    def make_queue(**kwargs):
        contract = next(c for c in profile.contracts.values()
                        if c.contract_snapshot_sha256 == kwargs['routing_key'])
        queue = queue_factory(contract)
        queues.append(queue)
        return queue

    try:
        with ExitStack() as patches:
            patches.enter_context(patch.object(request_development, 'BASE_URL', base + '/v1'))
            patches.enter_context(patch.object(qwen_runtime, 'load_huggingface_tokenizer',
                                               lambda path: FixtureTokenizer()))
            patches.enter_context(patch.object(qwen_development, 'prepare_store',
                                               lambda *args, **kwargs: (store, marker)))
            patches.enter_context(patch.object(qwen_development, 'RedisJobQueue', make_queue))
            patches.enter_context(patch.object(qwen_development, 'LocalModelProbe',
                lambda *args: LocalModelProbe(base, 'Qwen/Qwen3.5-9B', ttl_seconds=.01)))
            request_development.initialize_state(args.state_dir, profile)
            app = qwen_development.build_qwen_development(args)
            lifecycle.update(worker_stopped=False, requests_reconciled=False)
            with ExitStack() as clients:
                primary = clients.enter_context(TestClient(app, base_url='http://127.0.0.1:8090'))
                auth = qwen_development.AuthService(store, public_origin='http://127.0.0.1:8090',
                    development=True, development_cookie_name='loj_qwen_dev_session',
                    mailer=lambda *args: None)
                until = time.monotonic() + 15
                while primary.get('/health/ready').status_code != 200 and time.monotonic() < until:
                    time.sleep(.02)
                assert primary.get('/health/ready').status_code == 200
                assert len(primary.get('/v1/challenges').json()) == 22
                tasks = [('English', 'dependency'), ('Chinese', 'upos'),
                         ('English', 'segmentation'),
                         ('English', 'xpos'), ('English', 'transliteration')]
                # One TestClient owns the app lifespan. Other cookie jars do not restart it.
                for index in range(16):
                    name = f'User{index}'
                    email, password = f'user{index}@example.test', 'fixture-password-2026'
                    auth.provision_development_account(email, password, name)
                    client = TestClient(app, base_url='http://127.0.0.1:8090',
                        headers={'Origin': 'http://127.0.0.1:8090', 'X-LOJ-CSRF': '1'})
                    clients.callback(client.close)
                    response = client.post('/v1/auth/login', json={'email': email,
                                                                  'password': password})
                    assert response.status_code == 200
                    user_id = response.json()['user']['user_id']
                    language, task = tasks[index % len(tasks)]
                    prompt = (f'  owner-{index} 中文\r\ne\u0301 '
                              + ('wrong' if task == 'upos' else 'right'))
                    bindings.append((client, user_id, routes[language, task], prompt))
                submitted = []
                try:
                    for index, (client, user_id, key, prompt) in enumerate(bindings):
                        response = client.post('/v1/submissions', headers={
                            'X-LOJ-Expected-User': user_id, 'Idempotency-Key': f'fixture-{index}'},
                            json={'challenge_id': key, 'student_prompt': prompt})
                        assert response.status_code == 202
                        submitted.append((response.json()['submission_id'], client, prompt))
                        if index == 0:
                            assert first.wait(30)
                    pending = list(submitted)
                    until = time.monotonic() + 180
                    while pending and time.monotonic() < until:
                        for item in list(pending):
                            sid, client, prompt = item
                            response = client.get(f'/v1/submissions/{sid}/result')
                            if response.status_code == 409:
                                continue
                            assert response.status_code == 200
                            result = response.json()
                            assert result['outcome'] == 'succeeded'
                            assert result['samples_total'] == 50
                            assert result['score'] == (0 if 'wrong' in prompt else 1)
                            assert result['student_prompt_sha256'] == hashlib.sha256(
                                prompt.encode()).hexdigest()
                            results.append(sid)
                            pending.remove(item)
                            if sid != submitted[0][0] and not release.is_set():
                                assert primary.get(
                                    f'/v1/submissions/{submitted[0][0]}/result').status_code == 401
                                assert submitted[0][1].get(
                                    f'/v1/submissions/{submitted[0][0]}/result').status_code == 409
                                release.set()
                        if pending:
                            time.sleep(.05)
                    assert not pending and not errors and release.is_set()
                    healthy.clear()
                    time.sleep(.02)
                    assert primary.get('/health/ready').status_code == 503
                    catalog = primary.get('/v1/challenges').json()
                    assert all(not c['runtime_available'] for c in catalog)
                    assert submitted[0][1].get(
                        f'/v1/submissions/{submitted[0][0]}/result').status_code == 200
                    healthy.set()
                finally:
                    release.set()
            app.state.request_execution.state.require_clean()
            assert app.state.executor_exit_code == 0
            assert len(records) == 800 and peak == 32
            assert Counter(correlations) == {(hashlib.sha256(sid.encode()).hexdigest(), pos): 1
                for sid, *_ in submitted for pos in range(1, 51)}
            assert Counter(prompt for prompt, *_ in records) == dict.fromkeys(
                [b[3] for b in bindings], 50)
            assert len({task for _, task, _ in records}) == 5
            assert store.outstanding_contract_hashes() == set()
            for queue in queues:
                if hasattr(queue, '_client'):
                    assert queue._client.xlen(queue.stream_name) == 0
                    assert queue._client.xpending(
                        queue.stream_name, queue._group_name)['pending'] == 0
                    assert queue._client.hlen(queue._active_key) == 0
                    assert queue._client.hlen(queue._receipt_key) == 0
                else:
                    assert len(queue) == 0 and not queue._inflight
            lifecycle.update(worker_stopped=True, requests_reconciled=True)
        return {'passed': True, 'request_slots': 32, 'max_active_jobs': max_jobs,
            'catalog_languages': 18, 'catalog_routes': 22, 'users': 16, 'submissions': 16,
            'samples_per_submission': 50, 'fixture_model_calls': 800,
            'peak_fixture_requests': peak, 'real_qwen_calls': 0,
            'short_result_before_slow_peer': True, 'shared_health_verified': True,
            'production_started': False}
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        if app is not None:
            app.state.close_qwen_resources()
            lifecycle['worker_stopped'] = not app.state.request_execution._running.is_set()
            lifecycle['requests_reconciled'] = not bool(
                app.state.request_execution.state.snapshot()['pending'])
