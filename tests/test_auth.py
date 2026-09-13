import hashlib
import json
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import linguistic_oj.auth as auth_module
from linguistic_oj.api import APIError, AuthenticationError, create_app
from linguistic_oj.auth import AuthService, install_auth_routes, normalize_email
from linguistic_oj.mvp_contract import EvaluationContract
from linguistic_oj.submission_jobs import InMemoryJobQueue, OutboxDispatcher
from linguistic_oj.submission_store import SubmissionStore

ORIGIN = "https://judge.example.test"
PASSWORD = "long unique password 123"
NEW_PASSWORD = "another long password 456"
EMAIL = "alice@example.test"
HEADERS = {"Origin": ORIGIN, "X-LOJ-CSRF": "1"}


class MailLog(list):
    def wait(self):
        assert self.service._mail_queue.wait_for_idle(5), "mail worker did not finish"


def request(*, token=None, host="judge.example.test", peer="127.0.0.1", headers=()):
    values = [(b"host", host.encode()), *headers]
    if token is not None:
        values.append((b"cookie", f"__Host-loj_session={token}".encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/auth/login",
            "scheme": "https",
            "headers": values,
            "client": (peer, 1234),
            "server": (host, 443),
            "query_string": b"",
        }
    )


@pytest.fixture
def setup(tmp_path):
    store = SubmissionStore(tmp_path / "auth.db")
    messages = MailLog()
    service = AuthService(store, public_origin=ORIGIN, mailer=lambda *args: messages.append(args))
    messages.service = service
    app = FastAPI()
    install_auth_routes(app, service)
    with TestClient(app, base_url=ORIGIN, headers=HEADERS) as client:
        yield store, service, client, messages


def token_from(messages):
    messages.wait()
    recipient, purpose, link = messages[-1]
    parts = urlsplit(link)
    assert parts.query == "" and parts.path == "/"
    assert parts.fragment.startswith(purpose + "?token=")
    return parse_qs(parts.fragment.partition("?")[2])["token"][0]


