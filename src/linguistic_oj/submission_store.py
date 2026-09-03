"""SQLite persistence for asynchronous evaluation submissions."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .mvp_contract import EvaluationContract, canonical_json

SQLITE_LOCK_TIMEOUT_SECONDS = 5.0
SQLITE_SCHEMA_VERSION = 2


class SubmissionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REJECTED = "rejected"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IdempotencyConflictError(ValueError):
    pass


class SubmissionQuotaError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        limit: int,
        current: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.limit = limit
        self.current = current
        self.retry_after_seconds = retry_after_seconds


class GlobalQueueFullError(ValueError):
    def __init__(self, *, limit: int, current: int) -> None:
        super().__init__("GLOBAL_QUEUE_FULL")
        self.limit = limit
        self.current = current


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: str
    auth_subject: str
    public_handle: str


@dataclass(frozen=True, slots=True)
class SubmissionRecord:
    submission_id: str
    user_id: str
    challenge_id: str
    evaluation_identity_sha256: str
    status: SubmissionStatus
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class CreatedSubmission:
    submission: SubmissionRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class ClaimedSubmission:
    submission_id: str
    user_id: str
    student_prompt: str
    student_prompt_sha256: str
    contract_snapshot_json: str
    contract_snapshot_sha256: str
    evaluation_identity_sha256: str
    attempt_number: int
    lease_token: str
    deadline_at: str


@dataclass(frozen=True, slots=True)
class ClaimAttempt:
    claim: ClaimedSubmission | None
    retry_later: bool


@dataclass(frozen=True, slots=True)
class OwnerResultRecord:
    status: SubmissionStatus
    result: dict[str, Any] | None
    failure: dict[str, Any] | None
    contract_snapshot_json: str


@dataclass(frozen=True, slots=True)
class OwnerSubmissionRecord:
    submission_id: str
    challenge_id: str
    evaluation_identity_sha256: str
    status: SubmissionStatus
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    evaluation_identity_sha256: str
    public_handle: str
    rank: int
    samples_invalid: int
    samples_total: int
    samples_valid: int
    score: float
    succeeded_at: str
    contract_snapshot_json: str

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluation_identity_sha256": self.evaluation_identity_sha256,
            "public_handle": self.public_handle,
            "rank": self.rank,
            "samples_invalid": self.samples_invalid,
            "samples_total": self.samples_total,
            "samples_valid": self.samples_valid,
            "score": self.score,
            "succeeded_at": self.succeeded_at,
        }


class SubmissionStoreProtocol(Protocol):
    """Persistence operations shared by SQLite and PostgreSQL stores."""

    def health_check(self) -> None: ...

    def register_user(self, *, auth_subject: str, public_handle: str) -> UserRecord: ...

    def user_by_subject(self, auth_subject: str) -> UserRecord | None: ...

    def create_submission(
        self,
        *,
        user: UserRecord,
        idempotency_key: str,
        student_prompt: str,
        contract: EvaluationContract,
    ) -> CreatedSubmission: ...

    def unpublished_submission_ids(
        self, evaluation_identity_sha256: str, contract_snapshot_sha256: str
    ) -> tuple[str, ...]: ...

    def mark_outbox_published(self, submission_id: str) -> None: ...

    def published_queued_submission_ids(
        self, evaluation_identity_sha256: str, contract_snapshot_sha256: str
    ) -> tuple[str, ...]: ...

    def claim_submission(
        self,
        submission_id: str,
        *,
        evaluation_identity_sha256: str,
        contract_snapshot_sha256: str,
        lease_seconds: int,
        max_attempts: int,
        max_running_per_user: int,
    ) -> ClaimAttempt: ...

    def claim_deadline_expired(self, claim: ClaimedSubmission) -> bool: ...

    def expire_leases(self) -> int: ...

    def expire_queued_deadlines(self) -> int: ...

    def complete_success(
        self, claim: ClaimedSubmission, *, owner_result: dict[str, Any]
    ) -> bool: ...

    def retry_submission(self, claim: ClaimedSubmission, *, max_attempts: int) -> bool: ...

    def complete_failure(
        self,
        claim: ClaimedSubmission,
        *,
        failure_contract_version: str,
        code: str,
        retryable: bool,
    ) -> bool: ...

    def complete_rejected(self, claim: ClaimedSubmission) -> bool: ...

    def submission_for_owner(self, submission_id: str, user_id: str) -> SubmissionRecord | None: ...

    def submissions_for_owner(
        self,
        user_id: str,
        *,
        limit: int,
        before_created_at: str | None = None,
        before_submission_id: str | None = None,
    ) -> tuple[OwnerSubmissionRecord, ...]: ...

    def owner_result(self, submission_id: str, user_id: str) -> OwnerResultRecord | None: ...

    def leaderboard(
        self,
        evaluation_identity_sha256: str,
        *,
        limit: int = 100,
        as_of: str | None = None,
        after_rank: int = 0,
    ) -> tuple[LeaderboardEntry, ...]: ...


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    auth_subject TEXT NOT NULL UNIQUE,
    public_handle TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    challenge_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    evaluation_identity_sha256 TEXT NOT NULL,
    contract_snapshot_sha256 TEXT NOT NULL,
    contract_snapshot_json TEXT NOT NULL,
    student_prompt_utf8 BLOB NOT NULL,
    student_prompt_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'rejected', 'succeeded', 'failed')
    ),
    attempt_number INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    created_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    started_at TEXT,
    lease_expires_at TEXT,
    completed_at TEXT,
    failure_contract_version TEXT,
    failure_code TEXT,
    failure_retryable INTEGER,
    UNIQUE(user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS submission_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS results (
    submission_id TEXT PRIMARY KEY REFERENCES submissions(id),
    evaluation_identity_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    score REAL NOT NULL,
    samples_total INTEGER NOT NULL,
    samples_valid INTEGER NOT NULL,
    samples_invalid INTEGER NOT NULL,
    succeeded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_results_leaderboard
ON results(evaluation_identity_sha256, score DESC, succeeded_at ASC);
"""

