from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import linguistic_oj.qwen_api as qwen_api_module
from linguistic_oj.api import Principal

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


def test_qwen_api_composes_v2_contract_and_matching_redis_queue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(qwen_api_module, "RedisJobQueue", _Queue)
    runtime = qwen_api_module.build_qwen_api(
        root=ROOT,
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
        response = client.post(
            "/v1/submissions",
            headers={"Idempotency-Key": "qwen-api-composition"},
            json={
                "challenge_id": runtime.contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        )

    assert response.status_code == 202
    assert runtime.contract.contract_version == "mvp-evaluation-v2"
    assert runtime.queue.routing_key == runtime.contract.contract_snapshot_sha256
    assert runtime.queue.visibility_timeout_seconds == 315
    assert runtime.queue.health_checks == 1
    assert len(runtime.queue.published) == 1


def test_qwen_api_rejects_inline_production_redis_password() -> None:
    with pytest.raises(SystemExit):
        qwen_api_module.parse_args(
            [
                "--root",
                ".",
                "--database",
                "runtime/submissions.db",
                "--redis-url",
                "rediss://api:secret@redis.example/0",
                "--authenticate",
                "package.module:authenticate",
            ]
        )


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
