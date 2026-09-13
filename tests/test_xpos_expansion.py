import json
from pathlib import Path

import pytest

from linguistic_oj.challenge_registry import load_challenge_contract_registry
from scripts.build_xpos_catalog import read_profiles, validate_profile

ROOT = Path(__file__).resolve().parents[1]


def test_xpos_extension_preserves_all_foundation_contracts():
    old = load_challenge_contract_registry(
        ROOT, Path('config/challenge_contract_registry_foundation_v1.json'))
    new = load_challenge_contract_registry(
        ROOT, Path('config/challenge_contract_registry_xpos_v1.json'))
    assert len(new.public_challenges) == 74
    assert len(new.contracts) == 70
    assert all(new.contracts[key].snapshot_json == value.snapshot_json
               for key, value in old.contracts.items())
    xpos_languages = {new.public_challenges[key].language for key, contract in new.contracts.items()
                      if contract.evaluation_identity['task'] == 'xpos'}
    assert len(xpos_languages) == 15
    assert not xpos_languages.intersection({'Hebrew', 'Danish', 'Hungarian'})


def test_xpos_examples_use_complete_source_specific_labels():
    profiles = read_profiles(ROOT / 'src/linguistic_oj/web/assets/xpos-lessons.js')
    text = (ROOT / 'src/linguistic_oj/web/assets/xpos-labels.js').read_text(encoding='utf-8')
    inventories = json.loads(text.partition('const XPOS_LABEL_INVENTORIES =')[2].strip().
                             removesuffix(';'))
    assert len(profiles) == 14
    for profile in profiles.values():
        tags = inventories[profile['language'] + '/' + profile['treebank']]
        validate_profile(profile, {'samples': 50, 'tokens': 100, 'same_as_upos': 0, 'tags': tags})


def test_duplicate_upos_or_invented_labels_are_rejected():
    profile = {'language': 'Test', 'examples': [{'tokens': ['A'], 'tags': ['NN']}]}
    pool = {'samples': 50, 'tokens': 100, 'same_as_upos': 100, 'tags': ['NN']}
    with pytest.raises(ValueError, match='duplicates'):
        validate_profile(profile, pool)
    with pytest.raises(ValueError, match='unknown'):
        validate_profile(profile, {**pool, 'same_as_upos': 0, 'tags': ['VB']})
