import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import linguistic_oj.api as api_module
import linguistic_oj.qwen_runtime as qwen_runtime_module
import linguistic_oj.submission_jobs as submission_jobs_module
import linguistic_oj.submission_store as submission_store_module
from linguistic_oj.api import Principal, RequestBodyLimitMiddleware, create_app
from linguistic_oj.challenge import ChallengeArtifacts, build_challenge
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.providers import (
    DeterministicMockProvider,
    GenerationSettings,
    ModelGeneration,
    ModelIdentity,
    OpenAICompatibleProvider,
    ProviderTimeoutError,
    ProviderTransportError,
    deterministic_mock_generation_settings,
    deterministic_mock_model_identity,
    deterministic_mock_tokenizer_identity,
)
from linguistic_oj.qwen_runtime import TokenizerIdentity
from linguistic_oj.submission_jobs import (
    InMemoryJobQueue,
    JobMessage,
    OutboxDispatcher,
    QwenSubmissionWorker,
    SubmissionWorker,
)
from linguistic_oj.submission_store import SubmissionStore

ROOT = Path(__file__).parents[1]


def _sample(sample_id: str, text: str) -> dict[str, object]:
    tokens = list(text)
    return {
        "id": sample_id,
        "language": "Test",
        "treebank": "Tiny",
        "text": text,
        "answers": {
            "segmentation": tokens,
            "upos": ["X"] * len(tokens),
        },
        "tasks_available": ["segmentation", "upos"],
    }


def _artifacts(
    tmp_path: Path,
    *,
    task: str = "upos",
    version: str = "v1",
) -> ChallengeArtifacts:
    dataset_path = tmp_path / "dataset.jsonl"
    samples = [_sample("sample-b", "CD"), _sample("sample-a", "AB")]
    dataset_path.write_text(
        "".join(f"{json.dumps(sample, ensure_ascii=False)}\n" for sample in samples),
        encoding="utf-8",
    )
    return build_challenge(
        dataset_path,
        language="Test",
        treebank="Tiny",
        task=task,
        count=2,
        seed=2026,
        version=version,
    )


def _mock_contract(
    artifacts: ChallengeArtifacts,
    *,
    contract_version: str = "mock-evaluation-v1",
) -> EvaluationContract:
    config = json.loads((ROOT / "config" / "mvp_evaluation.json").read_text(encoding="utf-8"))
    config["contract_version"] = contract_version
    config["catalog"].update(
        artifacts.public.model_dump(
            mode="json",
            include={
                "challenge_id",
                "annotation_license",
                "attribution_requirements",
                "source_release",
                "source_commit",
                "source_file_sha256s",
                "share_alike_requirements",
                "underlying_text_rights",
            },
        )
    )
    identity = config["evaluation_identity"]
    identity.update(
        {
            "challenge_id": artifacts.public.challenge_id,
            "contract_version": config["contract_version"],
            "dataset_sha256": artifacts.public.dataset_sha256,
            "generation_settings": deterministic_mock_generation_settings(),
            "model_identity": deterministic_mock_model_identity(),
            "response_schema_version": artifacts.public.response_schema_version,
            "selection_sha256": artifacts.public.selection_sha256,
            "task": artifacts.public.task,
            "tokenizer_identity": deterministic_mock_tokenizer_identity(),
        }
    )
    config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(identity)
    return EvaluationContract.from_mapping(config)


_TEST_CHAT_TEMPLATE = "<|im_start|>{{ messages }}<|im_end|>"


def _qwen_snapshot(tmp_path: Path) -> tuple[Path, Path, TokenizerIdentity]:
    revision = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
    snapshot_path = tmp_path / "qwen-snapshot" / revision
    snapshot_path.mkdir(parents=True)
    (snapshot_path / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": _TEST_CHAT_TEMPLATE}),
        encoding="utf-8",
    )
    (snapshot_path / "tokenizer.json").write_bytes(b"test tokenizer")
    launch_evidence_path = tmp_path / "qwen-launch.json"
    launch_evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "linguistic-oj-vllm-launch-v1",
                "model_snapshot_path": str(snapshot_path.resolve()),
                "runtime_version": "0.27.1+cu129",
                "max_model_len": 4096,
                "max_num_seqs": 1,
                "language_model_only": True,
            }
        ),
        encoding="utf-8",
    )
    return (
        snapshot_path,
        launch_evidence_path,
        TokenizerIdentity.from_snapshot(
            snapshot_path,
            repository="Qwen/Qwen3.5-9B",
            revision=revision,
        ),
    )


def _qwen_contract(
    artifacts: ChallengeArtifacts,
    tokenizer_identity: TokenizerIdentity,
) -> EvaluationContract:
    config = json.loads(
        (ROOT / "config" / "mvp_evaluation_v2.json").read_text(encoding="utf-8")
    )
    config["catalog"].update(
        artifacts.public.model_dump(
            mode="json",
            include={
                "challenge_id",
                "annotation_license",
                "attribution_requirements",
                "source_release",
                "source_commit",
                "source_file_sha256s",
                "share_alike_requirements",
                "underlying_text_rights",
            },
        )
    )
    identity = config["evaluation_identity"]
    identity.update(
        {
            "challenge_id": artifacts.public.challenge_id,
            "dataset_sha256": artifacts.public.dataset_sha256,
            "response_schema_version": artifacts.public.response_schema_version,
            "selection_sha256": artifacts.public.selection_sha256,
            "task": artifacts.public.task,
            "tokenizer_identity": tokenizer_identity.to_dict(),
        }
    )
    config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(identity)
    return EvaluationContract.from_mapping(config)


def _authenticate(request: Request) -> Principal:
    return Principal(request.headers.get("X-Test-Subject", ""))


class _RecordingMockProvider(DeterministicMockProvider):
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request, *, timeout_seconds=None):
        self.calls += 1
        return super().generate(request, timeout_seconds=timeout_seconds)


class _ExplodingMockProvider(DeterministicMockProvider):
    def generate(self, request, *, timeout_seconds=None):
        raise RuntimeError("private provider detail")


