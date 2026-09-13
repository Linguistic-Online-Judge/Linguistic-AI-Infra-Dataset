"""Single-host executor ownership and write-ahead model-request recovery barriers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from .auth_config import _read_protected
from .providers import OpenAICompatibleProvider, ProviderContractError, ProviderRequestError

STATE_VERSION = "qwen-serial-executor-state-v1"


class ExecutorBusy(RuntimeError):
    pass


class RecoveryRequired(RuntimeError):
    pass


def _now():
    return datetime.now(UTC).isoformat()


def _sync_directory(directory: Path):
    # Linux is the deployment target; Windows supports process-crash tests, not power-loss proof.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_atomic(path: Path, document: dict):
    payload = (json.dumps(document, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".executor-", delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _check_directory(directory: Path):
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ValueError("executor state must not use symbolic links")
    metadata = directory.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("executor state must be a directory")
    if os.name != "nt" and (metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()):
        raise ValueError("executor state must be private and owned by the service user")


@contextmanager
def executor_lock(directory: Path, *, create=False):
    """Hold for the complete executor/recovery lifetime; never unlink the lock inode."""
    _check_directory(directory)
    path = directory / ".executor.lock"
    if path.is_symlink():
        raise ValueError("executor lock must be a regular file")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | (os.O_CREAT if create else 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("executor lock must be a private regular file")
        if os.name != "nt" and (metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()):
            raise ValueError("executor lock must be private and owned by the service user")
        if not metadata.st_size:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ExecutorBusy("executor state is already in use") from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)


def read_state(directory: Path):
    _check_directory(directory)
    state = json.loads(_read_protected(directory / "state.json"))
    if (not isinstance(state, dict) or set(state) != {
        "version", "binding_sha256", "pending", "last_recovery"
    } or state["version"] != STATE_VERSION
            or not isinstance(state["binding_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", state["binding_sha256"]) is None):
        raise ValueError("invalid executor state")
    pending = state["pending"]
    if pending is not None:
        if (not isinstance(pending, dict) or set(pending) != {
            "operation_id", "challenge_id", "started_at"
        } or not isinstance(pending["operation_id"], str)
                or re.fullmatch(r"[0-9a-f]{32}", pending["operation_id"]) is None
                or not isinstance(pending["challenge_id"], str)
                or not pending["challenge_id"]
                or not isinstance(pending["started_at"], str)):
            raise ValueError("invalid pending executor operation")
    last = state["last_recovery"]
    if last is not None and (
        not isinstance(last, str) or re.fullmatch(r"[0-9a-f]{32}", last) is None
    ):
        raise ValueError("invalid executor recovery reference")
    if last is not None:
        _check_directory(directory / "recoveries")
        audit = json.loads(_read_protected(directory / "recoveries" / (last + ".json")))
        if (not isinstance(audit, dict) or audit.get("binding_sha256") != state["binding_sha256"]
                or not isinstance(audit.get("pending"), dict)
                or audit["pending"].get("operation_id") != last):
            raise ValueError("executor recovery audit does not match state")
    return state


class ExecutorState:
    """Caller must hold executor_lock throughout this object's use."""

    def __init__(self, directory: Path, binding_sha256: str):
        self.directory = directory
        self.binding_sha256 = binding_sha256
        self.poisoned = False
        self._begin_lock = Lock()
        self.snapshot()

    @classmethod
    def initialize(cls, directory: Path, binding_sha256: str):
        if re.fullmatch(r"[0-9a-f]{64}", binding_sha256) is None:
            raise ValueError("invalid executor binding")
        if any(path.name != ".executor.lock" for path in directory.iterdir()):
            raise ValueError("initialization requires an empty dedicated state directory")
        _write_atomic(directory / "state.json", {
            "version": STATE_VERSION, "binding_sha256": binding_sha256,
            "pending": None, "last_recovery": None,
        })
        return cls(directory, binding_sha256)

    def snapshot(self):
        state = read_state(self.directory)
        if state["binding_sha256"] != self.binding_sha256:
            raise ValueError("executor binding changed; use the bound deployment configuration")
        return state

    def require_clean(self):
        if self.poisoned or self.snapshot()["pending"] is not None:
            raise RecoveryRequired("prior model request requires recorded termination confirmation")

    def begin(self, challenge_id: str):
        with self._begin_lock:
            return self._begin(challenge_id)

    def _begin(self, challenge_id: str):
        self.require_clean()
        state = self.snapshot()
        operation = {"operation_id": uuid.uuid4().hex, "challenge_id": challenge_id,
                     "started_at": _now()}
        # Any failed write blocks this process, even if the previous file still looks clean.
        self.poisoned = True
        _write_atomic(self.directory / "state.json", {**state, "pending": operation})
        self.poisoned = False
        return operation["operation_id"]

    def finish(self, operation_id: str):
        state = self.snapshot()
        if state["pending"] is None or state["pending"]["operation_id"] != operation_id:
            self.poisoned = True
            raise RecoveryRequired("executor operation changed before completion")
        self.poisoned = True
        _write_atomic(self.directory / "state.json", {**state, "pending": None})
        self.poisoned = False

    def recover(self, expected_operation_id: str, evidence_file: Path):
        """Record an operator attestation; never infer termination from elapsed time or health."""
        state = self.snapshot()
        pending = state["pending"]
        if pending is None or pending["operation_id"] != expected_operation_id:
            raise ValueError("recovery operation does not match pending state")
        raw = _read_protected(evidence_file)
        evidence = json.loads(raw)
        if (not isinstance(evidence, dict) or set(evidence) != {
            "operation_id", "binding_sha256", "prior_request_terminated", "confirmed_by",
            "evidence_reference"
        } or evidence["operation_id"] != expected_operation_id
                or evidence["binding_sha256"] != self.binding_sha256
                or evidence["prior_request_terminated"] is not True
                or any(not isinstance(evidence[key], str) or not evidence[key].strip()
                       or len(evidence[key]) > 1024
                       for key in ("confirmed_by", "evidence_reference"))):
            raise ValueError("explicit matching termination evidence is required")
        archive = self.directory / "recoveries"
        if not archive.exists():
            archive.mkdir(mode=0o700)
            _sync_directory(self.directory)
        _check_directory(archive)
        audit_path = archive / (expected_operation_id + ".json")
        evidence_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if audit_path.exists():
            prior = json.loads(_read_protected(audit_path))
            if (prior.get("pending") != pending or prior.get("evidence_sha256") != evidence_hash
                    or prior.get("binding_sha256") != self.binding_sha256
                    or prior.get("evidence") != evidence):
                raise ValueError("recovery audit already exists with different evidence")
        else:
            _write_atomic(audit_path, {"pending": pending, "binding_sha256": self.binding_sha256,
                                      "evidence": evidence, "evidence_sha256": evidence_hash,
                                      "recorded_at": _now()})
        # Archive is durable before clearing; a crash here can resume only the same evidence.
        self.poisoned = True
        _write_atomic(self.directory / "state.json", {
            **state, "pending": None, "last_recovery": expected_operation_id,
        })
        self.poisoned = False


class GuardedQwenProvider(OpenAICompatibleProvider):
    """Write ahead of generate(), so neither thread-start nor process-exit races bypass it."""

    def __init__(self, *, executor_state: ExecutorState, challenge_id: str, **kwargs):
        super().__init__(**kwargs)
        self._executor_state = executor_state
        self._challenge_id = challenge_id

    def generate(self, request, /, *, timeout_seconds=None):
        operation_id = self._executor_state.begin(self._challenge_id)
        try:
            result = super().generate(request, timeout_seconds=timeout_seconds)
        except (ProviderRequestError, ProviderContractError) as error:
            confirmed = (not isinstance(error, ProviderRequestError)
                         or error.termination_confirmed)
            if confirmed and not self.has_active_request:
                self._executor_state.finish(operation_id)
            raise
        # Unexpected exceptions, signals and ambiguous transport errors retain the barrier.
        if self.has_active_request:
            raise RecoveryRequired("model request is still active after completion")
        self._executor_state.finish(operation_id)
        return result
