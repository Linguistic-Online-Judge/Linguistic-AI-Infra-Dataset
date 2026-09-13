import hashlib
import json
from pathlib import Path

import pytest

from linguistic_oj.registry_extension import extend_marker, validate_extension
from scripts.build_foundation_catalog import output_budget, read_examples, token_count

ROOT = Path(__file__).resolve().parents[1]


def test_tokenizer_mapping_counts_ids_not_metadata_keys():
    assert token_count({'input_ids': list(range(1200)), 'attention_mask': [1] * 1200}) == 1200
    assert token_count([1, 2, 3]) == 3
    with pytest.raises(ValueError):
        token_count('invalid')
    assert output_budget(950) >= 951


def test_handwritten_examples_cover_all_languages_and_valid_trees():
    from linguistic_oj.challenge import LANGUAGE_CODES

    examples = read_examples(ROOT / 'src/linguistic_oj/web/assets/language-lessons.js')
    assert set(examples) == set(LANGUAGE_CODES.values())
    for example_set in examples.values():
        assert len(example_set['examples']) == 2
        for example in example_set['examples']:
            size = len(example['tokens'])
            assert size == len(example['heads']) == len(example['relations'])
            assert example['heads'].count(0) == 1
            for start in range(1, size + 1):
                visited = set()
                current = start
                while current:
                    assert current not in visited and 1 <= current <= size
                    visited.add(current)
                    current = example['heads'][current - 1]


def test_registry_extension_is_additive_and_preserves_instance(tmp_path):
    marker = {'instance': 'same-instance', 'database': 'same-database', 'owner': 'same-owner',
              'contracts': {'old': 'a' * 64}}
    path = tmp_path / 'instance.json'
    raw = json.dumps(marker).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        validate_extension(marker['contracts'], {'new': 'b' * 64})
    with pytest.raises(ValueError):
        validate_extension(marker['contracts'], {'old': 'b' * 64, 'new': 'c' * 64})
    with pytest.raises(ValueError):
        extend_marker(tmp_path, {'old': 'a' * 64, 'new': 'b' * 64}, 'wrong-digest')
    assert path.read_bytes() == raw
    result = extend_marker(tmp_path, {'old': 'a' * 64, 'new': 'b' * 64}, digest)
    assert result['added'] == ['new']
    updated = json.loads(path.read_text())
    assert {key: updated[key] for key in ('instance', 'database', 'owner')} == {
        key: marker[key] for key in ('instance', 'database', 'owner')}
    assert json.loads(Path(result['journal']).read_text())['before_text'] == raw.decode()
    with pytest.raises(ValueError):
        extend_marker(tmp_path, {**updated['contracts'], 'third': 'c' * 64}, digest)
