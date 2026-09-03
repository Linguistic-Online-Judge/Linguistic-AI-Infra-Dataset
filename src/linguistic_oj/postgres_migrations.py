"""Versioned PostgreSQL schema bootstrap for the production submission store."""

from __future__ import annotations

import os
import re
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

POSTGRES_SCHEMA_VERSION = 2
POSTGRES_CONNECT_TIMEOUT_SECONDS = 5
POSTGRES_SESSION_OPTIONS = (
    "-c timezone=UTC "
    "-c lock_timeout=5000 "
    "-c statement_timeout=10000 "
    "-c idle_in_transaction_session_timeout=15000"
)
_MAX_POSTGRES_CREDENTIAL_BYTES = 4096
_SECURE_POSTGRES_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
_LIBPQ_CONNECTION_ENVIRONMENT = re.compile(r"PG[A-Z0-9_]+")

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


def _is_loopback_postgres_host(hostname: str | None) -> bool:
    if hostname is None or hostname == "localhost" or hostname.startswith("/"):
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_postgres_url(database_url: str) -> str:
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("PostgreSQL database URL must not be empty")
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.path in {"", "/"}:
        raise ValueError("database URL must be a PostgreSQL URL with a database name")
    ambient_parameters = sorted(
        key for key in os.environ if _LIBPQ_CONNECTION_ENVIRONMENT.fullmatch(key)
    )
    if ambient_parameters:
        raise ValueError(
            "libpq PG* connection environment is not supported: "
            + ", ".join(ambient_parameters)
        )
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "service" in query:
        raise ValueError("PostgreSQL service indirection is not supported")
    authority = unquote(parsed.netloc.rpartition("@")[2])
    query_has_multiple_hosts = any(
        "," in value
        for key in ("host", "hostaddr")
        for value in query.get(key, [])
    )
    if "," in authority or query_has_multiple_hosts:
        raise ValueError("PostgreSQL URL must specify exactly one host")
    if len(query.get("host", [])) > 1 or len(query.get("hostaddr", [])) > 1:
        raise ValueError("PostgreSQL URL must not repeat host parameters")
    host_parameters = [*query.get("host", []), *query.get("hostaddr", [])]
    if parsed.hostname is None and not host_parameters:
        raise ValueError("PostgreSQL URL must specify an explicit host or Unix socket")
    if any(
        not _is_loopback_postgres_host(host)
        for parameter in host_parameters
        for host in parameter.split(",")
    ) or (not host_parameters and not _is_loopback_postgres_host(parsed.hostname)):
        ssl_modes = query.get("sslmode", [])
        if len(ssl_modes) != 1 or ssl_modes[0] not in _SECURE_POSTGRES_SSL_MODES:
            raise ValueError(
                "non-loopback PostgreSQL connections require a secure sslmode"
            )
    return database_url


def resolve_postgres_url(
    *,
    inline_url: str | None,
    credential_file: Path | None,
    allow_inline_credentials: bool,
) -> str:
    """Resolve a PostgreSQL URL without exposing production secrets in argv."""

    if (inline_url is None) == (credential_file is None):
        raise ValueError("configure exactly one PostgreSQL URL source")
    if credential_file is not None:
        try:
            if not credential_file.is_file():
                raise ValueError("PostgreSQL credential path must be a regular file")
            if credential_file.stat().st_size > _MAX_POSTGRES_CREDENTIAL_BYTES:
                raise ValueError("PostgreSQL credential file is too large")
            database_url = credential_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise ValueError("PostgreSQL credential file cannot be read") from error
        if not database_url or "\n" in database_url or "\r" in database_url:
            raise ValueError("PostgreSQL credential file must contain exactly one URL")
    else:
        database_url = inline_url
    if not isinstance(database_url, str) or not database_url:
        raise ValueError("PostgreSQL database URL must not be empty")
    parsed = urlparse(database_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not allow_inline_credentials and inline_url is not None and (
        parsed.password is not None
        or any(
            key == "password"
            or key.endswith("password")
            or "secret" in key
            or (key.startswith("scram_") and key.endswith("_key"))
            for key in query
        )
    ):
        raise ValueError(
            "production PostgreSQL credentials must use --postgres-database-url-file"
        )
    return validate_postgres_url(database_url)


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
