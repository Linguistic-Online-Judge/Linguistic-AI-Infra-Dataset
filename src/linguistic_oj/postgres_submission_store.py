"""PostgreSQL persistence for the submission API and evaluation workers."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta
from typing import Any

from .mvp_contract import EvaluationContract, canonical_json
from .postgres_migrations import (
    POSTGRES_CONNECT_TIMEOUT_SECONDS,
    POSTGRES_SCHEMA_VERSION,
    POSTGRES_SESSION_OPTIONS,
    validate_postgres_url,
)
from .submission_store import (
    ClaimAttempt,
    ClaimedSubmission,
    CreatedSubmission,
    GlobalQueueFullError,
    IdempotencyConflictError,
    LeaderboardEntry,
    OwnerResultRecord,
    OwnerSubmissionRecord,
    SubmissionQuotaError,
    SubmissionRecord,
    SubmissionStatus,
    UserRecord,
    _request_sha256,
    _timestamp,
)


class PostgresSubmissionStore:
    """PostgreSQL persistence shared by the submission API and Workers."""

    def __init__(self, database_url: str) -> None:
        self._database_url = validate_postgres_url(database_url)

    def _connect(self):
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError(
                "install the postgres extra to use PostgreSQL persistence"
            ) from error
        return psycopg.connect(
            self._database_url,
            connect_timeout=POSTGRES_CONNECT_TIMEOUT_SECONDS,
            options=POSTGRES_SESSION_OPTIONS,
        )

    @staticmethod
    def _database_now(cursor: Any) -> datetime:
        cursor.execute("SELECT clock_timestamp()")
        return cursor.fetchone()[0]

    def health_check(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('schema_migrations')")
                if cursor.fetchone()[0] is None:
                    raise RuntimeError("PostgreSQL schema has not been migrated")
                cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
                versions = tuple(row[0] for row in cursor.fetchall())
                expected = tuple(range(1, POSTGRES_SCHEMA_VERSION + 1))
                if versions != expected:
                    raise RuntimeError(f"unsupported PostgreSQL schema versions: {versions}")
                cursor.execute(
                    "SELECT to_regclass('users'), to_regclass('submissions'), "
                    "to_regclass('submission_outbox'), to_regclass('results')"
                )
                if any(table is None for table in cursor.fetchone()):
                    raise RuntimeError("PostgreSQL submission schema is incomplete")

    def register_user(self, *, auth_subject: str, public_handle: str) -> UserRecord:
        if not auth_subject or not public_handle or "@" in public_handle:
            raise ValueError("user subject and non-email public handle are required")
        user = UserRecord(uuid.uuid4().hex, auth_subject, public_handle)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                created_at = _timestamp(self._database_now(cursor))
                cursor.execute(
                    "INSERT INTO users(id, auth_subject, public_handle, created_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (user.user_id, user.auth_subject, user.public_handle, created_at),
                )
        return user

    def user_by_subject(self, auth_subject: str) -> UserRecord | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, auth_subject, public_handle FROM users WHERE auth_subject = %s",
                    (auth_subject,),
                )
                row = cursor.fetchone()
        return None if row is None else UserRecord(*row)

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
        submission_id = uuid.uuid4().hex
        with self._connect() as connection:
            with connection.cursor() as cursor:
                # Serialize quota checks across API processes before inserting a submission.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (user.user_id,),
                )
                cursor.execute(
                    "SELECT id, user_id, challenge_id, evaluation_identity_sha256, status, "
                    "created_at, started_at, completed_at, request_sha256 "
                    "FROM submissions WHERE user_id = %s AND idempotency_key = %s",
                    (user.user_id, idempotency_key),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[8] != request_sha256:
                        raise IdempotencyConflictError(
                            "idempotency key was already used for different content"
                        )
                    return CreatedSubmission(
                        SubmissionRecord(
                            submission_id=existing[0],
                            user_id=existing[1],
                            challenge_id=existing[2],
                            evaluation_identity_sha256=existing[3],
                            status=SubmissionStatus(existing[4]),
                            created_at=existing[5],
                            started_at=existing[6],
                            completed_at=existing[7],
                        ),
                        replayed=True,
                    )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('global-submission-queue', 0))"
                )
                now = self._database_now(cursor)
                now_text = _timestamp(now)
                deadline_at = _timestamp(
                    now + timedelta(seconds=contract.job_deadline_seconds)
                )
                cutoff = _timestamp(now - timedelta(hours=24))
                cursor.execute(
                    "SELECT COUNT(*), MIN(created_at) FROM submissions "
                    "WHERE user_id = %s AND challenge_id = %s AND created_at >= %s",
                    (user.user_id, contract.challenge_id, cutoff),
                )
                accepted_count, oldest_created_at = cursor.fetchone()
                if accepted_count >= contract.submissions_per_user_per_challenge_per_24h:
                    oldest = datetime.fromisoformat(oldest_created_at)
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
                cursor.execute(
                    "SELECT COUNT(*) FROM submissions "
                    "WHERE user_id = %s AND status IN ('queued', 'running')",
                    (user.user_id,),
                )
                outstanding_count = cursor.fetchone()[0]
                if outstanding_count >= contract.max_outstanding_submissions_per_user:
                    raise SubmissionQuotaError(
                        code="OUTSTANDING_SUBMISSION_LIMIT",
                        limit=contract.max_outstanding_submissions_per_user,
                        current=outstanding_count,
                    )
                cursor.execute(
                    "SELECT COUNT(*) FROM submissions WHERE status IN ('queued', 'running')"
                )
                global_count = cursor.fetchone()[0]
                if global_count >= contract.global_queue_depth:
                    raise GlobalQueueFullError(
                        limit=contract.global_queue_depth,
                        current=global_count,
                    )
                cursor.execute(
                    "INSERT INTO submissions("
                    "id, user_id, idempotency_key, request_sha256, challenge_id, "
                    "contract_version, evaluation_identity_sha256, "
                    "contract_snapshot_sha256, contract_snapshot_json, "
                    "student_prompt_utf8, student_prompt_sha256, status, created_at, "
                    "deadline_at, failure_contract_version"
                    ") VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s"
                    ")",
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
                        hashlib.sha256(prompt_utf8).hexdigest(),
                        now_text,
                        deadline_at,
                        contract.failure_contract_version,
                    ),
                )
                cursor.execute(
                    "INSERT INTO submission_outbox(submission_id, created_at) "
                    "VALUES (%s, %s)",
                    (submission_id, now_text),
                )
        return CreatedSubmission(
            SubmissionRecord(
                submission_id,
                user.user_id,
                contract.challenge_id,
                contract.evaluation_identity_sha256,
                SubmissionStatus.QUEUED,
                now_text,
                None,
                None,
            ),
            replayed=False,
        )

    def unpublished_submission_ids(
        self,
        evaluation_identity_sha256: str,
        contract_snapshot_sha256: str,
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT o.submission_id FROM submission_outbox AS o "
                    "JOIN submissions AS s ON s.id = o.submission_id "
                    "WHERE o.published_at IS NULL AND s.evaluation_identity_sha256 = %s "
                    "AND s.contract_snapshot_sha256 = %s AND s.status = 'queued' ORDER BY o.id",
                    (evaluation_identity_sha256, contract_snapshot_sha256),
                )
                return tuple(row[0] for row in cursor.fetchall())

    def mark_outbox_published(self, submission_id: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE submission_outbox SET published_at = %s "
                    "WHERE submission_id = %s AND published_at IS NULL",
                    (_timestamp(self._database_now(cursor)), submission_id),
                )

    def published_queued_submission_ids(
        self,
        evaluation_identity_sha256: str,
        contract_snapshot_sha256: str,
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT o.submission_id FROM submission_outbox AS o "
                    "JOIN submissions AS s ON s.id = o.submission_id "
                    "WHERE o.published_at IS NOT NULL AND s.status = 'queued' "
                    "AND s.evaluation_identity_sha256 = %s "
                    "AND s.contract_snapshot_sha256 = %s ORDER BY o.id",
                    (evaluation_identity_sha256, contract_snapshot_sha256),
                )
                return tuple(row[0] for row in cursor.fetchall())

    def claim_deadline_expired(self, claim: ClaimedSubmission) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                return claim.deadline_at <= _timestamp(self._database_now(cursor))

    def expire_leases(self) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                now_text = _timestamp(self._database_now(cursor))
                cursor.execute(
                    "UPDATE submissions SET status = 'failed', completed_at = %s, "
                    "lease_token = NULL, lease_expires_at = NULL, "
                    "failure_code = CASE WHEN deadline_at <= %s THEN 'JOB_DEADLINE' "
                    "ELSE 'WORKER_CRASH' END, failure_retryable = FALSE "
                    "WHERE status = 'running' AND lease_expires_at <= %s",
                    (now_text, now_text, now_text),
                )
                return cursor.rowcount

    def expire_queued_deadlines(self) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                now_text = _timestamp(self._database_now(cursor))
                cursor.execute(
                    "UPDATE submissions SET status = 'failed', completed_at = %s, "
                    "failure_code = 'JOB_DEADLINE', failure_retryable = FALSE "
                    "WHERE status = 'queued' AND deadline_at <= %s",
                    (now_text, now_text),
                )
                return cursor.rowcount

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
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, user_id, student_prompt_utf8, student_prompt_sha256, "
                    "contract_snapshot_json, contract_snapshot_sha256, "
                    "evaluation_identity_sha256, attempt_number, deadline_at, status "
                    "FROM submissions WHERE id = %s FOR UPDATE",
                    (submission_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return ClaimAttempt(claim=None, retry_later=False)
                if row[5] != contract_snapshot_sha256 or row[6] != evaluation_identity_sha256:
                    return ClaimAttempt(claim=None, retry_later=False)
                if row[9] == SubmissionStatus.RUNNING.value:
                    return ClaimAttempt(claim=None, retry_later=True)
                if row[9] != SubmissionStatus.QUEUED.value or row[7] >= max_attempts:
                    return ClaimAttempt(claim=None, retry_later=False)
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (row[1],),
                )
                now = self._database_now(cursor)
                now_text = _timestamp(now)
                if row[8] <= now_text:
                    cursor.execute(
                        "UPDATE submissions SET status = 'failed', completed_at = %s, "
                        "failure_code = 'JOB_DEADLINE', failure_retryable = FALSE "
                        "WHERE id = %s AND status = 'queued'",
                        (now_text, submission_id),
                    )
                    return ClaimAttempt(claim=None, retry_later=False)
                cursor.execute(
                    "SELECT COUNT(*) FROM submissions WHERE user_id = %s AND status = 'running'",
                    (row[1],),
                )
                if cursor.fetchone()[0] >= max_running_per_user:
                    return ClaimAttempt(claim=None, retry_later=True)
                attempt_number = row[7] + 1
                lease_token = uuid.uuid4().hex
                lease_expires_at = min(
                    _timestamp(now + timedelta(seconds=lease_seconds)), row[8]
                )
                cursor.execute(
                    "UPDATE submissions SET status = 'running', attempt_number = %s, "
                    "lease_token = %s, started_at = %s, lease_expires_at = %s "
                    "WHERE id = %s AND status = 'queued' AND attempt_number = %s",
                    (
                        attempt_number,
                        lease_token,
                        now_text,
                        lease_expires_at,
                        submission_id,
                        row[7],
                    ),
                )
                if cursor.rowcount != 1:
                    return ClaimAttempt(claim=None, retry_later=True)
        return ClaimAttempt(
            claim=ClaimedSubmission(
                submission_id=submission_id,
                user_id=row[1],
                student_prompt=bytes(row[2]).decode("utf-8"),
                student_prompt_sha256=row[3],
                contract_snapshot_json=row[4],
                contract_snapshot_sha256=row[5],
                evaluation_identity_sha256=row[6],
                attempt_number=attempt_number,
                lease_token=lease_token,
                deadline_at=row[8],
            ),
            retry_later=False,
        )

    def complete_success(
        self,
        claim: ClaimedSubmission,
        *,
        owner_result: dict[str, Any],
    ) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT deadline_at, lease_expires_at, evaluation_identity_sha256 "
                    "FROM submissions WHERE id = %s AND status = 'running' "
                    "AND attempt_number = %s AND lease_token = %s FOR UPDATE",
                    (claim.submission_id, claim.attempt_number, claim.lease_token),
                )
                lease = cursor.fetchone()
                if lease is None:
                    return False
                succeeded_at = _timestamp(self._database_now(cursor))
                if lease[1] <= succeeded_at or lease[0] <= succeeded_at:
                    return False
                cursor.execute(
                    "UPDATE submissions SET status = 'succeeded', completed_at = %s, "
                    "lease_token = NULL, lease_expires_at = NULL "
                    "WHERE id = %s AND status = 'running' AND attempt_number = %s "
                    "AND lease_token = %s",
                    (
                        succeeded_at,
                        claim.submission_id,
                        claim.attempt_number,
                        claim.lease_token,
                    ),
                )
                if cursor.rowcount != 1:
                    return False
                cursor.execute(
                    "INSERT INTO results(submission_id, evaluation_identity_sha256, result_json, "
                    "score, samples_total, samples_valid, samples_invalid, succeeded_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        claim.submission_id,
                        lease[2],
                        canonical_json(owner_result),
                        owner_result["score"],
                        owner_result["samples_total"],
                        owner_result["samples_valid"],
                        owner_result["samples_invalid"],
                        succeeded_at,
                    ),
                )
        return True

    def retry_submission(self, claim: ClaimedSubmission, *, max_attempts: int) -> bool:
        """Return a live claimed submission to queued without extending its deadline."""

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT attempt_number, deadline_at, lease_expires_at FROM submissions "
                    "WHERE id = %s AND status = 'running' AND attempt_number = %s "
                    "AND lease_token = %s FOR UPDATE",
                    (claim.submission_id, claim.attempt_number, claim.lease_token),
                )
                lease = cursor.fetchone()
                now_text = _timestamp(self._database_now(cursor))
                if (
                    lease is None
                    or lease[0] >= max_attempts
                    or lease[1] <= now_text
                    or lease[2] <= now_text
                ):
                    return False
                cursor.execute(
                    "UPDATE submissions SET status = 'queued', lease_token = NULL, "
                    "lease_expires_at = NULL, started_at = NULL "
                    "WHERE id = %s AND status = 'running' AND attempt_number = %s "
                    "AND lease_token = %s",
                    (
                        claim.submission_id,
                        claim.attempt_number,
                        claim.lease_token,
                    ),
                )
                return cursor.rowcount == 1

    def complete_failure(
        self,
        claim: ClaimedSubmission,
        *,
        failure_contract_version: str,
        code: str,
        retryable: bool,
    ) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT deadline_at, lease_expires_at, failure_contract_version "
                    "FROM submissions WHERE id = %s AND status = 'running' "
                    "AND attempt_number = %s AND lease_token = %s FOR UPDATE",
                    (claim.submission_id, claim.attempt_number, claim.lease_token),
                )
                lease = cursor.fetchone()
                if lease is None or lease[2] != failure_contract_version:
                    return False
                completed_at = _timestamp(self._database_now(cursor))
                if lease[1] <= completed_at and lease[0] > completed_at:
                    return False
                if lease[0] <= completed_at:
                    code = "JOB_DEADLINE"
                    retryable = False
                cursor.execute(
                    "UPDATE submissions SET status = 'failed', completed_at = %s, "
                    "lease_token = NULL, lease_expires_at = NULL, "
                    "failure_contract_version = %s, failure_code = %s, "
                    "failure_retryable = %s "
                    "WHERE id = %s AND status = 'running' AND attempt_number = %s "
                    "AND lease_token = %s",
                    (
                        completed_at,
                        failure_contract_version,
                        code,
                        retryable,
                        claim.submission_id,
                        claim.attempt_number,
                        claim.lease_token,
                    ),
                )
                return cursor.rowcount == 1

    def complete_rejected(self, claim: ClaimedSubmission) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT deadline_at, lease_expires_at FROM submissions "
                    "WHERE id = %s AND status = 'running' AND attempt_number = %s "
                    "AND lease_token = %s FOR UPDATE",
                    (claim.submission_id, claim.attempt_number, claim.lease_token),
                )
                lease = cursor.fetchone()
                if lease is None:
                    return False
                completed_at = _timestamp(self._database_now(cursor))
                if lease[1] <= completed_at and lease[0] > completed_at:
                    return False
                if lease[0] <= completed_at:
                    status = SubmissionStatus.FAILED.value
                    failure_code = "JOB_DEADLINE"
                    failure_retryable = False
                else:
                    status = SubmissionStatus.REJECTED.value
                    failure_code = "TOKEN_LIMIT_EXCEEDED"
                    failure_retryable = False
                cursor.execute(
                    "UPDATE submissions SET status = %s, completed_at = %s, "
                    "lease_token = NULL, lease_expires_at = NULL, failure_code = %s, "
                    "failure_retryable = %s WHERE id = %s AND status = 'running' "
                    "AND attempt_number = %s AND lease_token = %s",
                    (
                        status,
                        completed_at,
                        failure_code,
                        failure_retryable,
                        claim.submission_id,
                        claim.attempt_number,
                        claim.lease_token,
                    ),
                )
                return cursor.rowcount == 1

    def submission_for_owner(self, submission_id: str, user_id: str) -> SubmissionRecord | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, user_id, challenge_id, evaluation_identity_sha256, status, "
                    "created_at, started_at, completed_at "
                    "FROM submissions WHERE id = %s AND user_id = %s",
                    (submission_id, user_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return SubmissionRecord(
            submission_id=row[0],
            user_id=row[1],
            challenge_id=row[2],
            evaluation_identity_sha256=row[3],
            status=SubmissionStatus(row[4]),
            created_at=row[5],
            started_at=row[6],
            completed_at=row[7],
        )

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
            with connection.cursor() as cursor:
                if before_created_at is None:
                    cursor.execute(
                        "SELECT id, challenge_id, evaluation_identity_sha256, status, "
                        "created_at, started_at, completed_at FROM submissions "
                        "WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
                        (user_id, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT id, challenge_id, evaluation_identity_sha256, status, "
                        "created_at, started_at, completed_at FROM submissions "
                        "WHERE user_id = %s AND (created_at < %s OR "
                        "(created_at = %s AND id < %s)) "
                        "ORDER BY created_at DESC, id DESC LIMIT %s",
                        (
                            user_id,
                            before_created_at,
                            before_created_at,
                            before_submission_id,
                            limit,
                        ),
                    )
                rows = cursor.fetchall()
        return tuple(
            OwnerSubmissionRecord(
                submission_id=row[0],
                challenge_id=row[1],
                evaluation_identity_sha256=row[2],
                status=SubmissionStatus(row[3]),
                created_at=row[4],
                started_at=row[5],
                completed_at=row[6],
            )
            for row in rows
        )

    def owner_result(self, submission_id: str, user_id: str) -> OwnerResultRecord | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT s.status, s.failure_contract_version, s.failure_code, "
                    "s.failure_retryable, s.contract_snapshot_json, r.result_json "
                    "FROM submissions AS s LEFT JOIN results AS r ON r.submission_id = s.id "
                    "WHERE s.id = %s AND s.user_id = %s",
                    (submission_id, user_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        failure = None
        if row[2] is not None:
            failure = {
                "failure_contract_version": row[1],
                "code": row[2],
                "retryable": bool(row[3]),
            }
        return OwnerResultRecord(
            status=SubmissionStatus(row[0]),
            result=json.loads(row[5]) if row[5] is not None else None,
            failure=failure,
            contract_snapshot_json=row[4],
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
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if as_of is None:
                    as_of = _timestamp(self._database_now(cursor))
                cursor.execute(
                    "WITH candidates AS ("
                    "SELECT u.public_handle, r.evaluation_identity_sha256, r.score, "
                    "r.samples_total, r.samples_valid, r.samples_invalid, r.succeeded_at, "
                    "s.contract_snapshot_json, ROW_NUMBER() OVER (PARTITION BY s.user_id "
                    "ORDER BY r.score DESC, r.succeeded_at ASC, s.id ASC) AS user_choice "
                    "FROM results AS r JOIN submissions AS s ON s.id = r.submission_id "
                    "JOIN users AS u ON u.id = s.user_id WHERE r.evaluation_identity_sha256 = %s "
                    "AND s.status = 'succeeded' AND r.succeeded_at <= %s), ranked AS ("
                    "SELECT *, ROW_NUMBER() OVER (ORDER BY score DESC, succeeded_at ASC, "
                    "public_handle ASC) AS leaderboard_rank FROM candidates WHERE user_choice = 1"
                    ") SELECT * FROM ranked WHERE leaderboard_rank > %s "
                    "ORDER BY leaderboard_rank LIMIT %s",
                    (evaluation_identity_sha256, as_of, after_rank, limit),
                )
                rows = cursor.fetchall()
        return tuple(
            LeaderboardEntry(
                evaluation_identity_sha256=row[1],
                public_handle=row[0],
                rank=row[9],
                score=row[2],
                samples_total=row[3],
                samples_valid=row[4],
                samples_invalid=row[5],
                succeeded_at=row[6],
                contract_snapshot_json=row[7],
            )
            for row in rows
        )
