"""Opt-in tests use only a fresh, isolated schema in the explicitly configured test database."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.requests import Request

from linguistic_oj.api import APIError, AuthenticationError
from linguistic_oj.auth import AuthService
from linguistic_oj.postgres_migrations import (
    _SCHEMA_V1,
    _SCHEMA_V2,
    POSTGRES_SESSION_OPTIONS,
    migrate_postgres,
)
from linguistic_oj.postgres_submission_store import PostgresSubmissionStore

DATABASE_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set POSTGRES_TEST_DATABASE_URL to run isolated PostgreSQL auth tests",
)
ORIGIN = "https://judge.example.test"
PASSWORD = "long postgres test password"


def _request(token=None):
    headers = [] if token is None else [(b"cookie", f"__Host-loj_session={token}".encode())]
    return Request(
        {
            "type": "http",
            "scheme": "https",
            "method": "POST",
            "path": "/v1/auth/login",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "query_string": b"",
        }
    )


@pytest.fixture
def v2_store(monkeypatch):
    import psycopg
    from psycopg import sql

    original_connect = psycopg.connect
    schema = "auth_test_" + uuid.uuid4().hex
    with original_connect(DATABASE_URL) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def isolated_connect(url, **kwargs):
        kwargs["options"] = (
            kwargs.get("options", POSTGRES_SESSION_OPTIONS) + f" -c search_path={schema}"
        )
        return original_connect(url, **kwargs)

    monkeypatch.setattr(psycopg, "connect", isolated_connect)
    try:
        store = PostgresSubmissionStore(DATABASE_URL)
        with store._connect() as connection:
            connection.execute(_SCHEMA_V1)
            connection.execute(_SCHEMA_V2)
            connection.execute("INSERT INTO schema_migrations VALUES (1, 'old'), (2, 'old')")
        yield store
    finally:
        # This schema was created above, never a shared application schema.
        with original_connect(DATABASE_URL) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_pg_v3_append_is_explicit_repeatable_and_preserves_existing_ids(v2_store):
    store = v2_store
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO users VALUES ('old-user', 'old-subject', 'old-handle', 'old')"
        )
    with pytest.raises(RuntimeError, match="schema versions"):
        store.auth_health_check()
    with store._connect() as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1,),
            (2,),
        ]
        assert connection.execute("SELECT to_regclass('auth_credentials')").fetchone()[0] is None
    migrate_postgres(DATABASE_URL, applied_at="new")
    migrate_postgres(DATABASE_URL, applied_at="new-again")
    store.auth_health_check()
    user = store.user_by_subject("old-subject")
    assert user.user_id == "old-user" and user.role == "user" and user.public_handle == "old-handle"
    service = AuthService(store, public_origin=ORIGIN, mailer=lambda *args: None)
    with pytest.raises(RuntimeError, match="trusted account-enrollment"):
        service.start()
    assert service._mail_queue._worker is None
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_credentials").fetchone()[0] == 0
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]


@pytest.fixture
def pg_setup(v2_store):
    migrate_postgres(DATABASE_URL, applied_at="new")
    messages = []
    service = AuthService(
        v2_store, public_origin=ORIGIN, mailer=lambda *args: messages.append(args)
    )
    service.start()
    try:
        yield v2_store, service, messages
    finally:
        service.close()


def _register(service, messages, email="alice@example.test", handle="alice"):
    service.request_link(_request(), email, "verify-email")
    assert service._mail_queue.wait_for_idle(5)
    token = messages[-1][2].partition("token=")[2]
    service.complete_link(_request(), token, PASSWORD, "verify-email", handle)
    return token


def test_pg_email_lifecycle_restart_expiry_reset_and_role(pg_setup):
    store, service, messages = pg_setup
    token = _register(service, messages)
    with pytest.raises(APIError) as replay:
        service.complete_link(_request(), token, PASSWORD, "verify-email", "alice")
    assert replay.value.code == "AUTH_INVALID_TOKEN"
    body, first = service.login(_request(), "alice@example.test", PASSWORD)
    _, second = service.login(_request(), "alice@example.test", PASSWORD)
    store.set_account_role(body["user"]["user_id"], "admin")
    restarted = AuthService(
        PostgresSubmissionStore(DATABASE_URL), public_origin=ORIGIN, mailer=lambda *args: None
    )
    assert restarted.session(_request(first))[0].role == "admin"
    service.request_link(_request(), "alice@example.test", "reset-password")
    assert service._mail_queue.wait_for_idle(5)
    reset = messages[-1][2].partition("token=")[2]
    service.complete_link(_request(), reset, PASSWORD + " changed", "reset-password")
    for raw in (first, second):
        with pytest.raises(AuthenticationError):
            restarted.authenticate(_request(raw))
    updated, current = service.login(_request(), "alice@example.test", PASSWORD + " changed")
    assert updated["user"]["user_id"] == body["user"]["user_id"]
    assert updated["user"]["role"] == "admin"
    with store._connect() as connection:
        connection.execute("UPDATE auth_sessions SET expires_at = 0")
    with pytest.raises(AuthenticationError):
        service.authenticate(_request(current))


def test_pg_action_replay_race_and_durable_throttling(pg_setup):
    store, service, messages = pg_setup
    service.request_link(_request(), "alice@example.test", "verify-email")
    assert service._mail_queue.wait_for_idle(5)
    token = messages[-1][2].partition("token=")[2]
    other = AuthService(
        PostgresSubmissionStore(DATABASE_URL), public_origin=ORIGIN, mailer=lambda *args: None
    )

    def verify(selected):
        try:
            selected.complete_link(_request(), token, PASSWORD, "verify-email", "alice")
            return 200
        except APIError as error:
            return error.status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(verify, [service, other])) == [200, 401]
    for _ in range(10):
        with pytest.raises(APIError) as error:
            service.login(_request(), "alice@example.test", PASSWORD + " wrong")
        assert error.value.code == "AUTH_INVALID_CREDENTIALS"
    with pytest.raises(APIError) as limited:
        other.login(_request(), "alice@example.test", PASSWORD)
    assert limited.value.code == "AUTH_RATE_LIMITED"
    assert store.auth_account("alice@example.test").public_handle == "alice"


def test_pg_reset_fences_in_flight_login_across_store_instances(pg_setup, monkeypatch):
    store, service, messages = pg_setup
    _register(service, messages)
    service.request_link(_request(), "alice@example.test", "reset-password")
    assert service._mail_queue.wait_for_idle(5)
    token = messages[-1][2].partition("token=")[2]
    entered, release = threading.Event(), threading.Event()
    original = store.auth_create_session

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "auth_create_session", delayed)
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(service.login, _request(), "alice@example.test", PASSWORD)
        try:
            assert entered.wait(10)
            other = AuthService(
                PostgresSubmissionStore(DATABASE_URL),
                public_origin=ORIGIN,
                mailer=lambda *args: None,
            )
            other.complete_link(_request(), token, PASSWORD + " changed", "reset-password")
        finally:
            release.set()
        with pytest.raises(APIError) as error:
            result.result(timeout=10)
        assert error.value.code == "AUTH_INVALID_CREDENTIALS"
