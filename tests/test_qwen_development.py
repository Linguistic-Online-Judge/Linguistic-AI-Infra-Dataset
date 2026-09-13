import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import linguistic_oj.qwen_development as module
from linguistic_oj.auth import AuthService, install_auth_routes
from linguistic_oj.challenge import LANGUAGE_CODES, build_challenge, write_challenge
from linguistic_oj.mvp_contract import canonical_sha256
from linguistic_oj.submission_jobs import InMemoryJobQueue
from linguistic_oj.submission_store import SubmissionStore

ROOT = Path(__file__).resolve().parents[1]
CATALOG_FIELDS = {
    "challenge_id",
    "annotation_license",
    "attribution_requirements",
    "source_release",
    "source_commit",
    "source_file_sha256s",
    "share_alike_requirements",
    "underlying_text_rights",
    "status",
    "security_level",
}


@pytest.fixture
def catalog(tmp_path):
    root, data = tmp_path / "source", tmp_path / "data"
    (root / "config/evaluation_contracts").mkdir(parents=True)
    data_dir = data / "Standard_Dataset/by_language"
    data_dir.mkdir(parents=True)
    entries = []
    for language in LANGUAGE_CODES:
        dataset = data_dir / f"{language}_fixture.jsonl"
        dataset.write_text(
            json.dumps(
                {
                    "id": language + "-1",
                    "language": language,
                    "treebank": "Tiny",
                    "text": "Hi",
                    "tasks_available": ["upos"],
                    "answers": {"segmentation": ["Hi"], "upos": ["INTJ"]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts = build_challenge(
            dataset,
            language=language,
            treebank="Tiny",
            task="upos",
            count=1,
            seed=2026,
            version="test-v1",
        )
        write_challenge(
            artifacts,
            public_dir=root / "challenges/public",
            private_dir=data / "runtime/private/challenges",
        )
        key = artifacts.public.challenge_id
        config = json.loads((ROOT / "config/mvp_evaluation_v2.json").read_text(encoding="utf-8"))
        config["catalog"].update(artifacts.public.model_dump(mode="json", include=CATALOG_FIELDS))
        config["evaluation_identity"].update(
            {
                "challenge_id": key,
                "dataset_sha256": artifacts.public.dataset_sha256,
                "selection_sha256": artifacts.public.selection_sha256,
            }
        )
        config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
            config["evaluation_identity"]
        )
        relative = f"config/evaluation_contracts/{key}.json"
        (root / relative).write_text(json.dumps(config), encoding="utf-8")
        entries.append(
            {
                "public_descriptor_path": f"challenges/public/{key}.json",
                "evaluation_contract_path": relative,
            }
        )
    registry = root / "config/challenge_contract_registry_v1.json"
    registry.write_text(
        json.dumps({"schema_version": "challenge-contract-registry-v1", "entries": entries}),
        encoding="utf-8",
    )
    return root, data, registry


def test_all_18_languages_have_verified_artifacts(catalog):
    root, data, _ = catalog
    registry, artifacts = module.load_development_catalog(root, data)
    assert len(artifacts) == 18
    assert {item.public.language for item in artifacts.values()} == set(LANGUAGE_CODES)
    assert set(artifacts) == set(registry.contracts)


def test_partial_coverage_is_rejected_instead_of_silently_shrunk(catalog):
    root, data, registry = catalog
    content = json.loads(registry.read_text(encoding="utf-8"))
    content["entries"].pop()
    registry.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="all 18"):
        module.load_development_catalog(root, data)


def test_corrupted_data_is_not_served(catalog):
    root, data, _ = catalog
    dataset = data / "Standard_Dataset/by_language/English_fixture.jsonl"
    dataset.write_bytes(dataset.read_bytes() + b" ")
    with pytest.raises(ValueError):
        module.load_development_catalog(root, data)


def test_real_dev_cookie_does_not_overwrite_mock_cookie(tmp_path):
    store = SubmissionStore(tmp_path / "cookie.db")
    service = AuthService(
        store,
        public_origin="http://127.0.0.1:8090",
        mailer=lambda *args: None,
        development=True,
        development_cookie_name="loj_qwen_dev_session",
    )
    service.provision_development_account("alice@example.test", module.LOCAL_PASSWORD, "Alice")
    app = FastAPI()
    install_auth_routes(app, service)
    with TestClient(app, base_url="http://127.0.0.1:8090") as client:
        client.cookies.set("loj_local_session", "old-local-mock-session")
        response = client.post(
            "/v1/auth/login",
            headers={
                "Origin": "http://127.0.0.1:8090",
                "X-LOJ-CSRF": "1",
            },
            json={"email": "alice@example.test", "password": module.LOCAL_PASSWORD},
        )
        assert response.status_code == 200
        assert client.cookies.get("loj_qwen_dev_session")
        assert client.cookies.get("loj_local_session") == "old-local-mock-session"
        assert "session_token" not in response.json()
    with pytest.raises(ValueError, match="custom cookie"):
        AuthService(
            store,
            public_origin="https://judge.example",
            mailer=lambda *args: None,
            development_cookie_name="loj_qwen_dev_session",
        )


@pytest.mark.parametrize("unconfirmed", [False, True])
def test_composition_requires_real_provider_and_cleans_queues(
    catalog, tmp_path, monkeypatch, unconfirmed
):
    root, data, _ = catalog
    store = SubmissionStore(tmp_path / "service.db")
    monkeypatch.setattr(module, "prepare_store", lambda *a, **kw: (store, {"instance": "fixture"}))
    queues, workers = [], []

    class Queue(InMemoryJobQueue):
        def __init__(self, **kwargs):
            super().__init__(
                kwargs["routing_key"],
                visibility_timeout_seconds=kwargs["visibility_timeout_seconds"],
            )
            self.closed = False
            queues.append(self)

        def health_check(self):
            assert not self.closed

        def close(self):
            self.closed = True

    class Worker:
        def __init__(self, **kwargs):
            assert kwargs["provider"].identity.model == "Qwen/Qwen3.5-9B"
            self.provider = kwargs["provider"]
            self.calls = 0
            workers.append(self)

        def run_once(self):
            self.calls += 1
            if unconfirmed:
                self.provider._active_request = object()
            return False

    monkeypatch.setattr(module, "RedisJobQueue", Queue)
    monkeypatch.setattr(module, "QwenSubmissionWorker", Worker)
    app = module.build_qwen_development(
        SimpleNamespace(
            root=root,
            data_root=data,
            state_dir=tmp_path,
            postgres_socket=tmp_path,
            postgres_port=5433,
            initialize=False,
            port=8090,
            redis_socket=tmp_path / "redis.sock",
            tokenizer_snapshot=tmp_path,
            launch_evidence=tmp_path / "launch.json",
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8090") as client:
        if unconfirmed:
            deadline = time.monotonic() + 3
            while client.get("/health/ready").status_code == 200 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert client.get("/health/ready").status_code == 503
            assert (tmp_path / "uncertain-inference.json").exists()
            assert all(worker.calls == 0 for worker in workers[1:])
            assert all(
                not item["runtime_available"] for item in client.get("/v1/challenges").json()
            )
        else:
            assert client.get("/health/ready").status_code == 200
        assert client.get("/", headers={"Host": "remote.example"}).status_code == 403
        metadata = client.get("/v1/development", headers={"X-LOJ-Development": "1"}).json()
        assert metadata["evaluation_mode"] == "qwen"
        assert metadata["language_count"] == 18
        assert metadata["executable_challenges"] == 18
        assert len(client.get("/v1/challenges").json()) == 18
    assert len(workers) == 18
    assert all(queue.closed for queue in queues)


@pytest.mark.parametrize("altered", ["kind", "database", "contracts", "owner"])
def test_instance_marker_cannot_select_an_unrelated_database(tmp_path, monkeypatch, altered):
    def forbidden_connect(**kwargs):
        pytest.fail("An invalid marker must fail before connecting to PostgreSQL")

    monkeypatch.setitem(
        sys.modules, "psycopg", SimpleNamespace(connect=forbidden_connect, sql=None)
    )
    monkeypatch.setitem(
        sys.modules,
        "pwd",
        SimpleNamespace(
            getpwuid=lambda uid: SimpleNamespace(pw_name="developer"),
        ),
    )
    monkeypatch.setattr(module.os, "geteuid", lambda: 123, raising=False)
    marker = {
        "kind": module.INSTANCE_KIND,
        "instance": "a" * 32,
        "owner": "developer",
        "database": "loj_dev18_" + "a" * 16,
        "contracts": {"task": "hash"},
    }
    marker[altered] = "unrelated-production-value"
    (tmp_path / "instance.json").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(ValueError, match="ownership or contract"):
        module.prepare_store(
            tmp_path,
            pg_socket=tmp_path,
            pg_port=5433,
            initialize=False,
            contract_hashes={"task": "hash"},
        )