class _TransportFailureMockProvider(DeterministicMockProvider):
    def __init__(self, *, failures: int) -> None:
        self.calls = 0
        self.failures = failures

    def generate(self, request, *, timeout_seconds=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderTransportError("temporary transport detail")
        return super().generate(request, timeout_seconds=timeout_seconds)


class _TestQwenTokenizer:
    chat_template = _TEST_CHAT_TEMPLATE

    def encode(self, text, *, add_special_tokens):
        return [1, 2, 3]

    def apply_chat_template(
        self,
        conversation,
        *,
        tokenize,
        add_generation_prompt,
        enable_thinking,
    ):
        return list(range(100))


class _TestQwenProvider(OpenAICompatibleProvider):
    def __init__(self, contract: EvaluationContract, failures: list[Exception] | None = None):
        identity = contract.evaluation_identity["model_identity"]
        settings = contract.evaluation_identity["generation_settings"]
        super().__init__(
            base_url="http://127.0.0.1:8000/v1",
            identity=ModelIdentity(**identity),
            settings=GenerationSettings(**settings),
            timeout_seconds=contract.provider_request_timeout_seconds,
            max_response_body_bytes=contract.provider_response_body_bytes,
        )
        self.failures = list(failures or [])
        self.calls = 0

    def generate(self, request, *, timeout_seconds=None):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return ModelGeneration(raw_text='{"tags":["X","X"]}')

    def served_model_ids(self) -> frozenset[str]:
        return frozenset({self.identity.model})


@pytest.mark.parametrize('cached', [False, True])
def test_qwen_worker_full_50_prompt_fidelity_and_owner_isolation(tmp_path, monkeypatch, cached):
    import linguistic_oj.runner as runner
    import linguistic_oj.sample_cache as cache_module
    from linguistic_oj.providers import ModelRequest
    from linguistic_oj.responses import TaskType

    path = tmp_path / 'fifty.jsonl'
    path.write_text(''.join(json.dumps(_sample(f'sample-{i}', f'{i:02d}')) + '\n'
                            for i in range(50)), encoding='utf-8')
    artifacts = build_challenge(path, language='Test', treebank='Tiny', task='upos',
                                count=50, seed=2026, version='worker-cache-v1')
    prepared = runner._prepare_samples(artifacts)
    snapshot, evidence, identity = _qwen_snapshot(tmp_path)
    contract = _qwen_contract(artifacts, identity)
    store = SubmissionStore(tmp_path / 'cache-worker.db')
    for name in ('alice', 'bob'):
        store.register_user(auth_subject='subject-' + name, public_handle=name)
    queue = InMemoryJobQueue(contract.contract_snapshot_sha256,
                             visibility_timeout_seconds=contract.job_deadline_seconds + 15)
    prompts = ['  返回 X。\r\né e\u0301  ', '\nReturn NOUN. العربية\n']

    class PromptProvider(_TestQwenProvider):
        def __init__(self):
            super().__init__(contract)
            self.bodies = []

        def generate(self, request, *, timeout_seconds=None):
            self.bodies.append(self._request_body(request))
            label = 'X' if request.student_prompt == prompts[0] else 'NOUN'
            return ModelGeneration(json.dumps({'tags': [label] * len(request.model_input.tokens)}))

    provider = PromptProvider()
    monkeypatch.setattr(qwen_runtime_module, 'load_huggingface_tokenizer',
                        lambda path: _TestQwenTokenizer())
    parses = []
    original = runner.load_dataset_samples_by_id

    def load(*args, **kwargs):
        parses.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, 'load_dataset_samples_by_id', load)
    monkeypatch.setattr(cache_module, 'load_dataset_samples_by_id', load)
    worker = QwenSubmissionWorker(store=store, queue=queue, contract=contract,
        artifacts=artifacts, provider=provider, tokenizer_snapshot_path=snapshot,
        launch_evidence_path=evidence,
        selection_cache=cache_module.VerifiedSelectionCache() if cached else None)
    app = create_app(store=store, dispatcher=OutboxDispatcher(store, queue, contract),
        contract=contract, authenticate=_authenticate, allow_draft_submissions=True,
        environment='test')
    with TestClient(app) as client:
        for index, (name, prompt) in enumerate(zip(('alice', 'bob'), prompts, strict=True)):
            headers = _headers('subject-' + name, 'fidelity-' + name)
            created = client.post('/v1/submissions', headers=headers,
                json={'challenge_id': contract.challenge_id, 'student_prompt': prompt})
            assert created.status_code == 202
            sid = created.json()['submission_id']
            assert client.get(f'/v1/submissions/{sid}/result', headers=headers).status_code == 409
            assert worker.run_once()
            result = client.get(f'/v1/submissions/{sid}/result', headers=headers).json()
            assert result['outcome'] == 'succeeded'
            assert result['samples_total'] == result['samples_valid'] == 50
            assert result['score'] == (1.0 if index == 0 else 0.0)
            assert result['student_prompt_sha256'] == hashlib.sha256(prompt.encode()).hexdigest()
            saved = client.get(f'/v1/submissions/{sid}/prompt', headers=headers).json()
            assert saved['student_prompt'] == prompt
            other = _headers('subject-bob' if name == 'alice' else 'subject-alice')
            assert client.get(f'/v1/submissions/{sid}/result', headers=other).status_code == 404
            expected = [provider._request_body(ModelRequest(task=TaskType.UPOS,
                language='Test', treebank='Tiny', student_prompt=prompt,
                model_input=item.model_input))
                for item in prepared]
            assert provider.bodies[index * 50:(index + 1) * 50] == expected
        assert len(provider.bodies) == 100
        assert len(parses) == (1 if cached else 2)
        assert store.count_results() == 2
        assert not worker.run_once()
        assert len(provider.bodies) == 100
        # A warm selection never hides changed corpus bytes on the next claimed job.
        created = client.post('/v1/submissions', headers=_headers('subject-alice', 'changed-data'),
            json={'challenge_id': contract.challenge_id, 'student_prompt': prompts[0]})
        assert created.status_code == 202
        path.write_bytes(path.read_bytes().replace(b'"text": "00"', b'"text": "01"'))
        assert worker.run_once()
        result = client.get(f"/v1/submissions/{created.json()['submission_id']}/result",
                            headers=_headers('subject-alice')).json()
        assert result['outcome'] == 'failed' and result['code'] == 'DATASET_INTEGRITY'
        assert len(provider.bodies) == 100 and store.count_results() == 2


def _components(tmp_path: Path, artifacts: ChallengeArtifacts, contract: EvaluationContract):
    store = SubmissionStore(tmp_path / "submissions.db")
    store.register_user(auth_subject="subject-alice", public_handle="alice")
    store.register_user(auth_subject="subject-bob", public_handle="bob")
    queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
    dispatcher = OutboxDispatcher(store, queue, contract)
    provider = _RecordingMockProvider()
    worker = SubmissionWorker(
        store=store,
        queue=queue,
        contract=contract,
        artifacts=artifacts,
        provider=provider,
    )
    app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        allow_draft_submissions=True,
        environment="test",
    )
    return store, queue, dispatcher, provider, worker, app


def _qwen_components(
    tmp_path: Path,
    artifacts: ChallengeArtifacts,
    contract: EvaluationContract,
    tokenizer_snapshot_path: Path,
    launch_evidence_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: list[Exception] | None = None,
):
    store = SubmissionStore(tmp_path / "qwen-submissions.db")
    store.register_user(auth_subject="subject-alice", public_handle="alice")
    queue = InMemoryJobQueue(
        contract.contract_snapshot_sha256,
        visibility_timeout_seconds=contract.job_deadline_seconds + 15,
    )
    dispatcher = OutboxDispatcher(store, queue, contract)
    provider = _TestQwenProvider(contract, failures)
    monkeypatch.setattr(
        qwen_runtime_module,
        "load_huggingface_tokenizer",
        lambda path: _TestQwenTokenizer(),
    )
    worker = QwenSubmissionWorker(
        store=store,
        queue=queue,
        contract=contract,
        artifacts=artifacts,
        provider=provider,
        tokenizer_snapshot_path=tokenizer_snapshot_path,
        launch_evidence_path=launch_evidence_path,
    )
    app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        allow_draft_submissions=True,
        environment="test",
    )
    return store, queue, provider, worker, app


def _headers(subject: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"X-Test-Subject": subject}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _cursor(value: dict[str, object]) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def test_health_routes_are_safe_and_request_logs_are_allowlisted(tmp_path: Path, caplog) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, _, dispatcher, _, _, _ = _components(tmp_path, artifacts, contract)

    def unavailable_dependency() -> None:
        raise RuntimeError("redis password must not be exposed")

    app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        readiness_check=unavailable_dependency,
        allow_draft_submissions=True,
        environment="test",
    )
    with caplog.at_level(logging.INFO, logger="linguistic_oj.http"):
        with TestClient(app) as client:
            live = client.get(
                "/health/live",
                headers={
                    "Authorization": "Bearer top-secret",
                    "X-Request-ID": "health-check-1",
                },
            )
            ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "live"}
    assert live.headers["x-request-id"] == "health-check-1"
    assert ready.status_code == 503
    assert ready.json() == {
        "error": {
            "code": "SERVICE_NOT_READY",
            "details": {},
            "message": "Service is not ready",
        }
    }
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "linguistic_oj.http"
    ]
    assert [record["status_code"] for record in records] == [200, 503]
    assert records[0]["request_id"] == "health-check-1"
    assert all("top-secret" not in record.message for record in caplog.records)
    assert all("redis password" not in record.message for record in caplog.records)


