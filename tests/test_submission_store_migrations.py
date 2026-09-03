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

    user = store.user_by_subject("subject-1")
    assert user is not None and user.role is UserRole.USER
    assert _schema_state(database_path)[0] == (1, 2, 3)


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
            "INSERT INTO schema_migrations(version, applied_at) VALUES (4, 'future')"
        )

    with pytest.raises(RuntimeError, match="unsupported SQLite schema versions"):
        SubmissionStore(database_path)
