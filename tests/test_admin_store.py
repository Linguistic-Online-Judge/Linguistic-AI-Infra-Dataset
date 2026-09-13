"""Both stores share these fences. PostgreSQL is opt-in, loopback-only, and schema-isolated."""

import hashlib
import json
import os
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
from test_admin import CONTENT
from test_submission_integration import _artifacts, _mock_contract

import linguistic_oj.admin_store as admin_module
import linguistic_oj.postgres_submission_store as pg_module
import linguistic_oj.submission_store as sqlite_module
from linguistic_oj.admin_store import (
    AdminActor,
    AdminStoreError,
    ChallengePausedError,
    TeachingContent,
    source_fingerprint,
)
from linguistic_oj.auth_store import AUTH_SCHEMA_V3
from linguistic_oj.postgres_migrations import (
    _SCHEMA_V1,
    _SCHEMA_V2,
    POSTGRES_SESSION_OPTIONS,
    migrate_postgres,
)
from linguistic_oj.postgres_submission_store import PostgresSubmissionStore
from linguistic_oj.submission_store import IdempotencyConflictError, SubmissionStore, UserRecord


@pytest.fixture
def pg_v3(monkeypatch):
    url = os.environ.get("LOJ_ADMIN_POSTGRES_TEST_URL", "")
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.query or parsed.fragment:
        pytest.skip("set LOJ_ADMIN_POSTGRES_TEST_URL to an explicit loopback test database URL")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    connect = psycopg.connect
    schema = "admin_test_" + uuid.uuid4().hex
    with connect(url) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def isolated_connect(database_url, **kwargs):
        assert database_url == url
        kwargs["options"] = (
            kwargs.get("options", POSTGRES_SESSION_OPTIONS) + f" -c search_path={schema}"
        )
        return connect(url, **kwargs)

    monkeypatch.setattr(psycopg, "connect", isolated_connect)
    try:
        store = PostgresSubmissionStore(url)
        with store._connect() as connection:
            connection.execute(_SCHEMA_V1 + _SCHEMA_V2 + AUTH_SCHEMA_V3)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (1, 'old'), (2, 'old'), (3, 'old')"
            )
        yield store, url
    finally:
        with connect(url) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path):
    if request.param == "sqlite":
        return SubmissionStore(tmp_path / "store.db")
    store, url = request.getfixturevalue("pg_v3")
    migrate_postgres(url, applied_at="v4-test")
    return store


@pytest.fixture
def prepared(backend, tmp_path):
    public = _artifacts(tmp_path).public
    contract = _mock_contract(_artifacts(tmp_path))
    backend.auth_provision_account("admin@example.test", "test-hash", "admin", "admin", None)
    account = backend.auth_account("admin@example.test")
    digest = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    backend.auth_create_session(account, digest, 3600)
    actor = AdminActor(digest, account.user_id)
    user = backend.register_user(auth_subject="student", public_handle="student")

    def change(action="save", revision=0, **kwargs):
        return backend.admin_change(
            challenge_id=public.challenge_id,
            fingerprint=source_fingerprint(public),
            actor=actor,
            expected_revision=revision,
            action=action,
            has_contract=True,
            can_reopen=True,
            content=TeachingContent.model_validate(CONTENT) if action == "save" else None,
            **kwargs,
        )

    fingerprint = source_fingerprint(public)

    def submit(key="first", fingerprint=fingerprint):
        return backend.create_submission(
            user=user,
            idempotency_key=key,
            student_prompt="Return JSON",
            contract=contract,
            source_fingerprint=fingerprint,
        )

    return backend, public, actor, change, submit


def test_direct_submission_pause_precedes_quotas_and_preserves_replay(prepared):
    store, public, actor, change, submit = prepared
    first = submit()
    change("admissions", closed=True)
    assert submit().replayed is True
    assert submit().submission == first.submission
    with pytest.raises(ChallengePausedError):
        submit("new")
    with store._auth_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1
        assert tx.execute("SELECT COUNT(*) FROM submission_outbox").fetchone()[0] == 1
        row = tx.execute(
            "SELECT action, operator_user_id FROM challenge_admin_revisions"
        ).fetchone()
        assert tuple(row) == ("admissions", actor.expected_user)
    change("admissions", revision=1, closed=False)
    assert submit("new").replayed is False
    with pytest.raises(ChallengePausedError):
        submit("missing-fingerprint", fingerprint=None)
    assert store.admin_states((public.challenge_id,))[public.challenge_id].revision == 2


def test_concurrent_admin_revisions_have_one_winner(prepared):
    store, public, _, change, _ = prepared

    def save():
        try:
            return change().revision
        except AdminStoreError as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(save) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results.count(1) == results.count("ADMIN_REVISION_CONFLICT") == 1
    with store._auth_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM challenge_admin_revisions").fetchone()[0] == 1
    assert store.admin_states((public.challenge_id,))[public.challenge_id].revision == 1


