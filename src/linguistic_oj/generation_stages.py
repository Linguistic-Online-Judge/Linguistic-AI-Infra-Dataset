"""Private serial stage timing from vLLM metric deltas, retaining original nonstreaming requests."""

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener, getproxies

from .challenge import load_challenge_artifacts
from .executor_state import ExecutorState, GuardedQwenProvider, _write_atomic, executor_lock
from .mvp_contract import EvaluationContract, canonical_sha256
from .providers import GenerationSettings, PromptEnvelope
from .qwen_performance import prepare_cases, validate_output, verify_experiment_runtime
from .runner import evaluate_raw_response

STAGES = ('time_to_first_token_seconds', 'e2e_request_latency_seconds',
          'request_queue_time_seconds', 'request_prefill_time_seconds',
          'request_decode_time_seconds', 'request_inference_time_seconds')
HISTOGRAMS = (*STAGES, 'request_prompt_tokens', 'request_generation_tokens')
GAUGES = ('num_requests_running', 'num_requests_waiting')
REQUIRED = (*GAUGES, 'request_success_total',
            *(name + suffix for name in HISTOGRAMS for suffix in ('_count', '_sum')))
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)=("(?:[^"\\]|\\.)*")')


class MetricsUnavailable(ValueError):
    """Metrics are missing, stale, reset or not attributable to exactly one request."""


def parse_metrics(text, model='Qwen/Qwen3.5-9B'):
    values, series = {}, set()
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        matched = re.fullmatch(r'vllm:([a-z0-9_]+)(?:\{(.*)\})?\s+(\S+)', line)
        if not matched or matched[1] not in REQUIRED:
            continue
        name, raw_labels, raw_value = matched.groups()
        labels, cursor = {}, 0
        raw_labels = raw_labels or ''
        for label in _LABEL.finditer(raw_labels):
            if raw_labels[cursor:label.start()] != (',' if cursor else '') or label[1] in labels:
                raise MetricsUnavailable('malformed or duplicate metric labels')
            labels[label[1]] = json.loads(label[2])
            cursor = label.end()
        if (cursor != len(raw_labels) or labels.get('model_name') != model
                or labels.get('engine') != '0'):
            raise MetricsUnavailable('metrics are not from the selected single engine/model')
        allowed = ({'model_name', 'engine', 'finished_reason'}
                   if name == 'request_success_total' else {'model_name', 'engine'})
        if set(labels) != allowed:
            raise MetricsUnavailable('unexpected metric label set')
        identity = (name, tuple(sorted(labels.items())))
        value = float(raw_value)
        if identity in series or not math.isfinite(value) or value < 0:
            raise MetricsUnavailable('duplicate or invalid metric value')
        series.add(identity)
        values[name] = values.get(name, 0) + value
    if set(values) != set(REQUIRED):
        raise MetricsUnavailable('required timing metrics are missing')
    if any(values[name] != int(values[name]) for name in REQUIRED
           if name.endswith(('_count', '_total')) or name in GAUGES):
        raise MetricsUnavailable('request counters must be integers')
    return values


def require_settled(values):
    if any(values[name] != 0 for name in GAUGES):
        raise MetricsUnavailable('model has active or waiting requests')
    if any(values[name + '_count'] != values['request_success_total'] for name in HISTOGRAMS):
        raise MetricsUnavailable('request histograms have not settled')


def stage_delta(before, after, *, prompt_tokens, output_tokens):
    require_settled(before)
    require_settled(after)
    delta = {key: after[key] - before[key] for key in REQUIRED if key not in GAUGES}
    if any(value < 0 for value in delta.values()):
        raise MetricsUnavailable('metric counter reset')
    if delta['request_success_total'] != 1 or any(
            delta[name + '_count'] != 1 for name in HISTOGRAMS):
        raise MetricsUnavailable('metrics do not describe exactly one completed request')
    if (delta['request_prompt_tokens_sum'] != prompt_tokens
            or delta['request_generation_tokens_sum'] != output_tokens):
        raise MetricsUnavailable('metric token totals differ from the response')
    stages = {name: delta[name + '_sum'] for name in STAGES}
    prefill, decode, inference = (stages['request_' + name + '_time_seconds']
                                  for name in ('prefill', 'decode', 'inference'))
    if not math.isclose(prefill + decode, inference, abs_tol=1e-6, rel_tol=1e-6):
        raise MetricsUnavailable('engine phase intervals do not reconcile')
    stages['decode_tokens_per_second'] = ((output_tokens - 1) / decode
                                           if output_tokens > 1 and decode > 0 else None)
    stages['decode_fraction_of_inference'] = decode / inference if inference else None
    return stages


