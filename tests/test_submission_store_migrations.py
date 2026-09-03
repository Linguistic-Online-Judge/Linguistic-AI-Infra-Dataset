import sqlite3
from pathlib import Path

import pytest

import linguistic_oj.submission_store as store_module
from linguistic_oj.submission_store import SQLITE_SCHEMA_VERSION, SubmissionStore


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
    assert _schema_state(database_path)[0] == (1, 2)


def test_sqlite_store_rejects_unknown_future_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "submissions.db"
    SubmissionStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (3, 'future')"
        )

    with pytest.raises(RuntimeError, match="unsupported SQLite schema versions"):
        SubmissionStore(database_path)
