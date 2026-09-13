import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import linguistic_oj.qwen_api as qwen_api_module
from linguistic_oj.api import Principal
from linguistic_oj.auth import AuthService
from linguistic_oj.mvp_contract import canonical_sha256
from linguistic_oj.submission_store import SubmissionStore

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('mutation', ['envelope', 'generation', 'tokenizer', 'runtime'])
def test_invalid_static_contract_is_rejected_before_store_or_queue(tmp_path, monkeypatch, mutation):
    config = json.loads((ROOT / 'config/mvp_evaluation_v2.json').read_text(encoding='utf-8'))
    identity = config['evaluation_identity']
    if mutation == 'envelope':
        identity['prompt_envelope_version'] = 'unsupported'
    elif mutation == 'generation':
        del identity['generation_settings']['seed']
    elif mutation == 'tokenizer':
        identity['tokenizer_identity']['add_generation_prompt'] = False
    else:
        identity['model_identity']['runtime'] = 'unsupported'
    config['leaderboard_partition']['expected_sha256'] = canonical_sha256(identity)
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(config), encoding='utf-8')

    def forbidden(*args, **kwargs):
        pytest.fail('invalid static configuration reached persistence or queue setup')

    monkeypatch.setattr(qwen_api_module, 'build_submission_store', forbidden)
    monkeypatch.setattr(qwen_api_module, 'RedisJobQueue', forbidden)
    with pytest.raises(ValueError):
        qwen_api_module.build_qwen_api(
            root=ROOT, database_path=tmp_path / 'never-created.db',
            redis_url='redis://127.0.0.1/0', contract_paths=(path,),
            authenticate=lambda request: Principal('fixture'), environment='development',
        )
    assert not (tmp_path / 'never-created.db').exists()


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
    contract = runtime.contracts["en-childes-upos-v1"]

    with TestClient(runtime.app) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        catalog = client.get("/v1/challenges").json()
        current_user = client.get("/v1/users/me").json()
        response = client.post(
            "/v1/submissions",
            headers={"Idempotency-Key": "qwen-api-composition"},
            json={
                "challenge_id": contract.challenge_id,
                "student_prompt": "Return JSON.",
            },
        )

    assert response.status_code == 202
    assert contract.contract_version == "mvp-evaluation-v2"
    queue = runtime.queues[contract.challenge_id]
    assert queue.routing_key == contract.contract_snapshot_sha256
    assert queue.visibility_timeout_seconds == 315
    assert all(queue.health_checks == 1 for queue in runtime.queues.values())
    assert len(queue.published) == 1
    assert len(catalog) == 26
    assert {
        item["challenge_id"] for item in catalog if item["submission_enabled"]
    } == set(runtime.contracts)
    assert all(item["runtime_available"] for item in catalog if item["submission_enabled"])
    assert all(item["accepting_submissions"] for item in catalog if item["submission_enabled"])
    assert all(runtime.runtime_availability.values())
    assert current_user == {
        "user_id": runtime.store.user_by_subject("subject-alice").user_id,
        "public_handle": "alice",
    }
    en_ewt = next(item for item in catalog if item["challenge_id"] == "en-ewt-upos-v1")
    assert en_ewt["annotation_license"] == "CC BY-SA 4.0"
    assert en_ewt["source_release"] == "unrecorded"
    assert en_ewt["source_file_sha256s"] == []
    assert "not secret" in en_ewt["benchmark_limitations"]
    with pytest.raises(RuntimeError, match="single-challenge"):
        _ = runtime.contract


