"""Authenticated administration of loaded catalog teaching content, not evaluation contracts."""

from __future__ import annotations

import hashlib
import json

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .admin_store import (
    ADMIN_BODY_BYTES,
    MAX_EXPECTED_REVISION,
    AdminActor,
    AdminState,
    AdminStoreError,
    AdminStoreMixin,
    TeachingContent,
    source_fingerprint,
)
from .api import APIError, ChallengeResponse, _challenge_response
from .auth import AuthService, auth_error
from .auth_routes import _unique_object


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    expected_revision: int = Field(ge=0, le=MAX_EXPECTED_REVISION)


class DraftRequest(RevisionRequest):
    content: TeachingContent


class AdmissionsRequest(RevisionRequest):
    closed: bool


class AdminDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    challenge: ChallengeResponse
    revision: int
    draft: TeachingContent | None
    published: TeachingContent | None
    published_revision: int
    admissions_closed: bool
    can_reopen: bool


class AdminService:
    def __init__(
        self,
        *,
        auth: AuthService,
        store: AdminStoreMixin,
        catalog,
        contracts,
        runtime_availability,
        allow_draft_submissions: bool,
    ) -> None:
        if auth.store is not store:
            raise ValueError("admin and auth must share the submission store")
        self.auth = auth
        self.store = store
        self.catalog = catalog
        self.contracts = contracts
        self.runtime_availability = runtime_availability
        self.allow_draft_submissions = allow_draft_submissions

    def actor(self, request: Request, *, write: bool = False) -> AdminActor:
        token = self.auth.cookie_token(request)
        if token is None:
            raise auth_error("AUTH_INVALID_TOKEN", 401)
        expected = request.headers.getlist("x-loj-expected-user")
        if len(expected) > 1 or (write and not expected):
            raise auth_error("AUTH_ACCOUNT_CHANGED", 409)
        return AdminActor(
            hashlib.sha256(token.encode("utf-8")).hexdigest(), next(iter(expected), None)
        )

    def can_reopen(self, challenge_id: str) -> bool:
        contract = self.contracts.get(challenge_id)
        return bool(
            contract is not None
            and (contract.external_activation_ready or self.allow_draft_submissions)
            and self.runtime_availability.get(challenge_id, False)
        )

    def detail(self, state: AdminState) -> AdminDetail:
        public = self.catalog[state.challenge_id]
        fingerprint = source_fingerprint(public)
        closed = state.challenge_id in self.contracts and state.closed_for(fingerprint)
        published = state.published_content(fingerprint)
        return AdminDetail(
            challenge=_challenge_response(
                public,
                self.contracts.get(state.challenge_id),
                allow_draft_submissions=self.allow_draft_submissions,
                runtime_available=self.runtime_availability.get(state.challenge_id, False),
                admissions_closed=closed,
            ),
            revision=state.revision,
            draft=(
                None
                if state.draft_json is None
                else TeachingContent.model_validate_json(state.draft_json)
            ),
            published=published,
            published_revision=state.published_revision if published is not None else 0,
            admissions_closed=closed,
            can_reopen=self.can_reopen(state.challenge_id) and state.matches(fingerprint),
        )

    def read(self, request: Request, challenge_id: str | None = None):
        ids = tuple(self.catalog) if challenge_id is None else (challenge_id,)
        states = self.store.admin_states(ids, actor=self.actor(request))
        if challenge_id is not None:
            if challenge_id not in self.catalog:
                raise APIError(404, code="CHALLENGE_NOT_FOUND", message="Challenge not found")
            return self.detail(states[challenge_id])
        items = []
        for public in sorted(
            self.catalog.values(), key=lambda item: (item.language, item.task, item.challenge_id)
        ):
            state = states[public.challenge_id]
            fingerprint = source_fingerprint(public)
            items.append(
                {
                    "challenge_id": public.challenge_id,
                    "title": public.title,
                    "language": public.language,
                    "treebank": public.treebank,
                    "task": public.task,
                    "has_contract": public.challenge_id in self.contracts,
                    "admissions_closed": public.challenge_id in self.contracts
                    and state.closed_for(fingerprint),
                    "revision": state.revision,
                    "published_revision": state.published_revision
                    if state.published_content(fingerprint) is not None
                    else 0,
                    "can_reopen": self.can_reopen(public.challenge_id)
                    and state.matches(fingerprint),
                }
            )
        return {"items": items}

    def change(self, request: Request, challenge_id: str, action: str, payload: RevisionRequest):
        actor = self.actor(request, write=True)
        public = self.catalog.get(challenge_id)
        if public is None:
            # Authorize before revealing even an unknown catalog identifier.
            self.store.admin_states((), actor=actor)
            raise APIError(404, code="CHALLENGE_NOT_FOUND", message="Challenge not found")
        result = self.store.admin_change(
            challenge_id=challenge_id,
            fingerprint=source_fingerprint(public),
            actor=actor,
            expected_revision=payload.expected_revision,
            action=action,
            has_contract=challenge_id in self.contracts,
            can_reopen=self.can_reopen(challenge_id),
            content=payload.content if isinstance(payload, DraftRequest) else None,
            closed=payload.closed if isinstance(payload, AdmissionsRequest) else None,
        )
        return self.detail(result) if isinstance(result, AdminState) else result


def install_admin_routes(app: FastAPI) -> None:
    auth = getattr(app.state, "auth_service", None)
    if not isinstance(auth, AuthService):
        raise ValueError("install AuthService before administrator routes")
    if getattr(app.state, "admin_service", None) is not None:
        raise ValueError("administrator routes are already installed")
    service = AdminService(auth=auth, **app.state.admin_context)
    app.state.admin_service = service

    @app.exception_handler(AdminStoreError)
    async def store_error(_request: Request, error: AdminStoreError):
        return await app.exception_handlers[APIError](
            _request, APIError(error.status_code, code=error.code, message=error.message)
        )

    async def change(request: Request, challenge_id: str, action: str, model):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > ADMIN_BODY_BYTES:
                raise APIError(
                    413, code="REQUEST_BODY_TOO_LARGE", message="Request body is too large"
                )
        try:
            payload = model.model_validate(
                json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
            )
        except (ValueError, UnicodeError, RecursionError):
            raise APIError(
                422,
                code="REQUEST_VALIDATION_ERROR",
                message="Check the teaching fields and revision",
            ) from None
        return await run_in_threadpool(service.change, request, challenge_id, action, payload)

    @app.get("/v1/admin/challenges")
    def challenges(request: Request):
        return service.read(request)

    @app.get("/v1/admin/challenges/{challenge_id}", response_model=AdminDetail)
    def detail(request: Request, challenge_id: str):
        return service.read(request, challenge_id)

    @app.put("/v1/admin/challenges/{challenge_id}/teaching-draft", response_model=AdminDetail)
    async def save(request: Request, challenge_id: str):
        return await change(request, challenge_id, "save", DraftRequest)

    @app.post("/v1/admin/challenges/{challenge_id}/check")
    async def check(request: Request, challenge_id: str):
        return await change(request, challenge_id, "check", RevisionRequest)

    @app.post("/v1/admin/challenges/{challenge_id}/publish", response_model=AdminDetail)
    async def publish(request: Request, challenge_id: str):
        return await change(request, challenge_id, "publish", RevisionRequest)

    @app.post("/v1/admin/challenges/{challenge_id}/admissions", response_model=AdminDetail)
    async def admissions(request: Request, challenge_id: str):
        return await change(request, challenge_id, "admissions", AdmissionsRequest)
