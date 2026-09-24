import json
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from linguistic_oj.local_dev import LOCAL_PASSWORD, _state_lock, build_local_app, main

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8767"
HEADERS = {"Origin": ORIGIN, "X-LOJ-CSRF": "1"}


@pytest.fixture
def project(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/mvp_evaluation.json").write_bytes(
        (ROOT / "config/mvp_evaluation.json").read_bytes()
    )
    return tmp_path


def test_local_full_business_flow_and_restart(project):
    app = build_local_app(project, port=8767)
    with TestClient(app, base_url=ORIGIN) as alice, TestClient(
        app, base_url=ORIGIN
    ) as bob:
        assert alice.get("/").status_code == 200
        assert alice.get("/health/ready").status_code == 200
        assert alice.get("/v1/development").status_code == 403
        development = alice.get(
            "/v1/development", headers={"X-LOJ-Development": "1"}
        )
        assert development.json()["evaluation_mode"] == "mock"
        assert development.headers["cache-control"] == "no-store"
        alice_user_id = None
        for client, name in ((alice, "alice"), (bob, "bob")):
            response = client.post("/v1/auth/login", headers=HEADERS, json={
                "email": f"{name}@example.test", "password": LOCAL_PASSWORD,
            })
            assert response.status_code == 200
            assert response.json()["user"]["role"] == "user"
            if name == "alice":
                alice_user_id = response.json()["user"]["user_id"]
        challenges = alice.get("/v1/challenges").json()
        assert len(challenges) == 5
        assert all(item["model_identity"]["runtime"] == "mock" for item in challenges)
        for index, challenge in enumerate(challenges):
            payload = {"challenge_id": challenge["challenge_id"], "student_prompt": "Return JSON."}
            headers = {
                **HEADERS, "Idempotency-Key": f"run-{index}",
                "X-LOJ-Expected-User": alice_user_id,
            }
            created = alice.post("/v1/submissions", headers=headers, json=payload)
            assert created.status_code == 202, created.text
            submission_id = created.json()["submission_id"]
            replay = alice.post("/v1/submissions", headers=headers, json=payload)
            assert replay.json()["submission_id"] == submission_id
            assert bob.get(f"/v1/submissions/{submission_id}").status_code == 404
            assert bob.get(f"/v1/submissions/{submission_id}/result").status_code == 404
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = alice.get(f"/v1/submissions/{submission_id}/result")
                if result.status_code == 200:
                    break
                time.sleep(0.05)
            assert result.status_code == 200, result.text
            assert result.json()["outcome"] == "succeeded"
            assert result.json()["model_identity"]["runtime"] == "mock"
            board = alice.get(
                "/v1/leaderboards/" + challenge["evaluation_identity_sha256"]
            ).json()
            assert board["items"][0]["public_handle"] == "LocalAlice"
        assert len(alice.get("/v1/submissions").json()["items"]) == 5
        assert bob.get("/v1/submissions").json()["items"] == []
        saved_cookies = dict(alice.cookies)
    restarted = build_local_app(project, port=8767)
    with TestClient(restarted, base_url=ORIGIN) as client:
        client.cookies.update(saved_cookies)
        assert len(client.get("/v1/submissions").json()["items"]) == 5


def test_local_signup_mail_reset_and_host_boundaries(project):
    app = build_local_app(project, port=8767)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/", headers={"Host": "malicious.example"}).status_code == 403
        assert client.post("/v1/auth/login", json={}).status_code == 403
        assert client.get("/v1/development", headers={
            "X-LOJ-Development": "1", "Sec-Fetch-Site": "cross-site",
        }).status_code == 403
        email = "learner@example.test"
        assert client.post(
            "/v1/auth/register", headers=HEADERS, json={"email": email}
        ).status_code == 202
        mailbox = client.get(
            "/v1/development/mail", headers={"X-LOJ-Development": "1"}
        ).json()["items"]
        assert mailbox[0]["recipient"] == email
        token = mailbox[0]["link"].split("token=", 1)[1]
        assert client.post("/v1/auth/verify-email", headers=HEADERS, json={
            "token": token, "password": LOCAL_PASSWORD, "public_handle": "NewLearner",
        }).status_code == 200
        assert client.post("/v1/auth/login", headers=HEADERS, json={
            "email": email, "password": LOCAL_PASSWORD,
        }).status_code == 200
        assert client.post("/v1/auth/logout", headers=HEADERS, json={}).status_code == 200
        assert client.get("/v1/submissions").status_code == 401


def test_local_state_cannot_overwrite_other_data(project):
    with pytest.raises(ValueError, match="dedicated directory"):
        build_local_app(project, state_dir=project / "runtime")
    path = project / "runtime/other"
    path.mkdir(parents=True)
    (path / "important.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="unmarked"):
        build_local_app(project, state_dir=path)
    assert (path / "important.txt").read_text(encoding="utf-8") == "keep"
    app = build_local_app(project)
    fixture = app.state.local_state_directory / "handwritten-fixtures-v1.jsonl"
    fixture.write_text(json.dumps({"changed": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        build_local_app(project)


def test_local_state_has_an_exclusive_process_lock(project):
    path = project / "runtime/locked"
    with _state_lock(path), pytest.raises(RuntimeError, match="already in use"), _state_lock(path):
        pass


def test_occupied_port_does_not_stop_another_process(project, capsys):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert main(["--root", str(project), "--port", str(port)]) == 1
        assert "port" in capsys.readouterr().err
        assert not (project / "runtime").exists()