def measure_one(case, provider, snapshot, allowed, *, settle_seconds=15):
    """One guarded request; metrics failures never cause a resend."""
    if not allowed():
        raise MetricsUnavailable('online work or process identity changed')
    before = snapshot()
    require_settled(before)
    started = monotonic()
    generation = provider.generate(case.request)
    duration = monotonic() - started
    outcome = evaluate_raw_response(sample=case.prepared.dataset_sample,
        manifest_sample=case.prepared.manifest_sample, task=case.request.task,
        model_input=case.prepared.model_input, raw_response=generation.raw_text)
    row = {'position': case.position, 'client_request_seconds': duration,
        'input_tokens': generation.prompt_token_count,
        'output_tokens': generation.generated_token_count,
        'finish_reason': generation.finish_reason,
        'output_sha256': hashlib.sha256(generation.raw_text.encode()).hexdigest(),
        'format_error': outcome.error_code.value if outcome.error_code else None,
        'score_statistics': asdict(outcome.score) if outcome.score else None,
        'metric_before': before, 'stages': None, 'attribution_valid': False}
    until = monotonic() + settle_seconds
    try:
        if generation.prompt_token_count != case.input_tokens:
            raise MetricsUnavailable('response input count differs from preflight')
        while True:
            after = snapshot()
            row['metric_after'] = after
            if any(after[key] < before[key] for key in REQUIRED if key not in GAUGES):
                raise MetricsUnavailable('metric counter reset')
            if after['request_success_total'] - before['request_success_total'] > 1:
                raise MetricsUnavailable('other completed requests contaminated the metrics')
            if all(after[name + '_count'] - before[name + '_count'] == 1 for name in HISTOGRAMS):
                break
            if monotonic() >= until:
                raise MetricsUnavailable('metric settlement deadline exceeded')
            sleep(0.2)
        stages = stage_delta(before, after, prompt_tokens=generation.prompt_token_count,
                             output_tokens=generation.generated_token_count)
        if not allowed():
            raise MetricsUnavailable('online work or process identity changed')
        row.update(stages=stages, attribution_valid=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        row['attribution_error'] = 'metrics_or_online_state_changed'
    return row


def model_process(pid):
    """Pin the owner and start ticks of the explicitly selected local API process."""
    proc = Path('/proc') / str(pid)
    if proc.stat().st_uid != os.getuid():
        raise ValueError('model process belongs to another account')
    argv = (proc / 'cmdline').read_bytes().decode().strip('\0').split('\0')
    for flag, expected in (('--host', '127.0.0.1'), ('--port', '8000'),
                           ('--served-model-name', 'Qwen/Qwen3.5-9B'), ('--max-num-seqs', '1')):
        if flag not in argv or argv[argv.index(flag) + 1] != expected:
            raise ValueError('model process configuration mismatch')
    fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ValueError('model process exited')
    return {'pid': pid, 'uid': proc.stat().st_uid, 'start_ticks': int(fields[19]),
            'argv_sha256': canonical_sha256(argv)}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('contract', 'public-challenge', 'private-challenge', 'dataset', 'prompt-file',
                 'tokenizer-snapshot', 'launch-evidence', 'state-dir', 'output', 'operations'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--model-pid', type=int, required=True)
    parser.add_argument('--sample-limit', type=int, choices=range(1, 7), default=6)
    parser.add_argument('--repetitions', type=int, choices=(1, 2), default=2)
    parser.add_argument('--initialize-state', action='store_true')
    parser.add_argument('--run-real-qwen', action='store_true')
    args = parser.parse_args(arguments)
    if any(key in getproxies() for key in ('http', 'https', 'all')):
        raise ValueError('implicit model proxies are not supported')
    validate_output(args.output, args.state_dir)
    contract = EvaluationContract.from_path(args.contract)
    model, tokenizer, token_identity, launch = verify_experiment_runtime(
        contract, args.tokenizer_snapshot, args.launch_evidence, 1)
    if launch.max_num_seqs != 1:
        raise ValueError('stage screening requires single-sequence evidence')
    artifacts = load_challenge_artifacts(args.public_challenge, args.private_challenge,
                                         dataset_path=args.dataset)
    cases, preparation = prepare_cases(artifacts, contract, tokenizer, token_identity,
        args.prompt_file.read_text(encoding='utf-8'), args.sample_limit)
    identity = model_process(args.model_pid)
    opener = build_opener(ProxyHandler({}))

    def snapshot():
        with opener.open('http://127.0.0.1:8000/metrics', timeout=5) as response:
            raw = response.read(2097153)
        if len(raw) > 2097152:
            raise MetricsUnavailable('metrics response exceeds limit')
        return parse_metrics(raw.decode())

    observations = []

    def allowed():
        checked = subprocess.run([str(args.operations), 'status'], text=True,
                                 capture_output=True, check=True, timeout=30)
        status = json.loads(checked.stdout)
        observations.append(status)
        return (model_process(args.model_pid) == identity and status['ready']
                and not status['uncertain_inference']
                and not any(status['submissions'].get(key, 0) for key in ('queued', 'running'))
                and all(status[key] == observations[0][key]
                        for key in ('pid', 'instance', 'submissions')))

    if not allowed():
        raise MetricsUnavailable('online work is not idle')
    require_settled(snapshot())
    report = {'schema_version': 'generation-stages-v1',
        'prepared_at': datetime.now(UTC).isoformat(),
        'source_contract_sha256': contract.contract_snapshot_sha256,
        'model_identity': model.to_dict(), 'model_process': identity,
        'generation_settings': contract.evaluation_identity['generation_settings'],
        'request_set_sha256': canonical_sha256([
            PromptEnvelope.from_request(case.request).to_messages() for case in cases]),
        'planned_requests': 1 + len(cases) * args.repetitions, 'preparation': preparation,
        'measurement_scope': 'server_metric_deltas_not_browser_first_token',
        'warmup_requests': 1, 'services_changed': False, 'protocol_changed': False,
        'idle_checks_are_not_exclusive_reservation': True, 'classroom_capacity_verified': False}
    if not args.run_real_qwen:
        print(json.dumps({**report, 'model_requests_sent': 0}, indent=2))
        return 0
    binding = canonical_sha256({'kind': 'generation-stages-v1', 'model': model.to_dict(),
                                 'endpoint': 'http://127.0.0.1:8000/v1'})
    with executor_lock(args.state_dir, create=args.initialize_state):
        state = (ExecutorState.initialize(args.state_dir, binding) if args.initialize_state
                 else ExecutorState(args.state_dir, binding))
        state.require_clean()
        provider = GuardedQwenProvider(executor_state=state, challenge_id=contract.challenge_id,
            base_url='http://127.0.0.1:8000/v1', identity=model,
            settings=GenerationSettings(**contract.evaluation_identity['generation_settings']),
            timeout_seconds=contract.provider_request_timeout_seconds,
            max_response_body_bytes=contract.provider_response_body_bytes)
        if model.model not in provider.served_model_ids():
            raise ValueError('model alias mismatch')
        args.output.mkdir(mode=0o700)
        rows = []

        def save(status):
            _write_atomic(args.output / 'report.json', {**report, 'status': status, 'rows': rows,
                'application_observations': observations,
                'experiment_pending': state.snapshot()['pending'],
                'recorded_at': datetime.now(UTC).isoformat()})

        save('running')
        work = [(True, 0, cases[0])] + [(False, repetition, case)
            for repetition in range(1, args.repetitions + 1) for case in cases]
        try:
            for warmup, repetition, case in work:
                row = measure_one(case, provider, snapshot, allowed)
                rows.append({**row, 'warmup': warmup, 'repetition': repetition})
                state.require_clean()
                if not row['attribution_valid']:
                    save('unattributable_stopped')
                    return 2
                save('running')
                sleep(5)
            if not allowed():
                save('online_state_changed')
                return 2
            save('completed')
        except Exception:
            save('failed_or_blocked')
            raise
    print(json.dumps({'status': 'completed', 'model_requests_sent': len(rows),
                      'report': str(args.output / 'report.json')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