def test_frontend_is_packaged_with_restrictive_document_headers(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    _, _, _, _, _, app = _components(tmp_path, artifacts, contract)

    with TestClient(app) as client:
        document = client.get("/")
        stylesheet = client.get("/assets/app-shell.css")
        script = client.get("/assets/app.js")

    assert document.status_code == stylesheet.status_code == script.status_code == 200
    assert document.headers["content-type"].startswith("text/html")
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "javascript" in script.headers["content-type"]
    assert "default-src 'self'" in document.headers["content-security-policy"]
    assert document.headers["x-frame-options"] == "DENY"
    assert document.headers["x-content-type-options"] == "nosniff"
    assert '<main id="main-content" tabindex="-1">' in document.text
    assert 'src="/assets/app.js?v=pages-1"' in document.text
    assert 'href="/assets/brand-palette.css?v=fudan-1"' in document.text
    assert 'src="/assets/account.js?v=pages-1"' in document.text
    assert 'src="/assets/teaching.js?v=xpos-1"' in document.text


def test_api_errors_and_openapi_security_are_machine_readable(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    _, _, _, _, _, app = _components(tmp_path, artifacts, contract)

    with TestClient(app) as client:
        unauthenticated = client.get("/v1/users/me")
        missing_idempotency_key = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice"),
            json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
        )
        invalid_body = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "invalid-body"),
            json={"challenge_id": contract.challenge_id},
        )
        missing_route = client.get("/v1/not-a-route")
        schema = client.get("/openapi.json").json()

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Bearer"
    assert unauthenticated.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert missing_idempotency_key.status_code == 422
    assert missing_idempotency_key.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"
    assert invalid_body.status_code == 422
    assert invalid_body.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert missing_route.status_code == 404
    assert missing_route.json()["error"]["code"] == "ROUTE_NOT_FOUND"
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "scheme": "bearer",
        "type": "http",
    }
    assert schema["paths"]["/v1/users/me"]["get"]["security"] == [{"BearerAuth": []}]
    result_schema = schema["paths"]["/v1/submissions/{submission_id}/result"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert result_schema["discriminator"]["propertyName"] == "outcome"
    assert len(result_schema["oneOf"]) == 3
    assert {
        "DependencyResultResponse",
        "SegmentationResultResponse",
        "TransliterationResultResponse",
        "UposResultResponse",
        "XposResultResponse",
    } <= set(schema["components"]["schemas"])


def test_unavailable_challenge_runtime_is_visible_and_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, _, dispatcher, _, _, _ = _components(tmp_path, artifacts, contract)
    app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        public_challenges={contract.challenge_id: artifacts.public},
        authenticate=_authenticate,
        runtime_availability={contract.challenge_id: False},
        allow_draft_submissions=True,
        environment="test",
    )

    with TestClient(app) as client:
        challenge = client.get(f"/v1/challenges/{contract.challenge_id}").json()
        response = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "runtime-unavailable"),
            json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
        )

    assert challenge["submission_enabled"] is True
    assert challenge["runtime_available"] is False
    assert challenge["accepting_submissions"] is False
    assert challenge["student_prompt_utf8_bytes"] == contract.student_prompt_utf8_bytes
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CHALLENGE_RUNTIME_UNAVAILABLE"
    assert store.count_submissions() == 0


def test_post_returns_durable_acceptance_when_immediate_outbox_publish_fails(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store = SubmissionStore(tmp_path / "submissions.db")
    store.register_user(auth_subject="subject-alice", public_handle="alice")

    class UnavailableQueue(InMemoryJobQueue):
        def publish(self, message: JobMessage) -> None:
            raise OSError("private redis failure")

    queue = UnavailableQueue(contract.contract_snapshot_sha256)
    dispatcher = OutboxDispatcher(store, queue, contract)
    app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        allow_draft_submissions=True,
        environment="test",
    )
    payload = {"challenge_id": contract.challenge_id, "student_prompt": "Prompt."}

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "durable-outbox"),
            json=payload,
        )
        replay = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "durable-outbox"),
            json=payload,
        )
        visible = client.get(
            f"/v1/submissions/{created.json()['submission_id']}",
            headers=_headers("subject-alice"),
        )

    assert created.status_code == replay.status_code == 202
    assert replay.json()["submission_id"] == created.json()["submission_id"]
    assert created.json()["evaluation_identity_sha256"] == contract.evaluation_identity_sha256
    assert visible.status_code == 200
    assert store.count_submissions() == store.count_outbox_records() == 1
    assert store.unpublished_submission_ids(
        contract.evaluation_identity_sha256,
        contract.contract_snapshot_sha256,
    ) == (created.json()["submission_id"],)


def _assert_no_private_fields(value: object) -> None:
    forbidden = {
        "answers",
        "auth_subject",
        "gold_items",
        "idempotency_key",
        "model_input",
        "raw_response",
        "raw_responses",
        "sample_id",
        "sample_ids",
        "student_prompt",
    }
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for nested in value.values():
            _assert_no_private_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_private_fields(nested)


def test_current_user_and_submission_history_are_owner_scoped_and_paginated(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, _, _, _, _, app = _components(tmp_path, artifacts, contract)

    with TestClient(app) as client:
        assert client.get("/v1/users/me").status_code == 401
        assert client.get(
            "/v1/users/me",
            headers=_headers("subject-unknown"),
        ).status_code == 403
        current_user = client.get(
            "/v1/users/me",
            headers=_headers("subject-alice"),
        )
        submission_ids = []
        for index in range(3):
            response = client.post(
                "/v1/submissions",
                headers=_headers("subject-alice", f"history-{index}"),
                json={
                    "challenge_id": contract.challenge_id,
                    "student_prompt": f"Prompt {index}",
                },
            )
            assert response.status_code == 202
            submission_ids.append(response.json()["submission_id"])

        first_page = client.get(
            "/v1/submissions?limit=2",
            headers=_headers("subject-alice"),
        )
        empty_page = client.get(
            "/v1/submissions",
            headers=_headers("subject-bob"),
        )
        invalid_cursor = client.get(
            "/v1/submissions?cursor=not-base64!",
            headers=_headers("subject-alice"),
        )
        invalid_timestamp_cursor = client.get(
            "/v1/submissions",
            params={
                "cursor": _cursor(
                    {
                        "created_at": "not-a-timestamp",
                        "submission_id": "a" * 32,
                        "version": 1,
                    }
                )
            },
            headers=_headers("subject-alice"),
        )

        first_payload = first_page.json()
        second_page = client.get(
            "/v1/submissions",
            headers=_headers("subject-alice"),
            params={"limit": 2, "cursor": first_payload["next_cursor"]},
        )

    assert current_user.status_code == 200
    assert current_user.json()["public_handle"] == "alice"
    assert "auth_subject" not in current_user.json()
    assert first_page.status_code == 200
    assert len(first_payload["items"]) == 2
    assert first_payload["next_cursor"]
    assert second_page.status_code == 200
    all_history_ids = [
        item["submission_id"]
        for item in [*first_payload["items"], *second_page.json()["items"]]
    ]
    assert len(all_history_ids) == len(set(all_history_ids)) == 3
    assert set(all_history_ids) == set(submission_ids)
    assert all(
        item["evaluation_identity_sha256"] == contract.evaluation_identity_sha256
        for item in [*first_payload["items"], *second_page.json()["items"]]
    )
    _assert_no_private_fields(first_payload)
    assert empty_page.json() == {"items": [], "next_cursor": None}
    assert invalid_cursor.status_code == 422
    assert invalid_timestamp_cursor.status_code == 422
    assert invalid_timestamp_cursor.json()["error"]["code"] == "INVALID_SUBMISSION_CURSOR"
    assert store.count_submissions() == 3


def test_owner_prompt_preserves_exact_text_without_exposing_it_to_other_users(tmp_path):
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, _, _, provider, _, app = _components(tmp_path, artifacts, contract)
    prompt = '  中文 e\u0301 العربية\n<img src=x onerror=alert(1)>\n'
    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "owner-prompt"),
            json={"challenge_id": contract.challenge_id, "student_prompt": prompt},
        )
        assert created.status_code == 202
        submission_id = created.json()["submission_id"]
        path = f"/v1/submissions/{submission_id}/prompt"
        assert client.get(path).status_code == 401
        other = client.get(path, headers=_headers("subject-bob"))
        missing = client.get(
            "/v1/submissions/missing/prompt", headers=_headers("subject-bob")
        )
        assert other.status_code == missing.status_code == 404
        assert other.json() == missing.json()
        owned = client.get(path, headers=_headers("subject-alice"))
        assert owned.status_code == 200
        assert owned.headers["cache-control"] == "no-store"
        assert owned.json() == {
            "submission_id": submission_id,
            "challenge_id": contract.challenge_id,
            "student_prompt": prompt,
            "student_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        _assert_no_private_fields(client.get(
            "/v1/submissions", headers=_headers("subject-alice")
        ).json())
        assert store.count_submissions() == 1
        assert provider.calls == 0