def register(client, messages, *, email=EMAIL, handle="alice"):
    response = client.post("/v1/auth/register", json={"email": email})
    assert response.status_code == 202, response.text
    token = token_from(messages)
    response = client.post(
        "/v1/auth/verify-email",
        json={
            "token": token,
            "password": PASSWORD,
            "public_handle": handle,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified"}
    return token


def login(client, *, email=EMAIL, password=PASSWORD):
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


def test_email_first_lifecycle_cookie_logout_restart_and_secret_storage(setup):
    store, service, client, messages = setup
    assert client.get("/v1/auth/config").json() == {
        "enabled": True,
        "mode": "email",
        "mail_delivery": "smtp",
    }
    assert client.get("/v1/auth/session").status_code == 401
    assert client.post("/v1/auth/register", json={"email": " ALICE@EXAMPLE.TEST "}).json() == {
        "status": "accepted",
    }
    assert store.auth_account(EMAIL) is None
    verification_token = token_from(messages)
    assert messages[-1][0] == EMAIL
    assert len(verification_token) == 43
    response = client.post(
        "/v1/auth/verify-email",
        json={
            "token": verification_token,
            "password": PASSWORD,
            "public_handle": "alice",
        },
    )
    assert response.json() == {"status": "verified"}
    assert "set-cookie" not in response.headers
    response = login(client)
    assert set(response.json()) == {"user", "expires_at"}
    assert set(response.json()["user"]) == {"user_id", "public_handle", "role"}
    assert response.json()["user"]["role"] == "user"
    expires = datetime.fromisoformat(response.json()["expires_at"])
    assert timedelta(hours=7, minutes=59) < expires - datetime.now(UTC) <= timedelta(hours=8)
    cookie = response.headers["set-cookie"]
    for flag in ("__Host-loj_session=", "HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag in cookie
    assert "Domain=" not in cookie
    raw = client.cookies.get("__Host-loj_session")
    account = store.auth_account(EMAIL)
    assert account.password_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert service.authenticate(request(token=raw)).subject == account.subject
    assert client.get("/v1/auth/session").json() == response.json()
    restarted = AuthService(
        SubmissionStore(store._database_path),
        public_origin=ORIGIN,
        mailer=lambda *args: None,
    )
    assert restarted.authenticate(request(token=raw)).subject == account.subject
    with sqlite3.connect(store._database_path) as connection:
        dumped = "\n".join(connection.iterdump())
    for secret in (PASSWORD, raw, verification_token):
        assert secret not in dumped
    assert hashlib.sha256(raw.encode()).hexdigest() in dumped
    response = client.post("/v1/auth/logout", json={})
    assert response.json() == {"status": "signed_out"}
    assert "Max-Age=0" in response.headers["set-cookie"]
    with pytest.raises(AuthenticationError):
        restarted.authenticate(request(token=raw))
    assert client.get("/v1/auth/session").status_code == 401


def test_reset_revokes_all_sessions_and_all_links_without_changing_id_or_role(setup):
    store, service, client, messages = setup
    register(client, messages)
    account = store.auth_account(EMAIL)
    store.set_account_role(account.user_id, "admin")
    login(client)
    first = client.cookies.get("__Host-loj_session")
    client.cookies.clear()
    login(client)
    second = client.cookies.get("__Host-loj_session")
    assert first != second
    assert client.get("/v1/auth/session").json()["user"]["role"] == "admin"
    assert client.post("/v1/auth/password-reset/request", json={"email": EMAIL}).status_code == 202
    reset = token_from(messages)
    response = client.post(
        "/v1/auth/password-reset/confirm",
        json={
            "token": reset,
            "password": NEW_PASSWORD,
        },
    )
    assert response.json() == {"status": "password_updated"}
    assert "Max-Age=0" in response.headers["set-cookie"]
    for raw in (first, second):
        with pytest.raises(AuthenticationError):
            service.authenticate(request(token=raw))
    assert (
        client.post(
            "/v1/auth/password-reset/confirm",
            json={
                "token": reset,
                "password": PASSWORD,
            },
        ).status_code
        == 401
    )
    assert (
        client.post("/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}).json()["error"][
            "code"
        ]
        == "AUTH_INVALID_CREDENTIALS"
    )
    assert login(client, password=NEW_PASSWORD).json()["user"] == {
        "user_id": account.user_id,
        "public_handle": "alice",
        "role": "admin",
    }


def test_action_expiry_one_use_wrong_purpose_and_no_user_until_verified(setup):
    store, _, client, messages = setup
    token = register(client, messages)
    assert (
        client.post(
            "/v1/auth/verify-email",
            json={
                "token": token,
                "password": PASSWORD,
                "public_handle": "another",
            },
        ).status_code
        == 401
    )
    client.post("/v1/auth/register", json={"email": "bob@example.test"})
    token = token_from(messages)
    assert (
        client.post(
            "/v1/auth/password-reset/confirm",
            json={
                "token": token,
                "password": PASSWORD,
            },
        ).status_code
        == 401
    )
    with sqlite3.connect(store._database_path) as connection:
        connection.execute("UPDATE auth_action_tokens SET expires_at = 0")
    assert (
        client.post(
            "/v1/auth/verify-email",
            json={
                "token": token,
                "password": PASSWORD,
                "public_handle": "bob",
            },
        ).status_code
        == 401
    )
    assert store.auth_account("bob@example.test") is None
    login(client)
    with sqlite3.connect(store._database_path) as connection:
        connection.execute("UPDATE auth_sessions SET expires_at = 0")
    assert client.get("/v1/auth/session").status_code == 401


@pytest.mark.parametrize("password", ["short", "aB12345", "x" * 129, "\ud800" * 15, 123, None])
def test_password_validation_does_not_create_users_or_leak_inputs(setup, password):
    store, _, client, messages = setup
    client.post("/v1/auth/register", json={"email": EMAIL})
    token = token_from(messages)
    response = client.post(
        "/v1/auth/verify-email",
        content=json.dumps(
            {
                "token": token,
                "password": password,
                "public_handle": "alice",
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "AUTH_VALIDATION_ERROR"
    assert response.json()["error"]["details"] == {}
    assert token not in response.text
    assert store.auth_account(EMAIL) is None


@pytest.mark.parametrize("handle", ["ab", "x" * 33, "a@b", "alice\n", "ab\u200bc", " abc"])
def test_handle_validation(setup, handle):
    store, _, client, messages = setup
    client.post("/v1/auth/register", json={"email": EMAIL})
    response = client.post(
        "/v1/auth/verify-email",
        json={
            "token": token_from(messages),
            "password": PASSWORD,
            "public_handle": handle,
        },
    )
    assert response.status_code == 422
    assert store.auth_account(EMAIL) is None


def test_unicode_password_boundary_and_no_email_alias_rewrites(setup):
    _, _, client, messages = setup
    password = "\U0001f600" * 128
    client.post("/v1/auth/register", json={"email": EMAIL})
    assert (
        client.post(
            "/v1/auth/verify-email",
            json={
                "token": token_from(messages),
                "password": password,
                "public_handle": "alice",
            },
        ).status_code
        == 200
    )
    login(client, password=password)
    assert normalize_email(" A.Lice+Tag@Example.TEST ") == "a.lice+tag@example.test"


@pytest.mark.parametrize(
    "password",
    ["aBcd1234", "abcdefgh", "90714268", "A1! bcde", "中文密码测试八位", "x" * 128],
)
def test_eight_character_policy_registration_and_login(setup, password):
    store, _, client, messages = setup
    client.post("/v1/auth/register", json={"email": EMAIL})
    response = client.post("/v1/auth/verify-email", json={
        "token": token_from(messages), "password": password, "public_handle": "alice",
    })
    assert response.status_code == 200, response.text
    assert store.auth_account(EMAIL).password_hash.startswith("$argon2id$")
    login(client, password=password)


def test_existing_long_password_can_reset_to_eight_characters(setup):
    _, _, client, messages = setup
    register(client, messages)
    login(client)
    client.post("/v1/auth/password-reset/request", json={"email": EMAIL})
    token = token_from(messages)
    assert client.post("/v1/auth/password-reset/confirm", json={
        "token": token, "password": "seven77",
    }).status_code == 422
    # A rejected password must not consume the reset link.
    assert client.post("/v1/auth/password-reset/confirm", json={
        "token": token, "password": "NewPass8",
    }).status_code == 200
    login(client, password="NewPass8")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "invalid"},
        {"email": EMAIL, "password": PASSWORD},
        {"email": EMAIL, "role": "admin"},
        {"email": 123},
        {"email": "a\r\nBcc:x@y.test"},
    ],
)
def test_email_only_registration_rejects_invalid_or_extra_fields(setup, payload):
    _, _, client, messages = setup
    response = client.post("/v1/auth/register", json=payload)
    assert response.status_code == 422
    assert messages == []


@pytest.mark.parametrize("body", ['{"email":"x", "email":"y"}', "null", "[]", "{", "x" * 8193])
def test_bounded_strict_json(setup, body):
    _, _, client, _ = setup
    response = client.post(
        "/v1/auth/register",
        content=body,
        headers={
            "Content-Type": "application/json",
        },
    )
    assert response.status_code in {413, 422}
    assert response.json()["error"]["code"] == "AUTH_VALIDATION_ERROR"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": ORIGIN},
        {"X-LOJ-CSRF": "1"},
        {"Origin": ORIGIN + "/", "X-LOJ-CSRF": "1"},
        {"Origin": "https://evil.test", "X-LOJ-CSRF": "1"},
        {"Origin": "null", "X-LOJ-CSRF": "1"},
        {"Origin": ORIGIN, "X-LOJ-CSRF": "true"},
        {**HEADERS, "Content-Type": "text/plain"},
    ],
)
def test_auth_requires_exact_origin_csrf_marker_and_json(setup, headers):
    _, _, client, messages = setup
    client.headers.clear()
    response = client.post("/v1/auth/register", json={"email": EMAIL}, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_CSRF_REJECTED"
    assert "access-control-allow-origin" not in response.headers
    assert messages == []


def test_duplicate_headers_and_duplicate_cookies_fail_closed_without_bearer_fallback(setup):
    _, service, client, messages = setup
    register(client, messages)
    login(client)
    token = client.cookies.get("__Host-loj_session")
    for cookies in [
        [("Cookie", f"__Host-loj_session={token}; __Host-loj_session={token}")],
        [("Cookie", f"__Host-loj_session={token}"), ("Cookie", f"__Host-loj_session={token}")],
        [("Cookie", "__Host-loj_session=invalid")],
    ]:
        response = client.get(
            "/v1/auth/session",
            headers=[
                *cookies,
                ("Authorization", "Bearer valid-legacy-token"),
            ],
        )
        assert response.status_code == 401
    assert (
        service.authenticate(
            request(
                token=token,
                headers=[
                    (b"authorization", b"Bearer another-user"),
                ],
            )
        ).subject
        == service.store.auth_account(EMAIL).subject
    )
    with pytest.raises(AuthenticationError):
        service.authenticate(request(headers=[(b"authorization", b"Bearer " + token.encode())]))
    response = client.post(
        "/v1/auth/logout",
        json={},
        headers=[
            ("Origin", ORIGIN),
            ("Origin", ORIGIN),
            ("X-LOJ-CSRF", "1"),
        ],
    )
    assert response.status_code == 403


def test_generic_responses_and_durable_throttles_before_hash_mail(setup, monkeypatch):
    store, _, client, messages = setup
    register(client, messages)
    count = len(messages)
    assert client.post("/v1/auth/register", json={"email": EMAIL}).json() == {"status": "accepted"}
    assert client.post(
        "/v1/auth/password-reset/request",
        json={
            "email": "nobody@example.test",
        },
    ).json() == {"status": "accepted"}
    assert len(messages) == count
    seen = []
    original = auth_module._hash_password

    def tracked(password, **kwargs):
        seen.append(kwargs.get("verify"))
        return original(password, **kwargs)

    monkeypatch.setattr(auth_module, "_hash_password", tracked)
    for index in range(10):
        response = client.post(
            "/v1/auth/login",
            json={
                "email": "nobody@example.test",
                "password": PASSWORD,
            },
            headers={"X-Forwarded-For": f"10.0.0.{index}"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
    assert seen == [auth_module._DUMMY_HASH] * 10
    restarted = AuthService(
        SubmissionStore(store._database_path),
        public_origin=ORIGIN,
        mailer=lambda *args: None,
    )
    with pytest.raises(APIError) as error:
        restarted.login(request(peer="different"), "nobody@example.test", PASSWORD)
    assert error.value.code == "AUTH_RATE_LIMITED"
    assert len(seen) == 10
    for _ in range(3):
        assert client.post("/v1/auth/register", json={"email": EMAIL}).status_code == 202
    response = client.post("/v1/auth/register", json={"email": EMAIL})
    assert response.status_code == 429
    assert len(messages) == count


def test_hash_concurrency_is_bounded(setup):
    _, _, client, _ = setup
    slots = auth_module._HASH_SLOTS
    for _ in range(4):
        assert slots.acquire(blocking=False)
    try:
        response = client.post("/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert response.status_code == 429
    finally:
        for _ in range(4):
            slots.release()


def test_mail_database_failures_are_sanitized_and_no_store(setup, monkeypatch):
    store, service, client, _ = setup

    def broken(*args, **kwargs):
        raise RuntimeError("private password and mail token")

    monkeypatch.setattr(service, "mailer", broken)
    response = client.post("/v1/auth/register", json={"email": EMAIL})
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert service._mail_queue.wait_for_idle(5)
    assert "private" not in response.text
    monkeypatch.setattr(store, "auth_account", broken)
    response = client.post("/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.test:8080",
        "http://0.0.0.0:8080",
        "http://127.1:8080",
        "http://127.0.0.1:8080/",
        "http://localhost:8080/path",
        "http://LOCALHOST:8080",
        "http://localhost:8080?query=x",
        "http://localhost:8080/#x",
        "http://user@localhost:8080",
        "https://localhost:8080",
        "http://localhost.:8080",
        "http://[::1]:8080",
    ],
)
def test_development_origin_is_only_canonical_loopback(setup, origin):
    store, _, _, _ = setup
    with pytest.raises(ValueError):
        AuthService(store, public_origin=origin, development=True, mailer=lambda *args: None)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_local_provisioning_three_accounts_flags_and_all_route_host_guard(tmp_path, host):
    store = SubmissionStore(tmp_path / "local.db")
    origin = f"http://{host}:8080"
    service = AuthService(
        store,
        public_origin=origin,
        development=True,
        mailer=lambda *args: None,
    )
    passwords = {name: secrets.token_urlsafe(24) for name in ("alice", "bob", "admin")}
    for name, password in passwords.items():
        role = "admin" if name == "admin" else "user"
        service.provision_development_account(f"{name}@example.test", password, name, role)
        initial = store.auth_account(f"{name}@example.test")
        service.provision_development_account(f"{name}@example.test", password, name, role)
        assert store.auth_account(f"{name}@example.test") == initial
        for changed in [
            (PASSWORD, name, role),
            (password, "other", role),
            (password, name, "user" if role == "admin" else "admin"),
        ]:
            with pytest.raises(ValueError):
                service.provision_development_account(f"{name}@example.test", *changed)
        assert store.auth_account(f"{name}@example.test") == initial
    with pytest.raises(ValueError):
        service.provision_development_account("a@example.com", PASSWORD, "normal")
    app = FastAPI()

    @app.get("/")
    @app.get("/assets/test.js")
    @app.get("/health/live")
    def public():
        return {"status": "ok"}

    install_auth_routes(app, service)
    with TestClient(app, base_url=origin, headers={"Origin": origin, "X-LOJ-CSRF": "1"}) as client:
        assert client.get("/v1/auth/config").json() == {
            "enabled": True,
            "mode": "local",
            "mail_delivery": "local",
        }
        response = login(client, password=passwords["alice"])
        cookie = response.headers["set-cookie"]
        assert "loj_local_session=" in cookie and "SameSite=strict" in cookie
        assert "Secure" not in cookie and "HttpOnly" in cookie and "Domain=" not in cookie
        for path in ("/", "/assets/test.js", "/health/live", "/v1/auth/config", "/unknown"):
            for bad_host in ("evil.test", "127.0.0.2:8080", f"{host}:9090"):
                assert (
                    client.get(
                        path, headers={"Host": bad_host, "X-Forwarded-Host": f"{host}:8080"}
                    ).status_code
                    == 403
                )


def test_production_provisioning_and_http_origin_forbidden(setup):
    store, service, _, _ = setup
    with pytest.raises(ValueError):
        service.provision_development_account(EMAIL, PASSWORD, "alice", "admin")
    with pytest.raises(ValueError):
        AuthService(store, public_origin="http://localhost:8080", mailer=lambda *args: None)


def test_verify_race_has_one_winner_and_handle_conflict_rolls_back(setup):
    store, service, client, messages = setup
    client.post("/v1/auth/register", json={"email": EMAIL})
    token = token_from(messages)
    second = AuthService(
        SubmissionStore(store._database_path), public_origin=ORIGIN, mailer=lambda *args: None
    )

    def complete(selected):
        try:
            selected.complete_link(request(), token, PASSWORD, "verify-email", "alice")
            return 200
        except APIError as error:
            return error.status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(complete, [service, second])) == [200, 401]
    client.post("/v1/auth/register", json={"email": "bob@example.test"})
    bob_token = token_from(messages)
    response = client.post(
        "/v1/auth/verify-email",
        json={
            "token": bob_token,
            "password": PASSWORD,
            "public_handle": "alice",
        },
    )
    assert response.status_code == 422
    assert store.auth_account("bob@example.test") is None
    assert (
        client.post(
            "/v1/auth/verify-email",
            json={
                "token": bob_token,
                "password": PASSWORD,
                "public_handle": "bob",
            },
        ).status_code
        == 200
    )


def test_reset_fences_login_that_already_verified_old_password(setup, monkeypatch):
    store, service, client, messages = setup
    register(client, messages)
    client.post("/v1/auth/password-reset/request", json={"email": EMAIL})
    reset = token_from(messages)
    entered = threading.Event()
    release = threading.Event()
    original = store.auth_create_session

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "auth_create_session", delayed)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(service.login, request(), EMAIL, PASSWORD)
        try:
            assert entered.wait(10)
            other = AuthService(
                SubmissionStore(store._database_path),
                public_origin=ORIGIN,
                mailer=lambda *args: None,
            )
            other.complete_link(request(), reset, NEW_PASSWORD, "reset-password")
        finally:
            release.set()
        with pytest.raises(APIError) as error:
            future.result(timeout=10)
        assert error.value.code == "AUTH_INVALID_CREDENTIALS"


def test_cookie_auth_existing_submission_csrf_and_two_account_isolation(setup):
    from pathlib import Path

    store, service, _, messages = setup
    contract = EvaluationContract.from_path(
        Path(__file__).parents[1] / "config/mvp_evaluation_v2.json"
    )
    queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
    app = create_app(
        store=store,
        contract=contract,
        dispatcher=OutboxDispatcher(store, queue, contract),
        authenticate=service.authenticate,
        environment="test",
        allow_draft_submissions=True,
    )
    install_auth_routes(app, service)
    with TestClient(app, base_url=ORIGIN, headers=HEADERS) as client:
        register(client, messages)
        login(client)
        alice = client.cookies.get("__Host-loj_session")
        client.headers.clear()
        payload = {"challenge_id": contract.challenge_id, "student_prompt": "Return JSON."}
        assert client.post("/v1/submissions", json=payload).status_code == 403
        client.headers.update(HEADERS)
        response = client.post(
            "/v1/submissions",
            json=payload,
            headers={
                "Idempotency-Key": "alice-key",
                "X-LOJ-Expected-User": store.auth_account(EMAIL).user_id,
            },
        )
        assert response.status_code == 202
        submission_id = response.json()["submission_id"]
        register(client, messages, email="bob@example.test", handle="bob")
        login(client, email="bob@example.test")
        assert client.get(f"/v1/submissions/{submission_id}").status_code == 404
        assert client.get(f"/v1/submissions/{submission_id}/result").status_code == 404
        assert client.get("/v1/submissions").json()["items"] == []
        client.cookies.clear()
        response = client.get("/v1/users/me", headers={"Authorization": f"Bearer {alice}"})
        assert response.status_code == 401
        assert store.count_submissions() == 1


def test_coarse_peer_throttle_cannot_be_bypassed_with_email_rotation_or_forwarded_headers(setup):
    store, service, _, messages = setup
    for index in range(30):
        service.request_link(
            request(peer=f"192.0.2.{index + 1}"), f"person{index}@example.test", "verify-email"
        )
    restarted = AuthService(
        SubmissionStore(store._database_path),
        public_origin=ORIGIN,
        mailer=lambda *args: pytest.fail("rate-limited mail must not be delivered"),
    )
    with pytest.raises(APIError) as error:
        restarted.request_link(
            request(peer="192.0.2.200", headers=[(b"x-forwarded-for", b"198.51.100.1")]),
            "newperson@example.test",
            "verify-email",
        )
    assert error.value.code == "AUTH_RATE_LIMITED"
    messages.wait()
    assert len(messages) == 30


def test_expired_action_and_session_at_exact_database_time_are_invalid(setup):
    store, service, client, messages = setup
    register(client, messages)
    login(client)
    raw = client.cookies.get(service.cookie_name)
    client.post("/v1/auth/password-reset/request", json={"email": EMAIL})
    reset = token_from(messages)
    with sqlite3.connect(store._database_path) as connection:
        connection.execute(
            "UPDATE auth_sessions SET expires_at = CAST(strftime('%s','now') AS INTEGER)"
        )
        connection.execute(
            "UPDATE auth_action_tokens SET expires_at = CAST(strftime('%s','now') AS INTEGER)"
        )
    with pytest.raises(AuthenticationError):
        service.authenticate(request(token=raw))
    response = client.post(
        "/v1/auth/password-reset/confirm",
        json={
            "token": reset,
            "password": NEW_PASSWORD,
        },
    )
    assert response.status_code == 401
    login(client)


def test_two_simultaneous_resets_only_one_can_change_the_password(setup):
    store, service, client, messages = setup
    register(client, messages)
    client.post("/v1/auth/password-reset/request", json={"email": EMAIL})
    token = token_from(messages)
    other = AuthService(
        SubmissionStore(store._database_path), public_origin=ORIGIN, mailer=lambda *args: None
    )

    def reset(selected):
        try:
            selected.complete_link(request(), token, NEW_PASSWORD, "reset-password")
            return 200
        except APIError as error:
            return error.status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(reset, [service, other])) == [200, 401]
    login(client, password=NEW_PASSWORD)


def test_health_check_detects_missing_auth_schema_without_repairing_it(setup):
    store, service, _, _ = setup
    with sqlite3.connect(store._database_path) as connection:
        connection.execute("ALTER TABLE auth_sessions RENAME TO broken_sessions")
    with pytest.raises(sqlite3.OperationalError):
        service.health_check()
    with sqlite3.connect(store._database_path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'auth_sessions'"
            ).fetchone()
            is None
        )


