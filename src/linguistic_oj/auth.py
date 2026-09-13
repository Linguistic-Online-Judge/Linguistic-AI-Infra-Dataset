"""Email-first accounts and cookie-only authentication for the same-origin browser."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import threading
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from email_validator import EmailNotValidError, validate_email
from starlette.requests import Request

from .api import APIError, AuthenticationError, Principal
from .auth_mail import AuthMailQueue
from .auth_store import Account, AuthConflictError, AuthStoreMixin

SESSION_SECONDS = 8 * 60 * 60
VERIFY_SECONDS = 30 * 60
RESET_SECONDS = 15 * 60
PASSWORD_MIN_CHARACTERS = 8
PASSWORD_MAX_CHARACTERS = 128
PASSWORD_MAX_UTF8_BYTES = 512
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# OWASP Argon2id minimum: 19 MiB, two iterations, one lane. Bound process-wide memory.
_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)
_HASH_SLOTS = threading.BoundedSemaphore(4)
_DUMMY_HASH = _HASHER.hash(secrets.token_urlsafe(32))


def auth_error(code: str, status_code: int = 422) -> APIError:
    messages = {
        "AUTH_INVALID_CREDENTIALS": "Email or password is incorrect",
        "AUTH_INVALID_TOKEN": "Authentication link or session is invalid or expired",
        "AUTH_ACCOUNT_CHANGED": "Signed-in account changed. Refresh and try again",
        "AUTH_PROXY_REJECTED": "Trusted proxy client address is missing or invalid",
        "AUTH_RATE_LIMITED": "Too many attempts. Try again later",
        "AUTH_VALIDATION_ERROR": "Authentication request is invalid",
        "AUTH_CSRF_REJECTED": "Same-origin JSON request with X-LOJ-CSRF: 1 is required",
        "AUTH_HOST_REJECTED": "Local service requires its configured loopback host",
        "SERVICE_NOT_READY": "Service is not ready",
    }
    return APIError(
        status_code,
        code=code,
        message=messages[code],
        headers={"Retry-After": "900"} if code == "AUTH_RATE_LIMITED" else None,
    )


def validate_public_origin(value: str, *, development: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        scheme = "http" if development else "https"
        if (
            not isinstance(value, str)
            or not value.isascii()
            or parsed.scheme != scheme
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
            or (development and host not in {"localhost", "127.0.0.1"})
            or (port is not None and (port < 1 or port == (80 if development else 443)))
        ):
            raise ValueError
        canonical_host = f"[{host}]" if ":" in host else host
        try:
            ipaddress.ip_address(host)
        except ValueError:
            label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            if len(host) > 253 or re.fullmatch(rf"{label}(?:\.{label})*", host) is None:
                raise ValueError from None
        canonical = f"{scheme}://{canonical_host}" + (f":{port}" if port is not None else "")
        if value != canonical or host.endswith(".") or "\\" in host or "%" in host:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "public_origin must be a canonical root HTTPS origin (HTTP loopback in dev)"
        ) from None
    return value


def normalize_email(value: str) -> str:
    try:
        # No provider-specific plus-tag or dot rewriting; the whole address is casefolded.
        value = value.strip().casefold()
        if len(value.encode("utf-8")) > 254:
            raise ValueError
        result = validate_email(value, check_deliverability=False, test_environment=True)
        return result.normalized.casefold()
    except (EmailNotValidError, ValueError, UnicodeError, AttributeError):
        raise auth_error("AUTH_VALIDATION_ERROR") from None


def _validate_password(value: str) -> None:
    try:
        valid = (
            isinstance(value, str)
            and PASSWORD_MIN_CHARACTERS <= len(value) <= PASSWORD_MAX_CHARACTERS
            and len(value.encode("utf-8")) <= PASSWORD_MAX_UTF8_BYTES
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise auth_error("AUTH_VALIDATION_ERROR")


def _validate_handle(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= 32
        or "@" in value
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise auth_error("AUTH_VALIDATION_ERROR")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _ip_address(value: str):
    if not isinstance(value, str) or "%" in value:
        raise ValueError("an IP address without a scope is required")
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _hash_password(password: str, *, verify: str | None = None) -> str | bool:
    if not _HASH_SLOTS.acquire(blocking=False):
        raise auth_error("AUTH_RATE_LIMITED", 429)
    try:
        if verify is None:
            return _HASHER.hash(password)
        try:
            return _HASHER.verify(verify, password)
        except (VerificationError, InvalidHashError):
            return False
    finally:
        _HASH_SLOTS.release()


class AuthService:
    def __init__(
        self,
        store: AuthStoreMixin,
        *,
        public_origin: str,
        mailer: Callable[[str, str, str], None],
        development: bool = False,
        trusted_proxies: Sequence[str] = (),
        development_cookie_name: str = "loj_local_session",
    ) -> None:
        if type(development) is not bool or not callable(mailer):
            raise ValueError("explicit development flag and callable mailer are required")
        self.store = store
        self.public_origin = validate_public_origin(public_origin, development=development)
        self.mailer = mailer
        self.development = development
        if not re.fullmatch(r"loj_[a-z0-9_]{1,48}", development_cookie_name) or (
            not development and development_cookie_name != "loj_local_session"
        ):
            raise ValueError("custom cookie names are restricted to development")
        self.cookie_name = development_cookie_name if development else "__Host-loj_session"
        try:
            if not isinstance(trusted_proxies, (list, tuple)) or len(trusted_proxies) > 128:
                raise ValueError
            self.trusted_proxies = frozenset(_ip_address(value) for value in trusted_proxies)
        except ValueError:
            raise ValueError("trusted_proxies must contain only explicit IP addresses") from None
        self._mail_queue = None if development else AuthMailQueue(self._deliver_link)

    def start(self) -> None:
        if not self.development:
            self.store.auth_health_check()
            self.store.auth_require_bound_accounts()
            self._mail_queue.start()

    def close(self) -> None:
        if self._mail_queue is not None:
            self._mail_queue.stop()

    def health_check(self) -> None:
        self.store.auth_health_check()
        if not self.development:
            self.store.auth_require_bound_accounts()
        check = getattr(self.mailer, "health_check", None)
        if check is not None:
            check()

    def guard_host(self, request: Request) -> None:
        if self.development and (
            request.scope["scheme"] != "http"
            or request.headers.getlist("host") != [urlsplit(self.public_origin).netloc]
        ):
            raise auth_error("AUTH_HOST_REJECTED", 403)

    def cookie_token(self, request: Request) -> str | None:
        # SimpleCookie and Request.cookies silently pick a winner for duplicates.
        values = []
        for header in request.headers.getlist("cookie"):
            for part in header.split(";"):
                name, separator, value = part.strip().partition("=")
                name = name.strip()
                if name == self.cookie_name:
                    values.append(value if separator else "")
        if len(values) > 1:
            raise AuthenticationError
        if not values:
            return None
        if _TOKEN.fullmatch(values[0]) is None:
            raise AuthenticationError
        return values[0]

    def session(self, request: Request) -> tuple[Account, int]:
        self.guard_host(request)
        token = self.cookie_token(request)
        if token is None:
            raise AuthenticationError
        try:
            session = self.store.auth_session(_digest(token))
        except Exception:
            raise auth_error("SERVICE_NOT_READY", 503) from None
        if session is None:
            raise AuthenticationError
        return session

    def authenticate(self, request: Request) -> Principal:
        # Authorization headers never provide a fallback or override a browser account.
        account, _ = self.session(request)
        expected = request.headers.getlist("x-loj-expected-user")
        if expected and expected != [account.user_id]:
            raise auth_error("AUTH_ACCOUNT_CHANGED", 409)
        return Principal(account.subject)

    @staticmethod
    def session_payload(session: tuple[Account, int]) -> dict:
        account, expires_at = session
        return {
            "user": account.public(),
            "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
        }

    def client_address(self, request: Request):
        peer = request.client.host if request.client is not None else "unknown"
        try:
            address = _ip_address(peer)
        except ValueError:
            return None
        if address in self.trusted_proxies:
            values = request.headers.getlist("x-loj-client-ip")
            try:
                if len(values) != 1:
                    raise ValueError
                address = _ip_address(values[0])
            except ValueError:
                raise auth_error("AUTH_PROXY_REJECTED", 403) from None
        return address

    def _throttle(self, request: Request, operation: str, identity: str, *, mail=False) -> None:
        address = self.client_address(request)
        peer = "unknown"
        if address is not None:
            prefix = 24 if address.version == 4 else 64
            peer = str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
        # Forwarded and X-Forwarded-* remain ignored, even when a proxy is explicitly trusted.
        buckets = (
            ("peer:" + _digest(operation + ":" + peer), 30 if mail else 100),
            ("identity:" + _digest(operation + ":" + identity), 5 if mail else 10),
        )
        if not self.store.auth_throttle(buckets):
            raise auth_error("AUTH_RATE_LIMITED", 429)

    def request_link(self, request: Request, email: str, purpose: str) -> None:
        email = normalize_email(email)
        self._throttle(request, "mail:" + purpose, email, mail=True)
        try:
            if self._mail_queue is not None:
                # Eligibility, token issuance, and delivery all happen outside the HTTP response.
                self._mail_queue.submit(email, purpose)
            else:
                self._deliver_link(email, purpose)
        except Exception:
            raise auth_error("SERVICE_NOT_READY", 503) from None

    def _deliver_link(self, email: str, purpose: str) -> None:
        token = secrets.token_urlsafe(32)
        ttl = VERIFY_SECONDS if purpose == "verify-email" else RESET_SECONDS
        if self.store.auth_issue_action(email, purpose, _digest(token), ttl):
            # The fragment is not sent to the server, referrers, or HTTP access logs.
            self.mailer(email, purpose, f"{self.public_origin}/#{purpose}?token={token}")

    def complete_link(
        self,
        request: Request,
        token: str,
        password: str,
        purpose: str,
        public_handle: str | None = None,
    ) -> None:
        token_hash = _digest(token)
        email = None
        if _TOKEN.fullmatch(token) is not None:
            email = self.store.auth_action_email(token_hash, purpose)
        self._throttle(request, "confirm:" + purpose, email or token_hash)
        if email is None:
            raise auth_error("AUTH_INVALID_TOKEN", 401)
        _validate_password(password)
        if purpose == "verify-email":
            _validate_handle(public_handle)
        password_hash = _hash_password(password)
        try:
            completed = self.store.auth_complete_action(
                token_hash, purpose, password_hash, public_handle
            )
        except AuthConflictError:
            raise auth_error("AUTH_VALIDATION_ERROR") from None
        if not completed:
            raise auth_error("AUTH_INVALID_TOKEN", 401)

    def login(self, request: Request, email: str, password: str) -> tuple[dict, str]:
        email = normalize_email(email)
        self._throttle(request, "login", email)
        _validate_password(password)
        account = self.store.auth_account(email)
        valid = _hash_password(
            password, verify=_DUMMY_HASH if account is None else account.password_hash
        )
        if not valid or account is None:
            raise auth_error("AUTH_INVALID_CREDENTIALS", 401)
        old_token = self.cookie_token(request)
        token = secrets.token_urlsafe(32)
        session = self.store.auth_create_session(
            account,
            _digest(token),
            SESSION_SECONDS,
            None if old_token is None else _digest(old_token),
        )
        if session is None:
            raise auth_error("AUTH_INVALID_CREDENTIALS", 401)
        return self.session_payload(session), token

    def logout(self, request: Request) -> None:
        token = self.cookie_token(request)
        if token is not None:
            self.store.auth_revoke_session(_digest(token))

    def provision_development_account(
        self, email: str, password: str, handle: str, role: str = "user"
    ) -> None:
        if not self.development:
            raise ValueError("development account provisioning is disabled")
        email = normalize_email(email)
        if not email.rsplit("@", 1)[1].endswith(".test") or role not in {"user", "admin"}:
            raise ValueError("development accounts require a .test domain and a valid role")
        _validate_password(password)
        _validate_handle(handle)
        account = self.store.auth_account(email)
        if account is not None:
            matches = _hash_password(password, verify=account.password_hash)
            if not matches or account.public_handle != handle or account.role != role:
                raise ValueError("development account differs; refusing to overwrite")
            password_hash = account.password_hash
        else:
            password_hash = _hash_password(password)
        self.store.auth_provision_account(email, password_hash, handle, role, account)


def install_auth_routes(app, service: AuthService) -> None:
    """Public launcher entry point; route implementation avoids a circular import."""
    from .auth_routes import install_auth_routes as install

    install(app, service)