def test_qwen_api_routes_multiple_contracts_to_isolated_queues(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(qwen_api_module, "RedisJobQueue", _Queue)
    deployment_root = tmp_path / "deployment"
    config_dir = deployment_root / "config"
    public_dir = deployment_root / "challenges" / "public"
    config_dir.mkdir(parents=True)
    public_dir.mkdir(parents=True)
    first_config = json.loads(
        (ROOT / "config" / "mvp_evaluation_v2.json").read_text(encoding="utf-8")
    )
    second_config = json.loads(json.dumps(first_config))
    second_config["catalog"]["challenge_id"] = "en-ewt-upos-v2"
    second_config["evaluation_identity"]["challenge_id"] = "en-ewt-upos-v2"
    second_config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        second_config["evaluation_identity"]
    )
    (config_dir / "first.json").write_text(json.dumps(first_config), encoding="utf-8")
    (config_dir / "second.json").write_text(json.dumps(second_config), encoding="utf-8")
    first_public = json.loads(
        (ROOT / "challenges" / "public" / "en-ewt-upos-v1.json").read_text(
            encoding="utf-8"
        )
    )
    second_public = {**first_public, "challenge_id": "en-ewt-upos-v2", "version": "v2"}
    (public_dir / "en-ewt-upos-v1.json").write_text(
        json.dumps(first_public),
        encoding="utf-8",
    )
    (public_dir / "en-ewt-upos-v2.json").write_text(
        json.dumps(second_public),
        encoding="utf-8",
    )
    runtime = qwen_api_module.build_qwen_api(
        root=deployment_root,
        database_path=tmp_path / "submissions.db",
        redis_url="redis://127.0.0.1:6379/0",
        authenticate=lambda request: Principal("subject-alice"),
        contract_paths=(
            Path("config/first.json"),
            Path("config/second.json"),
        ),
        allow_draft_submissions=True,
        environment="development",
    )
    runtime.store.register_user(auth_subject="subject-alice", public_handle="alice")

    with TestClient(runtime.app) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.post(
            "/v1/submissions",
            headers={"Idempotency-Key": "second-contract"},
            json={
                "challenge_id": "en-ewt-upos-v2",
                "student_prompt": "Return JSON.",
            },
        )

    assert response.status_code == 202
    assert set(runtime.contracts) == {"en-ewt-upos-v1", "en-ewt-upos-v2"}
    assert len(runtime.queues["en-ewt-upos-v1"].published) == 0
    assert len(runtime.queues["en-ewt-upos-v2"].published) == 1
    assert all(queue.health_checks == 1 for queue in runtime.queues.values())
    with pytest.raises(RuntimeError, match="single-challenge"):
        _ = runtime.contract


