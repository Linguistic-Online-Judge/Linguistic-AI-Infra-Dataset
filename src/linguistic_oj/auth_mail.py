"""Bounded, process-local mail work. Acceptance is not a durable delivery guarantee."""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable

_LOGGER = logging.getLogger("linguistic_oj.auth_mail")
MAIL_QUEUE_CAPACITY = 64
MAIL_SHUTDOWN_SECONDS = 30


class MailQueueUnavailable(RuntimeError):
    pass


class AuthMailQueue:
    def __init__(self, deliver: Callable[[str, str], None], *, capacity: int = MAIL_QUEUE_CAPACITY):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("mail queue capacity must be positive")
        self._deliver = deliver
        self._capacity = capacity
        self._pending: deque[tuple[str, str]] = deque()
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._stopping = True
        self._active = False

    def start(self) -> None:
        with self._condition:
            if self._worker is not None:
                if self._stopping or not self._worker.is_alive():
                    raise MailQueueUnavailable("mail worker is unavailable")
                return
            self._stopping = False
            self._worker = threading.Thread(target=self._run, name="loj-auth-mail", daemon=True)
            try:
                self._worker.start()
            except Exception:
                self._worker = None
                self._stopping = True
                raise MailQueueUnavailable("mail worker could not start") from None

    def submit(self, email: str, purpose: str) -> None:
        with self._condition:
            if (
                self._worker is None
                or not self._worker.is_alive()
                or self._stopping
                or len(self._pending) >= self._capacity
            ):
                raise MailQueueUnavailable("mail queue is unavailable")
            # Queue only normalized inputs, never raw links, passwords, or tokens.
            self._pending.append((email, purpose))
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._stopping)
                if self._stopping:
                    return
                job = self._pending.popleft()
                self._active = True
            try:
                self._deliver(*job)
            except Exception:
                # No exception text, traceback, addresses, or link data in logs.
                _LOGGER.error("auth_mail_delivery_failed")
            finally:
                job = None
                with self._condition:
                    self._active = False
                    self._condition.notify_all()

    def wait_for_idle(self, timeout: float) -> bool:
        """Synchronization for trusted callers/tests, never part of an HTTP request."""
        with self._condition:
            return self._condition.wait_for(lambda: not self._pending and not self._active, timeout)

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            if self._pending:
                _LOGGER.warning("auth_mail_pending_discarded_on_shutdown")
            self._pending.clear()
            worker = self._worker
            self._condition.notify_all()
        if worker is not None:
            # A synchronous adapter cannot be forcibly cancelled. Wait only for the in-flight job;
            # on timeout retain the worker reference and reject restart rather than leaking more.
            worker.join(timeout=MAIL_SHUTDOWN_SECONDS)
            if worker.is_alive():
                raise MailQueueUnavailable("mail worker did not stop within the shutdown deadline")
            with self._condition:
                if self._worker is worker:
                    self._worker = None
