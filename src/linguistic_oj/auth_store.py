"""Account persistence shared by SQLite and PostgreSQL, with short fenced transactions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

ROLE_SCHEMA_V3 = """
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'
    CHECK (role IN ('user', 'admin'));
"""

AUTH_TABLES_V3 = """
CREATE TABLE auth_credentials (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES auth_credentials(user_id),
    expires_at BIGINT NOT NULL
);
CREATE INDEX idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX idx_auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE auth_action_tokens (
    token_hash TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose IN ('verify-email', 'reset-password')),
    email TEXT NOT NULL,
    user_id TEXT REFERENCES auth_credentials(user_id),
    expires_at BIGINT NOT NULL
);
CREATE INDEX idx_auth_actions_email ON auth_action_tokens(email, purpose);
CREATE INDEX idx_auth_actions_expiry ON auth_action_tokens(expires_at);
CREATE TABLE auth_rate_limits (
    bucket TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    expires_at BIGINT NOT NULL
);
CREATE INDEX idx_auth_rate_expiry ON auth_rate_limits(expires_at);
"""

AUTH_SCHEMA_V3 = ROLE_SCHEMA_V3 + AUTH_TABLES_V3


class AuthConflictError(ValueError):
    """An account uniqueness constraint rejected a transaction."""


@dataclass(frozen=True, slots=True)
class Account:
    user_id: str
    subject: str
    public_handle: str
    role: str
    email: str = field(repr=False)
    password_hash: str = field(repr=False)
    version: int

    def public(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "public_handle": self.public_handle,
            "role": self.role,
        }


_ACCOUNT_SELECT = (
    "SELECT u.id, u.auth_subject, u.public_handle, u.role, c.email, "
    "c.password_hash, c.version FROM users u JOIN auth_credentials c ON c.user_id = u.id "
)


class AuthTransaction:
    """Only internal, constant SQL uses the portable question-mark placeholders."""

    def __init__(self, cursor, *, postgres: bool = False) -> None:
        self.cursor = cursor
        self.postgres = postgres
        self.refresh_now()

    def refresh_now(self) -> None:
        query = (
            "SELECT floor(extract(epoch FROM clock_timestamp()))::bigint"
            if self.postgres
            else "SELECT CAST(strftime('%s', 'now') AS INTEGER)"
        )
        self.now = int(self.execute(query).fetchone()[0])

    def execute(self, query: str, params: tuple = ()):
        self.cursor.execute(query.replace("?", "%s") if self.postgres else query, params)
        return self.cursor


class AuthStoreMixin:
    """Backends supply _auth_transaction: a database-wide auth mutex, commit or rollback.

    Password hashing and mail delivery must stay outside this mutex. Login checks the
    credential version again inside it, so a concurrent reset cannot mint a stale session.
    """

    def _auth_transaction(self):
        raise NotImplementedError

    def auth_health_check(self) -> None:
        self.health_check()
        with self._auth_transaction() as tx:
            tx.execute(_ACCOUNT_SELECT + "WHERE 1 = 0")
            tx.execute("SELECT token_hash, user_id, expires_at FROM auth_sessions WHERE 1 = 0")
            tx.execute(
                "SELECT token_hash, purpose, email, user_id, expires_at "
                "FROM auth_action_tokens WHERE 1 = 0"
            )
            tx.execute("SELECT bucket, attempts, expires_at FROM auth_rate_limits WHERE 1 = 0")

    def auth_require_bound_accounts(self) -> None:
        with self._auth_transaction() as tx:
            unbound = tx.execute(
                "SELECT 1 FROM users u LEFT JOIN auth_credentials c ON c.user_id = u.id "
                "WHERE c.user_id IS NULL LIMIT 1"
            ).fetchone()
            if unbound is not None:
                raise RuntimeError(
                    "Production authentication has unbound legacy users; "
                    "a trusted account-enrollment "
                    "procedure or an isolated new database is required. Existing data is unchanged."
                )

    def auth_throttle(self, buckets: tuple[tuple[str, int], ...], *, seconds: int = 900) -> bool:
        with self._auth_transaction() as tx:
            tx.execute("DELETE FROM auth_rate_limits WHERE expires_at <= ?", (tx.now,))
            tx.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (tx.now,))
            tx.execute("DELETE FROM auth_action_tokens WHERE expires_at <= ?", (tx.now,))
            allowed = True
            for bucket, limit in buckets:
                row = tx.execute(
                    "SELECT attempts FROM auth_rate_limits WHERE bucket = ?", (bucket,)
                ).fetchone()
                if row is not None and row[0] >= limit:
                    allowed = False
            # Rejected requests do not extend the window or create unbounded email buckets.
            if allowed:
                for bucket, _ in buckets:
                    tx.execute(
                        "INSERT INTO auth_rate_limits(bucket, attempts, expires_at) "
                        "VALUES (?, 1, ?) ON CONFLICT(bucket) DO UPDATE "
                        "SET attempts = auth_rate_limits.attempts + 1",
                        (bucket, tx.now + seconds),
                    )
            return allowed

    def auth_account(self, email: str) -> Account | None:
        with self._auth_transaction() as tx:
            row = tx.execute(_ACCOUNT_SELECT + "WHERE c.email = ?", (email,)).fetchone()
            return None if row is None else Account(*row)

    def auth_issue_action(self, email: str, purpose: str, token_hash: str, ttl: int) -> bool:
        with self._auth_transaction() as tx:
            row = tx.execute(
                "SELECT user_id FROM auth_credentials WHERE email = ?", (email,)
            ).fetchone()
            if (purpose == "verify-email" and row is not None) or (
                purpose == "reset-password" and row is None
            ):
                return False
            tx.execute(
                "DELETE FROM auth_action_tokens WHERE email = ? AND purpose = ?", (email, purpose)
            )
            tx.execute(
                "INSERT INTO auth_action_tokens(token_hash, purpose, email, user_id, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, purpose, email, None if row is None else row[0], tx.now + ttl),
            )
            return True

    def auth_action_email(self, token_hash: str, purpose: str) -> str | None:
        with self._auth_transaction() as tx:
            row = tx.execute(
                "SELECT email FROM auth_action_tokens "
                "WHERE token_hash = ? AND purpose = ? AND expires_at > ?",
                (token_hash, purpose, tx.now),
            ).fetchone()
            return None if row is None else row[0]

    @staticmethod
    def _auth_insert_account(tx, email: str, password_hash: str, handle: str, role: str) -> None:
        user_id = uuid.uuid4().hex
        tx.execute(
            "INSERT INTO users(id, auth_subject, public_handle, role, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                "account:" + user_id,
                handle,
                role,
                datetime.fromtimestamp(tx.now, UTC).isoformat(timespec="microseconds"),
            ),
        )
        tx.execute(
            "INSERT INTO auth_credentials(user_id, email, password_hash) VALUES (?, ?, ?)",
            (user_id, email, password_hash),
        )

    def auth_complete_action(
        self, token_hash: str, purpose: str, password_hash: str, handle: str | None = None
    ) -> bool:
        with self._auth_transaction() as tx:
            row = tx.execute(
                "SELECT email, user_id FROM auth_action_tokens "
                "WHERE token_hash = ? AND purpose = ? AND expires_at > ?",
                (token_hash, purpose, tx.now),
            ).fetchone()
            if row is None:
                return False
            email, user_id = row
            if purpose == "verify-email":
                self._auth_insert_account(tx, email, password_hash, handle, "user")
            else:
                tx.execute(
                    "UPDATE auth_credentials SET password_hash = ?, version = version + 1 "
                    "WHERE user_id = ?",
                    (password_hash, user_id),
                )
                tx.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
            tx.execute("DELETE FROM auth_action_tokens WHERE email = ?", (email,))
            return True

    def auth_create_session(
        self, account: Account, token_hash: str, ttl: int, old_token_hash: str | None = None
    ) -> tuple[Account, int] | None:
        with self._auth_transaction() as tx:
            row = tx.execute(_ACCOUNT_SELECT + "WHERE c.user_id = ?", (account.user_id,)).fetchone()
            if row is None or row[6] != account.version or row[5] != account.password_hash:
                return None
            if old_token_hash is not None:
                tx.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (old_token_hash,))
            expires_at = tx.now + ttl
            tx.execute(
                "INSERT INTO auth_sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (token_hash, account.user_id, expires_at),
            )
            return Account(*row), expires_at

    def auth_session(self, token_hash: str) -> tuple[Account, int] | None:
        with self._auth_transaction() as tx:
            row = tx.execute(
                "SELECT u.id, u.auth_subject, u.public_handle, u.role, c.email, "
                "c.password_hash, c.version, s.expires_at FROM auth_sessions s "
                "JOIN auth_credentials c ON c.user_id = s.user_id JOIN users u ON u.id = c.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ?",
                (token_hash, tx.now),
            ).fetchone()
            return None if row is None else (Account(*row[:7]), row[7])

    def auth_revoke_session(self, token_hash: str) -> None:
        with self._auth_transaction() as tx:
            tx.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))

    def auth_provision_account(
        self, email: str, password_hash: str, handle: str, role: str, expected: Account | None
    ) -> None:
        with self._auth_transaction() as tx:
            row = tx.execute(_ACCOUNT_SELECT + "WHERE c.email = ?", (email,)).fetchone()
            current = None if row is None else Account(*row)
            if current != expected:
                raise AuthConflictError("development account changed; refusing to overwrite")
            if current is None:
                self._auth_insert_account(tx, email, password_hash, handle, role)

    def set_account_role(self, user_id: str, role: str) -> None:
        """Trusted server operation only. There is deliberately no public role endpoint."""
        if role not in {"user", "admin"}:
            raise ValueError("unsupported account role")
        with self._auth_transaction() as tx:
            if tx.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id)).rowcount != 1:
                raise ValueError("account does not exist")
