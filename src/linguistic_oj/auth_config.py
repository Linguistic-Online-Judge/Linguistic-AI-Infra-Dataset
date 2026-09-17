"""Protected production configuration and certificate-verified SMTP delivery."""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import stat
import subprocess
from email.message import EmailMessage
from pathlib import Path

from .auth import AuthService, normalize_email, validate_public_origin
from .auth_store import AuthStoreMixin


def _check_windows_acl(path: Path) -> None:
    # Resolve identities as SIDs, not localized account names. Fail closed if ACL inspection fails.
    script = (
        "$ErrorActionPreference='Stop'; "
        "$acl=Get-Acl -LiteralPath $env:LOJ_AUTH_SECRET_PATH; "
        "$me=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "$owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value; "
        "$rules=@($acl.GetAccessRules($true,$true,"
        "[System.Security.Principal.SecurityIdentifier]) | ForEach-Object { "
        "@{sid=$_.IdentityReference.Value; allow=($_.AccessControlType -eq 'Allow')} }); "
        "@{me=$me; owner=$owner; rules=$rules} | ConvertTo-Json -Compress -Depth 4"
    )
    result = subprocess.run(
        [
            str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        env={**os.environ, "LOJ_AUTH_SECRET_PATH": str(path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    acl = json.loads(result.stdout)
    permitted = {acl["me"], "S-1-5-18", "S-1-5-32-544"}
    if acl["owner"] not in permitted or any(
        rule["allow"] and rule["sid"] not in permitted for rule in acl["rules"]
    ):
        raise ValueError("private file permissions required")


def _read_protected(path: Path, *, max_bytes: int = 16384) -> str:
    try:
        if type(max_bytes) is not int or not 1 <= max_bytes <= 8388608:
            raise ValueError
        if path.is_symlink():
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                raise ValueError
            if os.name == "nt":
                _check_windows_acl(path)
                if not os.path.samestat(metadata, path.stat()):
                    raise ValueError
            elif metadata.st_mode & 0o077 or metadata.st_uid not in {0, os.getuid()}:
                raise ValueError
            raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError
            return raw.decode("utf-8")
    except Exception:
        raise ValueError(
            "auth configuration and password files must be private regular files"
        ) from None


class SMTPMailer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str,
        password: str,
        security: str,
    ) -> None:
        if (
            not isinstance(host, str)
            or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host)
            or type(port) is not int
            or not 1 <= port <= 65535
            or security not in {"ssl", "starttls"}
            or not isinstance(username, str)
            or not username
            or len(username) > 320
            or any(ord(char) < 32 for char in username)
            or not isinstance(password, str)
            or not password
            or len(password) > 4096
        ):
            raise ValueError("invalid SMTP configuration")
        self.host = host
        self.port = port
        self.sender = normalize_email(sender)
        self.username = username
        self._password = password
        self.security = security

    def _connect(self):
        context = ssl.create_default_context()
        client = None
        try:
            if self.security == "ssl":
                client = smtplib.SMTP_SSL(self.host, self.port, timeout=10, context=context)
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=10)
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(self.username, self._password)
            return client
        except Exception:
            if client is not None:
                client.close()
            raise RuntimeError("mail delivery unavailable") from None

    def health_check(self) -> None:
        try:
            with self._connect() as client:
                if client.noop()[0] != 250:
                    raise RuntimeError
        except Exception:
            raise RuntimeError("mail delivery unavailable") from None

    def __call__(self, recipient: str, purpose: str, link: str) -> None:
        if purpose not in {"verify-email", "reset-password"}:
            raise ValueError("unsupported mail purpose")
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = normalize_email(recipient)
        message["Subject"] = (
            "Verify your Linguistic Online Judge email"
            if purpose == "verify-email"
            else "Reset your Linguistic Online Judge password"
        )
        minutes = 30 if purpose == "verify-email" else 15
        action = "verify your email" if purpose == "verify-email" else "reset your password"
        message.set_content(
            f"Open this link to {action}:\n\n"
            f"{link}\n\nThis link expires in {minutes} minutes and can be used once.\n"
            "If you did not request this, you can ignore this message.\n"
        )
        try:
            with self._connect() as client:
                refused = client.send_message(message)
                if refused:
                    raise RuntimeError
        except Exception:
            raise RuntimeError("mail delivery unavailable") from None


def build_auth_service(store: AuthStoreMixin, config_path: Path) -> AuthService:
    try:
        config = json.loads(_read_protected(config_path))
        if (
            not isinstance(config, dict)
            or not {"public_origin", "smtp"} <= set(config)
            or set(config) - {"public_origin", "smtp", "trusted_proxies"}
        ):
            raise ValueError
        origin = validate_public_origin(config["public_origin"])
        smtp = config["smtp"]
        if not isinstance(smtp, dict) or set(smtp) != {
            "host",
            "port",
            "sender",
            "username",
            "password_file",
            "security",
        }:
            raise ValueError
        password_file = Path(smtp["password_file"])
        if not password_file.is_absolute():
            password_file = config_path.parent / password_file
        password = _read_protected(password_file).removesuffix("\n").removesuffix("\r")
        mailer = SMTPMailer(
            **{key: value for key, value in smtp.items() if key != "password_file"},
            password=password,
        )
        return AuthService(
            store,
            public_origin=origin,
            mailer=mailer,
            trusted_proxies=config.get("trusted_proxies", ()),
        )
    except Exception:
        raise ValueError("invalid or unprotected production authentication configuration") from None
