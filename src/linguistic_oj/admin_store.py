"""Portable teaching state, admission fencing, and append-only administrator revisions."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth_store import AuthTransaction
from .challenge import PublicChallenge

ADMIN_BODY_BYTES = 16 * 1024
MAX_EXPECTED_REVISION = 9223372036854775806
ADMIN_SCHEMA_V4 = """
CREATE TABLE challenge_admin_state (
    challenge_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    draft_json TEXT,
    published_json TEXT,
    published_revision BIGINT NOT NULL DEFAULT 0,
    published_source_fingerprint TEXT,
    admissions_closed INTEGER NOT NULL DEFAULT 0 CHECK (admissions_closed IN (0, 1))
);
CREATE TABLE challenge_admin_revisions (
    challenge_id TEXT NOT NULL REFERENCES challenge_admin_state(challenge_id),
    revision BIGINT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('save', 'publish', 'admissions')),
    operator_user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    previous_json TEXT NOT NULL,
    next_json TEXT NOT NULL,
    PRIMARY KEY (challenge_id, revision)
);
"""


class TeachingContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(max_length=500)
    instructions: str = Field(min_length=1, max_length=4000)
    zero_shot_prompt: str = Field(min_length=1, max_length=3000)
    few_shot_prompt: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def safe_text(self):
        values = self.model_dump()
        for name, value in values.items():
            if (name != "summary" and not value.strip()) or any(
                char != "\n" and unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value
            ):
                raise ValueError("teaching content must contain plain UTF-8 text")
        envelope = {"expected_revision": MAX_EXPECTED_REVISION, "content": values}
        if len(_json(envelope).encode("utf-8")) > ADMIN_BODY_BYTES:
            raise ValueError("teaching content exceeds the request byte limit")
        return self


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_fingerprint(public: PublicChallenge) -> str:
    return hashlib.sha256(_json(public.model_dump(mode="json")).encode("utf-8")).hexdigest()


class AdminStoreError(ValueError):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.message = message


class ChallengePausedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdminActor:
    token_hash: str = field(repr=False)
    expected_user: str | None = None


@dataclass(frozen=True, slots=True)
class AdminState:
    challenge_id: str
    source_fingerprint: str | None = None
    revision: int = 0
    draft_json: str | None = field(default=None, repr=False)
    published_json: str | None = field(default=None, repr=False)
    published_revision: int = 0
    published_source_fingerprint: str | None = None
    admissions_closed: bool = False

    def matches(self, fingerprint: str) -> bool:
        return self.source_fingerprint is None or self.source_fingerprint == fingerprint

    def closed_for(self, fingerprint: str) -> bool:
        return self.admissions_closed or not self.matches(fingerprint)

    def published_content(self, fingerprint: str) -> TeachingContent | None:
        if self.published_source_fingerprint != fingerprint or self.published_json is None:
            return None
        return TeachingContent.model_validate_json(self.published_json)

    def issues(self, fingerprint: str) -> list[dict[str, str]]:
        issues = []
        if not self.matches(fingerprint):
            issues.append(
                {
                    "code": "ADMIN_SOURCE_CHANGED",
                    "message": "The catalog source changed. Review and save an updated draft first",
                }
            )
        if self.draft_json is None:
            issues.append(
                {"code": "ADMIN_DRAFT_REQUIRED", "message": "Save a teaching draft first"}
            )
        else:
            try:
                TeachingContent.model_validate_json(self.draft_json)
            except ValueError:
                issues.append(
                    {
                        "code": "ADMIN_DRAFT_INVALID",
                        "message": "The saved draft exceeds text limits. Save a corrected draft",
                    }
                )
        return issues


def lock_admin_task(tx: AuthTransaction, challenge_id: str) -> None:
    if tx.postgres:
        # Unlike a row lock, this also fences the first policy write for a catalog task.
        tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            ("loj-admin-task-v4:" + challenge_id,),
        )


def assert_admissions_open(tx: AuthTransaction, challenge_id: str, fingerprint: str | None) -> None:
    lock_admin_task(tx, challenge_id)
    row = tx.execute(
        "SELECT source_fingerprint, admissions_closed FROM challenge_admin_state "
        "WHERE challenge_id = ?",
        (challenge_id,),
    ).fetchone()
    if row is not None and (row[1] or row[0] != fingerprint):
        raise ChallengePausedError("challenge admissions are paused")


_STATE_SELECT = (
    "SELECT challenge_id, source_fingerprint, revision, draft_json, published_json, "
    "published_revision, published_source_fingerprint, admissions_closed "
    "FROM challenge_admin_state "
)


class AdminStoreMixin:
    """Use the existing auth transaction, never open a second connection while locked.

    Admin operations acquire global auth then task locks. Submission admission acquires
    user then task then queue locks and never requests auth, avoiding a reverse lock edge.
    """

    def _auth_transaction(self):
        raise NotImplementedError

    @staticmethod
    def _admin_authorize(tx: AuthTransaction, actor: AdminActor) -> str:
        row = tx.execute(
            "SELECT u.id, u.role FROM auth_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (actor.token_hash, tx.now),
        ).fetchone()
        if row is None:
            raise AdminStoreError("AUTH_INVALID_TOKEN", 401, "Session is invalid or expired")
        if actor.expected_user is not None and actor.expected_user != row[0]:
            raise AdminStoreError(
                "AUTH_ACCOUNT_CHANGED", 409, "Signed-in account changed. Refresh and try again"
            )
        if row[1] != "admin":
            raise AdminStoreError("ADMIN_REQUIRED", 403, "Administrator access is required")
        return row[0]

    def admin_states(
        self, challenge_ids: Sequence[str], *, actor: AdminActor | None = None
    ) -> dict[str, AdminState]:
        with self._auth_transaction() as tx:
            if actor is not None:
                self._admin_authorize(tx, actor)
            states = {key: AdminState(key) for key in challenge_ids}
            # Bounded batches also work below older SQLite parameter limits.
            for offset in range(0, len(challenge_ids), 500):
                batch = challenge_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = tx.execute(
                    _STATE_SELECT + f"WHERE challenge_id IN ({placeholders})", tuple(batch)
                ).fetchall()
                for row in rows:
                    states[row[0]] = AdminState(*row[:7], admissions_closed=bool(row[7]))
            return states

    def admin_change(
        self,
        *,
        challenge_id: str,
        fingerprint: str,
        actor: AdminActor,
        expected_revision: int,
        action: Literal["save", "check", "publish", "admissions"],
        has_contract: bool,
        can_reopen: bool,
        content: TeachingContent | None = None,
        closed: bool | None = None,
    ) -> AdminState | dict:
        if (
            type(expected_revision) is not int
            or not 0 <= expected_revision <= MAX_EXPECTED_REVISION
        ):
            raise ValueError("invalid expected revision")
        if actor.expected_user is None:
            raise AdminStoreError("AUTH_ACCOUNT_CHANGED", 409, "Expected account is required")
        if action not in {"save", "check", "publish", "admissions"}:
            raise ValueError("unsupported admin operation")
        if action == "save":
            content = TeachingContent.model_validate(content)
        if action == "admissions" and type(closed) is not bool:
            raise ValueError("closed must be a boolean")
        with self._auth_transaction() as tx:
            self._admin_authorize(tx, actor)
            lock_admin_task(tx, challenge_id)
            # The session can expire while waiting for an admitted submission's task lock.
            tx.refresh_now()
            operator = self._admin_authorize(tx, actor)
            row = tx.execute(_STATE_SELECT + "WHERE challenge_id = ?", (challenge_id,)).fetchone()
            previous = (
                AdminState(challenge_id)
                if row is None
                else AdminState(*row[:7], admissions_closed=bool(row[7]))
            )
            if previous.revision != expected_revision:
                raise AdminStoreError(
                    "ADMIN_REVISION_CONFLICT", 409, "This challenge changed. Reload before saving"
                )
            issues = previous.issues(fingerprint)
            if action == "check":
                return {"can_publish": not issues, "issues": issues, "revision": previous.revision}
            current = replace(previous, revision=previous.revision + 1)
            if action == "save":
                current = replace(
                    current,
                    source_fingerprint=fingerprint,
                    draft_json=_json(content.model_dump()),
                    admissions_closed=previous.admissions_closed
                    or (has_contract and not previous.matches(fingerprint)),
                )
            else:
                if not previous.matches(fingerprint):
                    raise AdminStoreError("ADMIN_SOURCE_CHANGED", 409, issues[0]["message"])
                current = replace(current, source_fingerprint=fingerprint)
                if action == "publish":
                    if issues:
                        raise AdminStoreError(issues[0]["code"], 409, issues[0]["message"])
                    current = replace(
                        current,
                        published_json=previous.draft_json,
                        published_revision=current.revision,
                        published_source_fingerprint=fingerprint,
                    )
                else:
                    if not has_contract or (not closed and not can_reopen):
                        raise AdminStoreError(
                            "CHALLENGE_NOT_OPEN",
                            409,
                            "The existing evaluation contract and runtime do not allow admissions",
                        )
                    current = replace(current, admissions_closed=closed)
            tx.execute(
                "INSERT INTO challenge_admin_state(challenge_id, source_fingerprint, revision, "
                "draft_json, published_json, published_revision, published_source_fingerprint, "
                "admissions_closed) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(challenge_id) DO UPDATE SET "
                "source_fingerprint = excluded.source_fingerprint, "
                "revision = excluded.revision, draft_json = excluded.draft_json, "
                "published_json = excluded.published_json, "
                "published_revision = excluded.published_revision, "
                "published_source_fingerprint = excluded.published_source_fingerprint, "
                "admissions_closed = excluded.admissions_closed",
                (
                    current.challenge_id,
                    current.source_fingerprint,
                    current.revision,
                    current.draft_json,
                    current.published_json,
                    current.published_revision,
                    current.published_source_fingerprint,
                    int(current.admissions_closed),
                ),
            )
            tx.execute(
                "INSERT INTO challenge_admin_revisions(challenge_id, revision, action, "
                "operator_user_id, created_at, previous_json, next_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    challenge_id,
                    current.revision,
                    action,
                    operator,
                    datetime.fromtimestamp(tx.now, UTC).isoformat(timespec="microseconds"),
                    _json(asdict(previous)),
                    _json(asdict(current)),
                ),
            )
            return current