def test_multi_challenge_api_routes_jobs_and_results_by_contract(tmp_path: Path) -> None:
    upos_artifacts = _artifacts(tmp_path)
    segmentation_artifacts = _artifacts(tmp_path, task="segmentation", version="v2")
    upos_contract = _mock_contract(upos_artifacts)
    segmentation_contract = _mock_contract(segmentation_artifacts)
    store = SubmissionStore(tmp_path / "multi-challenge.db")
    store.register_user(auth_subject="subject-alice", public_handle="alice")
    queues = {
        upos_contract.challenge_id: InMemoryJobQueue(
            upos_contract.contract_snapshot_sha256
        ),
        segmentation_contract.challenge_id: InMemoryJobQueue(
            segmentation_contract.contract_snapshot_sha256
        ),
    }
    dispatchers = {
        upos_contract.challenge_id: OutboxDispatcher(
            store,
            queues[upos_contract.challenge_id],
            upos_contract,
        ),
        segmentation_contract.challenge_id: OutboxDispatcher(
            store,
            queues[segmentation_contract.challenge_id],
            segmentation_contract,
        ),
    }
    workers = {
        upos_contract.challenge_id: SubmissionWorker(
            store=store,
            queue=queues[upos_contract.challenge_id],
            contract=upos_contract,
            artifacts=upos_artifacts,
            provider=DeterministicMockProvider(),
        ),
        segmentation_contract.challenge_id: SubmissionWorker(
            store=store,
            queue=queues[segmentation_contract.challenge_id],
            contract=segmentation_contract,
            artifacts=segmentation_artifacts,
            provider=DeterministicMockProvider(),
        ),
    }
    contracts = {
        upos_contract.challenge_id: upos_contract,
        segmentation_contract.challenge_id: segmentation_contract,
    }
    app = create_app(
        store=store,
        dispatcher=dispatchers,
        contract=contracts,
        public_challenges={
            upos_artifacts.public.challenge_id: upos_artifacts.public,
            segmentation_artifacts.public.challenge_id: segmentation_artifacts.public,
        },
        authenticate=_authenticate,
        allow_draft_submissions=True,
        environment="test",
    )

    with TestClient(app) as client:
        catalog = client.get("/v1/challenges").json()
        created = {}
        for challenge_id in contracts:
            response = client.post(
                "/v1/submissions",
                headers=_headers("subject-alice", f"route-{challenge_id}"),
                json={
                    "challenge_id": challenge_id,
                    "student_prompt": "Return the required JSON.",
                },
            )
            assert response.status_code == 202
            created[challenge_id] = response.json()["submission_id"]

        assert len(queues[upos_contract.challenge_id]) == 1
        assert len(queues[segmentation_contract.challenge_id]) == 1
        assert all(worker.run_once() for worker in workers.values())

        for challenge_id, submission_id in created.items():
            result = client.get(
                f"/v1/submissions/{submission_id}/result",
                headers=_headers("subject-alice"),
            )
            assert result.status_code == 200
            assert result.json()["challenge_id"] == challenge_id

    assert {item["challenge_id"] for item in catalog} == set(contracts)
    assert all(item["submission_enabled"] for item in catalog)
    assert all(item["runtime_available"] for item in catalog)
    assert all(item["accepting_submissions"] for item in catalog)
    assert all(
        len(store.leaderboard(contract.evaluation_identity_sha256)) == 1
        for contract in contracts.values()
    )


def test_mock_submission_runs_asynchronously_and_isolates_leaderboards(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, queue, _, provider, worker, app = _components(tmp_path, artifacts, contract)
    payload = {
        "challenge_id": contract.challenge_id,
        "student_prompt": "Return the required UPOS JSON.",
    }

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "submission-1"),
            json=payload,
        )
        assert created.status_code == 202
        submission = created.json()
        submission_id = submission["submission_id"]
        assert submission["status"] == "queued"
        assert provider.calls == 0
        assert len(queue) == 1
        assert store.count_submissions() == store.count_outbox_records() == 1

        replay = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "submission-1"),
            json=payload,
        )
        assert replay.status_code == 202
        assert replay.json()["submission_id"] == submission_id
        assert len(queue) == 1
        assert store.count_submissions() == store.count_outbox_records() == 1

        changed = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "submission-1"),
            json={**payload, "student_prompt": "Different prompt."},
        )
        assert changed.status_code == 409

        assert (
            client.get(
                f"/v1/submissions/{submission_id}",
                headers=_headers("subject-bob"),
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/v1/submissions/{submission_id}/result",
                headers=_headers("subject-bob"),
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/v1/submissions/{submission_id}/result",
                headers=_headers("subject-alice"),
            ).status_code
            == 409
        )

        assert worker.run_once() is True
        assert provider.calls == artifacts.public.sample_count
        assert store.count_results() == 1

        status_response = client.get(
            f"/v1/submissions/{submission_id}",
            headers=_headers("subject-alice"),
        )
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "succeeded"

        result_response = client.get(
            f"/v1/submissions/{submission_id}/result",
            headers=_headers("subject-alice"),
        )
        assert result_response.status_code == 200
        result = result_response.json()
        assert set(result) == {*contract.owner_result_fields, "outcome"}
        assert result["outcome"] == "succeeded"
        assert result["score"] == 1.0
        assert result["samples_valid"] == result["samples_total"] == 2

        leaderboard_response = client.get(
            f"/v1/leaderboards/{contract.evaluation_identity_sha256}"
        )
        assert leaderboard_response.status_code == 200
        leaderboard = leaderboard_response.json()
        assert leaderboard == {
            "items": [
                {
                    "evaluation_identity_sha256": contract.evaluation_identity_sha256,
                    "public_handle": "alice",
                    "rank": 1,
                    "samples_invalid": 0,
                    "samples_total": 2,
                    "samples_valid": 2,
                    "score": 1.0,
                    "succeeded_at": leaderboard["items"][0]["succeeded_at"],
                }
            ],
            "next_cursor": None,
        }
        _assert_no_private_fields(submission)
        _assert_no_private_fields(result)
        _assert_no_private_fields(leaderboard)
        serialized = json.dumps([submission, result, leaderboard])
        assert "subject-alice" not in serialized
        assert payload["student_prompt"] not in serialized

        queue.publish(
            JobMessage(
                submission_id,
                contract.evaluation_identity_sha256,
                contract.contract_snapshot_sha256,
            )
        )
        assert worker.run_once() is False
        assert store.count_results() == 1

        second_contract = _mock_contract(artifacts, contract_version="mock-evaluation-v2")
        second_queue = InMemoryJobQueue(second_contract.contract_snapshot_sha256)
        second_dispatcher = OutboxDispatcher(store, second_queue, second_contract)
        second_worker = SubmissionWorker(
            store=store,
            queue=second_queue,
            contract=second_contract,
            artifacts=artifacts,
            provider=DeterministicMockProvider(),
        )
        second_app = create_app(
            store=store,
            dispatcher=second_dispatcher,
            contract=second_contract,
            authenticate=_authenticate,
            allow_draft_submissions=True,
            environment="test",
        )
        with TestClient(second_app) as second_client:
            second_created = second_client.post(
                "/v1/submissions",
                headers=_headers("subject-alice", "submission-2"),
                json=payload,
            )
            assert second_created.status_code == 202
            assert second_worker.run_once() is True
            current_leaderboard = second_client.get(
                f"/v1/leaderboards/{second_contract.evaluation_identity_sha256}"
            )
            historical_leaderboard = second_client.get(
                f"/v1/leaderboards/{contract.evaluation_identity_sha256}"
            )
            assert len(current_leaderboard.json()["items"]) == 1
            assert historical_leaderboard.status_code == 404
            assert historical_leaderboard.json()["error"]["code"] == "LEADERBOARD_NOT_FOUND"


