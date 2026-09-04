import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import linguistic_oj.qwen_api as qwen_api_module
from linguistic_oj.api import Principal
from linguistic_oj.mvp_contract import canonical_sha256
from linguistic_oj.qwen_runtime import QwenRuntimeAttestationError

ROOT = Path(__file__).parents[1]


class _Queue:
    def __init__(self, **kwargs) -> None:
        self.routing_key = kwargs["routing_key"]
        self.visibility_timeout_seconds = kwargs["visibility_timeout_seconds"]
        self.published = []
        self.health_checks = 0

    def publish(self, message) -> None:
        self.published.append(message)

    def health_check(self) -> None:
        self.health_checks += 1


def _write_qwen_registry(root: Path, *, count: int = 1) -> tuple[Path, tuple[str, ...]]:
    entries = []
    challenge_ids = []
    for index in range(count):
        suffix = chr(ord("a") + index)
        challenge_id = f"en-synthetic-{suffix}-upos-v1"
        public = json.loads(
            (ROOT / "challenges" / "public" / "en-ewt-upos-v1.json").read_text(
                encoding="utf-8"
            )
        )
        public.update(
            {
                "challenge_id": challenge_id,
                "dataset_sha256": suffix * 64,
                "selection_sha256": f"{suffix * 63}0",
                "title": f"Synthetic Qwen challenge {suffix}",
                "treebank": f"Synthetic {suffix}",
            }
        )
        contract = json.loads(
            (ROOT / "config" / "mvp_evaluation_v2.json").read_text(encoding="utf-8")
        )
        contract["catalog"]["challenge_id"] = challenge_id
        contract["evaluation_identity"].update(
            {
                "challenge_id": challenge_id,
                "dataset_sha256": public["dataset_sha256"],
                "selection_sha256": public["selection_sha256"],
            }
        )
        contract["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
            contract["evaluation_identity"]
        )
        public_path = root / "public" / f"{challenge_id}.json"
        contract_path = root / "contracts" / f"{challenge_id}.json"
        public_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text(json.dumps(public), encoding="utf-8")
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        entries.append(
            {
                "public_descriptor_path": f"public/{challenge_id}.json",
                "evaluation_contract_path": f"contracts/{challenge_id}.json",
            }
        )
        challenge_ids.append(challenge_id)
    registry_path = root / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "challenge-contract-registry-v1",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return registry_path, tuple(challenge_ids)


