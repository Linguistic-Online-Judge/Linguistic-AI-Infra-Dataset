"""CPU-only input preparation, exact preflight comparison and prefix layout audit."""

import argparse
import hashlib
import json
import statistics
from collections.abc import Mapping
from pathlib import Path
from time import perf_counter

from .challenge import load_challenge_artifacts
from .challenge_registry import validate_contract_matches_public
from .mvp_contract import EvaluationContract, canonical_sha256
from .providers import ModelRequest, PromptEnvelope
from .qwen_runtime import (
    QwenTokenizerPreflight,
    QwenTokenLimitExceeded,
    TokenizerIdentity,
    _token_count,
    load_huggingface_tokenizer,
    validate_qwen_evaluation_contract,
)
from .responses import TaskType
from .runner import _prepare_samples
from .sample_cache import VerifiedSelectionCache


def reference_preflight(contract, tokenizer, requests):
    """Previous loop, retained here solely as an offline timing/reference oracle."""
    output_tokens = contract.evaluation_identity['generation_settings']['max_tokens']
    for request in requests:
        if _token_count(tokenizer.encode(request.student_prompt, add_special_tokens=False)) > (
            contract.student_prompt_tokens
        ):
            raise QwenTokenLimitExceeded('student prompt exceeds limit')
        count = _token_count(tokenizer.apply_chat_template(
            list(PromptEnvelope.from_request(request).to_messages()), tokenize=True,
            add_generation_prompt=True, enable_thinking=False))
        if count > contract.max_rendered_input_tokens or count + output_tokens > (
            contract.model_context_tokens
        ):
            raise QwenTokenLimitExceeded('rendered input exceeds limit')


class TraceTokenizer:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.chat_template = tokenizer.chat_template
        self.prompt_calls = 0
        self.rendered_hashes = []

    def encode(self, *args, **kwargs):
        self.prompt_calls += 1
        return self.tokenizer.encode(*args, **kwargs)

    def apply_chat_template(self, *args, **kwargs):
        value = self.tokenizer.apply_chat_template(*args, **kwargs)
        self.rendered_hashes.append(canonical_sha256(_token_ids(value)))
        return value


def _token_ids(value):
    _token_count(value)
    return list(value['input_ids'] if isinstance(value, Mapping) else value)


def common_prefix(sequences):
    shortest = min(map(len, sequences))
    for index in range(shortest):
        if any(sequence[index] != sequences[0][index] for sequence in sequences[1:]):
            return index
    return shortest


def prefix_audit(tokenizer, requests):
    old, reordered = [], []
    for request in requests:
        envelope = PromptEnvelope.from_request(request)
        original = envelope.to_messages()
        payload = envelope.to_dict()
        order = ('envelope_version', 'task', 'language', 'treebank', 'required_output_schema',
                 'student_prompt', 'input')
        candidate = (original[0], {'role': 'user', 'content': json.dumps(
            {key: payload[key] for key in order}, ensure_ascii=False, separators=(',', ':'))})
        assert json.loads(original[1]['content']) == json.loads(candidate[1]['content'])
        for messages, target in ((original, old), (candidate, reordered)):
            target.append(_token_ids(tokenizer.apply_chat_template(list(messages), tokenize=True,
                add_generation_prompt=True, enable_thinking=False)))
    return {'current_common_prefix_tokens': common_prefix(old),
            'reordered_common_prefix_tokens': common_prefix(reordered),
            'current_input_tokens_mean': statistics.mean(map(len, old)),
            'reordered_input_tokens_mean': statistics.mean(map(len, reordered)),
            'json_field_values_equal': True, 'reordered_layout_sent_to_model': False,
            'model_output_equivalence_verified': False}


def _prepared_hash(samples):
    return canonical_sha256([{'sample': sample.dataset_sample.model_dump(mode='json'),
        'manifest': sample.manifest_sample.model_dump(mode='json'),
        'input': sample.model_input.model_dump(mode='json')} for sample in samples])