@pytest.mark.parametrize("revoke", ["role", "session", "expiry"])
def test_admin_transaction_rechecks_revocation_before_writing(prepared, revoke):
    store, public, actor, change, _ = prepared
    entered = threading.Event()

    def save():
        entered.set()
        return change()

    with ThreadPoolExecutor(1) as pool:
        with store._auth_transaction() as tx:
            future = pool.submit(save)
            assert entered.wait(3)
            if revoke == "role":
                tx.execute("UPDATE users SET role = 'user' WHERE id = ?", (actor.expected_user,))
            elif revoke == "session":
                tx.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (actor.token_hash,))
            else:
                tx.execute(
                    "UPDATE auth_sessions SET expires_at = 0 WHERE token_hash = ?",
                    (actor.token_hash,),
                )
        with pytest.raises(AdminStoreError) as rejected:
            future.result(timeout=10)
    assert rejected.value.status_code == (403 if revoke == "role" else 401)
    assert store.admin_states((public.challenge_id,))[public.challenge_id].revision == 0


@pytest.mark.parametrize("action", ["save", "publish", "admissions", "check"])
def test_session_expiring_during_task_lock_wait_cannot_change_state(prepared, monkeypatch, action):
    store, public, actor, change, _ = prepared
    change()
    before = store.admin_states((public.challenge_id,))[public.challenge_id]
    with store._auth_transaction() as tx:
        start = tx.now
        tx.execute(
            "UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
            (start + 1, actor.token_hash),
        )
        audit_before = [
            tuple(row)
            for row in tx.execute(
                "SELECT * FROM challenge_admin_revisions ORDER BY revision"
            ).fetchall()
        ]
    clock = [start]
    sampled_times, authorized_times = [], []
    original_execute = admin_module.AuthTransaction.execute
    original_lock = admin_module.lock_admin_task

    def database_clock(tx, query, params=()):
        if query in {
            "SELECT floor(extract(epoch FROM clock_timestamp()))::bigint",
            "SELECT CAST(strftime('%s', 'now') AS INTEGER)",
        }:
            sampled_times.append(clock[0])
            return original_execute(tx, "SELECT ?", (clock[0],))
        if query.startswith("SELECT u.id, u.role FROM auth_sessions"):
            authorized_times.append(params[-1])
        return original_execute(tx, query, params)

    def lock_after_expiry(tx, challenge_id):
        original_lock(tx, challenge_id)
        assert tx.now == start
        clock[0] = start + 1

    with monkeypatch.context() as patch:
        patch.setattr(admin_module.AuthTransaction, "execute", database_clock)
        patch.setattr(admin_module, "lock_admin_task", lock_after_expiry)
        with pytest.raises(AdminStoreError) as expired:
            change(action, revision=1, **({"closed": True} if action == "admissions" else {}))
    assert expired.value.status_code == 401
    assert expired.value.code == "AUTH_INVALID_TOKEN"
    assert sampled_times == authorized_times == [start, start + 1]
    assert store.admin_states((public.challenge_id,))[public.challenge_id] == before
    with store._auth_transaction() as tx:
        assert [
            tuple(row)
            for row in tx.execute(
                "SELECT * FROM challenge_admin_revisions ORDER BY revision"
            ).fetchall()
        ] == audit_before


def test_close_locks_same_task_before_first_policy_row_exists(prepared, monkeypatch):
    store, public, _, change, submit = prepared
    entered, release, submitting = threading.Event(), threading.Event(), threading.Event()
    original = admin_module.lock_admin_task

    def hold(tx, challenge_id):
        original(tx, challenge_id)
        if threading.current_thread().name.startswith("closing"):
            entered.set()
            assert release.wait(5)

    def new_submission():
        submitting.set()
        return submit()

    monkeypatch.setattr(admin_module, "lock_admin_task", hold)
    with (
        ThreadPoolExecutor(1, thread_name_prefix="closing") as closer,
        ThreadPoolExecutor(1) as writer,
    ):
        closing = closer.submit(change, "admissions", closed=True)
        try:
            assert entered.wait(3)
            submission = writer.submit(new_submission)
            assert submitting.wait(3)
            assert not submission.done()
        finally:
            release.set()
        assert closing.result(timeout=10).admissions_closed is True
        with pytest.raises(ChallengePausedError):
            submission.result(timeout=10)
    with store._auth_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
        assert tx.execute("SELECT COUNT(*) FROM submission_outbox").fetchone()[0] == 0
    assert store.admin_states((public.challenge_id,))[public.challenge_id].revision == 1


