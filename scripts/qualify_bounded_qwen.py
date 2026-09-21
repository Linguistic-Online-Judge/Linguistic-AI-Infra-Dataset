"""Explicit maintenance-only full-job qualification; never activates production contracts."""

import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.request import getproxies


def profiles(root):
    return (
        ('en-a', 'English', 'en-childes-upos-v1', root / 'prompts/performance/upos-v1.txt'),
        ('en-b', 'English', 'en-childes-upos-v1', root / 'prompts/qualification/upos-b.txt'),
        ('de-a', 'German', 'de-hdt-dependency-v1', root / 'prompts/performance/dependency-v1.txt'),
        ('de-b', 'German', 'de-hdt-dependency-v1', root / 'prompts/qualification/dependency-b.txt'),
    )


def calibration_contract(base, capacity):
    from linguistic_oj.mvp_contract import EvaluationContract

    if capacity not in (1, 2, 4):
        raise ValueError('qualification capacity must be 1, 2 or 4')
    value = json.loads(base.snapshot_json)
    value['limits']['worker_model_concurrency'] = capacity
    derived = EvaluationContract.from_mapping(value)
    assert derived.evaluation_identity == base.evaluation_identity
    return derived


def submission_plan(profile_ids):
    """Use distinct repeat users and separate quality calibration from queue pressure."""
    return [{'owner': f'{profile}-r{repetition}', 'profile': profile, 'repetition': repetition}
            for repetition in (1, 2) for profile in profile_ids]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'data-root', 'tokenizer-snapshot', 'launch-evidence', 'output',
                 'postgres-socket', 'redis-socket'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--capacity', type=int, choices=(1, 2, 4), required=True)
    parser.add_argument('--model-pid', type=int)
    parser.add_argument('--postgres-user', required=True)
    parser.add_argument('--postgres-database', default='postgres')
    parser.add_argument('--postgres-port', type=int, default=5433)
    parser.add_argument('--run-real-qwen', action='store_true')
    args = parser.parse_args()
    if any(key in getproxies() for key in ('http', 'https', 'all')):
        raise ValueError('qualification rejects implicit model proxies')
    root = args.root.resolve()
    sys.path.insert(0, str(root / 'src'))
    from check_bounded_pipeline import resources_module

    from linguistic_oj.challenge import load_challenge_artifacts
    from linguistic_oj.executor_state import _write_atomic
    from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
    from linguistic_oj.providers import PromptEnvelope
    from linguistic_oj.qwen_performance import prepare_cases, verify_experiment_runtime

    os.umask(0o077)
    owned = resources_module(root)
    owned.validate_output(root, args.output)
    selected, profiles_by_id, prompt_ids = {}, {}, {}
    model = tokenizer = token_identity = None
    for profile, language, key, prompt_path in profiles(root):
        if key not in selected:
            base = EvaluationContract.from_path(
                root / 'config/evaluation_contracts/v1' / (key + '.json'))
            observed, tokenizer, token_identity, _ = verify_experiment_runtime(
                base, args.tokenizer_snapshot, args.launch_evidence, args.capacity)
            if model is not None:
                assert model == observed
            model = observed
            datasets = list((args.data_root / 'Standard_Dataset/by_language').glob(
                language + '_*.jsonl'))
            assert len(datasets) == 1
            artifacts = load_challenge_artifacts(root / 'challenges/public' / (key + '.json'),
                args.data_root / 'runtime/private/challenges' / (key + '.json'),
                dataset_path=datasets[0])
            assert artifacts.public.sample_count == 50
            selected[key] = {'base': base, 'contract': calibration_contract(base, args.capacity),
                             'artifacts': artifacts}
        item = selected[key]
        prompt = prompt_path.read_text(encoding='utf-8')
        cases, _ = prepare_cases(item['artifacts'], item['base'], tokenizer,
                                 token_identity, prompt, 50)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        assert prompt_hash not in prompt_ids
        prompt_ids[prompt_hash] = profile
        profiles_by_id[profile] = {'key': key, 'prompt': prompt, 'cases': cases,
            'prompt_sha256': prompt_hash,
            'request_set_sha256': canonical_sha256([
                PromptEnvelope.from_request(case.request).to_dict() for case in cases])}
    report = {'schema_version': 'bounded-qwen-qualification-v1',
        'prepared_at': datetime.now(UTC).isoformat(), 'capacity': args.capacity,
        'model_identity': model.to_dict(), 'planned_model_requests': 400,
        'planned_submissions': 8, 'sample_count_per_submission': 50, 'repetitions': 2,
        'profiles': {key: {field: value[field] for field in (
            'key', 'prompt_sha256', 'request_set_sha256')}
            for key, value in profiles_by_id.items()},
        'contracts': {key: {'source_sha256': item['base'].contract_snapshot_sha256,
            'calibration_sha256': item['contract'].contract_snapshot_sha256,
            'generation_settings': item['base'].evaluation_identity['generation_settings']}
            for key, item in selected.items()},
        'transport': 'inprocess-cookie-API/PostgreSQL/Redis/real-loopback-Qwen-HTTP',
        'production_scores_written': False, 'classroom_capacity_verified': False,
        'arrival_policy': 'at-most-capacity-outstanding-submissions', 'users': 8,
        'queue_pressure_test': False, 'same_prompt_repetitions_use_distinct_users': True,
        'quality_qualification_passed': False, 'rows': [], 'results': [], 'cleanup': {}}
    if not args.run_real_qwen:
        print(json.dumps({**report, 'real_model_requests_started': False}, indent=2))
        return 0
    assert args.model_pid is not None
    proc = Path('/proc') / str(args.model_pid)
    assert proc.stat().st_uid == os.getuid()
    argv = (proc / 'cmdline').read_bytes().decode().strip('\0').split('\0')
    for flag, expected in (('--host', '127.0.0.1'), ('--port', '8001'),
                           ('--served-model-name', model.model),
                           ('--max-num-seqs', str(args.capacity))):
        assert flag in argv and argv[argv.index(flag) + 1] == expected
    process_ticks = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]

    from fastapi.testclient import TestClient

    from linguistic_oj.api import create_app
    from linguistic_oj.auth import AuthService, install_auth_routes
    from linguistic_oj.bounded_dispatch import run_bounded
    from linguistic_oj.bounded_executor_state import BoundedExecutorState, BoundedGuardedProvider
    from linguistic_oj.executor_state import executor_lock
    from linguistic_oj.providers import GenerationSettings, ProviderContractError
    from linguistic_oj.qwen_runtime import QwenTokenizerPreflight
    from linguistic_oj.runner import evaluate_raw_response
    from linguistic_oj.sample_cache import VerifiedSelectionCache
    from linguistic_oj.submission_jobs import OutboxDispatcher, _SubmissionWorkerCore

    directory = args.output.parent / ('qualification-c' + str(args.capacity))
    directory.mkdir(mode=0o700)
    state_dir = directory / 'state'
    state_dir.mkdir(mode=0o700)
    stop, fatal, records_lock = Event(), Event(), Lock()
    tokenizer_lock = Lock()
    rows, completed, contexts = report['rows'], report['results'], {}
    model_attempts = 0
    worker_thread = None
    worker_stopped = True
    state = None
    errors = []
    cleanup = report['cleanup']
    resources_proof = {}
    previous_signals = {sig: signal.signal(sig, lambda *unused: stop.set())
                        for sig in (signal.SIGTERM, signal.SIGINT)}

    class QualifiedProvider(BoundedGuardedProvider):
        submission_id = None
        sample_position = 0

        def generate(self, request, /, *, timeout_seconds=None):
            nonlocal model_attempts
            with records_lock:
                if fatal.is_set() or model_attempts >= 400:
                    raise ProviderContractError('qualification stopped or request budget exhausted')
                model_attempts += 1
            self.sample_position += 1
            context = contexts[self.submission_id]
            case = profiles_by_id[context['profile']]['cases'][self.sample_position - 1]
            assert request == case.request
            started = time.monotonic()
            try:
                generation = super().generate(request, timeout_seconds=timeout_seconds)
            except BaseException:
                fatal.set()
                stop.set()
                raise
            elapsed = time.monotonic() - started
            outcome = evaluate_raw_response(sample=case.prepared.dataset_sample,
                manifest_sample=case.prepared.manifest_sample, task=request.task,
                model_input=request.model_input, raw_response=generation.raw_text)
            with records_lock:
                rows.append({'submission_id': self.submission_id, **context,
                    'sample_position': self.sample_position, 'request_seconds': elapsed,
                    'input_tokens': generation.prompt_token_count,
                    'output_tokens': generation.generated_token_count,
                    'output_sha256': hashlib.sha256(generation.raw_text.encode()).hexdigest(),
                    'format_error': outcome.error_code.value if outcome.error_code else None,
                    'score_statistics': asdict(outcome.score) if outcome.score else None})
            return generation

    class QualifiedWorker(_SubmissionWorkerCore):
        def _evaluate_claim(self, delivery, claim):
            self._provider.submission_id = claim.submission_id
            self._provider.sample_position = 0
            return super()._evaluate_claim(delivery, claim)

    def checkpoint(status):
        with records_lock:
            snapshot = {**report, 'status': status, 'rows': list(rows), 'results': list(completed),
                'model_generation_attempts': model_attempts,
                'recorded_at': datetime.now(UTC).isoformat(),
                'experiment_pending': None if state is None else state.snapshot()['pending']}
        _write_atomic(args.output, snapshot)

    def safe_close(name, resource):
        if not worker_stopped or (state is not None and state.snapshot()['pending']):
            cleanup[name] = {'confirmed': False, 'preserved_for_reconciliation': True}
        else:
            resource.close()

    try:
        parameters = owned.postgres_parameters(argparse.Namespace(postgres_url_file=None,
            postgres_socket=args.postgres_socket, postgres_port=args.postgres_port,
            postgres_user=args.postgres_user, postgres_database=args.postgres_database))
        empty_passfile = directory / 'empty.pgpass'
        with empty_passfile.open('xb'):
            pass
        empty_passfile.chmod(0o600)
        parameters['passfile'] = str(empty_passfile)
        with executor_lock(state_dir, create=True), ExitStack() as resources:
            binding = canonical_sha256({'kind': report['schema_version'], 'capacity': args.capacity,
                'model': model.to_dict(), 'endpoint': 'http://127.0.0.1:8001/v1'})
            state = BoundedExecutorState.initialize(state_dir, binding, max_inflight=args.capacity)
            postgres = owned.OwnedPostgres(parameters, cleanup)
            resources.callback(safe_close, 'postgres', postgres)
            store = postgres.create()
            resources_proof['postgres'] = {'schema': postgres.schema, 'proof': postgres.proof}
            queues, dispatchers = {}, {}
            redis_resources = []
            for key, item in selected.items():
                per_cleanup = {}
                redis = owned.OwnedRedis(args.redis_socket, 15, item['contract'], per_cleanup)
                cleanup[key] = per_cleanup
                resources.callback(safe_close, key, redis)
                queues[key] = redis.create(item['contract'])
                redis_resources.append(redis)
                dispatchers[key] = OutboxDispatcher(store, queues[key], item['contract'])
                resources_proof[key] = {'keys': redis.keys, 'marker': redis.marker}
            _write_atomic(directory / 'resources.private.json', resources_proof)
            cache = VerifiedSelectionCache()
            lanes = []
            for _ in range(args.capacity):
                lane = {}
                for key, item in selected.items():
                    contract = item['contract']
                    provider = QualifiedProvider(executor_state=state, challenge_id=key,
                        base_url='http://127.0.0.1:8001/v1', identity=model,
                        settings=GenerationSettings(**contract.evaluation_identity['generation_settings']),
                        timeout_seconds=contract.provider_request_timeout_seconds,
                        max_response_body_bytes=contract.provider_response_body_bytes)
                    assert model.model in provider.served_model_ids()
                    preflight = QwenTokenizerPreflight(contract, tokenizer, token_identity)

                    def locked_preflight(requests, selected=preflight):
                        with tokenizer_lock:
                            selected(requests)

                    lane[key] = QualifiedWorker(store=store, queue=queues[key], contract=contract,
                        artifacts=item['artifacts'], provider=provider,
                        lease_seconds=contract.job_deadline_seconds,
                        request_preflight=locked_preflight,
                        require_termination_confirmation=True, selection_cache=cache,
                        claim_guard=state.claim_guard)
                lanes.append(lane)
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                origin = f'http://127.0.0.1:{reserved.getsockname()[1]}'
                auth = AuthService(store, public_origin=origin, development=True,
                    development_cookie_name='loj_bounded_qualification', mailer=lambda *args: None)
                jobs = submission_plan(profiles_by_id)
                passwords = {job['owner']: secrets.token_urlsafe(24) for job in jobs}
                for owner, password in passwords.items():
                    auth.provision_development_account(owner + '@example.test', password,
                                                       owner.replace('-', '').title())
                app = create_app(store=store, dispatcher=dispatchers,
                    contract={key: value['contract'] for key, value in selected.items()},
                    public_challenges={key: value['artifacts'].public
                                       for key, value in selected.items()},
                    authenticate=auth.authenticate, environment='test',
                    allow_draft_submissions=True)
                install_auth_routes(app, auth)
                with ExitStack() as clients:
                    sessions = {owner: clients.enter_context(TestClient(app, base_url=origin,
                        headers={'Origin': origin, 'X-LOJ-CSRF': '1'},
                        client=('127.0.0.1', 51000 + index)))
                        for index, owner in enumerate(passwords)}
                    users = {}
                    for owner, client in sessions.items():
                        login = client.post('/v1/auth/login', json={
                            'email': owner + '@example.test', 'password': passwords[owner]})
                        assert login.status_code == 200
                        users[owner] = login.json()['user']['user_id']
                    started_batch = time.monotonic()
                    report['submissions'] = contexts
                    checkpoint('running')

                    def execute():
                        try:
                            run_bounded(lanes, state, stop, model_capacity=args.capacity)
                        except BaseException as error:
                            errors.append(type(error).__name__)
                            fatal.set()

                    worker_thread = Thread(target=execute)
                    worker_stopped = False
                    worker_thread.start()
                    waiting, pending = list(jobs), set()
                    deadline = time.monotonic() + 4200
                    try:
                        while ((waiting or pending) and not fatal.is_set()
                               and time.monotonic() < deadline):
                            current_ticks = (proc / 'stat').read_text().rsplit(
                                ')', 1)[1].split()[19]
                            assert current_ticks == process_ticks
                            while waiting and len(pending) < args.capacity and not fatal.is_set():
                                job = waiting.pop(0)
                                details = profiles_by_id[job['profile']]
                                # Publish and register the observer context before any worker claim.
                                with state.claim_guard():
                                    response = sessions[job['owner']].post(
                                        '/v1/submissions', headers={
                                        'X-LOJ-Expected-User': users[job['owner']],
                                        'Idempotency-Key': job['owner']}, json={
                                            'challenge_id': details['key'],
                                            'student_prompt': details['prompt']})
                                    assert response.status_code == 202
                                    sid = response.json()['submission_id']
                                    contexts[sid] = dict(job)
                                    pending.add(sid)
                            for sid in tuple(pending):
                                context = contexts[sid]
                                response = sessions[context['owner']].get(
                                    f'/v1/submissions/{sid}/result')
                                if response.status_code == 409:
                                    continue
                                assert response.status_code == 200
                                result = response.json()
                                completed.append({'submission_id': sid, **context,
                                    'result': result,
                                    'final_readable_seconds': time.monotonic() - started_batch})
                                pending.remove(sid)
                                if result['outcome'] == 'succeeded':
                                    assert result['samples_total'] == 50
                            checkpoint('running')
                            if waiting or pending:
                                time.sleep(1)
                        assert not pending and not waiting and not fatal.is_set() and not errors
                    finally:
                        stop.set()
                        worker_thread.join(1500)
                        worker_stopped = not worker_thread.is_alive()
                    assert worker_stopped
                    state.require_clean()
                    assert len(completed) == 8 and len(rows) <= model_attempts <= 400
                    all_scores = all(item['result']['outcome'] == 'succeeded' for item in completed)
                    if all_scores:
                        assert len(rows) == model_attempts == 400
                    report['all_final_scores_returned'] = all_scores
                    report['all_terminal_readable_seconds'] = max(
                        result['final_readable_seconds'] for result in completed)
                    report['all_final_readable_seconds'] = (
                        report['all_terminal_readable_seconds'] if all_scores else None)
                    report['executor_drained_seconds'] = time.monotonic() - started_batch
                    for redis in redis_resources:
                        assert redis.client.xpending(
                            redis.keys[0], 'submission-workers-v1')['pending'] == 0
                        assert redis.client.hlen(redis.keys[1]) == 0
                        assert redis.client.hlen(redis.keys[2]) == 0
                    report['model_process_unchanged'] = True
                    report['status'] = 'completed' if all_scores else 'completed_with_failures'
        checkpoint(report['status'])
        print(json.dumps({'status': report['status'], 'capacity': args.capacity,
                          'completed_requests': len(rows), 'report': str(args.output)}))
        return 0
    except BaseException as error:
        stop.set()
        report['error_type'] = type(error).__name__
        checkpoint('failed_or_blocked')
        raise
    finally:
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
