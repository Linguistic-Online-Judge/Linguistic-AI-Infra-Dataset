import json
import os
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest

import linguistic_oj.auth_config as config_module
from linguistic_oj.auth_config import SMTPMailer, build_auth_service
from linguistic_oj.submission_store import SubmissionStore


@pytest.fixture
def protected_config(tmp_path, monkeypatch):
    config = {
        "public_origin": "https://judge.example.test",
        "smtp": {
            "host": "smtp.example.test",
            "port": 465,
            "sender": "judge@example.test",
            "username": "judge",
            "password_file": "smtp-password",
            "security": "ssl",
        },
    }
    path = tmp_path / "auth.json"
    password_path = tmp_path / "smtp-password"
    path.write_text(json.dumps(config), encoding="utf-8")
    password_path.write_text("private-smtp-password\n", encoding="utf-8")
    path.chmod(0o600)
    password_path.chmod(0o600)
    if os.name == "nt":
        # Windows ACL validation has separate tests; test temp dirs inherit the test runner's ACL.
        monkeypatch.setattr(config_module, "_check_windows_acl", lambda path: None)
    return SubmissionStore(tmp_path / "auth.db"), path, config


def test_build_production_service_uses_same_store_and_never_local_delivery(protected_config):
    store, path, _ = protected_config
    service = build_auth_service(store, path)
    assert service.store is store
    assert service.public_origin == "https://judge.example.test"
    assert service.development is False
    assert service.cookie_name == "__Host-loj_session"
    assert isinstance(service.mailer, SMTPMailer)
    assert service.mailer._password == "private-smtp-password"
    assert service.trusted_proxies == frozenset()


def test_protected_config_accepts_only_explicit_proxy_ip_addresses(protected_config):
    import ipaddress

    store, path, config = protected_config
    config["trusted_proxies"] = ["127.0.0.1", "::1"]
    path.write_text(json.dumps(config), encoding="utf-8")
    service = build_auth_service(store, path)
    assert service.trusted_proxies == frozenset(map(ipaddress.ip_address, ["127.0.0.1", "::1"]))


@pytest.mark.parametrize(
    "proxies",
    [
        "127.0.0.1",
        None,
        ["127.0.0.0/8"],
        ["localhost"],
        ["fe80::1%eth0"],
        [123],
        ["127.0.0.1,192.0.2.1"],
        [" 127.0.0.1"],
        ["*"],
    ],
)
def test_protected_config_rejects_ambiguous_proxy_trust(protected_config, proxies):
    store, path, config = protected_config
    config["trusted_proxies"] = proxies
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or unprotected"):
        build_auth_service(store, path)


@pytest.mark.parametrize(
    "change",
    [
        {"public_origin": "http://127.0.0.1:8080"},
        {"public_origin": "https://example.test/path"},
        {"development": True},
        {"smtp": {}},
        {"smtp": {"security": "none"}},
    ],
)
def test_invalid_or_development_production_config_rejected(protected_config, change):
    store, path, config = protected_config
    config.update(change)
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or unprotected") as error:
        build_auth_service(store, path)
    assert "private-smtp-password" not in str(error.value)


def test_unprotected_config_and_password_are_rejected(protected_config, monkeypatch):
    store, path, _ = protected_config
    for target in (path, path.parent / "smtp-password"):
        if os.name == "nt":

            def check(candidate, target=target):
                if candidate == target:
                    raise ValueError("unprotected")

            monkeypatch.setattr(config_module, "_check_windows_acl", check)
        else:
            target.chmod(0o644)
        with pytest.raises(ValueError, match="unprotected"):
            build_auth_service(store, path)
        if os.name != "nt":
            target.chmod(0o600)


@pytest.mark.parametrize(
    "sid,allowed",
    [
        ("S-1-5-21-current", True),
        ("S-1-5-18", True),
        ("S-1-5-32-544", True),
        ("S-1-1-0", False),
        ("S-1-5-32-545", False),
    ],
)
def test_windows_acl_checks_sids_and_fails_closed(monkeypatch, sid, allowed):
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setattr(
        config_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "me": "S-1-5-21-current",
                    "owner": "S-1-5-21-current",
                    "rules": [{"sid": sid, "allow": True}],
                }
            ),
        ),
    )
    if allowed:
        config_module._check_windows_acl(Path("private.json"))
    else:
        with pytest.raises(ValueError, match="private"):
            config_module._check_windows_acl(Path("private.json"))


@pytest.mark.parametrize("security", ["ssl", "starttls"])
def test_smtp_enforces_verified_tls_before_login_and_sends_fragment_link(monkeypatch, security):
    events = []

    class Client:
        def __init__(self, host, port, **kwargs):
            events.append("connect")
            assert host == "smtp.example.test"
            assert kwargs["timeout"] == 10
            if security == "ssl":
                assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
                assert kwargs["context"].check_hostname
                events.append("tls")

        def ehlo(self):
            events.append("ehlo")

        def starttls(self, *, context):
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            events.append("tls")

        def login(self, username, password):
            assert "tls" in events
            assert password == "private-password"
            events.append("login")

        def send_message(self, message):
            assert message["To"] == "alice@example.test"
            assert "#verify-email?token=secret" in message.get_content()
            events.append("send")
            return {}

        def noop(self):
            return 250, b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("close")

    monkeypatch.setattr(config_module.smtplib, "SMTP_SSL", Client)
    monkeypatch.setattr(config_module.smtplib, "SMTP", Client)
    mailer = SMTPMailer(
        host="smtp.example.test",
        port=465 if security == "ssl" else 587,
        sender="judge@example.test",
        username="judge",
        password="private-password",
        security=security,
    )
    mailer("alice@example.test", "verify-email", "https://judge.test/#verify-email?token=secret")
    assert events.index("tls") < events.index("login") < events.index("send")
    mailer.health_check()


def test_starttls_failure_never_authenticates_or_falls_back(monkeypatch):
    events = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def ehlo(self):
            pass

        def starttls(self, **kwargs):
            raise RuntimeError("private detail")

        def login(self, *args):
            pytest.fail("must not authenticate before TLS")

        def close(self):
            events.append("closed")

    monkeypatch.setattr(config_module.smtplib, "SMTP", Client)
    mailer = SMTPMailer(
        host="smtp.test",
        port=587,
        sender="judge@example.test",
        username="judge",
        password="private-password",
        security="starttls",
    )
    with pytest.raises(RuntimeError, match="mail delivery unavailable") as error:
        mailer.health_check()
    assert "private" not in str(error.value)
    assert events == ["closed"]
