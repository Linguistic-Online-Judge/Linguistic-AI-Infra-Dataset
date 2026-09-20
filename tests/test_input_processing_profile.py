import hashlib
import json
from pathlib import Path

import pytest

from linguistic_oj.challenge import build_challenge
from linguistic_oj.input_processing_profile import common_prefix, profile
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.qwen_runtime import TokenizerIdentity


@pytest.mark.parametrize('batch_encoding', [False, True])
def test_offline_profile_checks_exact_inputs_without_model_calls(tmp_path, batch_encoding):
    dataset = tmp_path / 'data.jsonl'
    dataset.write_text(''.join(json.dumps({
        'id': str(index), 'language': 'English', 'treebank': 'ProfileFixture', 'text': token,
        'tasks_available': ['upos'], 'answers': {'segmentation': [token], 'upos': ['INTJ']},
    }) + '\n' for index, token in enumerate(('Hi', 'Hello'))), encoding='utf-8')
    artifacts = build_challenge(dataset, language='English', treebank='ProfileFixture',
                                task='upos', count=2, seed=2026, version='test-v1')
    root = Path(__file__).parents[1]
    config = json.loads((root / 'config/mvp_evaluation_v2.json').read_text())
    public = artifacts.public.model_dump(mode='json')
    for key in config['catalog']:
        config['catalog'][key] = public[key]
    identity = config['evaluation_identity']
    identity.update(challenge_id=artifacts.public.challenge_id,
                    dataset_sha256=artifacts.public.dataset_sha256,
                    selection_sha256=artifacts.public.selection_sha256)
    identity['tokenizer_identity']['chat_template_sha256'] = hashlib.sha256(b'fixture').hexdigest()
    config['leaderboard_partition']['expected_sha256'] = canonical_sha256(identity)
    contract = EvaluationContract.from_mapping(config)
    token_identity = TokenizerIdentity.from_mapping(identity['tokenizer_identity'])

    class Tokenizer:
        chat_template = 'fixture'

        def encode(self, text, **kwargs):
            return list(text.encode('utf-8'))

        def apply_chat_template(self, messages, **kwargs):
            assert kwargs == {'tokenize': True, 'add_generation_prompt': True,
                              'enable_thinking': False}
            ids = list(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
            return {'input_ids': ids, 'attention_mask': [1] * len(ids)} if batch_encoding else ids

    report = profile(artifacts, contract, Tokenizer(), token_identity, 'Return tags.', repeats=2)
    assert report['prepared_samples_identical'] and report['rendered_token_sequences_identical']
    assert report['token_preflight']['baseline_prompt_encode_calls'] == 2
    assert report['token_preflight']['memoized_prompt_encode_calls'] == 1
    assert report['token_preflight']['full_rendered_checks'] == 2
    assert report['prefix_layout_audit']['json_field_values_equal']
    assert report['prefix_layout_audit']['current_input_tokens_mean'] > 100
    assert not report['prefix_layout_audit']['model_output_equivalence_verified']
    assert report['model_requests_sent'] == 0 and report['services_changed'] is False


def test_prefix_audit_counts_tokens_not_similar_text():
    assert common_prefix([[1, 2, 3], [1, 2, 4], [1, 2]]) == 2
    assert common_prefix([[1, 2], [3, 2]]) == 0
