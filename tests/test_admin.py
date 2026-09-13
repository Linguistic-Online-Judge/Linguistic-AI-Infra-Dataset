import hashlib
import json
import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_submission_integration import _artifacts, _mock_contract, _RecordingMockProvider

from linguistic_oj.admin import install_admin_routes
from linguistic_oj.admin_store import TeachingContent
from linguistic_oj.api import create_app
from linguistic_oj.auth import AuthService, install_auth_routes
from linguistic_oj.local_dev import LOCAL_PASSWORD, build_local_app
from linguistic_oj.submission_jobs import InMemoryJobQueue, OutboxDispatcher, SubmissionWorker
from linguistic_oj.submission_store import SubmissionStore

ORIGIN = "http://127.0.0.1:8080"
CONTENT = {
    "title": "A lesson",
    "summary": "",
    "instructions": "Private draft instructions\nNext line",
    "zero_shot_prompt": "Private zero-shot template",
    "few_shot_prompt": "Private few-shot template",
}


@pytest.fixture
def setup(tmp_path):
    artifacts = _artifacts(tmp_path)
    contract = _mock_contract(artifacts)
    store = SubmissionStore(tmp_path / "admin.db")
    auth = AuthService(store, public_origin=ORIGIN, mailer=lambda *args: None, development=True)
    users = {}
    for role in ("admin", "user"):
        store.auth_provision_account(f"{role}@example.test", "test-hash", role, role, None)
        account = store.auth_account(f"{role}@example.test")
        token = secrets.token_urlsafe(32)
        store.auth_create_session(account, hashlib.sha256(token.encode()).hexdigest(), 3600)
        users[role] = (account, token)
    queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
    dispatcher = OutboxDispatcher(store, queue, contract)
    catalog_only = _artifacts(tmp_path, version="catalog-only").public

    def make_app(*, override=True, runtime=True, public=artifacts.public, admin=True):
        app = create_app(
            store=store,
            contract=contract,
            dispatcher=dispatcher,
            authenticate=auth.authenticate,
            public_challenges={
                public.challenge_id: public,
                catalog_only.challenge_id: catalog_only,
            },
            runtime_availability={contract.challenge_id: runtime},
            allow_draft_submissions=override,
            environment="test",
        )
        if admin:
            install_auth_routes(app, auth)
            install_admin_routes(app)
        return app

    app = make_app()
    with TestClient(app, base_url=ORIGIN) as client:
        yield {
            "store": store,
            "auth": auth,
            "client": client,
            "users": users,
            "app": app,
            "make_app": make_app,
            "contract": contract,
            "artifacts": artifacts,
            "queue": queue,
            "dispatcher": dispatcher,
            "catalog_only_id": catalog_only.challenge_id,
            "url": "/v1/admin/challenges/" + contract.challenge_id,
        }


def sign_in(setup, role="admin", client=None):
    client = client or setup["client"]
    account, token = setup["users"][role]
    client.cookies.set("loj_local_session", token)
    client.headers.update(
        {"Origin": ORIGIN, "X-LOJ-CSRF": "1", "X-LOJ-Expected-User": account.user_id}
    )
    return account


def save(setup, revision=0, content=CONTENT, client=None):
    return (client or setup["client"]).put(
        setup["url"] + "/teaching-draft", json={"expected_revision": revision, "content": content}
    )


def action(setup, name, revision, **kwargs):
    return setup["client"].post(
        setup["url"] + "/" + name, json={"expected_revision": revision, **kwargs}
    )


def test_admin_authorization_cookie_only_fresh_roles_and_absent_legacy_routes(setup):
    client = setup["client"]
    for url in ("/v1/admin/challenges", setup["url"], "/v1/admin/unknown"):
        response = client.get(
            url, headers={"Authorization": "Bearer admin", "X-User-Role": "admin"}
        )
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    sign_in(setup, "user")
    assert client.get(setup["url"]).status_code == 403
    assert save(setup).status_code == 403
    admin = sign_in(setup)
    assert client.get(setup["url"]).status_code == 200
    setup["store"].set_account_role(admin.user_id, "user")
    assert client.get(setup["url"]).status_code == 403
    assert save(setup).status_code == 403
    legacy = setup["make_app"](admin=False)
    with TestClient(legacy, base_url=ORIGIN) as bare:
        assert bare.get(setup["url"]).status_code == 404
    with pytest.raises(ValueError, match="AuthService"):
        install_admin_routes(legacy)