def test_leaderboard_is_bounded_cursor_paginated_and_snapshot_stable(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, _, _, _, worker, app = _components(tmp_path, artifacts, contract)
    store.register_user(auth_subject="subject-charlie", public_handle="charlie")
    store.register_user(auth_subject="subject-dave", public_handle="dave")

    with TestClient(app) as client:
        for index, subject in enumerate(
            ("subject-alice", "subject-bob", "subject-charlie"),
            start=1,
        ):
            created = client.post(
                "/v1/submissions",
                headers=_headers(subject, f"leaderboard-{index}"),
                json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
            )
            assert created.status_code == 202
            assert worker.run_once() is True

        first = client.get(
            f"/v1/leaderboards/{contract.evaluation_identity_sha256}",
            params={"limit": 2},
        )
        first_payload = first.json()
        assert [item["rank"] for item in first_payload["items"]] == [1, 2]
        assert first_payload["next_cursor"] is not None

        later = client.post(
            "/v1/submissions",
            headers=_headers("subject-dave", "leaderboard-later"),
            json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
        )
        assert later.status_code == 202
        assert worker.run_once() is True

        second = client.get(
            f"/v1/leaderboards/{contract.evaluation_identity_sha256}",
            params={"limit": 2, "cursor": first_payload["next_cursor"]},
        )
        unknown = client.get(f"/v1/leaderboards/{'0' * 64}")
        invalid = client.get(
            f"/v1/leaderboards/{contract.evaluation_identity_sha256}",
            params={
                "cursor": _cursor(
                    {
                        "as_of": datetime.now(UTC).isoformat(timespec="microseconds"),
                        "evaluation_identity_sha256": "f" * 64,
                        "rank": 1,
                        "version": 1,
                    }
                )
            },
        )

    assert second.status_code == 200
    assert [item["rank"] for item in second.json()["items"]] == [3]
    assert second.json()["next_cursor"] is None
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "LEADERBOARD_NOT_FOUND"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_LEADERBOARD_CURSOR"


def test_api_returns_structured_rate_and_outstanding_quota_feedback(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    _, _, _, _, worker, app = _components(tmp_path, artifacts, contract)

    with TestClient(app) as client:
        for index in range(contract.submissions_per_user_per_challenge_per_24h):
            accepted = client.post(
                "/v1/submissions",
                headers=_headers("subject-alice", f"rate-{index}"),
                json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
            )
            assert accepted.status_code == 202
            assert worker.run_once() is True
        rate_limited = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "rate-exceeded"),
            json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
        )

        for index in range(contract.max_outstanding_submissions_per_user):
            accepted = client.post(
                "/v1/submissions",
                headers=_headers("subject-bob", f"outstanding-{index}"),
                json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
            )
            assert accepted.status_code == 202
        outstanding_limited = client.post(
            "/v1/submissions",
            headers=_headers("subject-bob", "outstanding-exceeded"),
            json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
        )

    rate_error = rate_limited.json()["error"]
    assert rate_limited.status_code == 429
    assert rate_error["code"] == "SUBMISSION_RATE_LIMIT"
    assert rate_error["details"]["limit"] == 5
    assert rate_error["details"]["current"] == 5
    assert int(rate_limited.headers["retry-after"]) == rate_error["details"][
        "retry_after_seconds"
    ]
    outstanding_error = outstanding_limited.json()["error"]
    assert outstanding_limited.status_code == 429
    assert outstanding_error == {
        "code": "OUTSTANDING_SUBMISSION_LIMIT",
        "details": {"current": 3, "limit": 3},
        "message": "Submission quota exceeded",
    }


def test_api_fails_closed_for_drafts_and_rejects_oversized_bodies(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, _, dispatcher, _, _, _ = _components(tmp_path, artifacts, contract)

    with pytest.raises(ValueError, match="forbidden in production"):
        create_app(
            store=store,
            dispatcher=dispatcher,
            contract=contract,
            authenticate=_authenticate,
            allow_draft_submissions=True,
            environment="production",
        )

    closed_app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        environment="test",
    )
    with TestClient(closed_app) as client:
        closed = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "closed-draft"),
            json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
        )
        assert closed.status_code == 409
        assert closed.json()["error"]["code"] == "CHALLENGE_NOT_OPEN"

    open_app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        allow_draft_submissions=True,
        environment="test",
    )
    with TestClient(open_app) as client:
        oversized = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "oversized"),
            content=b"x" * (contract.api_request_body_bytes + 1),
        )
        assert oversized.status_code == 413


def test_mock_worker_refuses_the_frozen_qwen_partition(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    production_contract = EvaluationContract.from_path(
        ROOT / "config" / "mvp_evaluation.json"
    )
    store = SubmissionStore(tmp_path / "submissions.db")

    with pytest.raises(ValueError, match="requires a mock evaluation identity"):
        SubmissionWorker(
            store=store,
            queue=InMemoryJobQueue(production_contract.contract_snapshot_sha256),
            contract=production_contract,
            artifacts=artifacts,
            provider=DeterministicMockProvider(),
        )


def test_platform_failure_returns_only_the_safe_failure_contract(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store = SubmissionStore(tmp_path / "submissions.db")
    store.register_user(auth_subject="subject-alice", public_handle="alice")
    queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
    dispatcher = OutboxDispatcher(store, queue, contract)
    worker = SubmissionWorker(
        store=store,
        queue=queue,
        contract=contract,
        artifacts=artifacts,
        provider=_ExplodingMockProvider(),
    )
    app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        allow_draft_submissions=True,
        environment="test",
    )

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "failure-1"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        )
        assert created.status_code == 202
        submission_id = created.json()["submission_id"]
        assert worker.run_once() is True

        failed = client.get(
            f"/v1/submissions/{submission_id}/result",
            headers=_headers("subject-alice"),
        )
        assert failed.status_code == 200
        assert failed.json() == {
            "code": "RUNTIME_MISCONFIGURATION",
            "failure_contract_version": "platform-failure-v1",
            "outcome": "failed",
            "retryable": False,
        }
        assert "private provider detail" not in failed.text


