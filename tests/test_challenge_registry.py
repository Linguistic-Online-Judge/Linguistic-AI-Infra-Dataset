import json
from pathlib import Path

import pytest

from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.mvp_contract import canonical_sha256


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _public_challenge(
    *,
    language: str,
    treebank: str,
    task: str,
    challenge_id: str,
    fingerprint: str,
) -> dict[str, object]:
    metrics = {
        "segmentation": ("micro_f1", ["micro_precision", "micro_recall"]),
        "upos": ("micro_accuracy", []),
    }
    primary_metric, secondary_metrics = metrics[task]
    return {
        "aggregation_version": "1.0",
        "challenge_id": challenge_id,
        "dataset_sha256": fingerprint * 64,
        "language": language,
        "primary_metric": primary_metric,
        "response_schema_version": f"{task}-v1",
        "sample_count": 12,
        "scorer_version": "1.0",
        "secondary_metrics": secondary_metrics,
        "security_level": "public_reproducible",
        "selection_sha256": f"{fingerprint * 63}0",
        "status": "draft",
        "task": task,
        "title": f"Synthetic {task} challenge",
        "treebank": treebank,
        "version": "v1",
    }


def _evaluation_contract(public: dict[str, object]) -> dict[str, object]:
    contract_version = "synthetic-evaluation-v1"
    identity = {
        "aggregation_version": public["aggregation_version"],
        "challenge_id": public["challenge_id"],
        "contract_version": contract_version,
        "dataset_sha256": public["dataset_sha256"],
        "response_schema_version": public["response_schema_version"],
        "scorer_version": public["scorer_version"],
        "selection_sha256": public["selection_sha256"],
        "task": public["task"],
    }
    return {
        "catalog": {
            "challenge_id": public["challenge_id"],
            "security_level": public["security_level"],
            "status": public["status"],
        },
        "contract_version": contract_version,
        "evaluation_identity": identity,
        "failure_contract": {
            "codes": {"TERMINAL": {"retryable": False}},
            "version": "synthetic-failure-v1",
        },
        "feedback": {
            "owner_failure_fields": [],
            "owner_result_fields": [],
            "public_leaderboard_fields": [],
        },
        "idempotency": {
            "key_ascii_pattern": "^[A-Za-z0-9]{1,32}$",
            "key_match_semantics": "ascii_fullmatch",
        },
        "job_policy": {
            "job_deadline_seconds": 30,
            "max_attempts": 1,
            "provider_request_timeout_seconds": 10,
            "retry_requires_prior_request_terminated": True,
            "retryable_failure_codes": [],
        },
        "leaderboard_partition": {
            "algorithm": "sha256",
            "canonical_json_source": "evaluation_identity",
            "canonicalization": "python-json-v1",
            "canonicalization_parameters": {
                "allow_nan": False,
                "ensure_ascii": False,
                "separators": [",", ":"],
                "sort_keys": True,
            },
            "expected_sha256": canonical_sha256(identity),
        },
        "limits": {
            "api_request_body_bytes": 1024,
            "global_queue_depth": 10,
            "max_outstanding_submissions_per_user": 2,
            "max_rendered_input_tokens": 256,
            "max_running_submissions_per_user": 1,
            "model_context_tokens": 512,
            "provider_response_body_bytes": 2048,
            "student_prompt_tokens": 128,
            "student_prompt_utf8_bytes": 512,
            "submissions_per_user_per_challenge_per_24h": 5,
            "worker_model_concurrency": 1,
        },
    }


def _write_registry(root: Path, entries: list[dict[str, object]]) -> Path:
    registry_path = root / "registry.json"
    _write_json(
        registry_path,
        {
            "schema_version": "challenge-contract-registry-v1",
            "entries": entries,
        },
    )
    return registry_path