@pytest.mark.parametrize("method", ["PUT", "POST", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "header,value,code",
    [
        ("Origin", "https://elsewhere.test", "AUTH_CSRF_REJECTED"),
        ("X-LOJ-CSRF", "0", "AUTH_CSRF_REJECTED"),
        ("Content-Type", "text/plain", "AUTH_CSRF_REJECTED"),
        ("X-LOJ-Expected-User", "someone-else", "AUTH_ACCOUNT_CHANGED"),
        ("X-LOJ-Expected-User", None, "AUTH_ACCOUNT_CHANGED"),
    ],
)
def test_every_unsafe_admin_method_is_same_origin_json_expected_account(
    setup, method, header, value, code
):
    client = setup["client"]
    sign_in(setup)
    if value is None:
        del client.headers[header]
    else:
        client.headers[header] = value
    response = client.request(method, setup["url"] + "/teaching-draft", json={})
    assert response.json()["error"]["code"] == code
    assert response.status_code == (409 if code == "AUTH_ACCOUNT_CHANGED" else 403)
    assert response.headers["cache-control"] == "no-store"


def test_content_lifecycle_exact_shapes_no_draft_leak_and_audit(setup, caplog):
    caplog.set_level(logging.INFO, logger="linguistic_oj.http")
    client = setup["client"]
    admin = sign_in(setup)
    initial = client.get(setup["url"]).json()
    assert set(initial) == {
        "challenge",
        "revision",
        "draft",
        "published",
        "published_revision",
        "admissions_closed",
        "can_reopen",
    }
    assert initial["revision"] == initial["published_revision"] == 0
    assert initial["draft"] is initial["published"] is None
    assert initial["admissions_closed"] is False
    items = client.get("/v1/admin/challenges").json()["items"]
    assert len(items) == 2
    assert set(items[0]) == {
        "challenge_id",
        "title",
        "language",
        "treebank",
        "task",
        "has_contract",
        "admissions_closed",
        "revision",
        "published_revision",
        "can_reopen",
    }
    assert action(setup, "check", 0).json()["can_publish"] is False
    assert save(setup).json()["revision"] == 1
    public_url = "/v1/challenges/" + setup["contract"].challenge_id + "/teaching"
    response = client.get(public_url)
    assert response.json() == {
        "challenge_id": setup["contract"].challenge_id,
        "published_revision": 0,
        "content": None,
    }
    assert response.headers["cache-control"] == "no-store"
    assert CONTENT["instructions"] not in client.get("/v1/challenges").text
    assert action(setup, "check", 1).json() == {"can_publish": True, "issues": [], "revision": 1}
    published = action(setup, "publish", 1).json()
    assert published["revision"] == published["published_revision"] == 2
    assert published["challenge"] == initial["challenge"]
    assert published["published"] == CONTENT
    next_content = {**CONTENT, "instructions": "An unpublished replacement", "title": "Next title"}
    assert save(setup, 2, next_content).json()["revision"] == 3
    assert client.get(public_url).json()["content"] == CONTENT
    assert client.get(public_url).json()["published_revision"] == 2
    assert action(setup, "publish", 2).json()["error"]["code"] == "ADMIN_REVISION_CONFLICT"
    assert save(setup, 0).status_code == 409
    assert action(setup, "check", 1).status_code == 409
    assert client.get(setup["url"]).json()["draft"] == next_content
    for secret in (admin.user_id, "An unpublished replacement"):
        assert secret not in client.get(public_url).text
        assert secret not in caplog.text
    with setup["store"]._auth_transaction() as tx:
        rows = tx.execute(
            "SELECT revision, action, operator_user_id, created_at, previous_json, next_json "
            "FROM challenge_admin_revisions ORDER BY revision"
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [(1, "save"), (2, "publish"), (3, "save")]
        assert all(row[2] == admin.user_id and row[3] for row in rows)
        assert json.loads(rows[2][4])["published_json"] == json.loads(rows[2][5])["published_json"]
    assert (
        setup["store"]
        .admin_states((setup["contract"].challenge_id,))[setup["contract"].challenge_id]
        .revision
        == 3
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", ""),
        ("title", "t" * 121),
        ("summary", "s" * 501),
        ("instructions", ""),
        ("instructions", "i" * 4001),
        ("zero_shot_prompt", ""),
        ("zero_shot_prompt", "z" * 3001),
        ("few_shot_prompt", ""),
        ("few_shot_prompt", "f" * 3001),
        ("summary", 1),
        ("instructions", "bad\x00text"),
        ("title", "bad\ttext"),
        ("summary", "bad\x7ftext"),
        ("summary", "bad\u0085text"),
        ("summary", "bad\ud800text"),
        ("summary", "bad\udffftext"),
        ("summary", "bad\u202etext"),
        ("source_fingerprint", "client-selected"),
    ],
    ids=lambda value: str(value)[:40],
)
def test_content_validation_is_strict_and_safe(setup, field, value):
    sign_in(setup)
    response = setup["client"].put(
        setup["url"] + "/teaching-draft",
        content=json.dumps({"expected_revision": 0, "content": {**CONTENT, field: value}}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert setup["client"].get(setup["url"]).json()["revision"] == 0


@pytest.mark.parametrize("revision", [True, False, -1, 1.0, "0", None, 9223372036854775807])
def test_revision_type_bounds_and_unknown_fields(setup, revision):
    sign_in(setup)
    assert save(setup, revision).status_code == 422


def test_body_limit_duplicate_json_and_plain_text_not_html(setup):
    sign_in(setup)
    content = {**CONTENT, "instructions": "<script>alert('lesson')</script>\nPlain text"}
    assert save(setup, content=content).status_code == 200
    assert action(setup, "publish", 1).status_code == 200
    response = setup["client"].get("/v1/challenges/" + setup["contract"].challenge_id + "/teaching")
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["content"] == content
    response = setup["client"].put(
        setup["url"] + "/teaching-draft",
        content=" " * 16385,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    response = setup["client"].post(
        setup["url"] + "/check",
        content='{"expected_revision": 2, "expected_revision": 2}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    oversized = {**CONTENT, "instructions": "\U0001f600" * 4000, "few_shot_prompt": "x" * 3000}
    with pytest.raises(ValueError):
        TeachingContent.model_validate(oversized)


def test_close_replay_conflict_outbox_history_results_and_restart(setup):
    client = setup["client"]
    user = sign_in(setup, "user")
    payload = {"challenge_id": setup["contract"].challenge_id, "student_prompt": "Return JSON"}
    accepted = client.post("/v1/submissions", json=payload, headers={"Idempotency-Key": "first"})
    assert accepted.status_code == 202
    submission_id = accepted.json()["submission_id"]
    sign_in(setup)
    closed = action(setup, "admissions", 0, closed=True)
    assert closed.status_code == 200, closed.text
    assert closed.json()["admissions_closed"] is True
    assert closed.json()["challenge"]["accepting_submissions"] is False
    assert save(setup, 1).status_code == 200
    assert action(setup, "publish", 2).json()["admissions_closed"] is True
    sign_in(setup, "user")
    assert (
        client.post("/v1/submissions", json=payload, headers={"Idempotency-Key": "first"}).json()
        == accepted.json()
    )
    conflict = client.post(
        "/v1/submissions",
        json={**payload, "student_prompt": "different"},
        headers={"Idempotency-Key": "first"},
    )
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    response = client.post("/v1/submissions", json=payload, headers={"Idempotency-Key": "new"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CHALLENGE_PAUSED"
    with setup["store"]._auth_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1
        assert tx.execute("SELECT COUNT(*) FROM submission_outbox").fetchone()[0] == 1
    provider = _RecordingMockProvider()
    worker = SubmissionWorker(
        store=setup["store"],
        queue=setup["queue"],
        contract=setup["contract"],
        artifacts=setup["artifacts"],
        provider=provider,
    )
    assert worker.run_once() is True
    assert provider.calls == 2
    assert client.get("/v1/submissions").json()["items"][0]["submission_id"] == submission_id
    result = client.get(f"/v1/submissions/{submission_id}/result")
    assert result.status_code == 200
    assert result.json()["outcome"] == "succeeded"
    assert setup["store"].owner_result(submission_id, user.user_id) is not None
    restarted = SubmissionStore(setup["store"]._database_path)
    assert restarted.admin_states((payload["challenge_id"],))[
        payload["challenge_id"]
    ].admissions_closed
    sign_in(setup)
    assert action(setup, "admissions", 3, closed=False).json()["admissions_closed"] is False
    sign_in(setup, "user")
    assert client.get(f"/v1/submissions/{submission_id}/result").json() == result.json()
    assert (
        client.post("/v1/submissions", json=payload, headers={"Idempotency-Key": "new"}).status_code
        == 202
    )


@pytest.mark.parametrize("override,runtime", [(False, True), (True, False), (False, False)])
def test_content_publication_never_overrides_rights_draft_or_runtime(setup, override, runtime):
    with TestClient(
        setup["make_app"](override=override, runtime=runtime), base_url=ORIGIN
    ) as client:
        sign_in(setup, client=client)
        assert (
            client.post(
                setup["url"] + "/admissions", json={"expected_revision": 0, "closed": True}
            ).status_code
            == 200
        )
        assert save(setup, 1, client=client).status_code == 200
        assert client.post(setup["url"] + "/check", json={"expected_revision": 2}).json() == {
            "can_publish": True,
            "issues": [],
            "revision": 2,
        }
        assert (
            client.post(setup["url"] + "/publish", json={"expected_revision": 2}).status_code == 200
        )
        detail = client.get(setup["url"]).json()
        assert detail["can_reopen"] is False
        assert detail["admissions_closed"] is True
        assert detail["challenge"]["status"] == setup["artifacts"].public.status
        response = client.post(
            setup["url"] + "/admissions", json={"expected_revision": 3, "closed": False}
        )
        assert response.status_code == 409


def test_catalog_only_teaching_allowed_unknown_ids_and_activation_forbidden(setup):
    sign_in(setup)
    original = setup["url"]
    setup["url"] = "/v1/admin/challenges/" + setup["catalog_only_id"]
    assert save(setup).status_code == 200
    assert action(setup, "check", 1).json()["can_publish"] is True
    detail = action(setup, "publish", 1).json()
    assert detail["published"] == CONTENT
    assert detail["can_reopen"] is False
    for closed in (True, False):
        assert action(setup, "admissions", 2, closed=closed).status_code == 409
    setup["url"] = "/v1/admin/challenges/not-loaded"
    assert save(setup).status_code == 404
    assert setup["client"].get(setup["url"]).status_code == 404
    assert setup["client"].get("/v1/challenges/not-loaded/teaching").status_code == 404
    assert setup["client"].get(original).json()["revision"] == 0


def test_source_binding_requires_new_draft_and_explicit_reopening(setup):
    sign_in(setup)
    assert save(setup).status_code == 200
    assert action(setup, "publish", 1).status_code == 200
    changed = setup["artifacts"].public.model_copy(update={"title": "Source description changed"})
    with TestClient(setup["make_app"](public=changed), base_url=ORIGIN) as client:
        sign_in(setup, client=client)
        detail = client.get(setup["url"]).json()
        assert detail["admissions_closed"] is True
        assert detail["can_reopen"] is False
        assert detail["published"] is None
        response = client.get("/v1/challenges/" + changed.challenge_id + "/teaching")
        assert response.json()["content"] is None
        assert client.post(setup["url"] + "/check", json={"expected_revision": 2}).json() == {
            "can_publish": False,
            "issues": [
                {
                    "code": "ADMIN_SOURCE_CHANGED",
                    "message": "The catalog source changed. Review and save an updated draft first",
                }
            ],
            "revision": 2,
        }
        for action_name, extra in (("publish", {}), ("admissions", {"closed": False})):
            response = client.post(
                setup["url"] + "/" + action_name, json={"expected_revision": 2, **extra}
            )
            assert response.json()["error"]["code"] == "ADMIN_SOURCE_CHANGED"
        sign_in(setup, "user", client)
        assert (
            client.post(
                "/v1/submissions",
                json={"challenge_id": changed.challenge_id, "student_prompt": "Test"},
                headers={"Idempotency-Key": "changed-source"},
            ).json()["error"]["code"]
            == "CHALLENGE_PAUSED"
        )
        sign_in(setup, client=client)
        assert save(setup, 2, client=client).json()["admissions_closed"] is True
        assert (
            client.post(setup["url"] + "/publish", json={"expected_revision": 3}).status_code == 200
        )
        assert (
            client.post(
                setup["url"] + "/admissions", json={"expected_revision": 4, "closed": False}
            ).json()["admissions_closed"]
            is False
        )


def test_concurrent_http_saves_use_revision_compare_and_swap(setup):
    sign_in(setup)
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: save(setup), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert setup["client"].get(setup["url"]).json()["revision"] == 1


@pytest.mark.parametrize("revoke", ["role", "session"])
def test_revocation_after_http_authentication_still_blocks_store_write(setup, monkeypatch, revoke):
    admin = sign_in(setup)
    entered, release = threading.Event(), threading.Event()
    original = setup["store"].admin_change

    def delayed(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(setup["store"], "admin_change", delayed)
    with ThreadPoolExecutor(1) as pool:
        response = pool.submit(save, setup)
        try:
            assert entered.wait(3)
            other = SubmissionStore(setup["store"]._database_path)
            if revoke == "role":
                other.set_account_role(admin.user_id, "user")
            else:
                token = setup["users"]["admin"][1]
                other.auth_revoke_session(hashlib.sha256(token.encode()).hexdigest())
        finally:
            release.set()
        rejected = response.result(timeout=10)
    assert rejected.status_code == (403 if revoke == "role" else 401)
    assert rejected.headers["cache-control"] == "no-store"
    assert (
        setup["store"]
        .admin_states((setup["contract"].challenge_id,))[setup["contract"].challenge_id]
        .revision
        == 0
    )


def test_teaching_is_anonymous_and_storage_failure_is_safe_no_store(setup, monkeypatch):
    url = "/v1/challenges/" + setup["contract"].challenge_id + "/teaching"
    assert setup["client"].get(url).status_code == 200

    def unavailable(*args, **kwargs):
        raise RuntimeError("private database failure with draft content")

    monkeypatch.setattr(setup["store"], "admin_states", unavailable)
    response = setup["client"].get(url)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text


def test_limits_allow_all_maximum_ascii_fields_and_unicode_text(setup):
    sign_in(setup)
    maximum = {
        "title": "t" * 120,
        "summary": "s" * 500,
        "instructions": "i" * 4000,
        "zero_shot_prompt": "z" * 3000,
        "few_shot_prompt": "f" * 3000,
    }
    assert save(setup, content=maximum).status_code == 200
    unicode_text = {**CONTENT, "title": "\u8bed\u8a00\u5b66", "summary": "\U0001f600\n\u00e9"}
    assert save(setup, 1, unicode_text).status_code == 200
    assert action(setup, "publish", 2).json()["published"] == unicode_text


def test_student_catalog_policy_reads_are_batched(setup, monkeypatch):
    sign_in(setup)
    action(setup, "admissions", 0, closed=True)
    calls = []
    original = setup["store"].admin_states

    def recorded(ids, **kwargs):
        calls.append(tuple(ids))
        return original(ids, **kwargs)

    monkeypatch.setattr(setup["store"], "admin_states", recorded)
    items = setup["client"].get("/v1/challenges").json()
    assert len(calls) == 1 and len(calls[0]) == len(items) == 2
    selected = next(
        item for item in items if item["challenge_id"] == setup["contract"].challenge_id
    )
    assert selected["admissions_closed"] is True
    assert selected["accepting_submissions"] is False
    calls.clear()
    response = setup["client"].get("/v1/challenges/" + setup["contract"].challenge_id)
    assert response.json() == selected
    assert calls == [(setup["contract"].challenge_id,)]


def test_local_launcher_installs_admin_and_persists_published_content(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "mvp_evaluation.json").write_bytes(
        (Path(__file__).parents[1] / "config/mvp_evaluation.json").read_bytes()
    )
    app = build_local_app(tmp_path, port=8080)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/v1/admin/challenges").status_code == 401
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": ORIGIN, "X-LOJ-CSRF": "1"},
            json={"email": "admin@example.test", "password": LOCAL_PASSWORD},
        )
        assert login.status_code == 200
        client.headers.update(
            {
                "Origin": ORIGIN,
                "X-LOJ-CSRF": "1",
                "X-LOJ-Expected-User": login.json()["user"]["user_id"],
            }
        )
        items = client.get("/v1/admin/challenges").json()["items"]
        assert len(items) == 5
        challenge_id = items[0]["challenge_id"]
        url = "/v1/admin/challenges/" + challenge_id
        assert (
            client.put(
                url + "/teaching-draft", json={"expected_revision": 0, "content": CONTENT}
            ).status_code
            == 200
        )
        assert client.post(url + "/publish", json={"expected_revision": 1}).status_code == 200
        assert (
            client.post(url + "/admissions", json={"expected_revision": 2, "closed": True}).json()[
                "admissions_closed"
            ]
            is True
        )
    with TestClient(build_local_app(tmp_path, port=8080), base_url=ORIGIN) as client:
        assert client.get("/v1/challenges/" + challenge_id + "/teaching").json() == {
            "challenge_id": challenge_id,
            "published_revision": 2,
            "content": CONTENT,
        }
        assert client.get("/v1/challenges/" + challenge_id).json()["admissions_closed"] is True
