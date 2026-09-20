import json
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest

from linguistic_oj.challenge import build_challenge
from linguistic_oj.compact_dependency import (
    BASELINE_PROTOCOL,
    PROTOCOL,
    TRIPLES_PROTOCOL,
    experiment_messages,
    experiment_prompt,
    parse_compact_dependency,
)
from linguistic_oj.dependency_protocol_study import ProtocolProvider, run_pairs
from linguistic_oj.model_inputs import DependencyModelInput
from linguistic_oj.mvp_contract import EvaluationContract
from linguistic_oj.providers import GenerationSettings, ModelIdentity, ModelRequest, PromptEnvelope
from linguistic_oj.qwen_runtime import (
    QwenRuntimeAttestation,
    QwenRuntimeAttestationError,
    TokenizerIdentity,
    verify_qwen_runtime,
)
from linguistic_oj.responses import ParseErrorCode, TaskType, parse_model_response
from linguistic_oj.runner import _prepare_samples, evaluate_raw_response


@pytest.mark.parametrize(('raw', 'code'), [
    ('not json', ParseErrorCode.INVALID_JSON),
    ('[]', ParseErrorCode.TOP_LEVEL_NOT_OBJECT),
    ('{"heads":[0]}', ParseErrorCode.MISSING_FIELD),
    ('{"heads":[0],"deprels":["root"],"extra":1}', ParseErrorCode.EXTRA_FIELD),
    ('{"heads":0,"deprels":["root"]}', ParseErrorCode.WRONG_TYPE),
    ('{"heads":[],"deprels":[]}', ParseErrorCode.EMPTY_VALUE),
    ('{"heads":[0,1],"deprels":["root"]}', ParseErrorCode.LENGTH_MISMATCH),
    ('{"heads":[true],"deprels":["root"]}', ParseErrorCode.WRONG_TYPE),
    ('{"heads":[0.0],"deprels":["root"]}', ParseErrorCode.WRONG_TYPE),
    ('{"heads":["0"],"deprels":["root"]}', ParseErrorCode.WRONG_TYPE),
    ('{"heads":[0],"deprels":[2]}', ParseErrorCode.WRONG_TYPE),
    ('{"heads":[0],"deprels":[""]}', ParseErrorCode.EMPTY_VALUE),
    ('{"heads":[-1],"deprels":["root"]}', ParseErrorCode.INVALID_HEAD_ID),
    ('{"heads":[2],"deprels":["root"]}', ParseErrorCode.INVALID_HEAD_ID),
    ('{"heads":[NaN],"deprels":["root"]}', ParseErrorCode.INVALID_JSON),
    ('{"heads":[0],"heads":[1],"deprels":["root"]}', ParseErrorCode.INVALID_JSON),
])
def test_compact_output_is_rejected_without_repair(raw, code):
    result = parse_compact_dependency(raw, expected_token_ids=(1,))
    assert result.value is None and result.error.code is code


def test_mapping_preserves_prediction_values_without_normalization_or_extra_tree_rules():
    heads, labels = [1, 0, 0], ["ROOT", "root", " "]
    result = parse_compact_dependency(json.dumps({'heads': heads, 'deprels': labels}),
                                      expected_token_ids=(1, 2, 3))
    baseline = {"arcs": [{"token_id": i, "head_id": head, "deprel": label}
                         for i, (head, label) in enumerate(zip(heads, labels, strict=True), 1)]}
    old = parse_model_response(TaskType.DEPENDENCY, json.dumps(baseline), expected_count=3,
                               expected_token_ids=(1, 2, 3))
    assert result == old
    assert [arc.deprel for arc in result.value.arcs] == labels
    # The old production parser has NOT been taught to accept this experimental format.
    assert parse_model_response(TaskType.DEPENDENCY,
        json.dumps({'heads': heads, 'deprels': labels}), expected_count=3).error is not None


@pytest.mark.parametrize('ids', [(), (0,), (2,), (2, 1), (1, 3), (True,)])
def test_invalid_operator_input_ids_fail_before_parsing(ids):
    with pytest.raises(ValueError):
        parse_compact_dependency('{}', expected_token_ids=ids)


def test_response_size_limit():
    assert parse_compact_dependency(' ' * 100, expected_token_ids=(1,),
                                    max_utf8_bytes=10).error.code == ParseErrorCode.INVALID_VALUE


def test_experiment_has_separate_envelope_and_preserves_original_messages():
    request = ModelRequest(task=TaskType.DEPENDENCY, language='English', treebank='Fixture',
        student_prompt=experiment_prompt(PROTOCOL),
        model_input=DependencyModelInput.model_validate_json(
            '{"tokens":[{"token_id":1,"form":"Hi"}]}'))
    baseline = PromptEnvelope.from_request(request).to_messages()
    assert experiment_messages(request, BASELINE_PROTOCOL) == baseline
    compact = experiment_messages(request, PROTOCOL)
    assert compact[0] == baseline[0]
    payload = json.loads(compact[1]['content'])
    assert payload['envelope_version'] == 'experimental-' + PROTOCOL
    assert set(payload['required_output_schema']['properties']) == {'heads', 'deprels'}
    assert payload['input'] == {'tokens': [{'token_id': 1, 'form': 'Hi'}]}
    assert 'answers' not in compact[1]['content']
    assert PromptEnvelope.from_request(request).to_messages() == baseline