def test_admitted_transaction_finishes_before_close_then_replays(prepared, monkeypatch):
    _, _, _, change, submit = prepared
    admitted, release, closing = threading.Event(), threading.Event(), threading.Event()
    original = admin_module.assert_admissions_open

    def hold(*args):
        original(*args)
        if threading.current_thread().name.startswith("admitting"):
            admitted.set()
            assert release.wait(5)

    def close():
        closing.set()
        return change("admissions", closed=True)

    monkeypatch.setattr(sqlite_module, "assert_admissions_open", hold)
    monkeypatch.setattr(pg_module, "assert_admissions_open", hold)
    with (
        ThreadPoolExecutor(1, thread_name_prefix="admitting") as writer,
        ThreadPoolExecutor(1) as closer,
    ):
        submission = writer.submit(submit)
        try:
            assert admitted.wait(3)
            closed = closer.submit(close)
            assert closing.wait(3)
            assert not closed.done()
        finally:
            release.set()
        first = submission.result(timeout=10)
        assert closed.result(timeout=10).admissions_closed is True
    assert submit().submission == first.submission
    with pytest.raises(ChallengePausedError):
        submit("after-close")


def test_stale_binding_cannot_publish_or_reopen_and_snapshots_survive(prepared):
    store, public, actor, change, submit = prepared
    change()
    change("publish", revision=1)
    fingerprint = "different-server-descriptor"
    for action in ("publish", "admissions"):
        with pytest.raises(AdminStoreError) as error:
            store.admin_change(
                challenge_id=public.challenge_id,
                fingerprint=fingerprint,
                actor=actor,
                expected_revision=2,
                action=action,
                has_contract=True,
                can_reopen=True,
                closed=False,
            )
        assert error.value.code == "ADMIN_SOURCE_CHANGED"
    with pytest.raises(ChallengePausedError):
        submit("source-changed", fingerprint=fingerprint)
    state = store.admin_change(
        challenge_id=public.challenge_id,
        fingerprint=fingerprint,
        actor=actor,
        expected_revision=2,
        action="save",
        has_contract=True,
        can_reopen=True,
        content=TeachingContent.model_validate({**CONTENT, "title": "Updated source"}),
    )
    assert state.admissions_closed is True
    assert json.loads(state.published_json) == CONTENT
    assert state.published_content(fingerprint) is None
    assert state.published_revision == 2


def test_check_revalidates_saved_draft_bounds_and_publish_is_atomic(prepared, monkeypatch):
    store, public, _, change, _ = prepared
    change()
    with store._auth_transaction() as tx:
        tx.execute(
            "UPDATE challenge_admin_state SET draft_json = ? WHERE challenge_id = ?",
            (json.dumps({**CONTENT, "title": "x" * 121}), public.challenge_id),
        )
    checked = change("check", revision=1)
    assert checked["can_publish"] is False
    assert checked["issues"][0]["code"] == "ADMIN_DRAFT_INVALID"
    with pytest.raises(AdminStoreError) as invalid:
        change("publish", revision=1)
    assert invalid.value.code == "ADMIN_DRAFT_INVALID"
    change(revision=1)
    original = admin_module.AuthTransaction.execute

    def fail_audit(tx, query, params=()):
        if query.startswith("INSERT INTO challenge_admin_revisions"):
            raise RuntimeError("isolated audit failure")
        return original(tx, query, params)

    with monkeypatch.context() as patch:
        patch.setattr(admin_module.AuthTransaction, "execute", fail_audit)
        with pytest.raises(RuntimeError, match="isolated audit failure"):
            change("publish", revision=2)
    state = store.admin_states((public.challenge_id,))[public.challenge_id]
    assert state.revision == 2
    assert state.published_json is None


def test_pg_v3_to_v4_preserves_old_rows_and_is_repeatable(pg_v3, tmp_path, monkeypatch):
    store, url = pg_v3
    store.auth_provision_account("old@example.test", "old-hash", "old", "admin", None)
    old = store.auth_account("old@example.test")
    store.auth_create_session(old, "old-token-hash", 3600)
    store.auth_issue_action("old@example.test", "reset-password", "old-action", 3600)
    store.auth_throttle((("old-bucket", 10),))
    contract = _mock_contract(_artifacts(tmp_path))
    user = UserRecord(old.user_id, old.subject, old.public_handle)
    with monkeypatch.context() as legacy:
        legacy.setattr(pg_module, "assert_admissions_open", lambda *args: None)
        created = store.create_submission(
            user=user, contract=contract, idempotency_key="old", student_prompt="Old prompt"
        )
    tables = (
        "users",
        "auth_credentials",
        "auth_sessions",
        "auth_action_tokens",
        "auth_rate_limits",
        "submissions",
        "submission_outbox",
        "results",
    )
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO results VALUES (%s, %s, '{}', 1, 1, 1, 0, 'old')",
            (created.submission.submission_id, contract.evaluation_identity_sha256),
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        }
    migrate_postgres(url, applied_at="v4")
    migrate_postgres(url, applied_at="v4-repeat")
    store.auth_health_check()
    with store._connect() as connection:
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]
    assert (
        store.create_submission(
            user=user, contract=contract, idempotency_key="old", student_prompt="Old prompt"
        ).replayed
        is True
    )
    with pytest.raises(IdempotencyConflictError):
        store.create_submission(
            user=user, contract=contract, idempotency_key="old", student_prompt="Different prompt"
        )