def test_qwen_api_finds_default_registry_below_a_relative_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    deployment_root = tmp_path / "deployment"
    config_dir = deployment_root / "config"
    public_dir = deployment_root / "challenges" / "public"
    config_dir.mkdir(parents=True)
    public_dir.mkdir(parents=True)
    contract_path = config_dir / "contract.json"
    contract_path.write_text(
        (ROOT / "config" / "mvp_evaluation_v2.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    public_path = public_dir / "en-ewt-upos-v1.json"
    public_path.write_text(
        (ROOT / "challenges" / "public" / "en-ewt-upos-v1.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (config_dir / "challenge_contract_registry_v1.json").write_text(
        json.dumps(
            {
                "schema_version": "challenge-contract-registry-v1",
                "entries": [
                    {
                        "public_descriptor_path": "challenges/public/en-ewt-upos-v1.json",
                        "evaluation_contract_path": "config/contract.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    contracts, public_challenges = qwen_api_module._load_runtime_registry(
        Path("deployment"),
        registry_path=None,
        contract_paths=None,
    )

    assert set(contracts) == {"en-ewt-upos-v1"}
    assert set(public_challenges) == {"en-ewt-upos-v1"}


def test_qwen_api_rejects_sqlite_in_production(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        qwen_api_module.build_qwen_api(
            root=ROOT,
            database_path=tmp_path / "submissions.db",
            redis_url="redis://127.0.0.1:6379/0",
            authenticate=lambda request: Principal("subject-alice"),
            environment="production",
        )


def test_qwen_api_requires_public_descriptor_for_each_explicit_contract(
    tmp_path: Path,
) -> None:
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        (ROOT / "config" / "mvp_evaluation_v2.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no public challenge descriptor"):
        qwen_api_module.build_qwen_api(
            root=tmp_path,
            database_path=tmp_path / "submissions.db",
            redis_url="redis://127.0.0.1:6379/0",
            authenticate=lambda request: Principal("subject-alice"),
            contract_paths=(contract_path,),
            environment="development",
        )


def test_qwen_api_cli_accepts_repeatable_contract_paths() -> None:
    args = qwen_api_module.parse_args(
        [
            "--root",
            ".",
            "--database",
            "runtime/submissions.db",
            "--redis-url",
            "redis://127.0.0.1:6379/0",
            "--environment",
            "development",
            "--authenticate",
            "deployment_auth:authenticate",
            "--contract",
            "config/first.json",
            "--contract",
            "config/second.json",
        ]
    )

    assert args.registry is None
    assert args.contracts == [Path("config/first.json"), Path("config/second.json")]


def test_qwen_api_cli_rejects_sqlite_in_production() -> None:
    with pytest.raises(SystemExit):
        qwen_api_module.parse_args(
            [
                "--root",
                ".",
                "--database",
                "runtime/submissions.db",
                "--redis-url",
                "redis://127.0.0.1:6379/0",
                "--authenticate",
                "deployment_auth:authenticate",
            ]
        )


def test_production_requires_config_and_rejects_callbacks_before_store_access(monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("must reject the auth policy before opening persistence")

    monkeypatch.setattr(qwen_api_module, "build_submission_store", forbidden)
    for options in ({}, {"authenticate": lambda request: Principal("legacy")}, {
        "authenticate": lambda request: Principal("legacy"), "auth_config_file": Path("auth.json"),
    }):
        with pytest.raises(ValueError):
            qwen_api_module.build_qwen_api(
                root=ROOT, redis_url="redis://127.0.0.1:6379/0",
                postgres_database_url="postgresql://localhost/judge", **options,
            )


@pytest.mark.parametrize("authentication", [
    [], ["--authenticate", "legacy:callback"],
    ["--authenticate", "legacy:callback", "--auth-config-file", "auth.json"],
])
def test_production_cli_requires_exclusive_auth_config(authentication):
    with pytest.raises(SystemExit):
        qwen_api_module.parse_args([
            "--root", ".", "--postgres-database-url", "postgresql://localhost/judge",
            "--redis-url", "redis://127.0.0.1:6379/0", *authentication,
        ])


def test_production_composes_cookie_auth_on_same_database_with_readiness(tmp_path, monkeypatch):
    store = SubmissionStore(tmp_path / "auth-composition.db")
    mail = []
    service = AuthService(store, public_origin="https://judge.example.test",
                          mailer=lambda *args: mail.append(args))
    checks = []
    original_check = service.health_check

    def health_check():
        checks.append(True)
        original_check()

    monkeypatch.setattr(service, "health_check", health_check)
    monkeypatch.setattr(qwen_api_module, "build_submission_store", lambda **kwargs: store)
    monkeypatch.setattr(qwen_api_module, "RedisJobQueue", _Queue)

    def build_auth(selected_store, path):
        assert selected_store is store
        assert path == Path("private-auth.json")
        return service

    monkeypatch.setattr(qwen_api_module, "build_auth_service", build_auth)
    runtime = qwen_api_module.build_qwen_api(
        root=ROOT, postgres_database_url="postgresql://localhost/judge",
        redis_url="redis://127.0.0.1:6379/0", auth_config_file=Path("private-auth.json"),
    )
    assert runtime.app.state.auth_service is service
    assert runtime.app.state.admin_service.auth is service
    assert runtime.app.state.admin_service.store is store
    assert runtime.store is store
    assert checks == [True]
    assert not any(runtime.runtime_availability.values())
    with TestClient(runtime.app, base_url=service.public_origin, headers={
        "Origin": service.public_origin, "X-LOJ-CSRF": "1",
    }) as client:
        assert client.get("/health/ready").status_code == 200
        assert len(checks) == 2
        assert client.get("/v1/auth/config").json()["mode"] == "email"
        assert client.get("/v1/admin/challenges").status_code == 401
        response = client.get("/v1/users/me", headers={"Authorization": "Bearer old"})
        assert response.status_code == 401
        response = client.post("/v1/auth/register", json={"email": "alice@example.test"})
        assert response.status_code == 202
        assert service._mail_queue.wait_for_idle(5)
        token = mail[-1][2].split("token=", 1)[1]
        assert client.post("/v1/auth/verify-email", json={
            "token": token, "password": "correct long password", "public_handle": "alice",
        }).status_code == 200
        assert client.post("/v1/auth/login", json={
            "email": "alice@example.test", "password": "correct long password",
        }).status_code == 200
        assert client.get("/v1/users/me").json()["public_handle"] == "alice"
        assert client.get("/v1/admin/challenges").status_code == 403
        store.set_account_role(store.auth_account("alice@example.test").user_id, "admin")
        admin_items = client.get("/v1/admin/challenges").json()["items"]
        assert len(admin_items) == len(runtime.public_challenges)
        assert all(item["can_reopen"] is False for item in admin_items)
        client.headers.pop("x-loj-csrf")
        assert client.post("/v1/submissions", json={}).status_code == 403

        def broken():
            raise RuntimeError("private database details")

        monkeypatch.setattr(service, "health_check", broken)
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_NOT_READY"
        assert "private" not in response.text


def test_unmigrated_auth_database_fails_before_composition_without_auto_upgrade(monkeypatch):
    class Store:
        def auth_health_check(self):
            raise RuntimeError("schema has not been migrated")

    store = Store()
    monkeypatch.setattr(qwen_api_module, "build_submission_store", lambda **kwargs: store)
    monkeypatch.setattr(qwen_api_module, "build_auth_service", lambda store, path: AuthService(
        store, public_origin="https://judge.test", mailer=lambda *args: None,
    ))
    with pytest.raises(RuntimeError, match="not been migrated"):
        qwen_api_module.build_qwen_api(
            root=ROOT, postgres_database_url="postgresql://localhost/judge",
            redis_url="redis://127.0.0.1:6379/0", auth_config_file=Path("private-auth.json"),
        )


def test_production_refuses_unbound_legacy_accounts_before_recovery(tmp_path, monkeypatch):
    store = SubmissionStore(tmp_path / "legacy-composition.db")
    user = store.register_user(auth_subject="legacy-subject", public_handle="legacy-handle")
    contract = qwen_api_module.EvaluationContract.from_path(ROOT / "config/mvp_evaluation_v2.json")
    created = store.create_submission(
        user=user, idempotency_key="legacy-key", student_prompt="Retained legacy prompt",
        contract=contract,
    )
    service = AuthService(
        store, public_origin="https://judge.example.test", mailer=lambda *args: None,
    )
    monkeypatch.setattr(qwen_api_module, "build_submission_store", lambda **kwargs: store)
    monkeypatch.setattr(qwen_api_module, "build_auth_service", lambda *args: service)

    def forbidden(**kwargs):
        pytest.fail("production must reject legacy accounts before queue creation or recovery")

    monkeypatch.setattr(qwen_api_module, "RedisJobQueue", forbidden)
    with pytest.raises(RuntimeError, match="trusted account-enrollment.*isolated new database"):
        qwen_api_module.build_qwen_api(
            root=ROOT, postgres_database_url="postgresql://localhost/judge",
            redis_url="redis://127.0.0.1:6379/0", auth_config_file=Path("private-auth.json"),
        )
    assert service._mail_queue._worker is None
    assert store.user_by_subject("legacy-subject") == user
    retained = store.submission_for_owner(created.submission.submission_id, user.user_id)
    assert retained == created.submission
    assert store.count_submissions() == store.count_outbox_records() == 1
    assert store.auth_account("legacy-handle@example.test") is None


def test_main_disables_access_logging_and_forwarded_header_trust(monkeypatch):
    import sys
    from types import SimpleNamespace

    captured = {}
    sentinel = object()

    def compose(**kwargs):
        captured["compose"] = kwargs
        return SimpleNamespace(app=sentinel)

    def run(app, **kwargs):
        assert app is sentinel
        captured["run"] = kwargs

    monkeypatch.setattr(qwen_api_module, "build_qwen_api", compose)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))
    assert qwen_api_module.main([
        "--root", ".", "--postgres-database-url", "postgresql://localhost/judge",
        "--redis-url", "redis://127.0.0.1:6379/0", "--auth-config-file", "private-auth.json",
    ]) == 0
    assert captured["compose"]["authenticate"] is None
    assert captured["compose"]["auth_config_file"] == Path("private-auth.json")
    assert captured["run"] == {
        "host": "127.0.0.1", "port": 8080, "access_log": False, "proxy_headers": False,
    }