def test_retryable_transport_failure_requeues_complete_job_once(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, queue, _, _, _, app = _components(tmp_path, artifacts, contract)
    provider = _TransportFailureMockProvider(failures=1)
    worker = SubmissionWorker(
        store=store,
        queue=queue,
        contract=contract,
        artifacts=artifacts,
        provider=provider,
    )

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "retry-success"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        )
        submission_id = created.json()["submission_id"]

        assert worker.run_once() is True
        retrying = client.get(
            f"/v1/submissions/{submission_id}",
            headers=_headers("subject-alice"),
        )
        assert retrying.json()["status"] == "queued"
        assert len(queue) == 1
        assert store.count_outbox_records() == 1
        assert store.count_results() == 0

        assert worker.run_once() is True
        completed = client.get(
            f"/v1/submissions/{submission_id}",
            headers=_headers("subject-alice"),
        )
        assert completed.json()["status"] == "succeeded"
        assert provider.calls == 3
        assert store.count_results() == 1


def test_retryable_transport_failure_stops_after_max_attempts(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, queue, _, _, _, app = _components(tmp_path, artifacts, contract)
    provider = _TransportFailureMockProvider(failures=2)
    worker = SubmissionWorker(
        store=store,
        queue=queue,
        contract=contract,
        artifacts=artifacts,
        provider=provider,
    )

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "retry-failed"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        )
        submission_id = created.json()["submission_id"]

        assert worker.run_once() is True
        assert worker.run_once() is True
        assert len(queue) == 0
        failed = client.get(
            f"/v1/submissions/{submission_id}/result",
            headers=_headers("subject-alice"),
        )
        assert failed.status_code == 200
        assert failed.json() == {
            "code": "PROVIDER_TRANSPORT",
            "failure_contract_version": "platform-failure-v1",
            "outcome": "failed",
            "retryable": False,
        }
        assert provider.calls == 2
        assert store.count_results() == 0


def test_attested_qwen_worker_completes_in_separate_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = _artifacts(tmp_path)
    snapshot_path, launch_evidence_path, tokenizer_identity = _qwen_snapshot(tmp_path)
    contract = _qwen_contract(artifacts, tokenizer_identity)
    store, queue, provider, worker, app = _qwen_components(
        tmp_path,
        artifacts,
        contract,
        snapshot_path,
        launch_evidence_path,
        monkeypatch,
    )

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "qwen-success"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        )
        assert created.status_code == 202
        submission_id = created.json()["submission_id"]

        assert worker.run_once() is True
        result = client.get(
            f"/v1/submissions/{submission_id}/result",
            headers=_headers("subject-alice"),
        )
        assert result.status_code == 200
        assert result.json()["model_identity"]["model"] == "Qwen/Qwen3.5-9B"
        assert provider.calls == 2
        assert len(queue) == 0
        assert store.count_results() == 1


def test_qwen_worker_does_not_claim_work_while_a_request_is_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = _artifacts(tmp_path)
    snapshot_path, launch_evidence_path, tokenizer_identity = _qwen_snapshot(tmp_path)
    contract = _qwen_contract(artifacts, tokenizer_identity)
    _, queue, provider, worker, app = _qwen_components(
        tmp_path,
        artifacts,
        contract,
        snapshot_path,
        launch_evidence_path,
        monkeypatch,
    )
    provider._active_request = object()

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "qwen-blocked-by-active-request"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        )

    assert created.status_code == 202
    assert worker.run_once() is False
    assert provider.calls == 0
    assert len(queue) == 1


def test_qwen_worker_retries_only_after_confirmed_request_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = _artifacts(tmp_path)
    snapshot_path, launch_evidence_path, tokenizer_identity = _qwen_snapshot(tmp_path)
    contract = _qwen_contract(artifacts, tokenizer_identity)
    _, queue, provider, worker, app = _qwen_components(
        tmp_path,
        artifacts,
        contract,
        snapshot_path,
        launch_evidence_path,
        monkeypatch,
        failures=[
            ProviderTransportError(
                "temporary transport detail",
                termination_confirmed=True,
            )
        ],
    )

    with TestClient(app) as client:
        submission_id = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "qwen-confirmed-retry"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        ).json()["submission_id"]

        assert worker.run_once() is True
        assert len(queue) == 1
        assert worker.run_once() is True
        assert provider.calls == 3
        completed = client.get(
            f"/v1/submissions/{submission_id}",
            headers=_headers("subject-alice"),
        )
        assert completed.json()["status"] == "succeeded"


def test_qwen_worker_does_not_retry_ambiguous_remote_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = _artifacts(tmp_path)
    snapshot_path, launch_evidence_path, tokenizer_identity = _qwen_snapshot(tmp_path)
    contract = _qwen_contract(artifacts, tokenizer_identity)
    _, queue, provider, worker, app = _qwen_components(
        tmp_path,
        artifacts,
        contract,
        snapshot_path,
        launch_evidence_path,
        monkeypatch,
        failures=[ProviderTimeoutError()],
    )

    with TestClient(app) as client:
        submission_id = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "qwen-ambiguous-timeout"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        ).json()["submission_id"]

        assert worker.run_once() is True
        assert provider.calls == 1
        assert len(queue) == 0
        failure = client.get(
            f"/v1/submissions/{submission_id}/result",
            headers=_headers("subject-alice"),
        )
        assert failure.json() == {
            "code": "PROVIDER_TIMEOUT",
            "failure_contract_version": "platform-failure-v1",
            "outcome": "failed",
            "retryable": False,
        }


def test_temporarily_unclaimable_job_is_requeued_behind_other_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(submission_jobs_module, "monotonic", lambda: now[0])
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, queue, _, _, worker, app = _components(tmp_path, artifacts, contract)
    payload = {"challenge_id": contract.challenge_id, "student_prompt": "Return JSON."}

    with TestClient(app) as client:
        first = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "queue-1"),
            json=payload,
        ).json()
        second = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "queue-2"),
            json=payload,
        ).json()
        user = store.user_by_subject("subject-alice")
        assert user is not None
        first_delivery = queue.receive()
        assert first_delivery is not None
        assert first_delivery.message.submission_id == first["submission_id"]
        claimed = store.claim_submission(
            first["submission_id"],
            evaluation_identity_sha256=contract.evaluation_identity_sha256,
            contract_snapshot_sha256=contract.contract_snapshot_sha256,
            lease_seconds=min(30, contract.job_deadline_seconds),
            max_attempts=contract.max_attempts,
            max_running_per_user=contract.max_running_submissions_per_user,
        ).claim
        assert claimed is not None

        assert worker.run_once() is False
        assert len(queue) == 1
        assert worker.run_once() is False
        assert len(queue) == 1
        queued = client.get(
            f"/v1/submissions/{second['submission_id']}",
            headers=_headers("subject-alice"),
        )
        assert queued.json()["status"] == "queued"

        assert store.complete_failure(
            claimed,
            failure_contract_version=contract.failure_contract_version,
            code="WORKER_CRASH",
            retryable=False,
        )
        assert queue.ack(first_delivery) is True
        assert worker.run_once() is True
        assert len(queue) == 0


def test_duplicate_running_submission_waits_for_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(submission_jobs_module, "monotonic", lambda: now[0])
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, queue, _, _, worker, app = _components(tmp_path, artifacts, contract)

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "duplicate-running"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        ).json()
        submission_id = created["submission_id"]
        message = JobMessage(
            submission_id=submission_id,
            evaluation_identity_sha256=contract.evaluation_identity_sha256,
            contract_snapshot_sha256=contract.contract_snapshot_sha256,
        )
        queue.publish(message)
        first_delivery = queue.receive()
        assert first_delivery is not None
        claim = store.claim_submission(
            submission_id,
            evaluation_identity_sha256=contract.evaluation_identity_sha256,
            contract_snapshot_sha256=contract.contract_snapshot_sha256,
            lease_seconds=min(30, contract.job_deadline_seconds),
            max_attempts=contract.max_attempts,
            max_running_per_user=contract.max_running_submissions_per_user,
        ).claim
        assert claim is not None

        assert worker.run_once() is False
        assert worker.run_once() is False
        assert len(queue) == 1
        assert store.complete_failure(
            claim,
            failure_contract_version=contract.failure_contract_version,
            code="WORKER_CRASH",
            retryable=False,
        )
        assert queue.ack(first_delivery) is True

        assert worker.run_once() is False
        assert queue.receive() is None