def test_legal_predictions_have_identical_scores_for_all_small_head_combinations(tmp_path):
    data = tmp_path / 'synthetic.jsonl'
    data.write_text(json.dumps({
        'id': 'fixture', 'language': 'English', 'treebank': 'CodecFixture', 'text': 'Birds fly .',
        'tasks_available': ['dependency'], 'answers': {
            'segmentation': ['Birds', 'fly', '.'],
            'dependency': [[1, 'Birds', 2, 'fly', 'nsubj'], [2, 'fly', 0, 'ROOT', 'root'],
                           [3, '.', 2, 'fly', 'punct']],
        },
    }) + '\n', encoding='utf-8')
    artifacts = build_challenge(data, language='English', treebank='CodecFixture',
                                task='dependency', count=1, seed=2026, version='test-v1')
    prepared = _prepare_samples(artifacts)[0]
    for heads in product(range(4), repeat=3):
        for labels in (['nsubj', 'root', 'punct'], ['dep', 'ROOT', 'unknown-label']):
            old = {'arcs': [{'token_id': i, 'head_id': h, 'deprel': r}
                            for i, (h, r) in enumerate(zip(heads, labels, strict=True), 1)]}
            compact = parse_compact_dependency(json.dumps({'heads': heads, 'deprels': labels}),
                                                expected_token_ids=(1, 2, 3))
            assert compact.error is None
            params = dict(sample=prepared.dataset_sample, manifest_sample=prepared.manifest_sample,
                          task=TaskType.DEPENDENCY, model_input=prepared.model_input)
            old_score = evaluate_raw_response(raw_response=json.dumps(old), **params)
            converted_score = evaluate_raw_response(
                raw_response=compact.value.model_dump_json(), **params)
            assert old_score == converted_score
            triples = parse_compact_dependency(json.dumps({'arcs': [
                [i, h, r] for i, (h, r) in enumerate(zip(heads, labels, strict=True), 1)]}),
                expected_token_ids=(1, 2, 3), protocol=TRIPLES_PROTOCOL)
            assert triples.error is None
            assert evaluate_raw_response(
                raw_response=triples.value.model_dump_json(), **params) == old_score


def test_experimental_provider_cannot_be_used_as_a_frozen_worker():
    root = Path(__file__).parents[1]
    contract = EvaluationContract.from_path(
        root / 'config/evaluation_contracts/v1/de-hdt-dependency-v1.json')
    model = ModelIdentity(**contract.evaluation_identity['model_identity'])
    provider = ProtocolProvider(protocol=PROTOCOL, executor_state=None,
        challenge_id=contract.challenge_id, base_url='http://127.0.0.1:8000/v1', identity=model,
        settings=GenerationSettings(**contract.evaluation_identity['generation_settings']),
        timeout_seconds=contract.provider_request_timeout_seconds,
        max_response_body_bytes=contract.provider_response_body_bytes)
    runtime = QwenRuntimeAttestation(model,
        TokenizerIdentity.from_mapping(contract.evaluation_identity['tokenizer_identity']),
        contract.model_context_tokens, contract.worker_model_concurrency, True)
    with pytest.raises(QwenRuntimeAttestationError, match='experimental protocol'):
        verify_qwen_runtime(contract, provider, runtime)
    with pytest.raises(AttributeError):
        provider.experimental_protocol = None


def test_study_yields_before_generation_when_online_work_is_present():
    state = SimpleNamespace(require_clean=lambda: None)
    result = run_pairs([object()], None, None, {}, state, [1], 1, lambda: False)
    assert result == {'status': 'yielded_to_online_work', 'rows': []}


@pytest.mark.parametrize(('arcs', 'error'), [
    ([[1, 0, 'root', 'extra']], ParseErrorCode.WRONG_TYPE),
    ([[True, 0, 'root']], ParseErrorCode.WRONG_TYPE),
    ([[1, '0', 'root']], ParseErrorCode.WRONG_TYPE),
    ([[1, 2, 'root']], ParseErrorCode.INVALID_HEAD_ID),
    ([[1, 0, '']], ParseErrorCode.EMPTY_VALUE),
])
def test_triples_reject_invalid_predictions(arcs, error):
    result = parse_compact_dependency(json.dumps({'arcs': arcs}), expected_token_ids=(1,),
                                      protocol=TRIPLES_PROTOCOL)
    assert result.error.code == error


def test_triples_preserve_ids_and_original_duplicate_detection():
    raw = json.dumps({'arcs': [[2, 0, 'root'], [1, 2, 'nsubj']]})
    result = parse_compact_dependency(raw, expected_token_ids=(1, 2), protocol=TRIPLES_PROTOCOL)
    assert [arc.token_id for arc in result.value.arcs] == [2, 1]
    duplicated = parse_compact_dependency('{"arcs":[[1,0,"root"],[1,1,"dep"]]}',
        expected_token_ids=(1, 2), protocol=TRIPLES_PROTOCOL)
    assert duplicated.error.code == ParseErrorCode.DUPLICATE_TOKEN_ID