def test_qwen_api_composes_registry_with_one_queue_per_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(qwen_api_module, "RedisJobQueue", _Queue)
    registry_path, challenge_ids = _write_qwen_registry(tmp_path, count=2)
    runtime = qwen_api_module.build_qwen_api(
        root=tmp_path,
        challenge_registry_path=registry_path,
        database_path=tmp_path / "submissions.db",
        redis_url="redis://127.0.0.1:6379/0",
        authenticate=lambda request: Principal("subject-alice"),
        allow_draft_submissions=True,
        environment="development",
    )
    runtime.store.register_user(auth_subject="subject-alice", public_handle="alice")

    with TestClient(runtime.app) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        responses = [
            client.post(
                "/v1/submissions",
                headers={"Idempotency-Key": f"qwen-api-composition-{index}"},
                json={"challenge_id": challenge_id, "student_prompt": "Return JSON."},
            )
            for index, challenge_id in enumerate(challenge_ids)
        ]

    assert [response.status_code for response in responses] == [202, 202]
    assert set(runtime.queues) == set(challenge_ids)
    assert set(runtime.dispatchers) == set(challenge_ids)
    for challenge_id in challenge_ids:
        contract = runtime.registry.contracts[challenge_id]
        queue = runtime.queues[challenge_id]
        assert contract.contract_version == "mvp-evaluation-v2"
        assert queue.routing_key == contract.contract_snapshot_sha256
        assert queue.visibility_timeout_seconds == 315
        assert queue.health_checks == 1
        assert len(queue.published) == 1


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("prompt-envelope", "prompt envelope"),
        ("missing-generation-setting", "generation settings are incomplete"),
        ("generation-prompt", "enable the generation prompt"),
        ("thinking-control", "thinking controls do not match"),
    ],
)
def test_qwen_api_rejects_contract_worker_cannot_execute(
    tmp_path: Path,
    monkeypatch,
    case: str,
    error: str,
) -> None:
    registry_path, challenge_ids = _write_qwen_registry(tmp_path)
    contract_path = tmp_path / "contracts" / f"{challenge_ids[0]}.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if case == "prompt-envelope":
        contract["evaluation_identity"]["prompt_envelope_version"] = "unsupported"
    elif case == "missing-generation-setting":
        contract["evaluation_identity"]["generation_settings"].pop("seed")
    elif case == "generation-prompt":
        contract["evaluation_identity"]["tokenizer_identity"][
            "add_generation_prompt"
        ] = False
    else:
        contract["evaluation_identity"]["tokenizer_identity"]["enable_thinking"] = True
    contract["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        contract["evaluation_identity"]
    )
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(qwen_api_module, "RedisJobQueue", _Queue)

    with pytest.raises(QwenRuntimeAttestationError, match=error):
        qwen_api_module.build_qwen_api(
            root=tmp_path,
            challenge_registry_path=registry_path,
            database_path=tmp_path / "submissions.db",
            redis_url="redis://127.0.0.1:6379/0",
            authenticate=lambda request: Principal("subject-alice"),
            allow_draft_submissions=True,
            environment="development",
        )


def test_qwen_api_production_cli_requires_postgres() -> None:
    with pytest.raises(SystemExit):
        qwen_api_module.parse_args(
            [
                "--root",
                ".",
                "--challenge-registry",
                "config/registry.json",
                "--database",
                "runtime/submissions.db",
                "--redis-url",
                "redis://127.0.0.1:6379/0",
                "--authenticate",
                "package.module:authenticate",
            ]
        )


def test_qwen_api_rejects_inline_production_redis_password() -> None:
    with pytest.raises(SystemExit):
        qwen_api_module.parse_args(
            [
                "--root",
                ".",
                "--challenge-registry",
                "config/registry.json",
                "--database",
                "runtime/submissions.db",
                "--redis-url",
                "rediss://api:secret@redis.example/0",
                "--authenticate",
                "package.module:authenticate",
            ]
        )


def test_qwen_api_rejects_inline_production_postgres_password() -> None:
    with pytest.raises(SystemExit):
        qwen_api_module.parse_args(
            [
                "--root",
                ".",
                "--challenge-registry",
                "config/registry.json",
                "--postgres-database-url",
                "postgresql://api:secret@127.0.0.1/linguistic_oj",
                "--redis-url",
                "redis://127.0.0.1:6379/0",
                "--authenticate",
                "package.module:authenticate",
            ]
        )


def test_qwen_api_reads_production_postgres_credential_file(tmp_path: Path) -> None:
    credential = tmp_path / "postgres-url"
    credential.write_text(
        "postgresql://api:secret@db.example/linguistic_oj?sslmode=verify-full\n",
        encoding="utf-8",
    )

    args = qwen_api_module.parse_args(
        [
            "--root",
            ".",
            "--challenge-registry",
            "config/registry.json",
            "--postgres-database-url-file",
            str(credential),
            "--redis-url",
            "redis://127.0.0.1:6379/0",
            "--authenticate",
            "package.module:authenticate",
        ]
    )

    assert args.postgres_database_url.endswith("sslmode=verify-full")


def test_safe_request_logging_has_an_explicit_non_propagating_handler() -> None:
    logger = qwen_api_module.logging.getLogger("linguistic_oj.http")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    try:
        qwen_api_module._configure_safe_request_logging()

        assert logger.level == qwen_api_module.logging.INFO
        assert logger.propagate is False
        assert len(logger.handlers) == 1
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