def test_worker_rejects_visibility_without_claim_and_safety_budgets(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store = SubmissionStore(tmp_path / "submissions.db")

    with pytest.raises(ValueError, match="claim acquisition"):
        SubmissionWorker(
            store=store,
            queue=InMemoryJobQueue(
                contract.contract_snapshot_sha256,
                visibility_timeout_seconds=44.999,
            ),
            contract=contract,
            artifacts=artifacts,
            provider=DeterministicMockProvider(),
        )


def test_mock_token_preflight_rejects_without_provider_calls(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, _, _, provider, worker, app = _components(tmp_path, artifacts, contract)
    prompt = "x" * (contract.student_prompt_tokens + 1)

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "token-limit"),
            json={"challenge_id": contract.challenge_id, "student_prompt": prompt},
        )
        assert created.status_code == 202
        submission_id = created.json()["submission_id"]
        assert worker.run_once() is True
        assert provider.calls == 0
        assert store.count_results() == 0
        rejected = client.get(
            f"/v1/submissions/{submission_id}",
            headers=_headers("subject-alice"),
        )
        assert rejected.json()["status"] == "rejected"
        result = client.get(
            f"/v1/submissions/{submission_id}/result",
            headers=_headers("subject-alice"),
        )
        assert result.status_code == 200
        assert result.json() == {
            "code": "TOKEN_LIMIT_EXCEEDED",
            "failure_contract_version": "platform-failure-v1",
            "outcome": "rejected",
            "retryable": False,
        }


def test_invalid_activation_and_hybrid_mock_identity_fail_closed(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    invalid_status = json.loads(contract.snapshot_json)
    invalid_status["catalog"]["status"] = "drfat"
    with pytest.raises(ValueError, match="catalog status"):
        EvaluationContract.from_mapping(invalid_status)

    store, _, dispatcher, _, _, _ = _components(tmp_path, artifacts, contract)
    active_but_unreviewed = json.loads(contract.snapshot_json)
    active_but_unreviewed["catalog"]["status"] = "active"
    unready_contract = EvaluationContract.from_mapping(active_but_unreviewed)
    assert unready_contract.external_activation_ready is False
    unready_dispatcher = OutboxDispatcher(
        store,
        InMemoryJobQueue(unready_contract.contract_snapshot_sha256),
        unready_contract,
    )
    unready_app = create_app(
        store=store,
        dispatcher=unready_dispatcher,
        contract=unready_contract,
        authenticate=_authenticate,
        environment="test",
    )
    with TestClient(unready_app) as client:
        response = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "not-ready"),
            json={"challenge_id": contract.challenge_id, "student_prompt": "Prompt."},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CHALLENGE_NOT_OPEN"

    with pytest.raises(ValueError, match="deployment environment"):
        create_app(
            store=store,
            dispatcher=dispatcher,
            contract=contract,
            authenticate=_authenticate,
            environment="prod",
        )

    hybrid_mapping = json.loads(contract.snapshot_json)
    hybrid_mapping["evaluation_identity"]["model_identity"]["model"] = "other-model"
    hybrid_mapping["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        hybrid_mapping["evaluation_identity"]
    )
    hybrid_contract = EvaluationContract.from_mapping(hybrid_mapping)
    with pytest.raises(ValueError, match="dispatcher does not match"):
        create_app(
            store=store,
            dispatcher=dispatcher,
            contract=hybrid_contract,
            authenticate=_authenticate,
            allow_draft_submissions=True,
            environment="test",
        )
    with pytest.raises(ValueError, match="artifacts do not match"):
        SubmissionWorker(
            store=store,
            queue=InMemoryJobQueue(hybrid_contract.contract_snapshot_sha256),
            contract=hybrid_contract,
            artifacts=artifacts,
            provider=DeterministicMockProvider(),
        )

    incompatible_mapping = json.loads(contract.snapshot_json)
    incompatible_mapping["feedback"]["owner_result_fields"].remove("errors")
    incompatible_contract = EvaluationContract.from_mapping(incompatible_mapping)
    incompatible_dispatcher = OutboxDispatcher(
        store,
        InMemoryJobQueue(incompatible_contract.contract_snapshot_sha256),
        incompatible_contract,
    )
    with pytest.raises(ValueError, match="owner result fields"):
        create_app(
            store=store,
            dispatcher=incompatible_dispatcher,
            contract=incompatible_contract,
            authenticate=_authenticate,
            allow_draft_submissions=True,
            environment="test",
        )


def test_published_queued_job_is_recovered_after_in_memory_queue_restart(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, original_queue, _, _, _, app = _components(tmp_path, artifacts, contract)

    with TestClient(app) as client:
        created = client.post(
            "/v1/submissions",
            headers=_headers("subject-alice", "restart-1"),
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        )
        assert created.status_code == 202
        assert len(original_queue) == 1

    restarted_queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
    restarted_dispatcher = OutboxDispatcher(store, restarted_queue, contract)
    assert restarted_dispatcher.recover_published_queued() == 1
    restarted_worker = SubmissionWorker(
        store=store,
        queue=restarted_queue,
        contract=contract,
        artifacts=artifacts,
        provider=DeterministicMockProvider(),
    )
    assert restarted_worker.run_once() is True
    assert store.count_results() == 1


def test_unpublished_outbox_job_is_dispatched_when_api_starts(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store = SubmissionStore(tmp_path / "submissions.db")
    user = store.register_user(auth_subject="subject-alice", public_handle="alice")
    created = store.create_submission(
        user=user,
        idempotency_key="startup-outbox",
        student_prompt="Return JSON.",
        contract=contract,
    )
    queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
    dispatcher = OutboxDispatcher(store, queue, contract)

    create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        allow_draft_submissions=True,
        environment="test",
    )

    assert len(queue) == 1
    assert store.unpublished_submission_ids(
        contract.evaluation_identity_sha256,
        contract.contract_snapshot_sha256,
    ) == ()
    delivery = queue.receive()
    assert delivery is not None
    assert delivery.message.submission_id == created.submission.submission_id


def test_outbox_background_dispatch_retries_after_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store = SubmissionStore(tmp_path / "submissions.db")
    user = store.register_user(auth_subject="subject-alice", public_handle="alice")
    created = store.create_submission(
        user=user,
        idempotency_key="background-outbox",
        student_prompt="Return JSON.",
        contract=contract,
    )

    class FailOnceJobQueue(InMemoryJobQueue):
        def __init__(self) -> None:
            super().__init__(contract.contract_snapshot_sha256)
            self.failures = 1

        def publish(self, message: JobMessage) -> None:
            if self.failures:
                self.failures -= 1
                raise OSError("temporary queue failure")
            super().publish(message)

    queue = FailOnceJobQueue()
    dispatcher = OutboxDispatcher(store, queue, contract)
    monkeypatch.setattr(api_module, "_OUTBOX_DISPATCH_INTERVAL_SECONDS", 0.01)
    app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=_authenticate,
        allow_draft_submissions=True,
        environment="test",
    )

    with TestClient(app):
        deadline = time.monotonic() + 1
        while len(queue) == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert len(queue) == 1
    assert store.unpublished_submission_ids(
        contract.evaluation_identity_sha256,
        contract.contract_snapshot_sha256,
    ) == ()
    delivery = queue.receive()
    assert delivery is not None
    assert delivery.message.submission_id == created.submission.submission_id


