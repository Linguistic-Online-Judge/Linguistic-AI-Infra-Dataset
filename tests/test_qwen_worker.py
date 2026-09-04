import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import linguistic_oj.qwen_worker as qwen_worker_module
from linguistic_oj.challenge import PublicChallenge
from linguistic_oj.challenge_registry import ChallengeContractRegistry
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.qwen_runtime import QwenRuntimeAttestationError
from linguistic_oj.qwen_worker import build_worker, parse_args

ROOT = Path(__file__).parents[1]


def _synthetic_registry() -> ChallengeContractRegistry:
    challenge_id = "en-synthetic-upos-v1"
    public_mapping = json.loads(
        (ROOT / "challenges" / "public" / "en-ewt-upos-v1.json").read_text(
            encoding="utf-8"
        )
    )
    public_mapping.update(
        {
            "challenge_id": challenge_id,
            "dataset_sha256": "a" * 64,
            "selection_sha256": f"{'a' * 63}0",
            "title": "Synthetic Qwen challenge",
            "treebank": "Synthetic",
        }
    )
    contract_mapping = json.loads(
        (ROOT / "config" / "mvp_evaluation_v2.json").read_text(encoding="utf-8")
    )
    contract_mapping["catalog"]["challenge_id"] = challenge_id
    contract_mapping["evaluation_identity"].update(
        {
            "challenge_id": challenge_id,
            "dataset_sha256": public_mapping["dataset_sha256"],
            "selection_sha256": public_mapping["selection_sha256"],
        }
    )
    contract_mapping["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        contract_mapping["evaluation_identity"]
    )
    public = PublicChallenge.model_validate_json(json.dumps(public_mapping))
    contract = EvaluationContract.from_mapping(contract_mapping)
    return ChallengeContractRegistry(
        public_challenges={challenge_id: public},
        contracts={challenge_id: contract},
    )


def _build_args(challenge_id: str = "en-synthetic-upos-v1") -> SimpleNamespace:
    return SimpleNamespace(
        challenge_id=challenge_id,
        challenge_registry=Path("config/registry.json"),
        consumer_name="worker-test",
        database=Path("runtime/submissions.db"),
        dataset=Path("runtime/dataset.jsonl"),
        environment="development",
        launch_evidence=Path("runtime/qwen-launch.json"),
        namespace="test",
        postgres_database_url=None,
        private_challenge=Path("runtime/private.json"),
        public_challenge=Path("runtime/public.json"),
        redis_url="redis://127.0.0.1:6379/0",
        root=ROOT,
        tokenizer_snapshot=Path("runtime/tokenizer"),
        vllm_base_url="http://127.0.0.1:8000/v1",
    )


def test_qwen_worker_cli_requires_deployment_owned_inputs() -> None:
    args = parse_args(
        [
            "--root",
            ".",
            "--challenge-registry",
            "config/registry.json",
            "--challenge-id",
            "en-ewt-upos-v1",
            "--database",
            "runtime/submissions.db",
            "--redis-url",
            "redis://127.0.0.1:6379/0",
            "--public-challenge",
            "challenges/public/en-ewt-upos-v1.json",
            "--private-challenge",
            "runtime/private/challenges/en-ewt-upos-v1.json",
            "--dataset",
            "Standard_Dataset/standard_dataset.jsonl",
            "--vllm-base-url",
            "http://127.0.0.1:8000/v1",
            "--tokenizer-snapshot",
            "runtime/models/c202236235762e1c871ad0ccb60c8ee5ba337b9a",
            "--launch-evidence",
            "runtime/qwen-launch.json",
            "--once",
            "--environment",
            "development",
        ]
    )

    assert args.database == Path("runtime/submissions.db")
    assert args.challenge_registry == Path("config/registry.json")
    assert args.challenge_id == "en-ewt-upos-v1"
    assert args.once is True


def test_qwen_worker_cli_rejects_non_positive_idle_sleep() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--root",
                ".",
                "--challenge-registry",
                "config/registry.json",
                "--challenge-id",
                "en-ewt-upos-v1",
                "--database",
                "runtime/submissions.db",
                "--redis-url",
                "redis://127.0.0.1:6379/0",
                "--public-challenge",
                "challenges/public/en-ewt-upos-v1.json",
                "--private-challenge",
                "runtime/private/challenges/en-ewt-upos-v1.json",
                "--dataset",
                "Standard_Dataset/standard_dataset.jsonl",
                "--vllm-base-url",
                "http://127.0.0.1:8000/v1",
                "--tokenizer-snapshot",
                "runtime/models/c202236235762e1c871ad0ccb60c8ee5ba337b9a",
                "--launch-evidence",
                "runtime/qwen-launch.json",
                "--idle-sleep-seconds",
                "0",
                "--environment",
                "development",
            ]
        )


