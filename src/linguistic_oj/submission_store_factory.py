"""Explicit persistence selection for API and Worker entry points."""

from __future__ import annotations

from pathlib import Path

from .postgres_submission_store import PostgresSubmissionStore
from .submission_store import SubmissionStore, SubmissionStoreProtocol


def build_submission_store(
    *,
    database_path: Path | None,
    postgres_database_url: str | None,
) -> SubmissionStoreProtocol:
    if (database_path is None) == (postgres_database_url is None):
        raise ValueError("configure exactly one of SQLite database path or PostgreSQL database URL")
    if database_path is not None:
        return SubmissionStore(database_path)
    assert postgres_database_url is not None
    return PostgresSubmissionStore(postgres_database_url)
