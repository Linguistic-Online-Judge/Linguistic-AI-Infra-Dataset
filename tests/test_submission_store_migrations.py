import sqlite3
from pathlib import Path

import pytest

import linguistic_oj.submission_store as store_module
from linguistic_oj.submission_store import (
    SQLITE_SCHEMA_VERSION,
    SubmissionStore,
    UserRole,
)


def _schema_state(database_path: Path) -> tuple[tuple[int, ...], set[str]]:
    with sqlite3.connect(database_path) as connection:
        versions = tuple(
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        )
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    return versions, indexes


def test_fresh_sqlite_store_applies_every_schema_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "submissions.db"

    SubmissionStore(database_path).health_check()

    versions, indexes = _schema_state(database_path)
    assert versions == tuple(range(1, SQLITE_SCHEMA_VERSION + 1))
    assert {
        "idx_submissions_owner_history",
        "idx_submissions_user_challenge_created",
        "idx_submissions_user_status",
        "idx_submissions_running_lease",
        "idx_submission_outbox_unpublished",
        "idx_results_leaderboard_v2",
    } <= indexes


def test_existing_sqlite_v1_store_upgrades_without_losing_users(tmp_path: Path) -> None:
    database_path = tmp_path / "submissions.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(store_module._SCHEMA_V1)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-09-02')"
        )
        connection.execute(
            "INSERT INTO users(id, auth_subject, public_handle, created_at) "
            "VALUES ('user-1', 'subject-1', 'student-1', '2026-09-02')"
        )

    store = SubmissionStore(database_path)

    assert store.user_by_subject("subject-1") is not None
    assert store.user_by_subject("subject-1").user_id == "user-1"
    assert store.user_by_subject("subject-1").role == "user"
    assert _schema_state(database_path)[0] == (1, 2, 3, 4)
    user = store.user_by_subject("subject-1")
    assert user is not None and user.role is UserRole.USER


def test_sqlite_store_persists_user_and_admin_roles(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / "submissions.db")

    user = store.register_user(auth_subject="subject-user", public_handle="user")
    admin = store.register_user(
        auth_subject="subject-admin",
        public_handle="admin",
        role=UserRole.ADMIN,
    )

    assert user.role is UserRole.USER
    assert admin.role is UserRole.ADMIN
    assert store.user_by_subject("subject-user") == user
    assert store.user_by_subject("subject-admin") == admin

    with pytest.raises(TypeError, match="UserRole"):
        store.register_user(
            auth_subject="subject-invalid",
            public_handle="invalid",
            role="admin",  # type: ignore[arg-type]
        )


def test_sqlite_store_rejects_unknown_future_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "submissions.db"
    SubmissionStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'future')",
            (SQLITE_SCHEMA_VERSION + 1,),
        )

    with pytest.raises(RuntimeError, match="unsupported SQLite schema versions"):
        SubmissionStore(database_path)


