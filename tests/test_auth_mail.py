import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import linguistic_oj.auth_mail as mail_module
from linguistic_oj.api import APIError
from linguistic_oj.auth import AuthService, install_auth_routes
from linguistic_oj.auth_mail import AuthMailQueue, MailQueueUnavailable
from linguistic_oj.submission_store import SubmissionStore

ORIGIN = "https://judge.example.test"
HEADERS = {"Origin": ORIGIN, "X-LOJ-CSRF": "1"}


@pytest.fixture
def service(tmp_path):
    store = SubmissionStore(tmp_path / "mail.db")
    local = AuthService(
        store, public_origin="http://localhost:8080", development=True, mailer=lambda *args: None
    )
    local.provision_development_account("known@example.test", "long unique password", "known")
    service = AuthService(store, public_origin=ORIGIN, mailer=lambda *args: None)
    yield service
    service.close()


def test_uniform_acceptance_despite_eligibility_or_delivery_failure(service, caplog):
    calls = []
    entered, release = threading.Event(), threading.Event()

    def broken(recipient, purpose, link):
        calls.append((recipient, purpose, link))
        entered.set()
        assert release.wait(5)
        raise RuntimeError(f"secret SMTP password: {recipient} {link}")

    service.mailer = broken
    app = FastAPI()
    install_auth_routes(app, service)
    checked = []
    original = service.store.auth_issue_action

    def check_eligibility(*args):
        checked.append(args[:2])
        return original(*args)

    service.store.auth_issue_action = check_eligibility
    with (
        caplog.at_level(logging.ERROR),
        TestClient(app, base_url=ORIGIN, headers=HEADERS) as client,
    ):
        with ThreadPoolExecutor(1) as pool:
            try:
                # A blocked adapter must not hold up the HTTP response, even for an eligible email.
                future = pool.submit(
                    client.post, "/v1/auth/register", json={"email": "new@example.test"}
                )
                assert entered.wait(5)
                response = future.result(timeout=2)
                assert response.status_code == 202
                for path, email in (
                    ("register", "known@example.test"),
                    ("password-reset/request", "known@example.test"),
                    ("password-reset/request", "missing@example.test"),
                ):
                    result = client.post("/v1/auth/" + path, json={"email": email})
                    assert result.status_code == 202
                    assert result.json() == response.json() == {"status": "accepted"}
                # Noneligible jobs are queued too; no eligibility queries happen in these callers.
                assert checked == [("new@example.test", "verify-email")]
            finally:
                release.set()
        assert service._mail_queue.wait_for_idle(5)
        assert checked == [
            ("new@example.test", "verify-email"),
            ("known@example.test", "verify-email"),
            ("known@example.test", "reset-password"),
            ("missing@example.test", "reset-password"),
        ]
        assert len(calls) == 2
        worker = service._mail_queue._worker
    assert not worker.is_alive()
    assert service._mail_queue._worker is None
    assert "auth_mail_delivery_failed" in caplog.text
    for recipient, _, link in calls:
        assert recipient not in caplog.text and link not in caplog.text
    assert "secret SMTP password" not in caplog.text
    assert "Traceback" not in caplog.text


def test_full_queue_returns_same_503_for_known_unknown_and_never_checks_eligibility(service):
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(5)

    service._mail_queue = AuthMailQueue(blocked, capacity=1)
    app = FastAPI()
    install_auth_routes(app, service)
    with TestClient(app, base_url=ORIGIN, headers=HEADERS) as client:
        try:
            assert (
                client.post("/v1/auth/register", json={"email": "active@example.test"}).status_code
                == 202
            )
            assert entered.wait(5)
            assert (
                client.post("/v1/auth/register", json={"email": "queued@example.test"}).status_code
                == 202
            )
            for email in ("known@example.test", "unknown@example.test"):
                result = client.post("/v1/auth/register", json={"email": email})
                assert result.status_code == 503
                assert result.json()["error"]["code"] == "SERVICE_NOT_READY"
                assert result.headers["cache-control"] == "no-store"
            assert service.store.auth_action_email("unused", "verify-email") is None
        finally:
            release.set()
        assert service._mail_queue.wait_for_idle(5)


def test_worker_lifecycle_wraps_existing_lifespan_and_closes_on_startup_failure(service):
    seen = []

    @asynccontextmanager
    async def lifespan(app):
        worker = service._mail_queue._worker
        seen.append(worker)
        assert worker.is_alive()
        raise RuntimeError("existing startup failed")
        yield  # pragma: no cover

    app = FastAPI(lifespan=lifespan)
    install_auth_routes(app, service)
    with pytest.raises(RuntimeError, match="existing startup failed"), TestClient(app):
        pass
    assert len(seen) == 1 and not seen[0].is_alive()
    assert service._mail_queue._worker is None


def test_unstarted_and_stopped_queue_reject_work_and_clean_start_can_repeat():
    received = []
    queue = AuthMailQueue(lambda *args: received.append(args))
    with pytest.raises(MailQueueUnavailable):
        queue.submit("email@example.test", "verify-email")
    for _ in range(2):
        queue.start()
        worker = queue._worker
        queue.submit("email@example.test", "verify-email")
        assert queue.wait_for_idle(5)
        queue.stop()
        assert not worker.is_alive()
        with pytest.raises(MailQueueUnavailable):
            queue.submit("email@example.test", "verify-email")
    assert len(received) == 2


def test_shutdown_discards_pending_and_does_not_spawn_replacement_for_stuck_worker(monkeypatch):
    monkeypatch.setattr(mail_module, "MAIL_SHUTDOWN_SECONDS", 0.01)
    entered, release = threading.Event(), threading.Event()
    received = []

    def blocked(email, purpose):
        received.append(email)
        entered.set()
        assert release.wait(5)

    queue = AuthMailQueue(blocked, capacity=1)
    queue.start()
    worker = queue._worker
    try:
        queue.submit("active@example.test", "verify-email")
        assert entered.wait(5)
        queue.submit("pending@example.test", "verify-email")
        with pytest.raises(MailQueueUnavailable, match="shutdown deadline"):
            queue.stop()
        with pytest.raises(MailQueueUnavailable):
            queue.start()
        with pytest.raises(MailQueueUnavailable):
            queue.submit("later@example.test", "verify-email")
        assert queue._pending == mail_module.deque()
    finally:
        release.set()
        worker.join(timeout=5)
        queue.stop()
    assert not worker.is_alive()
    assert received == ["active@example.test"]


def test_rate_limit_runs_before_queue_submission(service):
    from starlette.requests import Request

    service.store.auth_throttle = lambda *args, **kwargs: False
    request = Request({"type": "http", "headers": [], "client": ("192.0.2.1", 1234)})
    with pytest.raises(APIError) as error:
        service.request_link(request, "known@example.test", "verify-email")
    # The queue is deliberately not started: 429, rather than its unavailable 503.
    assert error.value.code == "AUTH_RATE_LIMITED"


def test_explicit_development_mail_capture_is_still_synchronous(tmp_path):
    from starlette.requests import Request

    received = []
    service = AuthService(
        SubmissionStore(tmp_path / "local.db"),
        development=True,
        public_origin="http://localhost:8080",
        mailer=lambda *args: received.append(args),
    )
    service.request_link(
        Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)}),
        "local@example.test",
        "verify-email",
    )
    assert len(received) == 1
    assert service._mail_queue is None
