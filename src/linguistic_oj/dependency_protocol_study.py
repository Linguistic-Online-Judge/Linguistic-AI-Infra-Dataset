"""Small private protocol A/B study, yielding to live work and never changing live contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener, getproxies

from .aggregation import SampleEvaluationOutcome
from .challenge import load_challenge_artifacts
from .challenge_registry import validate_contract_matches_public
from .compact_dependency import (
    BASELINE_PROTOCOL,
    PROTOCOL,
    PROTOCOLS,
    SEMANTIC_INSTRUCTION,
    TRIPLES_PROTOCOL,
    experiment_messages,
    experiment_prompt,
    parse_compact_dependency,
)
from .executor_state import ExecutorState, GuardedQwenProvider, _write_atomic, executor_lock
from .mvp_contract import EvaluationContract, canonical_sha256
from .providers import GenerationSettings, ModelRequest
from .qwen_performance import validate_output, verify_experiment_runtime
from .qwen_runtime import _token_count
from .responses import TaskType
from .runner import _prepare_samples, evaluate_raw_response


class ProtocolProvider(GuardedQwenProvider):
    @property
    def experimental_protocol(self):
        return 'dependency-protocol-study-v1'

    def __init__(self, *, protocol, **kwargs):
        if protocol not in PROTOCOLS:
            raise ValueError('unknown experimental protocol')
        self._protocol = protocol
        super().__init__(**kwargs)

    def _request_body(self, request):
        payload = json.loads(super()._request_body(request))
        payload['messages'] = list(experiment_messages(request, self._protocol))
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _score(prepared, raw, protocol, byte_limit):
    if protocol in (PROTOCOL, TRIPLES_PROTOCOL):
        parsed = parse_compact_dependency(raw, expected_token_ids=tuple(
            token.token_id for token in prepared.model_input.tokens), max_utf8_bytes=byte_limit,
            protocol=protocol)
        if parsed.error is not None:
            return SampleEvaluationOutcome.malformed(prepared.dataset_sample.id,
                TaskType.DEPENDENCY, gold_items=prepared.manifest_sample.gold_items,
                error_code=parsed.error.code)
        raw = parsed.value.model_dump_json()
    elif protocol != BASELINE_PROTOCOL:
        raise ValueError('unknown experimental protocol')
    return evaluate_raw_response(sample=prepared.dataset_sample,
        manifest_sample=prepared.manifest_sample, task=TaskType.DEPENDENCY,
        model_input=prepared.model_input, raw_response=raw)


def audit_gold(prepared, tokenizer, byte_limit):
    counts = {name: [] for name in PROTOCOLS}
    for sample in prepared:
        gold = sample.dataset_sample.answers['dependency']
        original = {'arcs': [{'token_id': arc[0], 'head_id': arc[2], 'deprel': arc[4]}
                             for arc in gold]}
        compact = {'heads': [arc[2] for arc in gold], 'deprels': [arc[4] for arc in gold]}
        triples = {'arcs': [[arc[0], arc[2], arc[4]] for arc in gold]}
        raw = [json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
               for value in (original, compact, triples)]
        scores = [_score(sample, text, protocol, byte_limit)
                  for protocol, text in zip(PROTOCOLS, raw, strict=True)]
        if any(score != scores[0] for score in scores) or scores[0].error_code is not None:
            raise ValueError('gold conversion changed the existing score')
        for protocol, text in zip(PROTOCOLS, raw, strict=True):
            counts[protocol].append(_token_count(tokenizer.encode(text, add_special_tokens=False)))
    return {'samples_checked': len(prepared), 'all_scores_equal': True,
            'canonical_gold_output_tokens': {key: {'total': sum(values), 'maximum': max(values)}
                                            for key, values in counts.items()},
            'model_speedup_measured': False}


def preflight(request, protocol, tokenizer, contract):
    prompt_tokens = _token_count(tokenizer.encode(request.student_prompt, add_special_tokens=False))
    if prompt_tokens > contract.student_prompt_tokens:
        raise ValueError('experimental prompt exceeds the source budget')
    count = _token_count(tokenizer.apply_chat_template(list(experiment_messages(request, protocol)),
                        tokenize=True, add_generation_prompt=True, enable_thinking=False))
    output = contract.evaluation_identity['generation_settings']['max_tokens']
    if count > contract.max_rendered_input_tokens or count + output > contract.model_context_tokens:
        raise ValueError('actual experimental messages exceed source token budgets')
    return count


def run_pairs(prepared, contract, tokenizer, providers, state, positions, repetitions, can_run,
              checkpoint=None, cooldown_seconds=0, candidate=PROTOCOL):
    protocols = (BASELINE_PROTOCOL, candidate)
    rows = []
    for repetition in range(1, repetitions + 1):
        for position in positions:
            sample = prepared[position - 1]
            order = protocols if (repetition + position) % 2 == 0 else tuple(reversed(protocols))
            for protocol in order:
                state.require_clean()
                if not can_run():
                    return {'status': 'yielded_to_online_work', 'rows': rows}
                request = ModelRequest(task=TaskType.DEPENDENCY,
                    language=sample.dataset_sample.language,
                    treebank=sample.dataset_sample.treebank,
                    student_prompt=experiment_prompt(protocol), model_input=sample.model_input)
                input_count = preflight(request, protocol, tokenizer, contract)
                started = monotonic()
                generation = providers[protocol].generate(request)
                duration = monotonic() - started
                outcome = _score(sample, generation.raw_text, protocol,
                                 contract.provider_response_body_bytes)
                state.require_clean()
                rows.append({'protocol': protocol, 'position': position, 'repetition': repetition,
                    'input_tokens_local': input_count,
                    'input_tokens_reported': generation.prompt_token_count,
                    'output_tokens': generation.generated_token_count, 'request_seconds': duration,
                    'finish_reason': generation.finish_reason,
                    'output_sha256': hashlib.sha256(generation.raw_text.encode()).hexdigest(),
                    'parse_error': outcome.error_code.value if outcome.error_code else None,
                    'gold_items': outcome.gold_items,
                    'score_statistics': asdict(outcome.score) if outcome.score else None})
                if checkpoint is not None:
                    checkpoint(rows)
                if cooldown_seconds:
                    sleep(cooldown_seconds)
    return {'status': 'completed', 'rows': rows}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('contract', 'public-challenge', 'private-challenge', 'dataset',
                 'tokenizer-snapshot', 'launch-evidence', 'state-dir', 'output', 'operations'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--positions', type=int, nargs='+', default=[1, 2, 4])
    parser.add_argument('--repetitions', type=int, choices=(1, 2), default=2)
    parser.add_argument('--candidate', choices=(PROTOCOL, TRIPLES_PROTOCOL), default=PROTOCOL)
    parser.add_argument('--initialize-state', action='store_true')
    parser.add_argument('--run-real-qwen', action='store_true')
    args = parser.parse_args(arguments)
    protocols = (BASELINE_PROTOCOL, args.candidate)
    try:
        if any(key in getproxies() for key in ('http', 'https', 'all')):
            raise ValueError('implicit model proxy is not supported')
        validate_output(args.output, args.state_dir)
        contract = EvaluationContract.from_path(args.contract)
        if contract.evaluation_identity['task'] != 'dependency':
            raise ValueError('only dependency is supported')
        model, tokenizer, _, launch = verify_experiment_runtime(
            contract, args.tokenizer_snapshot, args.launch_evidence, 1)
        if launch.max_num_seqs != 1:
            raise ValueError('protocol screening requires a single-sequence baseline')
        artifacts = load_challenge_artifacts(args.public_challenge, args.private_challenge,
                                              dataset_path=args.dataset)
        validate_contract_matches_public(contract, artifacts.public)
        prepared = _prepare_samples(artifacts)
        if (len(set(args.positions)) != len(args.positions) or not args.positions
                or len(args.positions) > 6
                or any(not 1 <= p <= len(prepared) for p in args.positions)):
            raise ValueError('choose at most six distinct existing sample positions')
        audit = audit_gold(prepared, tokenizer, contract.provider_response_body_bytes)
        for position in args.positions:
            for protocol in protocols:
                preflight(ModelRequest(task=TaskType.DEPENDENCY, language=artifacts.public.language,
                    treebank=artifacts.public.treebank, student_prompt=experiment_prompt(protocol),
                    model_input=prepared[position - 1].model_input), protocol, tokenizer, contract)
        report = {'schema_version': 'dependency-protocol-study-v1',
            'source_contract_sha256': contract.contract_snapshot_sha256,
            'model_identity': model.to_dict(),
            'generation_settings': contract.evaluation_identity['generation_settings'],
            'protocols': list(protocols), 'semantic_instruction_sha256': hashlib.sha256(
                SEMANTIC_INSTRUCTION.encode()).hexdigest(), 'gold_audit': audit,
            'sample_positions': args.positions, 'repetitions': args.repetitions,
            'semantic_inputs_sha256': canonical_sha256([
                {'position': p, 'input': prepared[p - 1].model_input.model_dump(mode='json')}
                for p in args.positions]),
            'condition_fingerprints': {protocol: {
                'prompt_sha256': hashlib.sha256(experiment_prompt(protocol).encode()).hexdigest(),
                'messages_sha256': canonical_sha256([experiment_messages(ModelRequest(
                    task=TaskType.DEPENDENCY, language=artifacts.public.language,
                    treebank=artifacts.public.treebank, student_prompt=experiment_prompt(protocol),
                    model_input=prepared[p - 1].model_input), protocol) for p in args.positions]),
            } for protocol in protocols},
            'planned_requests': len(args.positions) * len(protocols) * args.repetitions,
            'platform_scores_written': False, 'live_protocol_changed': False,
            'classroom_capacity_verified': False}
        if not args.run_real_qwen:
            print(json.dumps(report, indent=2))
            return 0

        observations = []
        opener = build_opener(ProxyHandler({}))

        def can_run():
            checked = subprocess.run([str(args.operations), 'status'], text=True,
                                     capture_output=True, check=True, timeout=30)
            status = json.loads(checked.stdout)
            with opener.open('http://127.0.0.1:8000/metrics', timeout=5) as response:
                raw = response.read(2097153)
            if len(raw) > 2097152:
                raise ValueError('model metrics response too large')
            gauges = {'running': [], 'waiting': []}
            for line in raw.decode('utf-8').splitlines():
                matched = re.match(
                    r'^vllm:num_requests_(running|waiting)(?:\{[^}]*\})?\s+([0-9.eE+-]+)', line)
                if matched:
                    gauges[matched[1]].append(float(matched[2]))
            status['model_request_gauges'] = gauges
            observations.append(status)
            return (status['ready'] and not status['uncertain_inference']
                    and not any(status['submissions'].get(key, 0) for key in ('queued', 'running'))
                    and status['submissions'] == observations[0]['submissions']
                    and all(values and all(value == 0 for value in values)
                            for values in gauges.values()))

        binding = canonical_sha256({'kind': 'compact-dependency-study-v1', 'model': model.to_dict(),
                                    'endpoint': 'http://127.0.0.1:8000/v1'})
        with executor_lock(args.state_dir, create=args.initialize_state):
            state = (ExecutorState.initialize(args.state_dir, binding) if args.initialize_state
                     else ExecutorState(args.state_dir, binding))
            state.require_clean()
            if not can_run():
                raise RuntimeError('online work is not idle')
            providers = {protocol: ProtocolProvider(protocol=protocol, executor_state=state,
                challenge_id=contract.challenge_id, base_url='http://127.0.0.1:8000/v1',
                identity=model,
                settings=GenerationSettings(**contract.evaluation_identity['generation_settings']),
                timeout_seconds=contract.provider_request_timeout_seconds,
                max_response_body_bytes=contract.provider_response_body_bytes)
                for protocol in protocols}
            if model.model not in providers[BASELINE_PROTOCOL].served_model_ids():
                raise ValueError('model alias mismatch')
            args.output.mkdir(mode=0o700)
            _write_atomic(args.output / 'report.json', {**report, 'status': 'running'})

            def checkpoint(rows):
                _write_atomic(args.output / 'report.json', {**report, 'status': 'running',
                    'rows': rows, 'application_observations': observations})

            result = run_pairs(prepared, contract, tokenizer, providers, state, args.positions,
                               args.repetitions, can_run, checkpoint, cooldown_seconds=5,
                               candidate=args.candidate)
            can_run()
            report.update(result, application_observations=observations,
                          experiment_pending=state.snapshot()['pending'],
                          application_submission_counts_unchanged=all(
                              item['submissions'] == observations[0]['submissions']
                              for item in observations),
                          idle_checks_are_not_an_exclusive_gpu_reservation=True)
            _write_atomic(args.output / 'report.json', report)
            print(json.dumps({'status': report['status'], 'completed_requests': len(result['rows']),
                              'report': str(args.output / 'report.json')}))
            return 0 if report['status'] == 'completed' else 2
    except Exception:
        print('dependency_protocol_study_failed_or_blocked')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
