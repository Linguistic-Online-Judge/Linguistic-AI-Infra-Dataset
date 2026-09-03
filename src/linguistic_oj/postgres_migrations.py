"""Versioned PostgreSQL schema bootstrap for the production submission store."""

from __future__ import annotations

from urllib.parse import urlparse

POSTGRES_SCHEMA_VERSION = 2
POSTGRES_CONNECT_TIMEOUT_SECONDS = 5
POSTGRES_SESSION_OPTIONS = (
    "-c timezone=UTC "
    "-c lock_timeout=5000 "
    "-c statement_timeout=10000 "
    "-c idle_in_transaction_session_timeout=15000"
)

_EXPECTED_SCHEMA_VERSIONS = tuple(range(1, POSTGRES_SCHEMA_VERSION + 1))

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
    student_prompt_utf8 BYTEA NOT NULL,
    student_prompt_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'rejected', 'succeeded', 'failed')),
    attempt_number INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    created_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    started_at TEXT,
    lease_expires_at TEXT,
    completed_at TEXT,
    failure_contract_version TEXT,
    failure_code TEXT,
    failure_retryable BOOLEAN,
    UNIQUE(user_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS submission_outbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS results (
    submission_id TEXT PRIMARY KEY REFERENCES submissions(id),
    evaluation_identity_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
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

_POSTGRES_MIGRATIONS = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
}


def validate_postgres_url(database_url: str) -> str:
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("PostgreSQL database URL must not be empty")
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.path in {"", "/"}:
        raise ValueError("database URL must be a PostgreSQL URL with a database name")
    return database_url


def migrate_postgres(database_url: str, *, applied_at: str) -> None:
    """Apply every missing schema migration before accepting application traffic."""

    validate_postgres_url(database_url)
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("install the postgres extra to use PostgreSQL persistence") from error
    with psycopg.connect(
        database_url,
        connect_timeout=POSTGRES_CONNECT_TIMEOUT_SECONDS,
        options=POSTGRES_SESSION_OPTIONS,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('linguistic-oj-schema-migrations', 0))"
            )
            cursor.execute("SELECT to_regclass('schema_migrations')")
            if cursor.fetchone()[0] is not None:
                cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
                versions = tuple(row[0] for row in cursor.fetchall())
                if versions != _EXPECTED_SCHEMA_VERSIONS[: len(versions)]:
                    raise RuntimeError(f"unsupported PostgreSQL schema versions: {versions}")
            else:
                versions = ()
            for version in _EXPECTED_SCHEMA_VERSIONS[len(versions) :]:
                cursor.execute(_POSTGRES_MIGRATIONS[version])
                cursor.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, %s)",
                    (version, applied_at),
                )
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = tuple(row[0] for row in cursor.fetchall())
            if versions != _EXPECTED_SCHEMA_VERSIONS:
                raise RuntimeError(f"PostgreSQL schema migration is incomplete: {versions}")
