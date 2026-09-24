"""FastAPI boundary for asynchronous contract-routed submissions."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path as FileSystemPath
from time import perf_counter
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Path, Query, Request, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .admin_store import ChallengePausedError, TeachingContent, source_fingerprint
from .challenge import PublicChallenge, validate_public_challenge
from .challenge_registry import ChallengeContractRegistry, validate_contract_matches_public
from .mvp_contract import EvaluationContract
from .runtime_availability import ProbedAvailability
from .submission_jobs import OutboxDispatcher
from .submission_store import (
    GlobalQueueFullError,
    IdempotencyConflictError,
    LeaderboardEntry,
    OwnerSubmissionRecord,
    SubmissionQuotaError,
    SubmissionRecord,
    SubmissionStatus,
    SubmissionStoreProtocol,
    UserRecord,
    UserRole,
)


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str


class AuthenticationError(Exception):
    """Expected authentication-verifier rejection with no private detail."""


Authenticate = Callable[[Request], Principal]
ReadinessCheck = Callable[[], None]

_REQUEST_ID_HEADER = b"x-request-id"
_OUTBOX_DISPATCH_INTERVAL_SECONDS = 5.0
_OUTBOX_LOGGER = logging.getLogger("linguistic_oj.outbox")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SUBMISSION_ID = re.compile(r"[0-9a-f]{32}")
_BEARER_AUTH = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


class ErrorDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    error: ErrorDetailResponse


class APIError(StarletteHTTPException):
    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=dict(headers or {}))
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _error_payload(
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": dict(details or {}),
        }
    }


_ERROR_RESPONSES = {
    status_code: {"model": ErrorResponse}
    for status_code in (400, 401, 403, 404, 409, 413, 422, 429, 500, 503)
}


class CreateSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    challenge_id: str
    student_prompt: str


class SubmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    submission_id: str
    challenge_id: str
    evaluation_identity_sha256: str
    status: SubmissionStatus
    created_at: str
    started_at: str | None
    completed_at: str | None


class CurrentUserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    user_id: str
    public_handle: str
    role: UserRole


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


class SourceFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    path: str
    sha256: str


class ChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    challenge_id: str
    title: str
    version: str
    language: str
    treebank: str
    task: str
    sample_count: int
    primary_metric: str
    secondary_metrics: tuple[str, ...]
    response_schema_version: str
    scorer_version: str | None
    aggregation_version: str | None
    dataset_sha256: str
    selection_sha256: str
    security_level: str
    status: str
    annotation_license: str
    attribution_requirements: str
    source_release: str
    source_commit: str
    source_file_sha256s: tuple[SourceFileResponse, ...]
    share_alike_requirements: str
    underlying_text_rights: str
    benchmark_limitations: str
    evaluation_identity_sha256: str | None
    model_identity: ModelIdentityResponse | None
    student_prompt_utf8_bytes: int | None
    submission_enabled: bool
    runtime_available: bool
    accepting_submissions: bool
    submissions_open: bool
    admissions_closed: bool = False


class TeachingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    challenge_id: str
    published_revision: int = 0
    content: TeachingContent | None = None


class OwnerSubmissionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    submission_id: str
    challenge_id: str
    evaluation_identity_sha256: str
    status: SubmissionStatus
    created_at: str
    started_at: str | None
    completed_at: str | None


class OwnerPromptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    submission_id: str
    challenge_id: str
    student_prompt: str
    student_prompt_sha256: str


class OwnerSubmissionPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    items: tuple[OwnerSubmissionSummaryResponse, ...]
    next_cursor: str | None


class SegmentationMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    micro_f1: float
    micro_precision: float
    micro_recall: float


class AccuracyMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    micro_accuracy: float


class DependencyMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    las: float
    uas: float


class TransliterationMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    sentence_exact_match_rate: float
    token_accuracy: float


class _OwnerSuccessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    outcome: Literal["succeeded"] = "succeeded"
    aggregation_version: str
    challenge_id: str
    dataset_sha256: str
    errors: dict[str, int]
    generation_settings: GenerationSettingsResponse
    model_identity: ModelIdentityResponse
    prompt_envelope_version: str
    samples_invalid: int
    samples_total: int
    samples_valid: int
    score: float
    scorer_version: str
    selection_sha256: str
    student_prompt_sha256: str


class SegmentationResultResponse(_OwnerSuccessResponse):
    task: Literal["segmentation"]
    primary_metric: Literal["micro_f1"]
    metrics: SegmentationMetricsResponse


class UposResultResponse(_OwnerSuccessResponse):
    task: Literal["upos"]
    primary_metric: Literal["micro_accuracy"]
    metrics: AccuracyMetricsResponse


class XposResultResponse(_OwnerSuccessResponse):
    task: Literal["xpos"]
    primary_metric: Literal["micro_accuracy"]
    metrics: AccuracyMetricsResponse


class DependencyResultResponse(_OwnerSuccessResponse):
    task: Literal["dependency"]
    primary_metric: Literal["las"]
    metrics: DependencyMetricsResponse


class TransliterationResultResponse(_OwnerSuccessResponse):
    task: Literal["transliteration"]
    primary_metric: Literal["token_accuracy"]
    metrics: TransliterationMetricsResponse


OwnerSuccessResponse = Annotated[
    SegmentationResultResponse
    | UposResultResponse
    | XposResultResponse
    | DependencyResultResponse
    | TransliterationResultResponse,
    Field(discriminator="task"),
]


class OwnerFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    outcome: Literal["failed"] = "failed"
    code: Literal[
        "DATASET_INTEGRITY",
        "JOB_DEADLINE",
        "MODEL_IDENTITY_MISMATCH",
        "PROVIDER_TIMEOUT",
        "PROVIDER_TRANSPORT",
        "RUNTIME_MISCONFIGURATION",
        "WORKER_CRASH",
    ]
    failure_contract_version: str
    retryable: bool


class OwnerRejectedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    outcome: Literal["rejected"] = "rejected"
    code: Literal["TOKEN_LIMIT_EXCEEDED"]
    failure_contract_version: str
    retryable: Literal[False]


OwnerTerminalResponse = Annotated[
    OwnerSuccessResponse | OwnerFailureResponse | OwnerRejectedResponse,
    Field(discriminator="outcome"),
]
_OWNER_SUCCESS_ADAPTER = TypeAdapter(OwnerSuccessResponse)


_OWNER_RESULT_FIELDS = frozenset(
    {
        "aggregation_version",
        "challenge_id",
        "dataset_sha256",
        "errors",
        "generation_settings",
        "metrics",
        "model_identity",
        "primary_metric",
        "prompt_envelope_version",
        "samples_invalid",
        "samples_total",
        "samples_valid",
        "score",
        "scorer_version",
        "selection_sha256",
        "student_prompt_sha256",
        "task",
    }
)
_OWNER_FAILURE_FIELDS = frozenset({"code", "failure_contract_version", "retryable"})
_PUBLIC_LEADERBOARD_FIELDS = frozenset(
    {
        "evaluation_identity_sha256",
        "public_handle",
        "rank",
        "samples_invalid",
        "samples_total",
        "samples_valid",
        "score",
        "succeeded_at",
    }
)


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


class LeaderboardPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    items: tuple[LeaderboardRowResponse, ...]
    next_cursor: str | None


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
                    content=_error_payload(
                        "INVALID_CONTENT_LENGTH",
                        "Content-Length header is invalid",
                    ),
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
            content=_error_payload("REQUEST_BODY_TOO_LARGE", "Request body is too large"),
        )
        await response(scope, receive, send)


class SafeRequestLoggingMiddleware:
    """Emit one body-free, allowlisted completion record per HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._logger = logging.getLogger("linguistic_oj.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = _request_id(scope["headers"])
        started = perf_counter()
        response_status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
                headers = list(message.get("headers", []))
                if not any(name.lower() == _REQUEST_ID_HEADER for name, _ in headers):
                    headers.append((_REQUEST_ID_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            self._logger.info(
                json.dumps(
                    {
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                        "event": "http_request_complete",
                        "method": scope["method"],
                        "path": scope["path"],
                        "request_id": request_id,
                        "status_code": response_status,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )


def _request_id(headers: list[tuple[bytes, bytes]]) -> str:
    for name, value in headers:
        if name.lower() != _REQUEST_ID_HEADER:
            continue
        try:
            candidate = value.decode("ascii")
        except UnicodeDecodeError:
            break
        if _REQUEST_ID.fullmatch(candidate) is not None:
            return candidate
        break
    return uuid.uuid4().hex


def _submission_response(submission: SubmissionRecord) -> SubmissionResponse:
    return SubmissionResponse(
        submission_id=submission.submission_id,
        challenge_id=submission.challenge_id,
        evaluation_identity_sha256=submission.evaluation_identity_sha256,
        status=submission.status,
        created_at=submission.created_at,
        started_at=submission.started_at,
        completed_at=submission.completed_at,
    )


def _owner_submission_response(
    submission: OwnerSubmissionRecord,
) -> OwnerSubmissionSummaryResponse:
    return OwnerSubmissionSummaryResponse(
        submission_id=submission.submission_id,
        challenge_id=submission.challenge_id,
        evaluation_identity_sha256=submission.evaluation_identity_sha256,
        status=submission.status,
        created_at=submission.created_at,
        started_at=submission.started_at,
        completed_at=submission.completed_at,
    )


def _history_cursor(submission: OwnerSubmissionRecord) -> str:
    payload = json.dumps(
        {
            "created_at": submission.created_at,
            "submission_id": submission.submission_id,
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_history_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if cursor is None:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
        value = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if (
        not isinstance(value, dict)
        or set(value) != {"created_at", "submission_id", "version"}
        or value["version"] != 1
        or not isinstance(value["created_at"], str)
        or not _is_utc_timestamp(value["created_at"])
        or not isinstance(value["submission_id"], str)
        or _SUBMISSION_ID.fullmatch(value["submission_id"]) is None
    ):
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="INVALID_SUBMISSION_CURSOR",
            message="Submission history cursor is invalid",
        )
    return value["created_at"], value["submission_id"]


def _is_utc_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _leaderboard_cursor(
    entry: LeaderboardEntry,
    *,
    as_of: str,
) -> str:
    payload = json.dumps(
        {
            "as_of": as_of,
            "evaluation_identity_sha256": entry.evaluation_identity_sha256,
            "rank": entry.rank,
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_leaderboard_cursor(
    cursor: str | None,
    *,
    evaluation_identity_sha256: str,
) -> tuple[str, int]:
    if cursor is None:
        return datetime.now(UTC).isoformat(timespec="microseconds"), 0
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
        value = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if (
        not isinstance(value, dict)
        or set(value) != {"as_of", "evaluation_identity_sha256", "rank", "version"}
        or value["version"] != 1
        or value["evaluation_identity_sha256"] != evaluation_identity_sha256
        or not isinstance(value["as_of"], str)
        or not _is_utc_timestamp(value["as_of"])
        or type(value["rank"]) is not int
        or value["rank"] <= 0
    ):
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="INVALID_LEADERBOARD_CURSOR",
            message="Leaderboard cursor is invalid",
        )
    return value["as_of"], value["rank"]


def _normalize_routes(
    store: SubmissionStoreProtocol,
    contract: EvaluationContract | Mapping[str, EvaluationContract],
    dispatcher: OutboxDispatcher | Mapping[str, OutboxDispatcher],
) -> tuple[dict[str, EvaluationContract], dict[str, OutboxDispatcher]]:
    contracts_are_mapped = isinstance(contract, Mapping)
    dispatchers_are_mapped = isinstance(dispatcher, Mapping)
    if contracts_are_mapped != dispatchers_are_mapped:
        raise ValueError("contracts and dispatchers must both be singular or mapped")
    if contracts_are_mapped:
        contracts = dict(contract)
        dispatchers = dict(dispatcher)
    else:
        if not isinstance(contract, EvaluationContract) or not isinstance(
            dispatcher, OutboxDispatcher
        ):
            raise TypeError("invalid evaluation contract or dispatcher")
        contracts = {contract.challenge_id: contract}
        dispatchers = {contract.challenge_id: dispatcher}
    if not contracts or set(contracts) != set(dispatchers):
        raise ValueError("contracts and dispatchers must have the same non-empty challenge IDs")

    for challenge_id, selected_contract in contracts.items():
        if (
            not isinstance(challenge_id, str)
            or not isinstance(selected_contract, EvaluationContract)
            or challenge_id != selected_contract.challenge_id
        ):
            raise ValueError("contract keys must match their challenge IDs")
        selected_dispatcher = dispatchers[challenge_id]
        if not isinstance(selected_dispatcher, OutboxDispatcher) or not selected_dispatcher.matches(
            store, selected_contract
        ):
            raise ValueError("dispatcher does not match its evaluation contract")

    common_fields = (
        "api_request_body_bytes",
        "global_queue_depth",
        "max_outstanding_submissions_per_user",
        "max_running_submissions_per_user",
        "owner_result_fields",
        "owner_failure_fields",
        "public_leaderboard_fields",
    )
    first = next(iter(contracts.values()))
    for selected_contract in contracts.values():
        mismatches = [field for field in common_fields
                      if getattr(selected_contract, field) != getattr(first, field)]
        if mismatches:
            raise ValueError(
                "multi-challenge contracts have incompatible process-wide policies: "
                + ", ".join(mismatches)
            )
        if frozenset(selected_contract.owner_result_fields) != _OWNER_RESULT_FIELDS:
            raise ValueError("owner result fields are incompatible with the API contract")
        if frozenset(selected_contract.owner_failure_fields) != _OWNER_FAILURE_FIELDS:
            raise ValueError("owner failure fields are incompatible with the API contract")
        if (
            frozenset(selected_contract.public_leaderboard_fields)
            != _PUBLIC_LEADERBOARD_FIELDS
        ):
            raise ValueError("leaderboard fields are incompatible with the API contract")
    return contracts, dispatchers


def _normalize_public_challenges(
    public_challenges: Mapping[str, PublicChallenge] | None,
    contracts: Mapping[str, EvaluationContract],
) -> dict[str, PublicChallenge]:
    catalog = {} if public_challenges is None else dict(public_challenges)
    for challenge_id, public in catalog.items():
        if (
            not isinstance(challenge_id, str)
            or not isinstance(public, PublicChallenge)
            or challenge_id != public.challenge_id
        ):
            raise ValueError("public challenge keys must match their challenge IDs")
        validate_public_challenge(public)
        selected_contract = contracts.get(challenge_id)
        if selected_contract is not None:
            validate_contract_matches_public(selected_contract, public)
    return catalog


def _normalize_runtime_availability(
    contracts: Mapping[str, EvaluationContract],
    runtime_availability: Mapping[str, bool] | None,
) -> dict[str, bool]:
    if runtime_availability is None:
        return dict.fromkeys(contracts, True)
    availability = dict(runtime_availability)
    if set(availability) != set(contracts) or any(
        type(value) is not bool for value in availability.values()
    ):
        raise ValueError(
            "runtime availability must provide one boolean for every executable challenge"
        )
    return availability


def _challenge_response(
    public: PublicChallenge,
    contract: EvaluationContract | None,
    *,
    allow_draft_submissions: bool,
    runtime_available: bool,
    admissions_closed: bool = False,
) -> ChallengeResponse:
    model_identity = None
    if contract is not None:
        model_identity = ModelIdentityResponse.model_validate(
            contract.evaluation_identity["model_identity"]
        )
    submission_enabled = contract is not None and (
        contract.external_activation_ready or allow_draft_submissions
    )
    return ChallengeResponse(
        **public.model_dump(),
        evaluation_identity_sha256=(
            None if contract is None else contract.evaluation_identity_sha256
        ),
        model_identity=model_identity,
        student_prompt_utf8_bytes=(
            None if contract is None else contract.student_prompt_utf8_bytes
        ),
        submission_enabled=submission_enabled,
        runtime_available=runtime_available,
        accepting_submissions=submission_enabled and runtime_available and not admissions_closed,
        submissions_open=submission_enabled and runtime_available and not admissions_closed,
        admissions_closed=admissions_closed,
    )


def create_app(
    *,
    store: SubmissionStoreProtocol,
    dispatcher: OutboxDispatcher | Mapping[str, OutboxDispatcher] | None = None,
    contract: EvaluationContract | Mapping[str, EvaluationContract] | None = None,
    registry: ChallengeContractRegistry | None = None,
    dispatchers: Mapping[str, OutboxDispatcher] | None = None,
    authenticate: Authenticate,
    readiness_check: ReadinessCheck | None = None,
    public_challenges: Mapping[str, PublicChallenge] | None = None,
    runtime_availability: Mapping[str, bool] | None = None,
    runtime_probe: Callable[[str], bool] | None = None,
    allow_draft_submissions: bool = False,
    environment: Literal["development", "test", "production"] = "production",
) -> FastAPI:
    if environment not in {"development", "test", "production"}:
        raise ValueError("unsupported deployment environment")
    if environment == "production" and allow_draft_submissions:
        raise ValueError("draft submission override is forbidden in production")
    if environment == "production" and readiness_check is None:
        raise ValueError("production requires a readiness check")
    if registry is not None:
        if any(value is not None for value in (contract, dispatcher, public_challenges)):
            raise ValueError('registry routing cannot be mixed with individual route arguments')
        if not isinstance(registry, ChallengeContractRegistry) or dispatchers is None:
            raise ValueError('registry routing requires a registry and dispatchers')
        contract, dispatcher = registry.contracts, dispatchers
        public_challenges = registry.public_challenges
    elif dispatchers is not None:
        raise ValueError('dispatchers requires registry')
    contracts, dispatchers = _normalize_routes(store, contract, dispatcher)
    catalog = _normalize_public_challenges(public_challenges, contracts)
    available_runtimes = _normalize_runtime_availability(contracts, runtime_availability)
    if runtime_probe is not None:
        available_runtimes = ProbedAvailability(available_runtimes, runtime_probe)
    known_leaderboards = {
        selected_contract.evaluation_identity_sha256
        for selected_contract in contracts.values()
    }
    first_contract = next(iter(contracts.values()))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def dispatch_outbox() -> None:
            while True:
                await asyncio.sleep(_OUTBOX_DISPATCH_INTERVAL_SECONDS)
                for challenge_id, selected_dispatcher in dispatchers.items():
                    try:
                        store.expire_queued_deadlines(
                            evaluation_identity_sha256=(
                                contracts[challenge_id].evaluation_identity_sha256
                            )
                        )
                        await asyncio.to_thread(selected_dispatcher.dispatch_pending)
                    except Exception:
                        _OUTBOX_LOGGER.error("outbox_dispatch_failed")

        dispatch_task = asyncio.create_task(dispatch_outbox())
        try:
            yield
        finally:
            dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await dispatch_task

    app = FastAPI(
        title="Linguistic Online Judge API",
        version="0.1.0",
        lifespan=lifespan,
        responses=_ERROR_RESPONSES,
    )
    app.state.admin_context = {
        "store": store,
        "catalog": catalog,
        "contracts": contracts,
        "runtime_availability": available_runtimes,
        "allow_draft_submissions": allow_draft_submissions,
    }

    @app.exception_handler(APIError)
    async def api_error_handler(_request: Request, error: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content=_error_payload(error.code, error.message, error.details),
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        issues = [
            {
                "location": [str(part) for part in issue["loc"]],
                "type": issue["type"],
            }
            for issue in error.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_payload(
                "REQUEST_VALIDATION_ERROR",
                "Request validation failed",
                {"issues": issues},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        code = "ROUTE_NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR"
        message = "Route not found" if error.status_code == 404 else "Request failed"
        return JSONResponse(
            status_code=error.status_code,
            content=_error_payload(code, message),
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _error: Exception) -> JSONResponse:
        _OUTBOX_LOGGER.error("unhandled_api_error")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_payload("INTERNAL_SERVER_ERROR", "Internal server error"),
        )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=first_contract.api_request_body_bytes,
    )
    app.add_middleware(SafeRequestLoggingMiddleware)
    web_root = FileSystemPath(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")
    for challenge_id, selected_dispatcher in dispatchers.items():
        try:
            store.expire_queued_deadlines(
                evaluation_identity_sha256=contracts[challenge_id].evaluation_identity_sha256
            )
            selected_dispatcher.recover()
        except Exception:
            if environment == 'production':
                raise
            _OUTBOX_LOGGER.error("outbox_recovery_failed")

    @app.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", include_in_schema=False)
    def ready() -> dict[str, str]:
        try:
            if readiness_check is not None:
                readiness_check()
        except Exception:
            raise APIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="SERVICE_NOT_READY",
                message="Service is not ready",
            ) from None
        return {"status": "ready"}

    @app.get("/", include_in_schema=False, response_class=FileResponse)
    def frontend() -> FileResponse:
        return FileResponse(
            web_root / "index.html",
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": (
                    "default-src 'self'; base-uri 'none'; connect-src 'self'; "
                    "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                    "img-src 'self' data:; object-src 'none'; script-src 'self'; "
                    "style-src 'self'"
                ),
                "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    @app.get("/v1/challenges", response_model=list[ChallengeResponse])
    def get_challenges() -> list[ChallengeResponse]:
        policies = store.admin_states(tuple(catalog))
        return [
            _challenge_response(
                public,
                contracts.get(public.challenge_id),
                allow_draft_submissions=allow_draft_submissions,
                runtime_available=available_runtimes.get(public.challenge_id, False),
                admissions_closed=public.challenge_id in contracts and policies[
                    public.challenge_id
                ].closed_for(source_fingerprint(public)),
            )
            for public in sorted(
                catalog.values(),
                key=lambda item: (item.language, item.task, item.challenge_id),
            )
        ]

    @app.get("/v1/challenges/{challenge_id}", response_model=ChallengeResponse)
    def get_challenge(challenge_id: str) -> ChallengeResponse:
        public = catalog.get(challenge_id)
        if public is None:
            raise APIError(
                status.HTTP_404_NOT_FOUND,
                code="CHALLENGE_NOT_FOUND",
                message="Challenge not found",
            )
        policies = store.admin_states((challenge_id,))
        return _challenge_response(
            public,
            contracts.get(challenge_id),
            allow_draft_submissions=allow_draft_submissions,
            runtime_available=available_runtimes.get(challenge_id, False),
            admissions_closed=challenge_id in contracts and policies[
                challenge_id
            ].closed_for(source_fingerprint(public)),
        )

    @app.get("/v1/challenges/{challenge_id}/teaching", response_model=TeachingResponse)
    def get_teaching(challenge_id: str) -> JSONResponse:
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        public = catalog.get(challenge_id)
        if public is None:
            raise APIError(
                404, code="CHALLENGE_NOT_FOUND", message="Challenge not found", headers=headers
            )
        try:
            policy = store.admin_states((challenge_id,))[challenge_id]
            content = policy.published_content(source_fingerprint(public))
        except Exception:
            raise APIError(
                503, code="SERVICE_NOT_READY", message="Service is not ready", headers=headers
            ) from None
        return JSONResponse(
            TeachingResponse(
                challenge_id=challenge_id,
                published_revision=policy.published_revision if content is not None else 0,
                content=content,
            ).model_dump(),
            headers=headers,
        )

    def current_user(
        request: Request,
        _credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER_AUTH),
        ] = None,
    ) -> UserRecord:
        try:
            principal = authenticate(request)
        except AuthenticationError:
            principal = None
        if (
            not isinstance(principal, Principal)
            or not principal.subject
            or principal.subject.strip() != principal.subject
        ):
            raise APIError(
                status.HTTP_401_UNAUTHORIZED,
                code="AUTHENTICATION_REQUIRED",
                message="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = store.user_by_subject(principal.subject)
        if user is None:
            raise APIError(
                status.HTTP_403_FORBIDDEN,
                code="USER_NOT_REGISTERED",
                message="Authenticated user is not registered",
            )
        return user

    current_user_dependency = Depends(current_user)

    @app.get("/v1/users/me", response_model=CurrentUserResponse)
    def get_current_user(user: UserRecord = current_user_dependency) -> CurrentUserResponse:
        return CurrentUserResponse(user_id=user.user_id, public_handle=user.public_handle,
                                   role=UserRole(user.role))

    @app.get("/v1/submissions", response_model=OwnerSubmissionPageResponse)
    def get_submissions(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        user: UserRecord = current_user_dependency,
    ) -> OwnerSubmissionPageResponse:
        before_created_at, before_submission_id = _decode_history_cursor(cursor)
        records = store.submissions_for_owner(
            user.user_id,
            limit=limit + 1,
            before_created_at=before_created_at,
            before_submission_id=before_submission_id,
        )
        visible = records[:limit]
        next_cursor = _history_cursor(visible[-1]) if len(records) > limit else None
        return OwnerSubmissionPageResponse(
            items=tuple(_owner_submission_response(item) for item in visible),
            next_cursor=next_cursor,
        )

    @app.post(
        "/v1/submissions",
        response_model=SubmissionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_submission(
        payload: CreateSubmissionRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        user: UserRecord = current_user_dependency,
    ) -> SubmissionResponse:
        selected_contract = contracts.get(payload.challenge_id)
        if selected_contract is None:
            if payload.challenge_id in catalog:
                raise APIError(
                    status.HTTP_409_CONFLICT,
                    code="CHALLENGE_NOT_OPEN",
                    message="Challenge is not open for submissions",
                )
            raise APIError(
                status.HTTP_404_NOT_FOUND,
                code="CHALLENGE_NOT_FOUND",
                message="Challenge not found",
            )
        if (
            not selected_contract.external_activation_ready
            and not allow_draft_submissions
        ):
            raise APIError(
                status.HTTP_409_CONFLICT,
                code="CHALLENGE_NOT_OPEN",
                message="Challenge is not open for submissions",
            )
        if not available_runtimes[payload.challenge_id]:
            raise APIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="CHALLENGE_RUNTIME_UNAVAILABLE",
                message="Challenge runtime is unavailable",
            )
        if not payload.student_prompt.strip():
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="INVALID_STUDENT_PROMPT",
                message="Student prompt must not be empty",
            )
        if (
            len(payload.student_prompt.encode("utf-8"))
            > selected_contract.student_prompt_utf8_bytes
        ):
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="STUDENT_PROMPT_BYTE_LIMIT",
                message="Student prompt exceeds the byte limit",
            )
        if idempotency_key is None or not selected_contract.idempotency_key_is_valid(
            idempotency_key
        ):
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="INVALID_IDEMPOTENCY_KEY",
                message="Idempotency-Key header is missing or invalid",
            )

        try:
            created = store.create_submission(
                user=user,
                idempotency_key=idempotency_key,
                student_prompt=payload.student_prompt,
                contract=selected_contract,
                source_fingerprint=(
                    source_fingerprint(catalog[payload.challenge_id])
                    if payload.challenge_id in catalog else None
                ),
            )
        except ChallengePausedError:
            raise APIError(
                status.HTTP_409_CONFLICT,
                code="CHALLENGE_PAUSED",
                message="New submissions are paused for this challenge",
            ) from None
        except IdempotencyConflictError:
            raise APIError(
                status.HTTP_409_CONFLICT,
                code="IDEMPOTENCY_CONFLICT",
                message="Idempotency key conflicts with an existing request",
            ) from None
        except SubmissionQuotaError as error:
            details: dict[str, object] = {
                "current": error.current,
                "limit": error.limit,
            }
            headers = None
            if error.retry_after_seconds is not None:
                details["retry_after_seconds"] = error.retry_after_seconds
                headers = {"Retry-After": str(error.retry_after_seconds)}
            raise APIError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                code=error.code,
                message="Submission quota exceeded",
                details=details,
                headers=headers,
            ) from None
        except GlobalQueueFullError as error:
            raise APIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="GLOBAL_QUEUE_FULL",
                message="Submission queue is full",
                details={"current": error.current, "limit": error.limit},
            ) from None

        try:
            dispatchers[payload.challenge_id].dispatch_pending()
        except Exception:
            _OUTBOX_LOGGER.error("post_commit_outbox_dispatch_failed")
        return _submission_response(created.submission)

    @app.get("/v1/submissions/{submission_id}", response_model=SubmissionResponse)
    def get_submission(
        submission_id: str,
        user: UserRecord = current_user_dependency,
    ) -> SubmissionResponse:
        submission = store.submission_for_owner(submission_id, user.user_id)
        if submission is None:
            raise APIError(
                status.HTTP_404_NOT_FOUND,
                code="SUBMISSION_NOT_FOUND",
                message="Submission not found",
            )
        return _submission_response(submission)

    @app.get("/v1/submissions/{submission_id}/prompt", response_model=OwnerPromptResponse)
    def get_owner_prompt(
        submission_id: str,
        user: UserRecord = current_user_dependency,
    ) -> JSONResponse:
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        prompt = store.owner_prompt(submission_id, user.user_id)
        if prompt is None:
            raise APIError(
                404, code="SUBMISSION_NOT_FOUND", message="Submission not found", headers=headers
            )
        return JSONResponse(
            OwnerPromptResponse(
                submission_id=prompt.submission_id,
                challenge_id=prompt.challenge_id,
                student_prompt=prompt.student_prompt,
                student_prompt_sha256=prompt.student_prompt_sha256,
            ).model_dump(),
            headers=headers,
        )

    @app.get(
        "/v1/submissions/{submission_id}/result",
        response_model=OwnerTerminalResponse,
    )
    def get_result(
        submission_id: str,
        user: UserRecord = current_user_dependency,
    ) -> OwnerTerminalResponse:
        stored = store.owner_result(submission_id, user.user_id)
        if stored is None:
            raise APIError(
                status.HTTP_404_NOT_FOUND,
                code="SUBMISSION_NOT_FOUND",
                message="Submission not found",
            )
        if stored.status in {SubmissionStatus.QUEUED, SubmissionStatus.RUNNING}:
            raise APIError(
                status.HTTP_409_CONFLICT,
                code="RESULT_NOT_READY",
                message="Result is not ready",
            )
        stored_contract = EvaluationContract.from_mapping(
            json.loads(stored.contract_snapshot_json)
        )
        if stored.status is SubmissionStatus.REJECTED:
            rejection = stored.failure or {
                "code": "TOKEN_LIMIT_EXCEEDED",
                "failure_contract_version": stored_contract.failure_contract_version,
                "retryable": False,
            }
            if set(rejection) != set(stored_contract.owner_failure_fields):
                raise RuntimeError("stored rejection violates the evaluation contract")
            return OwnerRejectedResponse.model_validate(
                {"outcome": "rejected", **rejection}
            )
        if stored.result is not None:
            if set(stored.result) != set(stored_contract.owner_result_fields):
                raise RuntimeError("stored owner result violates the evaluation contract")
            return _OWNER_SUCCESS_ADAPTER.validate_python(
                {"outcome": "succeeded", **stored.result}
            )
        if stored.failure is not None:
            if set(stored.failure) != set(stored_contract.owner_failure_fields):
                raise RuntimeError("stored failure result violates the evaluation contract")
            return OwnerFailureResponse.model_validate(
                {"outcome": "failed", **stored.failure}
            )
        raise RuntimeError("terminal submission has no result")

    @app.get(
        "/v1/leaderboards/{evaluation_identity_sha256}",
        response_model=LeaderboardPageResponse,
    )
    def get_leaderboard(
        evaluation_identity_sha256: Annotated[
            str,
            Path(pattern="^[0-9a-f]{64}$"),
        ],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
    ) -> LeaderboardPageResponse:
        if evaluation_identity_sha256 not in known_leaderboards:
            raise APIError(
                status.HTTP_404_NOT_FOUND,
                code="LEADERBOARD_NOT_FOUND",
                message="Leaderboard not found",
            )
        as_of, after_rank = _decode_leaderboard_cursor(
            cursor,
            evaluation_identity_sha256=evaluation_identity_sha256,
        )
        response = []
        entries = store.leaderboard(
            evaluation_identity_sha256,
            limit=limit + 1,
            as_of=as_of,
            after_rank=after_rank,
        )
        visible = entries[:limit]
        for entry in visible:
            row = entry.to_dict()
            stored_contract = EvaluationContract.from_mapping(
                json.loads(entry.contract_snapshot_json)
            )
            if set(row) != set(stored_contract.public_leaderboard_fields):
                raise RuntimeError("leaderboard row violates the evaluation contract")
            response.append(LeaderboardRowResponse.model_validate(row))
        next_cursor = (
            _leaderboard_cursor(visible[-1], as_of=as_of)
            if len(entries) > limit
            else None
        )
        return LeaderboardPageResponse(items=tuple(response), next_cursor=next_cursor)

    return app
