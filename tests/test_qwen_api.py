from pathlib import Path

from fastapi.testclient import TestClient

import linguistic_oj.qwen_api as qwen_api_module
from linguistic_oj.api import Principal

ROOT = Path(__file__).parents[1]


class _Queue:
    def __init__(self, **kwargs) -> None:
        self.routing_key = kwargs["routing_key"]
        self.visibility_timeout_seconds = kwargs["visibility_timeout_seconds"]
        self.published = []

    def publish(self, message) -> None:
        self.published.append(message)


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
    assert len(runtime.queue.published) == 1