def test_qwen_worker_cli_reads_redis_credential_file(tmp_path: Path) -> None:
    credential = tmp_path / "redis-url"
    credential.write_text("rediss://worker:secret@redis.example/0\n", encoding="utf-8")
    args = parse_args(
        [
            "--root",
            ".",
            "--challenge-registry",
            "config/registry.json",
            "--challenge-id",
            "en-ewt-upos-v1",
            "--database",
            "runtime/submissions.db",
            "--redis-url-file",
            str(credential),
            "--public-challenge",
            "challenges/public/en-ewt-upos-v1.json",
            "--private-challenge",
            "runtime/private/challenges/en-ewt-upos-v1.json",
            "--dataset",
            "Standard_Dataset/standard_dataset.jsonl",
            "--vllm-base-url",
            "http://127.0.0.1:8000/v1",
            "--tokenizer-snapshot",
            "runtime/models/c202236235762e1c871ad0ccb60c8ee5ba337b9a",
            "--launch-evidence",
            "runtime/qwen-launch.json",
            "--once",
            "--environment",
            "development",
        ]
    )

    assert args.redis_url == "rediss://worker:secret@redis.example/0"


def test_qwen_worker_production_cli_requires_postgres() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--root",
                ".",
                "--challenge-registry",
                "config/registry.json",
                "--challenge-id",
                "en-ewt-upos-v1",
                "--database",
                "runtime/submissions.db",
                "--redis-url",
                "redis://127.0.0.1:6379/0",
                "--public-challenge",
                "challenges/public/en-ewt-upos-v1.json",
                "--private-challenge",
                "runtime/private/challenges/en-ewt-upos-v1.json",
                "--dataset",
                "Standard_Dataset/standard_dataset.jsonl",
                "--vllm-base-url",
                "http://127.0.0.1:8000/v1",
                "--tokenizer-snapshot",
                "runtime/models/c202236235762e1c871ad0ccb60c8ee5ba337b9a",
                "--launch-evidence",
                "runtime/qwen-launch.json",
            ]
        )


def test_build_worker_selects_registry_contract_and_queue_route(monkeypatch) -> None:
    registry = _synthetic_registry()
    contract = registry.contracts["en-synthetic-upos-v1"]
    artifacts = SimpleNamespace(public=registry.public_challenges[contract.challenge_id])
    store = object()

    class Provider:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Queue:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Worker:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(
        qwen_worker_module,
        "load_challenge_contract_registry",
        lambda root, path: registry,
    )
    monkeypatch.setattr(
        qwen_worker_module,
        "load_challenge_artifacts",
        lambda public, private, *, dataset_path: artifacts,
    )
    monkeypatch.setattr(qwen_worker_module, "OpenAICompatibleProvider", Provider)
    monkeypatch.setattr(qwen_worker_module, "RedisJobQueue", Queue)
    monkeypatch.setattr(qwen_worker_module, "QwenSubmissionWorker", Worker)
    monkeypatch.setattr(qwen_worker_module, "build_submission_store", lambda **kwargs: store)

    worker = build_worker(_build_args())

    assert isinstance(worker, Worker)
    assert worker.kwargs["contract"] is contract
    assert worker.kwargs["artifacts"] is artifacts
    assert worker.kwargs["store"] is store
    assert worker.kwargs["queue"].kwargs["routing_key"] == contract.contract_snapshot_sha256


def test_build_worker_rejects_unknown_and_public_only_challenges(monkeypatch) -> None:
    registry = _synthetic_registry()
    monkeypatch.setattr(
        qwen_worker_module,
        "load_challenge_contract_registry",
        lambda root, path: registry,
    )

    with pytest.raises(ValueError, match="challenge is not registered"):
        build_worker(_build_args("en-missing-upos-v1"))

    public_only = ChallengeContractRegistry(
        public_challenges=registry.public_challenges,
        contracts={},
    )
    monkeypatch.setattr(
        qwen_worker_module,
        "load_challenge_contract_registry",
        lambda root, path: public_only,
    )
    with pytest.raises(ValueError, match="has no evaluation contract"):
        build_worker(_build_args())


def test_build_worker_rejects_public_artifact_outside_registry(monkeypatch) -> None:
    registry = _synthetic_registry()
    expected_public = registry.public_challenges["en-synthetic-upos-v1"]
    mismatched_artifacts = SimpleNamespace(
        public=expected_public.model_copy(update={"title": "Different synthetic challenge"})
    )
    monkeypatch.setattr(
        qwen_worker_module,
        "load_challenge_contract_registry",
        lambda root, path: registry,
    )
    monkeypatch.setattr(
        qwen_worker_module,
        "load_challenge_artifacts",
        lambda public, private, *, dataset_path: mismatched_artifacts,
    )

    with pytest.raises(ValueError, match="public challenge does not match the registry"):
        build_worker(_build_args())


def test_build_worker_rejects_static_contract_mismatch_before_artifacts(monkeypatch) -> None:
    registry = _synthetic_registry()
    contract = registry.contracts["en-synthetic-upos-v1"]
    mapping = json.loads(contract.snapshot_json)
    mapping["evaluation_identity"]["prompt_envelope_version"] = "unsupported"
    mapping["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        mapping["evaluation_identity"]
    )
    invalid_contract = EvaluationContract.from_mapping(mapping)
    invalid_registry = ChallengeContractRegistry(
        public_challenges=registry.public_challenges,
        contracts={invalid_contract.challenge_id: invalid_contract},
    )
    monkeypatch.setattr(
        qwen_worker_module,
        "load_challenge_contract_registry",
        lambda root, path: invalid_registry,
    )
    monkeypatch.setattr(
        qwen_worker_module,
        "load_challenge_artifacts",
        lambda *args, **kwargs: pytest.fail("artifacts loaded before contract validation"),
    )

    with pytest.raises(QwenRuntimeAttestationError, match="prompt envelope"):
        build_worker(_build_args())
