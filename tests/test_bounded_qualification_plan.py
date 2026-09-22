from pathlib import Path

import pytest

from linguistic_oj.mvp_contract import EvaluationContract
from scripts.qualify_bounded_qwen import calibration_contract, profiles, submission_plan

ROOT = Path(__file__).parents[1]


def test_qualification_preserves_source_identity_settings_and_files():
    path = ROOT / 'config/evaluation_contracts/v1/de-hdt-dependency-v1.json'
    original = path.read_bytes()
    base = EvaluationContract.from_path(path)
    for capacity in (1, 2, 4, 8, 16, 32):
        derived = calibration_contract(base, capacity)
        assert derived.worker_model_concurrency == capacity
        assert derived.evaluation_identity == base.evaluation_identity
        assert derived.job_deadline_seconds == base.job_deadline_seconds
        assert derived.max_attempts == base.max_attempts
    assert path.read_bytes() == original
    assert base.worker_model_concurrency == 1
    with pytest.raises(ValueError):
        calibration_contract(base, 64)


def test_four_fixed_prompts_cover_two_tasks_before_any_model_call():
    selected = profiles(ROOT)
    assert len(selected) == 4
    assert len({entry[2] for entry in selected}) == 2
    assert len({entry[3].read_text(encoding='utf-8') for entry in selected}) == 4


def test_repeats_have_independent_users_and_fixed_round_order():
    identifiers = ('en-a', 'en-b', 'de-a', 'de-b')
    jobs = submission_plan(identifiers)
    assert len(jobs) == len({job['owner'] for job in jobs}) == 8
    assert [(job['profile'], job['repetition']) for job in jobs] == [
        (profile, repetition) for repetition in (1, 2) for profile in identifiers]
