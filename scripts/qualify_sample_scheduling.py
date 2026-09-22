"""Maintenance-only model-request-layer comparison, not website/classroom acceptance."""

import argparse
import hashlib
import json
import os
import signal
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from urllib.request import getproxies

PHASES = (('serial-1', 1), ('shared-1', 4), ('shared-2', 4), ('serial-2', 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'data-root', 'tokenizer-snapshot', 'launch-evidence', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--model-pid', type=int)
    parser.add_argument('--run-real-qwen', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root / 'src'))
    from check_bounded_pipeline import resources_module
    from qualify_bounded_qwen import calibration_contract

    from linguistic_oj.challenge import load_challenge_artifacts
    from linguistic_oj.executor_state import _write_atomic
    from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
    from linguistic_oj.providers import PromptEnvelope
    from linguistic_oj.qwen_performance import verify_experiment_runtime
    from linguistic_oj.qwen_runtime import QwenTokenizerPreflight
    from linguistic_oj.runner import JobDeadline, _prepare_samples
    from linguistic_oj.sample_cache import VerifiedSelectionCache
    from linguistic_oj.sample_scheduler import prepare_job

    if any(key in getproxies() for key in ('http', 'https', 'all')):
        raise ValueError('implicit model proxies are not supported')
    os.umask(0o077)
    resources_module(root).validate_output(root, args.output)
    selected, model = {}, None
    cache = VerifiedSelectionCache()
    for label, language, key, prompt in (
        ('en', 'English', 'en-childes-upos-v1', 'upos-v1.txt'),
        ('de', 'German', 'de-hdt-dependency-v1', 'dependency-v1.txt'),
    ):
        base = EvaluationContract.from_path(
            root / 'config/evaluation_contracts/v1' / (key + '.json'))
        observed, tokenizer, token_identity, _ = verify_experiment_runtime(
            base, args.tokenizer_snapshot, args.launch_evidence, 4 if args.run_real_qwen else 1)
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
        contract = calibration_contract(base, 4)
        preflight = QwenTokenizerPreflight(contract, tokenizer, token_identity)
        text = (root / 'prompts/performance' / prompt).read_text(encoding='utf-8')
        _prepare_samples(artifacts, selection_cache=cache)
        job = prepare_job(submission_id='preflight-' + label, owner_id=label,
            contract=contract, artifacts=artifacts, student_prompt=text,
            deadline=JobDeadline(datetime.now(UTC) + timedelta(
                seconds=contract.job_deadline_seconds)),
            request_preflight=preflight, selection_cache=cache)
        selected[label] = {'base': base, 'contract': contract, 'artifacts': artifacts,
            'preflight': preflight, 'prompt': text, 'profile': {
                'source_contract_sha256': base.contract_snapshot_sha256,
                'calibration_contract_sha256': contract.contract_snapshot_sha256,
                'prompt_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'request_set_sha256': canonical_sha256([
                    PromptEnvelope.from_request(request).to_dict() for request in job.requests]),
                'generation_settings': contract.evaluation_identity['generation_settings']}}
    report = {'schema_version': 'sample-scheduling-study-v1',
        'prepared_at': datetime.now(UTC).isoformat(), 'model_identity': model.to_dict(),
        'profiles': {label: item['profile'] for label, item in selected.items()},
        'model_capacity': 4, 'planned_requests': 400, 'phases': [],
        'plan': [{'phase': name, 'per_job_limit': limit, 'jobs': 2, 'samples_per_job': 50}
                 for name, limit in PHASES], 'queue_database_browser_tested': False,
        'quality_qualification_passed': False, 'classroom_capacity_verified': False}
    if not args.run_real_qwen:
        print(json.dumps({**report, 'model_requests_sent': 0,
                          'capacity4_attested': False}, indent=2))
        return 0
    assert args.model_pid is not None
    proc = Path('/proc') / str(args.model_pid)
    assert proc.stat().st_uid == os.getuid()
    argv = (proc / 'cmdline').read_bytes().decode().strip('\0').split('\0')
    for key, value in (('--host', '127.0.0.1'), ('--port', '8001'),
                       ('--served-model-name', model.model), ('--max-num-seqs', '4')):
        assert key in argv and argv[argv.index(key) + 1] == value
    ticks = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]

    from linguistic_oj.bounded_executor_state import BoundedExecutorState
    from linguistic_oj.executor_state import executor_lock
    from linguistic_oj.providers import GenerationSettings, ProviderContractError
    from linguistic_oj.sample_scheduler import SampleScheduler, ScheduledProvider

    state_dir = args.output.parent / 'sample-request-state'
    state_dir.mkdir(mode=0o700)
    stop = Event()
    previous = {sig: signal.signal(sig, lambda *unused: stop.set())
                for sig in (signal.SIGINT, signal.SIGTERM)}
    requests_started = 0
    records_lock = Lock()
    state = None

    def save(status):
        with records_lock:
            document = json.loads(json.dumps(report))
            document.update(status=status, generation_attempts=requests_started,
                            recorded_at=datetime.now(UTC).isoformat())
        document['pending'] = None if state is None else state.snapshot()['pending']
        _write_atomic(args.output, document)

    try:
        with executor_lock(state_dir, create=True):
            binding = canonical_sha256({'kind': report['schema_version'],
                'model': model.to_dict(), 'profiles': report['profiles'], 'endpoint': 'http://127.0.0.1:8001/v1'})
            state = BoundedExecutorState.initialize(state_dir, binding, max_inflight=4)
            save('running')
            for phase_name, per_job_limit in PHASES:
                if stop.is_set():
                    report['status'] = 'stopped_between_phases'
                    break
                state.require_clean()
                started = monotonic()
                admitted = datetime.now(UTC)
                jobs = [prepare_job(submission_id=phase_name + '-' + label, owner_id=label,
                    contract=item['contract'], artifacts=item['artifacts'],
                    student_prompt=item['prompt'], deadline=JobDeadline(admitted + timedelta(
                        seconds=item['contract'].job_deadline_seconds)),
                    request_preflight=item['preflight'], selection_cache=cache)
                    for label, item in selected.items()]
                by_hash = {hashlib.sha256(job.submission_id.encode()).hexdigest(): job
                           for job in jobs}
                phase = {'name': phase_name, 'per_job_limit': per_job_limit, 'rows': [],
                         'preparation_seconds': monotonic() - started, 'jobs': {}}
                report['phases'].append(phase)

                class ObservedProvider(ScheduledProvider):
                    def __init__(self, *, observed_jobs, observed_phase, **kwargs):
                        super().__init__(**kwargs)
                        self._observed_jobs = observed_jobs
                        self._observed_phase = observed_phase

                    def generate_sample(self, request, *, context, timeout_seconds):
                        nonlocal requests_started
                        job = self._observed_jobs[context['submission_sha256']]
                        position = context['sample_position']
                        assert request == job.requests[position - 1]
                        with records_lock:
                            if requests_started >= 400:
                                raise ProviderContractError('sample study request budget exhausted')
                            requests_started += 1
                        before = monotonic()
                        generation = super().generate_sample(request, context=context,
                            timeout_seconds=timeout_seconds)
                        with records_lock:
                            self._observed_phase['rows'].append({'submission_id': job.submission_id,
                                'sample_position': position,
                                'request_sha256': context['request_sha256'],
                                'output_sha256': hashlib.sha256(
                                    generation.raw_text.encode()).hexdigest(),
                                'request_seconds': monotonic() - before,
                                'input_tokens': generation.prompt_token_count,
                                'output_tokens': generation.generated_token_count})
                        return generation

                slots = []
                for _ in range(4):
                    slot = {}
                    for item in selected.values():
                        contract = item['contract']
                        provider = ObservedProvider(executor_state=state,
                            observed_jobs=by_hash, observed_phase=phase,
                            challenge_id=contract.challenge_id, base_url='http://127.0.0.1:8001/v1',
                            identity=model, settings=GenerationSettings(
                                **contract.evaluation_identity['generation_settings']),
                            timeout_seconds=contract.provider_request_timeout_seconds,
                            max_response_body_bytes=contract.provider_response_body_bytes)
                        slot[contract.challenge_id] = provider
                    slots.append(slot)
                assert model.model in next(iter(slots[0].values())).served_model_ids()
                scheduler = SampleScheduler(slots, state, model_capacity=4, stop=stop,
                                            per_job_limit=per_job_limit)
                for job in jobs:
                    scheduler.add_job(job)
                execution_error = []

                def execute(selected_scheduler=scheduler, errors=execution_error):
                    try:
                        selected_scheduler.run()
                    except BaseException as error:
                        errors.append(type(error).__name__)

                thread = Thread(target=execute)
                thread.start()
                terminal_times = {}
                try:
                    while thread.is_alive():
                        assert (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19] == ticks
                        for key, value in scheduler.snapshot().items():
                            if value['status'] != 'running' and key not in terminal_times:
                                terminal_times[key] = monotonic() - started
                        phase['completed_responses'] = len(phase['rows'])
                        save('running')
                        thread.join(1)
                except BaseException:
                    scheduler.abort()
                    raise
                finally:
                    thread.join(1200)
                assert not thread.is_alive() and not execution_error
                state.require_clean()
                phase['batch_seconds'] = monotonic() - started
                observed_rows = {(row['submission_id'], row['sample_position']): row
                                 for row in phase['rows']}
                assert len(observed_rows) == len(phase['rows'])
                for key, value in scheduler.snapshot().items():
                    value = dict(value)
                    value['result'] = None if value['result'] is None else value['result'].to_dict()
                    value['terminal_observed_seconds'] = terminal_times.get(
                        key, phase['batch_seconds'])
                    phase['jobs'][key] = value
                    progress = scheduler._jobs[key]
                    for position, outcome in progress.outcomes.items():
                        row = observed_rows[key, position + 1]
                        row['score_statistics'] = asdict(outcome.score) if outcome.score else None
                        row['format_error'] = (outcome.error_code.value
                                               if outcome.error_code else None)
                    if value['status'] == 'succeeded':
                        assert value['samples_completed'] == 50
                        assert value['result']['samples_total'] == 50
                phase['all_full_results'] = all(value['status'] == 'succeeded'
                                                for value in phase['jobs'].values())
                save('running')
            else:
                report['status'] = ('completed' if all(phase['all_full_results']
                    for phase in report['phases']) else 'completed_with_failures')
            save(report['status'])
        print(json.dumps({'status': report['status'], 'generation_attempts': requests_started,
                          'report': str(args.output)}))
        return 0
    except BaseException as error:
        report['error_type'] = type(error).__name__
        save('failed_or_blocked')
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
