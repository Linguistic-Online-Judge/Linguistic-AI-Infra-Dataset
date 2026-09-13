"""Bounded JSON auth routes and same-origin cookie security middleware."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import APIError, AuthenticationError
from .auth import SESSION_SECONDS, AuthService, auth_error


def _error_response(error: APIError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": error.code, "message": error.message, "details": error.details}},
        status_code=error.status_code,
        headers=error.headers,
    )


class AuthMiddleware:
    def __init__(self, app: ASGIApp, *, service: AuthService) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket" and self.service.development:
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope["path"]
        auth_path = path == "/v1/auth" or path.startswith("/v1/auth/")
        admin_path = path == "/v1/admin" or path.startswith("/v1/admin/")
        private = admin_path or path in {"/v1/users/me", "/v1/submissions"} or path.startswith(
            "/v1/submissions/"
        )
        response_started = False

        async def secure_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            if message["type"] == "http.response.start" and (auth_path or private):
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in {b"cache-control", b"pragma", b"referrer-policy"}
                ]
                headers += [
                    (b"cache-control", b"no-store"),
                    (b"pragma", b"no-cache"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                message = {**message, "headers": headers}
            await send(message)

        try:
            self.service.guard_host(request)
            self.service.client_address(request)
            if auth_path or private:
                self.service.cookie_token(request)
                if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                    types = request.headers.getlist("content-type")
                    if (
                        request.headers.getlist("origin") != [self.service.public_origin]
                        or request.headers.getlist("x-loj-csrf") != ["1"]
                        or len(types) != 1
                        or types[0].split(";", 1)[0].strip().lower() != "application/json"
                    ):
                        raise auth_error("AUTH_CSRF_REJECTED", 403)
                    if auth_path:
                        lengths = request.headers.getlist("content-length")
                        if len(lengths) > 1 or (
                            lengths and (not lengths[0].isascii() or not lengths[0].isdigit())
                        ):
                            raise auth_error("AUTH_VALIDATION_ERROR")
                        if lengths and (len(lengths[0]) > 8 or int(lengths[0]) > 8192):
                            raise auth_error("AUTH_VALIDATION_ERROR", 413)
                if private:
                    await run_in_threadpool(self.service.authenticate, request)
                    if (
                        scope["method"] not in {"GET", "HEAD", "OPTIONS"}
                        and (admin_path or path.rstrip("/") == "/v1/submissions")
                        and not request.headers.getlist("x-loj-expected-user")
                    ):
                        raise auth_error("AUTH_ACCOUNT_CHANGED", 409)
        except AuthenticationError:
            response = _error_response(auth_error("AUTH_INVALID_TOKEN", 401))
        except APIError as error:
            response = _error_response(error)
        else:
            try:
                await self.app(scope, receive, secure_send)
                return
            except Exception:
                if response_started or not (auth_path or private):
                    raise
                response = _error_response(auth_error("SERVICE_NOT_READY", 503))
        await response(scope, receive, secure_send)


def _unique_object(pairs: list[tuple]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


async def _payload(request: Request, fields: set[str]) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            raise auth_error("AUTH_VALIDATION_ERROR", 413)
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or any(not isinstance(item, str) for item in value.values())
        ):
            raise ValueError
    except (ValueError, UnicodeError, RecursionError):
        raise auth_error("AUTH_VALIDATION_ERROR") from None
    return value


def _cookie(response: JSONResponse, service: AuthService, token: str = "") -> None:
    options = {
        "key": service.cookie_name,
        "httponly": True,
        "secure": not service.development,
        "samesite": "strict" if service.development else "lax",
        "path": "/",
    }
    if token:
        response.set_cookie(value=token, max_age=SESSION_SECONDS, **options)
    else:
        response.delete_cookie(**options)


def install_auth_routes(app: FastAPI, service: AuthService) -> None:
    if getattr(app.state, "auth_service", None) is not None:
        raise ValueError("authentication is already installed")
    app.state.auth_service = service
    app.add_middleware(AuthMiddleware, service=service)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            await run_in_threadpool(service.start)
            async with original_lifespan(application) as state:
                yield state
        finally:
            await run_in_threadpool(service.close)

    app.router.lifespan_context = lifespan

    async def dispatch(request: Request, operation: str, fields: set[str] | None = None):
        try:
            payload = {} if fields is None else await _payload(request, fields)
            if operation == "config":
                return JSONResponse(
                    {
                        "enabled": True,
                        "mode": "local" if service.development else "email",
                        "mail_delivery": "local" if service.development else "smtp",
                    }
                )
            if operation == "session":
                session = await run_in_threadpool(service.session, request)
                return JSONResponse(service.session_payload(session))
            if operation in {"register", "reset-request"}:
                purpose = "verify-email" if operation == "register" else "reset-password"
                await run_in_threadpool(service.request_link, request, payload["email"], purpose)
                return JSONResponse({"status": "accepted"}, status_code=202)
            if operation in {"verify", "reset-confirm"}:
                purpose = "verify-email" if operation == "verify" else "reset-password"
                await run_in_threadpool(
                    service.complete_link,
                    request,
                    payload["token"],
                    payload["password"],
                    purpose,
                    payload.get("public_handle"),
                )
                response = JSONResponse(
                    {
                        "status": "verified" if operation == "verify" else "password_updated",
                    }
                )
                if operation == "reset-confirm":
                    _cookie(response, service)
                return response
            if operation == "login":
                body, token = await run_in_threadpool(
                    service.login,
                    request,
                    payload["email"],
                    payload["password"],
                )
                response = JSONResponse(body)
                _cookie(response, service, token)
                return response
            await run_in_threadpool(service.logout, request)
            response = JSONResponse({"status": "signed_out"})
            _cookie(response, service)
            return response
        except AuthenticationError:
            return _error_response(auth_error("AUTH_INVALID_TOKEN", 401))
        except APIError as error:
            return _error_response(error)
        except Exception:
            # Never expose or log request bodies, tokens, SMTP errors, or database credentials.
            return _error_response(auth_error("SERVICE_NOT_READY", 503))

    @app.get("/v1/auth/config")
    async def config(request: Request):
        return await dispatch(request, "config")

    @app.get("/v1/auth/session")
    async def session(request: Request):
        return await dispatch(request, "session")

    @app.post("/v1/auth/register", status_code=202)
    async def register(request: Request):
        return await dispatch(request, "register", {"email"})

    @app.post("/v1/auth/verify-email")
    async def verify(request: Request):
        return await dispatch(request, "verify", {"token", "password", "public_handle"})

    @app.post("/v1/auth/login")
    async def login(request: Request):
        return await dispatch(request, "login", {"email", "password"})

    @app.post("/v1/auth/logout")
    async def logout(request: Request):
        return await dispatch(request, "logout", set())

    @app.post("/v1/auth/password-reset/request", status_code=202)
    async def reset_request(request: Request):
        return await dispatch(request, "reset-request", {"email"})

    @app.post("/v1/auth/password-reset/confirm")
    async def reset_confirm(request: Request):
        return await dispatch(request, "reset-confirm", {"token", "password"})