def test_queued_deadline_sweep_fails_stale_unpublished_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [datetime(2026, 8, 30, tzinfo=UTC)]
    monkeypatch.setattr(submission_store_module, "_utc_now", lambda: now[0])
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store = SubmissionStore(tmp_path / "submissions.db")
    user = store.register_user(auth_subject="subject-alice", public_handle="alice")
    created = store.create_submission(
        user=user,
        idempotency_key="queued-deadline",
        student_prompt="Prompt.",
        contract=contract,
    )

    now[0] += timedelta(seconds=contract.job_deadline_seconds + 1)
    assert (
        store.expire_queued_deadlines(
            evaluation_identity_sha256=contract.evaluation_identity_sha256
        )
        == 1
    )
    submission = store.submission_for_owner(created.submission.submission_id, user.user_id)
    result = store.owner_result(created.submission.submission_id, user.user_id)

    assert submission is not None and submission.status.value == "failed"
    assert result is not None and result.failure == {
        "code": "JOB_DEADLINE",
        "failure_contract_version": "platform-failure-v1",
        "retryable": False,
    }
    assert store.unpublished_submission_ids(
        contract.evaluation_identity_sha256,
        contract.contract_snapshot_sha256,
    ) == ()


def test_idle_worker_throttles_lease_sweeps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store, _, _, _, worker, _ = _components(tmp_path, artifacts, contract)
    sweep_calls = 0
    expire_leases = store.expire_leases

    def count_sweep(*, evaluation_identity_sha256: str) -> int:
        nonlocal sweep_calls
        sweep_calls += 1
        return expire_leases(
            evaluation_identity_sha256=evaluation_identity_sha256,
        )

    monkeypatch.setattr(store, "expire_leases", count_sweep)

    assert worker.run_once() is False
    assert worker.run_once() is False
    assert sweep_calls == 1


def test_streaming_body_limit_rejects_chunked_oversize_before_route() -> None:
    downstream_called = False

    async def downstream(scope, receive, send) -> None:
        nonlocal downstream_called
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        downstream_called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"de", "more_body": False},
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message) -> None:
        sent.append(message)

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=4)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/", "headers": []},
            receive,
            send,
        )
    )

    assert downstream_called is False
    assert sent[0]["status"] == 413


@pytest.mark.parametrize(
    "invalid_path",
    [
        "/absolute.jsonl",
        "../escape.jsonl",
        "bad\\path",
        "data/source.jsonl:descriptor",
    ],
)
def test_external_activation_requires_safe_complete_provenance(
    tmp_path: Path,
    invalid_path: str,
) -> None:
    contract = _mock_contract(_artifacts(tmp_path))
    ready = json.loads(contract.snapshot_json)
    ready["catalog"].update(
        {
            "attribution_requirements": "recorded",
            "share_alike_requirements": "reviewed",
            "source_file_sha256s": [
                {"path": "data/source.jsonl", "sha256": "a" * 64}
            ],
            "source_release": "UD 2.15",
            "status": "active",
            "underlying_text_rights": "reviewed",
        }
    )
    assert EvaluationContract.from_mapping(ready).external_activation_ready is True

    missing_source = json.loads(json.dumps(ready))
    missing_source["catalog"]["source_release"] = ""
    assert EvaluationContract.from_mapping(missing_source).external_activation_ready is False

    for field in (
        "source_release",
        "attribution_requirements",
        "share_alike_requirements",
        "underlying_text_rights",
    ):
        whitespace_only = json.loads(json.dumps(ready))
        whitespace_only["catalog"][field] = "   "
        assert (
            EvaluationContract.from_mapping(whitespace_only).external_activation_ready
            is False
        )

    ready["catalog"]["source_file_sha256s"][0]["path"] = invalid_path
    assert EvaluationContract.from_mapping(ready).external_activation_ready is False


def test_lease_expiration_is_fenced_and_scoped_by_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [datetime(2026, 8, 30, tzinfo=UTC)]
    monkeypatch.setattr(submission_store_module, "_utc_now", lambda: now[0])
    artifacts = _artifacts(tmp_path)
    contracts = [
        _mock_contract(artifacts, contract_version=f"mock-evaluation-v{version}")
        for version in (1, 2, 3)
    ]
    store = SubmissionStore(tmp_path / "submissions.db")
    user = store.register_user(auth_subject="subject-alice", public_handle="alice")
    claims = []
    for index, contract in enumerate(contracts, start=1):
        created = store.create_submission(
            user=user,
            idempotency_key=f"lease-{index}",
            student_prompt="Return JSON.",
            contract=contract,
        )
        attempt = store.claim_submission(
            created.submission.submission_id,
            evaluation_identity_sha256=contract.evaluation_identity_sha256,
            contract_snapshot_sha256=contract.contract_snapshot_sha256,
            lease_seconds=30,
            max_attempts=contract.max_attempts,
            max_running_per_user=10,
        )
        assert attempt.claim is not None
        claims.append(attempt.claim)

    now[0] += timedelta(seconds=31)
    assert store.complete_failure(
        claims[0],
        failure_contract_version=contracts[0].failure_contract_version,
        code="PROVIDER_TIMEOUT",
        retryable=True,
    ) is False
    assert store.expire_leases(
        evaluation_identity_sha256=contracts[0].evaluation_identity_sha256
    ) == 1
    first = store.owner_result(claims[0].submission_id, user.user_id)
    second = store.owner_result(claims[1].submission_id, user.user_id)
    assert first is not None and first.failure is not None
    assert first.failure["code"] == "WORKER_CRASH"
    assert second is not None and second.status.value == "running"

    now[0] += timedelta(seconds=270)
    assert store.complete_failure(
        claims[1],
        failure_contract_version=contracts[1].failure_contract_version,
        code="PROVIDER_TIMEOUT",
        retryable=True,
    ) is True
    assert store.complete_rejected(claims[2]) is True
    for claim in claims[1:]:
        expired = store.owner_result(claim.submission_id, user.user_id)
        assert expired is not None and expired.failure is not None
        assert expired.failure["code"] == "JOB_DEADLINE"
        assert expired.failure["retryable"] is False


def test_terminal_timestamp_is_sampled_after_sqlite_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [datetime(2026, 8, 30, tzinfo=UTC)]
    monkeypatch.setattr(submission_store_module, "_utc_now", lambda: now[0])
    contract = _mock_contract(_artifacts(tmp_path))
    database_path = tmp_path / "submissions.db"
    store = SubmissionStore(database_path)
    user = store.register_user(auth_subject="subject-alice", public_handle="alice")
    created = store.create_submission(
        user=user,
        idempotency_key="lock-race",
        student_prompt="Return JSON.",
        contract=contract,
    )
    attempt = store.claim_submission(
        created.submission.submission_id,
        evaluation_identity_sha256=contract.evaluation_identity_sha256,
        contract_snapshot_sha256=contract.contract_snapshot_sha256,
        lease_seconds=2,
        max_attempts=contract.max_attempts,
        max_running_per_user=contract.max_running_submissions_per_user,
    )
    assert attempt.claim is not None

    blocker = sqlite3.connect(database_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    started = threading.Event()
    result = []

    def complete_after_lock() -> None:
        started.set()
        result.append(
            store.complete_failure(
                attempt.claim,
                failure_contract_version=contract.failure_contract_version,
                code="PROVIDER_TIMEOUT",
                retryable=True,
            )
        )

    thread = threading.Thread(target=complete_after_lock)
    thread.start()
    assert started.wait(timeout=1)
    time.sleep(0.05)
    now[0] += timedelta(seconds=3)
    blocker.rollback()
    blocker.close()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert result == [False]
    assert store.expire_leases(
        evaluation_identity_sha256=contract.evaluation_identity_sha256
    ) == 1
