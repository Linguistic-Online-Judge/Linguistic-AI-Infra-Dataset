import hashlib
import json
from pathlib import Path

import pytest

from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.dataset import DatasetSample
from linguistic_oj.mvp_contract import canonical_sha256
from linguistic_oj.responses import TaskType
from scripts.audit_qwen_token_budgets import (
    _next_power_of_two,
    canonical_gold_response,
)

ROOT = Path(__file__).parents[1]


def _sample(task: str, gold: list) -> DatasetSample:
    answers = {"segmentation": ["A", "B"], task: gold}
    return DatasetSample.model_validate(
        {
            "id": "sample-1",
            "language": "English",
            "treebank": "Test",
            "text": "AB",
            "answers": answers,
            "tasks_available": [task],
        }
    )


@pytest.mark.parametrize(
    ("task", "gold", "expected"),
    [
        ("segmentation", ["A", "B"], {"tokens": ["A", "B"]}),
        ("upos", ["NOUN", "VERB"], {"tags": ["NOUN", "VERB"]}),
        ("xpos", ["NN", "VB"], {"tags": ["NN", "VB"]}),
        (
            "dependency",
            [[1, "A", 0, "ROOT", "root"], [2, "B", 1, "A", "dep"]],
            {
                "arcs": [
                    {"deprel": "root", "head_id": 0, "token_id": 1},
                    {"deprel": "dep", "head_id": 1, "token_id": 2},
                ]
            },
        ),
        (
            "transliteration",
            ["a", "b"],
            {"transliterations": ["a", "b"]},
        ),
    ],
)
def test_canonical_gold_response_uses_task_response_contract(
    task: str,
    gold: list,
    expected: dict,
) -> None:
    response = canonical_gold_response(_sample(task, gold), TaskType(task))

    assert json.loads(response) == expected
    assert " " not in response


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1), (175, 256), (257, 512), (504, 512), (513, 1024)],
)
def test_recommended_budget_uses_the_next_power_of_two(value: int, expected: int) -> None:
    assert _next_power_of_two(value) == expected


def test_recommended_budget_rejects_non_positive_values() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _next_power_of_two(0)


def test_repository_token_budget_evidence_matches_registered_contracts() -> None:
    report_path = (
        ROOT / "benchmarks" / "observations" / "qwen3.5-9b-v1-token-budget-audit.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_hash = report.pop("report_sha256")
    registry_path = ROOT / "config" / "challenge_contract_registry_v1.json"
    registry = load_challenge_contract_registry(ROOT, registry_path)

    assert report_hash == canonical_sha256(report)
    assert report["registry_sha256"] == hashlib.sha256(registry_path.read_bytes()).hexdigest()
    assert report["verdict"] == "pass"
    assert len(report["challenges"]) == len(registry.contracts) == 22
    assert {item["task"] for item in report["challenges"]} == {
        "dependency",
        "segmentation",
        "transliteration",
        "upos",
        "xpos",
    }
    for item in report["challenges"]:
        contract = registry.contracts[item["challenge_id"]]
        assert item["verdict"] == "pass"
        assert item["contract_snapshot_sha256"] == contract.contract_snapshot_sha256
        assert item["evaluation_identity_sha256"] == contract.evaluation_identity_sha256


def test_repository_five_task_gpu_smoke_matches_frozen_inputs() -> None:
    report_path = (
        ROOT / "benchmarks" / "observations" / "qwen3.5-9b-v1-five-task-gpu-smoke.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_hash = report.pop("report_sha256")
    registry = load_challenge_contract_registry(
        ROOT,
        ROOT / "config" / "challenge_contract_registry_v1.json",
    )
    token_report = json.loads(
        (
            ROOT
            / "benchmarks"
            / "observations"
            / "qwen3.5-9b-v1-token-budget-audit.json"
        ).read_text(encoding="utf-8")
    )

    assert report_hash == canonical_sha256(report)
    assert report["verdict"] == "pass"
    assert report["token_budget_audit_report_sha256"] == token_report["report_sha256"]
    assert report["checks"] == {
        "api_submission_http_202": True,
        "api_terminal_result_http_200": True,
        "contract_snapshots_match_repository": True,
        "five_task_families_succeeded": True,
        "logs_contain_credentials_or_prompt_bodies": False,
        "model_runtime_attestation_passed": True,
        "result_identities_match_contracts": True,
        "token_budget_audit_matches_repository": True,
    }
    assert {item["task"] for item in report["results"]} == {
        "dependency",
        "segmentation",
        "transliteration",
        "upos",
        "xpos",
    }
    assert len(report["results"]) == 5
    for item in report["results"]:
        contract = registry.contracts[item["challenge_id"]]
        prompt = ROOT / item["prompt_path"]
        model_identity = contract.evaluation_identity["model_identity"]
        assert item["outcome"] == "succeeded"
        assert item["contract_snapshot_sha256"] == contract.contract_snapshot_sha256
        assert item["evaluation_identity_sha256"] == contract.evaluation_identity_sha256
        assert item["student_prompt_sha256"] == hashlib.sha256(prompt.read_bytes()).hexdigest()
        assert item["samples_total"] == 50
        assert item["samples_valid"] > 0
        assert item["samples_valid"] + item["samples_invalid"] == item["samples_total"]
        assert sum(item["errors"].values()) == item["samples_invalid"]
        assert model_identity["model"] == report["runtime"]["model"]
        assert model_identity["revision"] == report["runtime"]["model_revision"]
        assert model_identity["runtime"] == report["runtime"]["runtime"]
        assert model_identity["runtime_version"] == report["runtime"]["runtime_version"]

    dependency = registry.contracts["de-hdt-dependency-v1"]
    frozen_calibration = report["dependency_deadline_calibration"][-1]
    assert dependency.job_deadline_seconds == 900
    assert frozen_calibration["deadline_seconds"] == dependency.job_deadline_seconds
    assert frozen_calibration["contract_snapshot_sha256"] == (
        dependency.contract_snapshot_sha256
    )