def _write_challenge_pair(
    root: Path,
    public: dict[str, object],
    name: str,
) -> dict[str, object]:
    public_path = f"public/{name}.json"
    contract_path = f"contracts/{name}.json"
    _write_json(root / public_path, public)
    _write_json(root / contract_path, _evaluation_contract(public))
    return {
        "public_descriptor_path": public_path,
        "evaluation_contract_path": contract_path,
    }


def test_registry_loads_two_synthetic_challenges_as_read_only_maps(tmp_path: Path) -> None:
    first = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    second = _public_challenge(
        language="Chinese",
        treebank="Fictional",
        task="segmentation",
        challenge_id="zh-fictional-segmentation-v1",
        fingerprint="b",
    )
    registry_path = _write_registry(
        tmp_path,
        [
            _write_challenge_pair(tmp_path, first, "first"),
            _write_challenge_pair(tmp_path, second, "second"),
        ],
    )

    registry = load_challenge_contract_registry(tmp_path, registry_path)

    assert set(registry.public_challenges) == {
        "en-synthetic-upos-v1",
        "zh-fictional-segmentation-v1",
    }
    assert set(registry.contracts) == set(registry.public_challenges)
    with pytest.raises(TypeError):
        registry.public_challenges["new-challenge"] = registry.public_challenges[
            "en-synthetic-upos-v1"
        ]
    with pytest.raises(TypeError):
        registry.contracts["new-challenge"] = registry.contracts["en-synthetic-upos-v1"]


def test_registry_rejects_duplicate_challenge_ids(tmp_path: Path) -> None:
    public = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    _write_json(tmp_path / "public/first.json", public)
    _write_json(tmp_path / "public/second.json", public)
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/first.json",
                "evaluation_contract_path": None,
            },
            {
                "public_descriptor_path": "public/second.json",
                "evaluation_contract_path": None,
            },
        ],
    )

    with pytest.raises(ValueError, match="duplicate challenge ID"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_rejects_duplicate_public_descriptor_paths(tmp_path: Path) -> None:
    public = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    _write_json(tmp_path / "public/challenge.json", public)
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/challenge.json",
                "evaluation_contract_path": None,
            },
            {
                "public_descriptor_path": "public/./challenge.json",
                "evaluation_contract_path": None,
            },
        ],
    )

    with pytest.raises(ValueError, match="duplicate public descriptor path"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_rejects_duplicate_evaluation_contract_paths(tmp_path: Path) -> None:
    first = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    second = _public_challenge(
        language="Chinese",
        treebank="Fictional",
        task="segmentation",
        challenge_id="zh-fictional-segmentation-v1",
        fingerprint="b",
    )
    _write_json(tmp_path / "public/first.json", first)
    _write_json(tmp_path / "public/second.json", second)
    _write_json(tmp_path / "contracts/shared.json", _evaluation_contract(first))
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/first.json",
                "evaluation_contract_path": "contracts/shared.json",
            },
            {
                "public_descriptor_path": "public/second.json",
                "evaluation_contract_path": "contracts/shared.json",
            },
        ],
    )

    with pytest.raises(ValueError, match="duplicate evaluation contract path"):
        load_challenge_contract_registry(tmp_path, registry_path)


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    [
        ("catalog", "status", "active"),
        ("catalog", "security_level", "private"),
        ("evaluation_identity", "dataset_sha256", "c" * 64),
        ("evaluation_identity", "selection_sha256", "d" * 64),
        ("evaluation_identity", "task", "segmentation"),
        ("evaluation_identity", "response_schema_version", "segmentation-v1"),
        ("evaluation_identity", "scorer_version", "2.0"),
        ("evaluation_identity", "aggregation_version", "2.0"),
    ],
)
def test_registry_rejects_public_contract_mismatches(
    tmp_path: Path,
    section: str,
    field: str,
    replacement: str,
) -> None:
    public = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    contract = _evaluation_contract(public)
    target = contract[section]
    assert isinstance(target, dict)
    target[field] = replacement
    if section == "evaluation_identity":
        partition = contract["leaderboard_partition"]
        assert isinstance(partition, dict)
        partition["expected_sha256"] = canonical_sha256(target)

    _write_json(tmp_path / "public/challenge.json", public)
    _write_json(tmp_path / "contracts/challenge.json", contract)
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/challenge.json",
                "evaluation_contract_path": "contracts/challenge.json",
            }
        ],
    )

    with pytest.raises(ValueError, match=field):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_rejects_contract_with_a_different_challenge_id(tmp_path: Path) -> None:
    public = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    contract = _evaluation_contract(public)
    catalog = contract["catalog"]
    identity = contract["evaluation_identity"]
    partition = contract["leaderboard_partition"]
    assert isinstance(catalog, dict)
    assert isinstance(identity, dict)
    assert isinstance(partition, dict)
    catalog["challenge_id"] = "en-other-upos-v1"
    identity["challenge_id"] = "en-other-upos-v1"
    partition["expected_sha256"] = canonical_sha256(identity)

    _write_json(tmp_path / "public/challenge.json", public)
    _write_json(tmp_path / "contracts/challenge.json", contract)
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/challenge.json",
                "evaluation_contract_path": "contracts/challenge.json",
            }
        ],
    )

    with pytest.raises(ValueError, match="challenge_id"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_requires_runtime_versions_when_contract_is_present(tmp_path: Path) -> None:
    public = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    contract = _evaluation_contract(public)
    public.pop("scorer_version")
    public.pop("aggregation_version")
    identity = contract["evaluation_identity"]
    partition = contract["leaderboard_partition"]
    assert isinstance(identity, dict)
    assert isinstance(partition, dict)
    identity.pop("scorer_version")
    identity.pop("aggregation_version")
    partition["expected_sha256"] = canonical_sha256(identity)

    _write_json(tmp_path / "public/challenge.json", public)
    _write_json(tmp_path / "contracts/challenge.json", contract)
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/challenge.json",
                "evaluation_contract_path": "contracts/challenge.json",
            }
        ],
    )

    with pytest.raises(ValueError, match="requires public scorer and aggregation versions"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_loads_public_only_descriptor_without_runtime_versions(tmp_path: Path) -> None:
    public = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    public.pop("scorer_version")
    public.pop("aggregation_version")
    _write_json(tmp_path / "public/challenge.json", public)
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/challenge.json",
                "evaluation_contract_path": None,
            }
        ],
    )

    registry = load_challenge_contract_registry(tmp_path, registry_path)

    assert set(registry.public_challenges) == {"en-synthetic-upos-v1"}
    assert not registry.contracts