@pytest.mark.parametrize("version", [2, 3, "role-only-v3"])
def test_append_migration_preserves_existing_data(tmp_path: Path, monkeypatch, version) -> None:
    from linguistic_oj.mvp_contract import EvaluationContract
    from linguistic_oj.submission_store import UserRecord

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(store_module._SCHEMA_V1 + store_module._SCHEMA_V2)
        connection.executemany(
            "INSERT INTO schema_migrations VALUES (?, 'legacy')", [(1,), (2,)]
        )
        connection.execute(
            "INSERT INTO users VALUES ('old-id', 'old-subject', 'old-handle', 'old')"
        )
        if version == 3:
            connection.executescript(store_module.AUTH_SCHEMA_V3)
            connection.execute("INSERT INTO schema_migrations VALUES (3, 'legacy')")
            connection.execute("UPDATE users SET role = 'admin'")
            connection.execute(
                "INSERT INTO auth_credentials VALUES ('old-id', 'old@example.test', 'hash', 2)"
            )
            connection.execute("INSERT INTO auth_sessions VALUES ('session', 'old-id', 9999999999)")
            connection.execute(
                "INSERT INTO auth_action_tokens VALUES "
                "('action', 'reset-password', 'old@example.test', 'old-id', 9999999999)"
            )
            connection.execute("INSERT INTO auth_rate_limits VALUES ('bucket', 3, 9999999999)")
        elif version == "role-only-v3":
            from linguistic_oj.auth_store import ROLE_SCHEMA_V3

            connection.executescript(ROLE_SCHEMA_V3)
            connection.execute("INSERT INTO schema_migrations VALUES (3, 'legacy')")
            connection.execute("UPDATE users SET role = 'admin'")
    old = object.__new__(SubmissionStore)
    old._database_path = path
    contract = EvaluationContract.from_path(
        Path(__file__).parents[1] / "config/mvp_evaluation_v2.json"
    )
    # Model the historical writer, which predates the v4 policy table.
    with monkeypatch.context() as legacy:
        legacy.setattr(store_module, "assert_admissions_open", lambda *args: None)
        created = old.create_submission(
            user=UserRecord("old-id", "old-subject", "old-handle"), idempotency_key="old-key",
            student_prompt="old prompt", contract=contract,
        )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO results VALUES (?, ?, '{}', 1, 1, 1, 0, 'old')",
            (created.submission.submission_id, contract.evaluation_identity_sha256),
        )
        before = {table: connection.execute(f"SELECT * FROM {table}").fetchall()
                  for table in ("submissions", "submission_outbox", "results")}
        if version == 3:
            for table in (
                "users", "auth_credentials", "auth_sessions", "auth_action_tokens",
                "auth_rate_limits",
            ):
                before[table] = connection.execute(f"SELECT * FROM {table}").fetchall()
        elif version == "role-only-v3":
            before["users"] = connection.execute("SELECT * FROM users").fetchall()
    upgraded = SubmissionStore(path)
    upgraded.auth_health_check()
    SubmissionStore(path).auth_health_check()
    assert upgraded.user_by_subject("old-subject") == UserRecord(
        "old-id", "old-subject", "old-handle", "user" if version == 2 else "admin"
    )
    with sqlite3.connect(path) as connection:
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert connection.execute("SELECT COUNT(*) FROM auth_credentials").fetchone()[0] == (
            1 if version == 3 else 0
        )
        assert connection.execute("SELECT COUNT(*) FROM challenge_admin_state").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM challenge_admin_revisions"
        ).fetchone()[0] == 0
    assert _schema_state(path)[0] == (1, 2, 3, 4)


@pytest.mark.parametrize("corruption", ["partial-auth", "missing-role", "missing-auth-v4"])
def test_incomplete_legacy_sqlite_schema_is_not_silently_repaired(tmp_path, corruption):
    from linguistic_oj.auth_store import ROLE_SCHEMA_V3

    path = tmp_path / "broken.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(store_module._SCHEMA_V1 + store_module._SCHEMA_V2)
        if corruption != "missing-role":
            connection.executescript(ROLE_SCHEMA_V3)
        if corruption == "partial-auth":
            connection.execute("CREATE TABLE auth_credentials (user_id TEXT)")
        versions = (1, 2, 3, 4) if corruption == "missing-auth-v4" else (1, 2, 3)
        connection.executemany("INSERT INTO schema_migrations VALUES (?, 'old')",
                               [(version,) for version in versions])
        before = connection.execute("SELECT * FROM sqlite_master").fetchall()
    with pytest.raises(RuntimeError, match="authentication schema"):
        SubmissionStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM sqlite_master").fetchall() == before
    assert _schema_state(path)[0] == versions


def test_legacy_auth_completion_rolls_back_with_failed_v4(tmp_path, monkeypatch):
    from linguistic_oj.auth_store import ROLE_SCHEMA_V3

    path = tmp_path / "rollback.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(store_module._SCHEMA_V1 + store_module._SCHEMA_V2 + ROLE_SCHEMA_V3)
        connection.executemany(
            "INSERT INTO schema_migrations VALUES (?, 'old')", [(1,), (2,), (3,)]
        )
        before = connection.execute("SELECT * FROM sqlite_master").fetchall()
    monkeypatch.setitem(store_module._SQLITE_MIGRATIONS, 4, "CREATE TABLE invalid syntax")
    with pytest.raises(sqlite3.OperationalError):
        SubmissionStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM sqlite_master").fetchall() == before
    assert _schema_state(path)[0] == (1, 2, 3)