def test_existing_private_route_unhandled_failure_is_generic_and_no_store(setup):
    _, service, client, messages = setup
    register(client, messages)
    login(client)
    app = FastAPI()

    @app.get("/v1/submissions")
    def broken():
        raise RuntimeError("private password token")

    install_auth_routes(app, service)
    with TestClient(app, base_url=ORIGIN) as private_client:
        response = private_client.get(
            "/v1/submissions",
            headers={
                "Cookie": f"{service.cookie_name}={client.cookies.get(service.cookie_name)}",
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_NOT_READY"
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text


def test_auth_logs_do_not_include_passwords_tokens_or_emails(setup, caplog):
    import logging
    from pathlib import Path

    store, service, _, messages = setup
    contract = EvaluationContract.from_path(
        Path(__file__).parents[1] / "config/mvp_evaluation_v2.json"
    )
    app = create_app(
        store=store,
        contract=contract,
        authenticate=service.authenticate,
        environment="test",
        dispatcher=OutboxDispatcher(
            store, InMemoryJobQueue(contract.contract_snapshot_sha256), contract
        ),
    )
    install_auth_routes(app, service)
    with caplog.at_level(logging.INFO), TestClient(app, base_url=ORIGIN, headers=HEADERS) as client:
        token = register(client, messages)
        login(client)
        raw = client.cookies.get(service.cookie_name)
        client.post("/v1/auth/password-reset/request", json={"email": EMAIL})
        reset = token_from(messages)
        client.post(
            "/v1/auth/password-reset/confirm", json={"token": reset, "password": NEW_PASSWORD}
        )
    for secret in (EMAIL, PASSWORD, NEW_PASSWORD, token, raw, reset):
        assert secret not in caplog.text


@pytest.mark.parametrize("length", ["-1", "invalid", "100000000000000000", "8193"])
def test_auth_rejects_invalid_or_oversized_content_length_before_downstream(setup, length):
    _, _, client, messages = setup
    response = client.post(
        "/v1/auth/register", json={"email": EMAIL}, headers={"Content-Length": length}
    )
    assert response.status_code in {413, 422}
    assert response.json()["error"]["code"] == "AUTH_VALIDATION_ERROR"
    assert messages == []


@pytest.mark.parametrize("expected", ["stale-user", "", " ", "duplicate", "combined"])
def test_expected_user_is_a_single_exact_cookie_account_snapshot(setup, expected):
    store, service, client, messages = setup
    register(client, messages)
    login(client)
    account = store.auth_account(EMAIL)
    values = [(b"x-loj-expected-user", expected.encode())]
    if expected == "duplicate":
        values = [(b"x-loj-expected-user", account.user_id.encode())] * 2
    if expected == "combined":
        values = [(b"x-loj-expected-user", f"{account.user_id},{account.user_id}".encode())]
    with pytest.raises(APIError) as error:
        service.authenticate(request(token=client.cookies.get(service.cookie_name), headers=values))
    assert error.value.status_code == 409
    assert error.value.code == "AUTH_ACCOUNT_CHANGED"
    assert error.value.details == {}
    with pytest.raises(AuthenticationError):
        service.authenticate(request(headers=[(b"x-loj-expected-user", account.user_id.encode())]))


def test_stale_alice_tab_with_bob_cookie_cannot_write_or_read_as_bob(setup):
    from pathlib import Path

    store, service, _, messages = setup
    contract = EvaluationContract.from_path(
        Path(__file__).parents[1] / "config/mvp_evaluation_v2.json"
    )
    queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
    app = create_app(
        store=store,
        contract=contract,
        dispatcher=OutboxDispatcher(store, queue, contract),
        authenticate=service.authenticate,
        environment="test",
        allow_draft_submissions=True,
    )
    install_auth_routes(app, service)
    with TestClient(app, base_url=ORIGIN, headers=HEADERS) as client:
        register(client, messages)
        alice_id = login(client).json()["user"]["user_id"]
        register(client, messages, email="bob@example.test", handle="bob")
        bob_id = login(client, email="bob@example.test").json()["user"]["user_id"]
        payload = {"challenge_id": contract.challenge_id, "student_prompt": "Alice's tab"}
        for headers in ({}, {"X-LOJ-Expected-User": alice_id}):
            response = client.post(
                "/v1/submissions",
                json=payload,
                headers={
                    "Idempotency-Key": "stale-tab",
                    **headers,
                },
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "AUTH_ACCOUNT_CHANGED"
            assert response.headers["cache-control"] == "no-store"
        assert store.count_submissions() == store.count_outbox_records() == 0
        for path in (
            "/v1/submissions",
            "/v1/submissions/unknown",
            "/v1/submissions/unknown/result",
        ):
            response = client.get(path, headers={"X-LOJ-Expected-User": alice_id})
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "AUTH_ACCOUNT_CHANGED"
        assert client.get("/v1/submissions").status_code == 200
        assert client.get("/v1/users/me").json()["user_id"] == bob_id
        assert client.get("/v1/auth/session").json()["user"]["user_id"] == bob_id
        assert client.get("/v1/auth/config").status_code == 200
        response = client.post(
            "/v1/submissions",
            json=payload,
            headers={
                "Idempotency-Key": "current-tab",
                "X-LOJ-Expected-User": bob_id,
            },
        )
        assert response.status_code == 202
        assert store.count_submissions() == store.count_outbox_records() == 1
        client.cookies.clear()
        response = client.post(
            "/v1/submissions",
            json=payload,
            headers={
                "Idempotency-Key": "spoof-tab",
                "X-LOJ-Expected-User": bob_id,
            },
        )
        assert response.status_code == 401
        assert store.count_submissions() == store.count_outbox_records() == 1


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-forwarded-for", b"192.0.2.1")],
        [(b"x-loj-client-ip", b"")],
        [(b"x-loj-client-ip", b"192.0.2.1,198.51.100.1")],
        [(b"x-loj-client-ip", b"192.0.2.1")] * 2,
        [(b"x-loj-client-ip", b"192.0.2.1:1234")],
        [(b"x-loj-client-ip", b"example.test")],
        [(b"x-loj-client-ip", b"fe80::1%eth0")],
        [(b"x-loj-client-ip", b" 192.0.2.1")],
    ],
)
def test_trusted_proxy_requires_one_valid_actual_client_ip(setup, headers):
    store, _, _, _ = setup
    service = AuthService(
        store, public_origin=ORIGIN, mailer=lambda *args: None, trusted_proxies=["127.0.0.1"]
    )
    with pytest.raises(APIError) as error:
        service.client_address(request(headers=headers))
    assert error.value.status_code == 403
    assert error.value.code == "AUTH_PROXY_REJECTED"


def test_only_exact_trusted_tcp_peer_can_supply_client_ip_and_xff_is_ignored(setup):
    store, direct, _, _ = setup
    service = AuthService(
        store, public_origin=ORIGIN, mailer=lambda *args: None, trusted_proxies=["127.0.0.1", "::1"]
    )
    headers = [(b"x-loj-client-ip", b"192.0.2.9"), (b"x-forwarded-for", b"198.51.100.1")]
    assert str(service.client_address(request(peer="127.0.0.1", headers=headers))) == "192.0.2.9"
    assert str(service.client_address(request(peer="::1", headers=headers))) == "192.0.2.9"
    assert str(service.client_address(request(peer="127.0.0.2", headers=headers))) == "127.0.0.2"
    assert str(direct.client_address(request(peer="127.0.0.1", headers=headers))) == "127.0.0.1"
    assert (
        str(
            service.client_address(
                request(
                    peer="::ffff:127.0.0.1",
                    headers=[
                        (b"x-loj-client-ip", b"::ffff:192.0.2.9"),
                    ],
                )
            )
        )
        == "192.0.2.9"
    )


def test_reverse_proxy_clients_do_not_share_the_proxy_login_or_mail_bucket(setup):
    store, _, _, _ = setup
    service = AuthService(
        store, public_origin=ORIGIN, mailer=lambda *args: None, trusted_proxies=["127.0.0.1"]
    )
    for index in range(101):
        selected = request(headers=[(b"x-loj-client-ip", f"198.51.{index}.1".encode())])
        service._throttle(selected, "login", f"client{index}@example.test")
        service._throttle(selected, "mail:verify-email", f"client{index}@example.test", mail=True)
    same_subnet = request(headers=[(b"x-loj-client-ip", b"198.51.0.200")])
    for index in range(29):
        service._throttle(same_subnet, "mail:verify-email", f"other{index}@example.test", mail=True)
    with pytest.raises(APIError) as error:
        service._throttle(same_subnet, "mail:verify-email", "blocked@example.test", mail=True)
    assert error.value.code == "AUTH_RATE_LIMITED"


def test_trusted_proxy_header_requirement_is_enforced_by_middleware(setup):
    store, _, _, _ = setup
    service = AuthService(
        store, public_origin=ORIGIN, mailer=lambda *args: None, trusted_proxies=["127.0.0.1"]
    )
    app = FastAPI()
    install_auth_routes(app, service)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 1234)) as client:
        assert client.get("/v1/auth/config").status_code == 403
        assert client.get("/unrelated-route").status_code == 403
        response = client.get("/v1/auth/config", headers={"X-LOJ-Client-IP": "192.0.2.1"})
        assert response.status_code == 200


def test_local_development_does_not_require_legacy_account_enrollment(setup):
    store, _, _, _ = setup
    user = store.register_user(auth_subject="old-local-user", public_handle="old-local-handle")
    service = AuthService(
        store, public_origin="http://localhost:8080", development=True, mailer=lambda *args: None
    )
    service.start()
    service.health_check()
    service.close()
    assert store.user_by_subject(user.auth_subject) == user