def test_registry_rejects_invalid_public_only_descriptor(tmp_path: Path) -> None:
    public = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    public["primary_metric"] = "invalid_metric"
    _write_json(tmp_path / "public/challenge.json", public)
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/challenge.json",
                "evaluation_contract_path": None,
            }
        ],
    )

    with pytest.raises(ValueError, match="public metrics do not match"):
        load_challenge_contract_registry(tmp_path, registry_path)


@pytest.mark.parametrize(
    "path",
    [
        ".",
        "../outside.json",
        "/outside.json",
        "C:/outside.json",
        "public\\challenge.json",
        "public/challenge.json:stream",
    ],
)
def test_registry_rejects_invalid_public_descriptor_paths(
    tmp_path: Path,
    path: str,
) -> None:
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": path,
                "evaluation_contract_path": None,
            }
        ],
    )

    with pytest.raises(ValueError, match="root-relative POSIX path|stay below the project root"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_rejects_contract_path_outside_project_root(tmp_path: Path) -> None:
    public = _public_challenge(
        language="English",
        treebank="Synthetic",
        task="upos",
        challenge_id="en-synthetic-upos-v1",
        fingerprint="a",
    )
    _write_json(tmp_path / "public/challenge.json", public)
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "public_descriptor_path": "public/challenge.json",
                "evaluation_contract_path": "../outside.json",
            }
        ],
    )

    with pytest.raises(ValueError, match="stay below the project root"):
        load_challenge_contract_registry(tmp_path, registry_path)