def profile(artifacts, contract, tokenizer, identity, prompt, repeats=5):
    if type(repeats) is not int or not 2 <= repeats <= 10:
        raise ValueError('profile repetitions must be in 2..10')
    validate_contract_matches_public(contract, artifacts.public)
    expected = _prepare_samples(artifacts)
    expected_hash = _prepared_hash(expected)
    cache = VerifiedSelectionCache()
    start = perf_counter()
    cold = _prepare_samples(artifacts, selection_cache=cache)
    cold_seconds = perf_counter() - start
    assert _prepared_hash(cold) == expected_hash
    baseline_times, warm_times = [], []
    for repeat in range(repeats):
        conditions = (False, True) if repeat % 2 == 0 else (True, False)
        for cached in conditions:
            start = perf_counter()
            samples = _prepare_samples(artifacts, selection_cache=cache if cached else None)
            elapsed = perf_counter() - start
            assert _prepared_hash(samples) == expected_hash
            (warm_times if cached else baseline_times).append(elapsed)
    requests = tuple(ModelRequest(task=TaskType(artifacts.public.task),
        language=artifacts.public.language, treebank=artifacts.public.treebank,
        student_prompt=prompt, model_input=sample.model_input) for sample in expected)
    old_trace, new_trace = TraceTokenizer(tokenizer), TraceTokenizer(tokenizer)
    reference_preflight(contract, old_trace, requests)
    QwenTokenizerPreflight(contract, new_trace, identity)(requests)
    assert old_trace.rendered_hashes == new_trace.rendered_hashes
    optimized = QwenTokenizerPreflight(contract, tokenizer, identity)
    old_times, new_times = [], []
    for repeat in range(repeats):
        conditions = (False, True) if repeat % 2 == 0 else (True, False)
        for memoized in conditions:
            start = perf_counter()
            if memoized:
                optimized(requests)
            else:
                reference_preflight(contract, tokenizer, requests)
            (new_times if memoized else old_times).append(perf_counter() - start)
    return {'schema_version': 'input-processing-profile-v1', 'samples': len(expected),
            'source_contract_sha256': contract.contract_snapshot_sha256,
            'prepared_samples_identical': True, 'rendered_token_sequences_identical': True,
            'dataset_preparation': {'baseline_seconds': baseline_times,
                'cold_cache_seconds': cold_seconds, 'warm_cache_seconds': warm_times,
                'baseline_median': statistics.median(baseline_times),
                'warm_cache_median': statistics.median(warm_times)},
            'token_preflight': {'baseline_seconds': old_times, 'memoized_seconds': new_times,
                'baseline_median': statistics.median(old_times),
                'memoized_median': statistics.median(new_times),
                'baseline_prompt_encode_calls': old_trace.prompt_calls,
                'memoized_prompt_encode_calls': new_trace.prompt_calls,
                'full_rendered_checks': len(new_trace.rendered_hashes)},
            'prefix_layout_audit': prefix_audit(tokenizer, requests),
            'model_requests_sent': 0, 'services_changed': False, 'classroom_target_verified': False}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('contract', 'public-challenge', 'private-challenge', 'dataset',
                 'tokenizer-snapshot', 'prompt-file'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args(arguments)
    contract = EvaluationContract.from_path(args.contract)
    validate_qwen_evaluation_contract(contract)
    expected = TokenizerIdentity.from_mapping(contract.evaluation_identity['tokenizer_identity'])
    actual = TokenizerIdentity.from_snapshot(args.tokenizer_snapshot,
        repository=expected.repository, revision=expected.revision,
        add_generation_prompt=expected.add_generation_prompt,
        enable_thinking=expected.enable_thinking)
    if actual != expected:
        raise ValueError('tokenizer identity mismatch')
    tokenizer = load_huggingface_tokenizer(args.tokenizer_snapshot)
    template_digest = hashlib.sha256(tokenizer.chat_template.encode()).hexdigest()
    if template_digest != expected.chat_template_sha256:
        raise ValueError('loaded template mismatch')
    artifacts = load_challenge_artifacts(args.public_challenge, args.private_challenge,
                                         dataset_path=args.dataset)
    print(json.dumps(profile(artifacts, contract, tokenizer, expected,
                             args.prompt_file.read_text(encoding='utf-8'), args.repeats), indent=2))


if __name__ == '__main__':
    main()