_SCHEMA_V2 = """
CREATE INDEX IF NOT EXISTS idx_submissions_owner_history
ON submissions(user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_submissions_user_challenge_created
ON submissions(user_id, challenge_id, created_at);
CREATE INDEX IF NOT EXISTS idx_submissions_user_status
ON submissions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_submissions_running_lease
ON submissions(evaluation_identity_sha256, lease_expires_at)
WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_submission_outbox_unpublished
ON submission_outbox(id)
WHERE published_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_results_leaderboard_v2
ON results(evaluation_identity_sha256, score DESC, succeeded_at ASC, submission_id ASC);
"""

_SQLITE_MIGRATIONS = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
}
_EXPECTED_SQLITE_SCHEMA_VERSIONS = tuple(range(1, SQLITE_SCHEMA_VERSION + 1))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _request_sha256(
    *,
    challenge_id: str,
    contract_version: str,
    student_prompt_utf8: bytes,
) -> str:
    digest = hashlib.sha256(b"submission-request-v1\0")
    values = (
        challenge_id.encode("utf-8"),
        contract_version.encode("utf-8"),
        student_prompt_utf8,
    )
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _submission_from_row(row: sqlite3.Row) -> SubmissionRecord:
    return SubmissionRecord(
        submission_id=row["id"],
        user_id=row["user_id"],
        challenge_id=row["challenge_id"],
        evaluation_identity_sha256=row["evaluation_identity_sha256"],
        status=SubmissionStatus(row["status"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _owner_submission_from_row(row: sqlite3.Row) -> OwnerSubmissionRecord:
    return OwnerSubmissionRecord(
        submission_id=row["id"],
        challenge_id=row["challenge_id"],
        evaluation_identity_sha256=row["evaluation_identity_sha256"],
        status=SubmissionStatus(row["status"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _validate_migration_prefix(versions: tuple[int, ...], expected: tuple[int, ...]) -> None:
    if versions != expected[: len(versions)]:
        raise RuntimeError(f"unsupported SQLite schema versions: {versions}")


def _execute_sqlite_migration(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


class SubmissionStore:
    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a Path")
        self._database_path = database_path
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self._database_path,
            isolation_level=None,
            timeout=SQLITE_LOCK_TIMEOUT_SECONDS,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            versions: tuple[int, ...] = ()
            if exists is not None:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall()
                )
                _validate_migration_prefix(versions, _EXPECTED_SQLITE_SCHEMA_VERSIONS)
            for version in _EXPECTED_SQLITE_SCHEMA_VERSIONS[len(versions) :]:
                _execute_sqlite_migration(connection, _SQLITE_MIGRATIONS[version])
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _timestamp(_utc_now())),
                )
            migrated = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
            if migrated != _EXPECTED_SQLITE_SCHEMA_VERSIONS:
                connection.rollback()
                raise RuntimeError(f"SQLite schema migration is incomplete: {migrated}")
            connection.commit()

    def health_check(self) -> None:
        with self._connect() as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
            if versions != _EXPECTED_SQLITE_SCHEMA_VERSIONS:
                raise RuntimeError(f"unsupported SQLite schema versions: {versions}")

    def register_user(self, *, auth_subject: str, public_handle: str) -> UserRecord:
        if not auth_subject or not public_handle or "@" in public_handle:
            raise ValueError("user subject and non-email public handle are required")
        user_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO users(id, auth_subject, public_handle, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, auth_subject, public_handle, _timestamp(_utc_now())),
            )
            connection.commit()
        return UserRecord(user_id, auth_subject, public_handle)

    def user_by_subject(self, auth_subject: str) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, auth_subject, public_handle FROM users WHERE auth_subject = ?",
                (auth_subject,),
            ).fetchone()
        if row is None:
            return None
        return UserRecord(row["id"], row["auth_subject"], row["public_handle"])

    def create_submission(
        self,
        *,
        user: UserRecord,
        idempotency_key: str,
        student_prompt: str,
        contract: EvaluationContract,
    ) -> CreatedSubmission:
        prompt_utf8 = student_prompt.encode("utf-8")
        request_sha256 = _request_sha256(
            challenge_id=contract.challenge_id,
            contract_version=contract.contract_version,
            student_prompt_utf8=prompt_utf8,
        )
        now = _utc_now()
        now_text = _timestamp(now)
        deadline_at = _timestamp(now + timedelta(seconds=contract.job_deadline_seconds))
        cutoff = _timestamp(now - timedelta(hours=24))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, user_id, challenge_id, evaluation_identity_sha256, status,
                       created_at, started_at, completed_at, request_sha256
                FROM submissions
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user.user_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    connection.rollback()
                    raise IdempotencyConflictError(
                        "idempotency key was already used for different content"
                    )
                connection.commit()
                return CreatedSubmission(_submission_from_row(existing), replayed=True)

            accepted_quota = connection.execute(
                """
                SELECT COUNT(*) AS accepted_count, MIN(created_at) AS oldest_created_at
                FROM submissions
                WHERE user_id = ? AND challenge_id = ? AND created_at >= ?
                """,
                (user.user_id, contract.challenge_id, cutoff),
            ).fetchone()
            accepted_count = accepted_quota["accepted_count"]
            if accepted_count >= contract.submissions_per_user_per_challenge_per_24h:
                connection.rollback()
                oldest = datetime.fromisoformat(accepted_quota["oldest_created_at"])
                retry_after = max(
                    1,
                    math.ceil((oldest + timedelta(hours=24) - now).total_seconds()),
                )
                raise SubmissionQuotaError(
                    code="SUBMISSION_RATE_LIMIT",
                    limit=contract.submissions_per_user_per_challenge_per_24h,
                    current=accepted_count,
                    retry_after_seconds=retry_after,
                )

            outstanding_count = connection.execute(
                """
                SELECT COUNT(*) FROM submissions
                WHERE user_id = ? AND status IN ('queued', 'running')
                """,
                (user.user_id,),
            ).fetchone()[0]
            if outstanding_count >= contract.max_outstanding_submissions_per_user:
                connection.rollback()
                raise SubmissionQuotaError(
                    code="OUTSTANDING_SUBMISSION_LIMIT",
                    limit=contract.max_outstanding_submissions_per_user,
                    current=outstanding_count,
                )

            global_count = connection.execute(
                "SELECT COUNT(*) FROM submissions WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
            if global_count >= contract.global_queue_depth:
                connection.rollback()
                raise GlobalQueueFullError(
                    limit=contract.global_queue_depth,
                    current=global_count,
                )

            submission_id = uuid.uuid4().hex
            prompt_sha256 = hashlib.sha256(prompt_utf8).hexdigest()
            connection.execute(
                """
                INSERT INTO submissions(
                    id, user_id, idempotency_key, request_sha256, challenge_id,
                    contract_version, evaluation_identity_sha256, contract_snapshot_sha256,
                    contract_snapshot_json, student_prompt_utf8, student_prompt_sha256,
                    status, created_at, deadline_at, failure_contract_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    submission_id,
                    user.user_id,
                    idempotency_key,
                    request_sha256,
                    contract.challenge_id,
                    contract.contract_version,
                    contract.evaluation_identity_sha256,
                    contract.contract_snapshot_sha256,
                    contract.snapshot_json,
                    prompt_utf8,
                    prompt_sha256,
                    now_text,
                    deadline_at,
                    contract.failure_contract_version,
                ),
            )
            connection.execute(
                "INSERT INTO submission_outbox(submission_id, created_at) VALUES (?, ?)",
                (submission_id, now_text),
            )
            connection.commit()

        return CreatedSubmission(
            SubmissionRecord(
                submission_id=submission_id,
                user_id=user.user_id,
                challenge_id=contract.challenge_id,
                evaluation_identity_sha256=contract.evaluation_identity_sha256,
                status=SubmissionStatus.QUEUED,
                created_at=now_text,
                started_at=None,
                completed_at=None,
            ),
            replayed=False,
        )

    def unpublished_submission_ids(
        self,
        evaluation_identity_sha256: str,
        contract_snapshot_sha256: str,
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.submission_id
                FROM submission_outbox AS o
                JOIN submissions AS s ON s.id = o.submission_id
                WHERE o.published_at IS NULL AND s.evaluation_identity_sha256 = ?
                      AND s.contract_snapshot_sha256 = ? AND s.status = 'queued'
                ORDER BY o.id
                """,
                (evaluation_identity_sha256, contract_snapshot_sha256),
            ).fetchall()
        return tuple(row["submission_id"] for row in rows)

    def mark_outbox_published(self, submission_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE submission_outbox SET published_at = ?
                WHERE submission_id = ? AND published_at IS NULL
                """,
                (_timestamp(_utc_now()), submission_id),
            )

    def published_queued_submission_ids(
        self,
        evaluation_identity_sha256: str,
        contract_snapshot_sha256: str,
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.submission_id
                FROM submission_outbox AS o
                JOIN submissions AS s ON s.id = o.submission_id
                WHERE o.published_at IS NOT NULL AND s.status = 'queued'
                      AND s.evaluation_identity_sha256 = ?
                      AND s.contract_snapshot_sha256 = ?
                ORDER BY o.id
                """,
                (evaluation_identity_sha256, contract_snapshot_sha256),
            ).fetchall()
        return tuple(row["submission_id"] for row in rows)

    def claim_submission(
        self,
        submission_id: str,
        *,
        evaluation_identity_sha256: str,
        contract_snapshot_sha256: str,
        lease_seconds: int,
        max_attempts: int,
        max_running_per_user: int,
    ) -> ClaimAttempt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM submissions WHERE id = ?",
                (submission_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return ClaimAttempt(claim=None, retry_later=False)
            if (
                row["evaluation_identity_sha256"] != evaluation_identity_sha256
                or row["contract_snapshot_sha256"] != contract_snapshot_sha256
            ):
                connection.rollback()
                return ClaimAttempt(claim=None, retry_later=False)
            if row["status"] == SubmissionStatus.RUNNING.value:
                connection.rollback()
                return ClaimAttempt(claim=None, retry_later=True)
            if row["status"] != SubmissionStatus.QUEUED.value:
                connection.rollback()
                return ClaimAttempt(claim=None, retry_later=False)
            if row["attempt_number"] >= max_attempts:
                connection.rollback()
                return ClaimAttempt(claim=None, retry_later=False)
            now_text = _timestamp(_utc_now())
            if row["deadline_at"] <= now_text:
                connection.execute(
                    """
                    UPDATE submissions
                    SET status = 'failed', completed_at = ?,
                        failure_code = 'JOB_DEADLINE', failure_retryable = 0
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now_text, submission_id),
                )
                connection.commit()
                return ClaimAttempt(claim=None, retry_later=False)
            running_count = connection.execute(
                "SELECT COUNT(*) FROM submissions WHERE user_id = ? AND status = 'running'",
                (row["user_id"],),
            ).fetchone()[0]
            if running_count >= max_running_per_user:
                connection.rollback()
                return ClaimAttempt(claim=None, retry_later=True)

            attempt_number = row["attempt_number"] + 1
            lease_token = uuid.uuid4().hex
            started_at = now_text
            lease_expires_at = min(
                _timestamp(_utc_now() + timedelta(seconds=lease_seconds)),
                row["deadline_at"],
            )
            updated = connection.execute(
                """
                UPDATE submissions
                SET status = 'running', attempt_number = ?, lease_token = ?,
                    started_at = ?, lease_expires_at = ?
                WHERE id = ? AND status = 'queued' AND attempt_number = ?
                """,
                (
                    attempt_number,
                    lease_token,
                    started_at,
                    lease_expires_at,
                    submission_id,
                    row["attempt_number"],
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return ClaimAttempt(claim=None, retry_later=True)
            connection.commit()

        return ClaimAttempt(
            claim=ClaimedSubmission(
                submission_id=submission_id,
                user_id=row["user_id"],
                student_prompt=bytes(row["student_prompt_utf8"]).decode("utf-8"),
                student_prompt_sha256=row["student_prompt_sha256"],
                contract_snapshot_json=row["contract_snapshot_json"],
                contract_snapshot_sha256=row["contract_snapshot_sha256"],
                evaluation_identity_sha256=row["evaluation_identity_sha256"],
                attempt_number=attempt_number,
                lease_token=lease_token,
                deadline_at=row["deadline_at"],
            ),
            retry_later=False,
        )

    def claim_deadline_expired(self, claim: ClaimedSubmission) -> bool:
        return claim.deadline_at <= _timestamp(_utc_now())

    def expire_leases(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_text = _timestamp(_utc_now())
            updated = connection.execute(
                """
                UPDATE submissions
                SET status = 'failed', completed_at = ?, lease_token = NULL,
                    lease_expires_at = NULL,
                    failure_code = CASE
                        WHEN deadline_at <= ? THEN 'JOB_DEADLINE'
                        ELSE 'WORKER_CRASH'
                    END,
                    failure_retryable = 0
                WHERE status = 'running' AND lease_expires_at <= ?
                """,
                (now_text, now_text, now_text),
            )
            connection.commit()
        return updated.rowcount

    def expire_queued_deadlines(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_text = _timestamp(_utc_now())
            updated = connection.execute(
                """
                UPDATE submissions
                SET status = 'failed', completed_at = ?, failure_code = 'JOB_DEADLINE',
                    failure_retryable = 0
                WHERE status = 'queued' AND deadline_at <= ?
                """,
                (now_text, now_text),
            )
            connection.commit()
        return updated.rowcount

    def complete_success(
        self,
        claim: ClaimedSubmission,
        *,
        owner_result: dict[str, Any],
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            succeeded_at = _timestamp(_utc_now())
            owns_lease = connection.execute(
                """
                SELECT 1 FROM submissions
                WHERE id = ? AND status = 'running' AND attempt_number = ?
                      AND lease_token = ? AND lease_expires_at > ? AND deadline_at > ?
                """,
                (
                    claim.submission_id,
                    claim.attempt_number,
                    claim.lease_token,
                    succeeded_at,
                    succeeded_at,
                ),
            ).fetchone()
            if owns_lease is None:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO results(
                    submission_id, evaluation_identity_sha256, result_json, score,
                    samples_total, samples_valid, samples_invalid, succeeded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.submission_id,
                    claim.evaluation_identity_sha256,
                    canonical_json(owner_result),
                    owner_result["score"],
                    owner_result["samples_total"],
                    owner_result["samples_valid"],
                    owner_result["samples_invalid"],
                    succeeded_at,
                ),
            )
            updated = connection.execute(
                """
                UPDATE submissions
                SET status = 'succeeded', completed_at = ?, lease_token = NULL,
                    lease_expires_at = NULL
                WHERE id = ? AND status = 'running' AND attempt_number = ? AND lease_token = ?
                """,
                (
                    succeeded_at,
                    claim.submission_id,
                    claim.attempt_number,
                    claim.lease_token,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
        return True

    def retry_submission(
        self,
        claim: ClaimedSubmission,
        *,
        max_attempts: int,
    ) -> bool:
        """Return a live claimed submission to queued without extending its deadline."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_text = _timestamp(_utc_now())
            row = connection.execute(
                """
                SELECT attempt_number, deadline_at, lease_expires_at
                FROM submissions
                WHERE id = ? AND status = 'running' AND attempt_number = ? AND lease_token = ?
                """,
                (claim.submission_id, claim.attempt_number, claim.lease_token),
            ).fetchone()
            if (
                row is None
                or row["attempt_number"] >= max_attempts
                or row["deadline_at"] <= now_text
                or row["lease_expires_at"] <= now_text
            ):
                connection.rollback()
                return False
            updated = connection.execute(
                """
                UPDATE submissions
                SET status = 'queued', lease_token = NULL, lease_expires_at = NULL,
                    started_at = NULL
                WHERE id = ? AND status = 'running' AND attempt_number = ? AND lease_token = ?
                """,
                (claim.submission_id, claim.attempt_number, claim.lease_token),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
        return True

    def complete_failure(
        self,
        claim: ClaimedSubmission,
        *,
        failure_contract_version: str,
        code: str,
        retryable: bool,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            completed_at = _timestamp(_utc_now())
            lease = connection.execute(
                """
                SELECT deadline_at, lease_expires_at, failure_contract_version
                FROM submissions
                WHERE id = ? AND status = 'running' AND attempt_number = ? AND lease_token = ?
                """,
                (claim.submission_id, claim.attempt_number, claim.lease_token),
            ).fetchone()
            if lease is None or lease["failure_contract_version"] != failure_contract_version:
                connection.rollback()
                return False
            if (
                lease["lease_expires_at"] <= completed_at
                and lease["deadline_at"] > completed_at
            ):
                connection.rollback()
                return False
            if lease["deadline_at"] <= completed_at:
                code = "JOB_DEADLINE"
                retryable = False
            updated = connection.execute(
                """
                UPDATE submissions
                SET status = 'failed', completed_at = ?, lease_token = NULL,
                    lease_expires_at = NULL,
                    failure_contract_version = ?, failure_code = ?, failure_retryable = ?
                WHERE id = ? AND status = 'running' AND attempt_number = ? AND lease_token = ?
                """,
                (
                    completed_at,
                    failure_contract_version,
                    code,
                    int(retryable),
                    claim.submission_id,
                    claim.attempt_number,
                    claim.lease_token,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
        return True

    def complete_rejected(self, claim: ClaimedSubmission) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            completed_at = _timestamp(_utc_now())
            lease = connection.execute(
                """
                SELECT deadline_at, lease_expires_at FROM submissions
                WHERE id = ? AND status = 'running' AND attempt_number = ? AND lease_token = ?
                """,
                (claim.submission_id, claim.attempt_number, claim.lease_token),
            ).fetchone()
            if lease is None or (
                lease["lease_expires_at"] <= completed_at
                and lease["deadline_at"] > completed_at
            ):
                connection.rollback()
                return False
            if lease["deadline_at"] <= completed_at:
                updated = connection.execute(
                    """
                    UPDATE submissions
                    SET status = 'failed', completed_at = ?, lease_token = NULL,
                        lease_expires_at = NULL, failure_code = 'JOB_DEADLINE',
                        failure_retryable = 0
                    WHERE id = ? AND status = 'running' AND attempt_number = ?
                          AND lease_token = ?
                    """,
                    (
                        completed_at,
                        claim.submission_id,
                        claim.attempt_number,
                        claim.lease_token,
                    ),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            updated = connection.execute(
                """
                UPDATE submissions
                SET status = 'rejected', completed_at = ?, lease_token = NULL,
                    lease_expires_at = NULL, failure_code = 'TOKEN_LIMIT_EXCEEDED',
                    failure_retryable = 0
                WHERE id = ? AND status = 'running' AND attempt_number = ? AND lease_token = ?
                """,
                (
                    completed_at,
                    claim.submission_id,
                    claim.attempt_number,
                    claim.lease_token,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
        return True

    def submission_for_owner(self, submission_id: str, user_id: str) -> SubmissionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, challenge_id, evaluation_identity_sha256, status,
                       created_at, started_at, completed_at
                FROM submissions WHERE id = ? AND user_id = ?
                """,
                (submission_id, user_id),
            ).fetchone()
        return None if row is None else _submission_from_row(row)

    def submissions_for_owner(
        self,
        user_id: str,
        *,
        limit: int,
        before_created_at: str | None = None,
        before_submission_id: str | None = None,
    ) -> tuple[OwnerSubmissionRecord, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if (before_created_at is None) != (before_submission_id is None):
            raise ValueError("history cursor fields must be provided together")
        with self._connect() as connection:
            if before_created_at is None:
                rows = connection.execute(
                    "SELECT id, challenge_id, evaluation_identity_sha256, status, created_at, "
                    "started_at, completed_at FROM submissions WHERE user_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, challenge_id, evaluation_identity_sha256, status, created_at, "
                    "started_at, completed_at FROM submissions WHERE user_id = ? AND "
                    "(created_at < ? OR (created_at = ? AND id < ?)) "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (
                        user_id,
                        before_created_at,
                        before_created_at,
                        before_submission_id,
                        limit,
                    ),
                ).fetchall()
        return tuple(_owner_submission_from_row(row) for row in rows)

    def owner_result(self, submission_id: str, user_id: str) -> OwnerResultRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.status, s.failure_contract_version, s.failure_code,
                       s.failure_retryable, s.contract_snapshot_json, r.result_json
                FROM submissions AS s
                LEFT JOIN results AS r ON r.submission_id = s.id
                WHERE s.id = ? AND s.user_id = ?
                """,
                (submission_id, user_id),
            ).fetchone()
        if row is None:
            return None
        status = SubmissionStatus(row["status"])
        result = json.loads(row["result_json"]) if row["result_json"] is not None else None
        failure = None
        if row["failure_code"] is not None:
            failure = {
                "failure_contract_version": row["failure_contract_version"],
                "code": row["failure_code"],
                "retryable": bool(row["failure_retryable"]),
            }
        return OwnerResultRecord(
            status=status,
            result=result,
            failure=failure,
            contract_snapshot_json=row["contract_snapshot_json"],
        )

    def leaderboard(
        self,
        evaluation_identity_sha256: str,
        *,
        limit: int = 100,
        as_of: str | None = None,
        after_rank: int = 0,
    ) -> tuple[LeaderboardEntry, ...]:
        if limit <= 0 or after_rank < 0:
            raise ValueError("leaderboard pagination values are invalid")
        as_of = _timestamp(_utc_now()) if as_of is None else as_of
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH candidates AS (
                    SELECT u.public_handle, r.evaluation_identity_sha256, r.score,
                           r.samples_total, r.samples_valid, r.samples_invalid, r.succeeded_at,
                           s.contract_snapshot_json,
                           ROW_NUMBER() OVER (
                               PARTITION BY s.user_id
                               ORDER BY r.score DESC, r.succeeded_at ASC, s.id ASC
                           ) AS user_choice
                    FROM results AS r
                    JOIN submissions AS s ON s.id = r.submission_id
                    JOIN users AS u ON u.id = s.user_id
                    WHERE r.evaluation_identity_sha256 = ? AND s.status = 'succeeded'
                          AND r.succeeded_at <= ?
                ), ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        ORDER BY score DESC, succeeded_at ASC, public_handle ASC
                    ) AS leaderboard_rank
                    FROM candidates WHERE user_choice = 1
                )
                SELECT * FROM ranked WHERE leaderboard_rank > ?
                ORDER BY leaderboard_rank LIMIT ?
                """,
                (evaluation_identity_sha256, as_of, after_rank, limit),
            ).fetchall()
        return tuple(
            LeaderboardEntry(
                evaluation_identity_sha256=row["evaluation_identity_sha256"],
                public_handle=row["public_handle"],
                rank=row["leaderboard_rank"],
                samples_invalid=row["samples_invalid"],
                samples_total=row["samples_total"],
                samples_valid=row["samples_valid"],
                score=row["score"],
                succeeded_at=row["succeeded_at"],
                contract_snapshot_json=row["contract_snapshot_json"],
            )
            for row in rows
        )

    def count_submissions(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])

    def count_outbox_records(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM submission_outbox").fetchone()[0])

    def count_results(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM results").fetchone()[0])
