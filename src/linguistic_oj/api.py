"""FastAPI boundary for the asynchronous Mock submission slice."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .mvp_contract import EvaluationContract
from .submission_jobs import OutboxDispatcher
from .submission_store import (
    GlobalQueueFullError,
    IdempotencyConflictError,
    SubmissionQuotaError,
    SubmissionRecord,
    SubmissionStatus,
    SubmissionStore,
    UserRecord,
)


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str


Authenticate = Callable[[Request], Principal]


class CreateSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    challenge_id: str
    student_prompt: str


class SubmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    submission_id: str
    challenge_id: str
    status: SubmissionStatus
    created_at: str
    started_at: str | None
    completed_at: str | None


class GenerationSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enable_thinking: bool
    max_tokens: int
    seed: int
    temperature: float
    top_p: float


class ModelIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    model: str
    revision: str
    runtime: str
    runtime_version: str


class OwnerResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    aggregation_version: str
    challenge_id: str
    dataset_sha256: str
    errors: dict[str, int]
    generation_settings: GenerationSettingsResponse
    metrics: dict[str, float]
    model_identity: ModelIdentityResponse
    primary_metric: str
    prompt_envelope_version: str
    samples_invalid: int
    samples_total: int
    samples_valid: int
    score: float
    scorer_version: str
    selection_sha256: str
    student_prompt_sha256: str
    task: str


class OwnerFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    code: str
    failure_contract_version: str
    retryable: bool


class LeaderboardRowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    evaluation_identity_sha256: str
    public_handle: str
    rank: int
    samples_invalid: int
    samples_total: int
    samples_valid: int
    score: float
    succeeded_at: str


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                response = JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header"},
                )
                await response(scope, receive, send)
                return
            if declared_bytes > self._max_bytes:
                await self._reject(scope, receive, send)
                return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self._app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )
        await response(scope, receive, send)


def _submission_response(submission: SubmissionRecord) -> SubmissionResponse:
    return SubmissionResponse(
        submission_id=submission.submission_id,
        challenge_id=submission.challenge_id,
        status=submission.status,
        created_at=submission.created_at,
        started_at=submission.started_at,
        completed_at=submission.completed_at,
    )


def create_app(
    *,
    store: SubmissionStore,
    dispatcher: OutboxDispatcher,
    contract: EvaluationContract,
    authenticate: Authenticate,
    allow_draft_submissions: bool = False,
    environment: Literal["development", "test", "production"] = "production",
) -> FastAPI:
    if environment not in {"development", "test", "production"}:
        raise ValueError("unsupported deployment environment")
    if environment == "production" and allow_draft_submissions:
        raise ValueError("draft submission override is forbidden in production")
    if not dispatcher.matches(store, contract):
        raise ValueError("outbox dispatcher does not match the store and contract")

    app = FastAPI(title="Linguistic Online Judge API", version="0.1.0")
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=contract.api_request_body_bytes)
    dispatcher.recover_published_queued()

    def current_user(request: Request) -> UserRecord:
        principal = authenticate(request)
        if not isinstance(principal, Principal) or not principal.subject:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = store.user_by_subject(principal.subject)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authenticated user is not registered",
            )
        return user

    current_user_dependency = Depends(current_user)

    @app.post(
        "/v1/submissions",
        response_model=SubmissionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_submission(
        payload: CreateSubmissionRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        user: UserRecord = current_user_dependency,
    ) -> SubmissionResponse:
        if not contract.external_activation_ready and not allow_draft_submissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Challenge is not open for submissions",
            )
        if payload.challenge_id != contract.challenge_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Challenge not found")
        if not payload.student_prompt.strip():
            raise HTTPException(
                status_code=422,
                detail="Student prompt must not be empty",
            )
        if len(payload.student_prompt.encode("utf-8")) > contract.student_prompt_utf8_bytes:
            raise HTTPException(
                status_code=422,
                detail="Student prompt exceeds the byte limit",
            )
        if not contract.idempotency_key_is_valid(idempotency_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Idempotency-Key header",
            )

        try:
            created = store.create_submission(
                user=user,
                idempotency_key=idempotency_key,
                student_prompt=payload.student_prompt,
                contract=contract,
            )
        except IdempotencyConflictError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
        except SubmissionQuotaError as error:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(error),
            ) from None
        except GlobalQueueFullError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from None

        dispatcher.dispatch_pending()
        return _submission_response(created.submission)

    @app.get("/v1/submissions/{submission_id}", response_model=SubmissionResponse)
    def get_submission(
        submission_id: str,
        user: UserRecord = current_user_dependency,
    ) -> SubmissionResponse:
        submission = store.submission_for_owner(submission_id, user.user_id)
        if submission is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Submission not found",
            )
        return _submission_response(submission)

    @app.get(
        "/v1/submissions/{submission_id}/result",
        response_model=OwnerResultResponse | OwnerFailureResponse,
    )
    def get_result(
        submission_id: str,
        user: UserRecord = current_user_dependency,
    ) -> OwnerResultResponse | OwnerFailureResponse:
        stored = store.owner_result(submission_id, user.user_id)
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Submission not found",
            )
        if stored.status in {SubmissionStatus.QUEUED, SubmissionStatus.RUNNING}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Result is not ready")
        if stored.status is SubmissionStatus.REJECTED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Submission was rejected during preflight",
            )
        stored_contract = EvaluationContract.from_mapping(
            json.loads(stored.contract_snapshot_json)
        )
        if stored.result is not None:
            if set(stored.result) != set(stored_contract.owner_result_fields):
                raise RuntimeError("stored owner result violates the evaluation contract")
            return OwnerResultResponse.model_validate(stored.result)
        if stored.failure is not None:
            if set(stored.failure) != set(stored_contract.owner_failure_fields):
                raise RuntimeError("stored failure result violates the evaluation contract")
            return OwnerFailureResponse.model_validate(stored.failure)
        raise RuntimeError("terminal submission has no result")

    @app.get(
        "/v1/leaderboards/{evaluation_identity_sha256}",
        response_model=list[LeaderboardRowResponse],
    )
    def get_leaderboard(
        evaluation_identity_sha256: Annotated[
            str,
            Path(pattern="^[0-9a-f]{64}$"),
        ],
    ) -> list[LeaderboardRowResponse]:
        response = []
        for entry in store.leaderboard(evaluation_identity_sha256):
            row = entry.to_dict()
            stored_contract = EvaluationContract.from_mapping(
                json.loads(entry.contract_snapshot_json)
            )
            if set(row) != set(stored_contract.public_leaderboard_fields):
                raise RuntimeError("leaderboard row violates the evaluation contract")
            response.append(LeaderboardRowResponse.model_validate(row))
        return response

    return app
