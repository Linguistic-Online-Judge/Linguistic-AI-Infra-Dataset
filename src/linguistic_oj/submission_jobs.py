"""Queue boundary and explicit worker for submission evaluation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Protocol

from .challenge import ChallengeArtifacts
from .contracts import AGGREGATION_VERSION, SCORER_VERSION
from .mvp_contract import EvaluationContract
from .providers import (
    PROMPT_ENVELOPE_VERSION,
    DeterministicMockProvider,
    ModelProvider,
    ModelRequest,
    OpenAICompatibleProvider,
    PromptEnvelope,
    ProviderContractError,
    ProviderTimeoutError,
    ProviderTransportError,
    deterministic_mock_generation_settings,
    deterministic_mock_model_identity,
    deterministic_mock_tokenizer_identity,
)
from .qwen_runtime import (
    QWEN_EVALUATION_CONTRACT_VERSION,
    QwenTokenizerPreflight,
    QwenTokenLimitExceeded,
    attest_qwen_runtime_from_snapshot,
)
from .runner import (
    EvaluationPreflightError,
    JobDeadline,
    JobDeadlineExceeded,
    run_challenge,
)
from .submission_store import (
    SQLITE_LOCK_TIMEOUT_SECONDS,
    ClaimedSubmission,
    SubmissionStore,
)

_CLAIM_PROCESSING_BUDGET_SECONDS = 5.0
_VISIBILITY_SAFETY_SECONDS = 5.0
QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS = int(
    SQLITE_LOCK_TIMEOUT_SECONDS + _CLAIM_PROCESSING_BUDGET_SECONDS + _VISIBILITY_SAFETY_SECONDS
)


@dataclass(frozen=True, slots=True)
class JobMessage:
    submission_id: str
    evaluation_identity_sha256: str
    contract_snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class JobDelivery:
    delivery_id: str
    message: JobMessage
    receipt_token: str


class JobQueue(Protocol):
    @property
    def routing_key(self) -> str: ...

    @property
    def visibility_timeout_seconds(self) -> float: ...

    def publish(self, message: JobMessage) -> None: ...

    def receive(self) -> JobDelivery | None: ...

    def ack(self, delivery: JobDelivery) -> bool: ...

    def nack(self, delivery: JobDelivery) -> bool: ...


class InMemoryJobQueue:
    """Process-local at-least-once queue used only by tests and development."""

    def __init__(self, routing_key: str, *, visibility_timeout_seconds: float = 45.0) -> None:
        if not routing_key:
            raise ValueError("routing_key must not be empty")
        if visibility_timeout_seconds <= 0:
            raise ValueError("visibility_timeout_seconds must be positive")
        self._routing_key = routing_key
        self._visibility_timeout_seconds = float(visibility_timeout_seconds)
        self._messages: deque[JobMessage] = deque()
        self._inflight: dict[str, tuple[JobMessage, float]] = {}
        self._next_delivery_id = 1
        self._lock = Lock()

    @property
    def routing_key(self) -> str:
        return self._routing_key

    @property
    def visibility_timeout_seconds(self) -> float:
        return self._visibility_timeout_seconds

    def publish(self, message: JobMessage) -> None:
        if not isinstance(message, JobMessage):
            raise TypeError("message must be a JobMessage")
        if message.contract_snapshot_sha256 != self._routing_key:
            raise ValueError("message does not match the queue routing key")
        with self._lock:
            self._messages.append(message)

    def receive(self) -> JobDelivery | None:
        with self._lock:
            now = monotonic()
            expired = [
                delivery_id
                for delivery_id, (_, received_at) in self._inflight.items()
                if now - received_at >= self._visibility_timeout_seconds
            ]
            for delivery_id in expired:
                message, _ = self._inflight.pop(delivery_id)
                self._messages.append(message)
            if not self._messages:
                return None
            delivery_id = str(self._next_delivery_id)
            self._next_delivery_id += 1
            message = self._messages.popleft()
            self._inflight[delivery_id] = (message, now)
            return JobDelivery(delivery_id, message, delivery_id)

    def ack(self, delivery: JobDelivery) -> bool:
        with self._lock:
            inflight = self._inflight.get(delivery.delivery_id)
            if (
                inflight is None
                or inflight[0] != delivery.message
                or delivery.receipt_token != delivery.delivery_id
            ):
                return False
            self._inflight.pop(delivery.delivery_id)
            return True

    def nack(self, delivery: JobDelivery) -> bool:
        with self._lock:
            inflight = self._inflight.get(delivery.delivery_id)
            if (
                inflight is None
                or inflight[0] != delivery.message
                or delivery.receipt_token != delivery.delivery_id
            ):
                return False
            self._inflight.pop(delivery.delivery_id)
            self._messages.append(delivery.message)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)


class OutboxDispatcher:
    def __init__(
        self,
        store: SubmissionStore,
        queue: JobQueue,
        contract: EvaluationContract,
    ) -> None:
        if queue.routing_key != contract.contract_snapshot_sha256:
            raise ValueError("queue does not match the evaluation contract")
        self._store = store
        self._queue = queue
        self._evaluation_identity_sha256 = contract.evaluation_identity_sha256
        self._contract_snapshot_sha256 = contract.contract_snapshot_sha256

    def matches(self, store: SubmissionStore, contract: EvaluationContract) -> bool:
        return (
            self._store is store
            and self._evaluation_identity_sha256 == contract.evaluation_identity_sha256
            and self._contract_snapshot_sha256 == contract.contract_snapshot_sha256
        )

    def dispatch_pending(self) -> int:
        dispatched = 0
        for submission_id in self._store.unpublished_submission_ids(
            self._evaluation_identity_sha256,
            self._contract_snapshot_sha256,
        ):
            self._queue.publish(
                JobMessage(
                    submission_id,
                    self._evaluation_identity_sha256,
                    self._contract_snapshot_sha256,
                )
            )
            self._store.mark_outbox_published(submission_id)
            dispatched += 1
        return dispatched

    def recover_published_queued(self) -> int:
        recovered = 0
        for submission_id in self._store.published_queued_submission_ids(
            self._evaluation_identity_sha256,
            self._contract_snapshot_sha256,
        ):
            self._queue.publish(
                JobMessage(
                    submission_id,
                    self._evaluation_identity_sha256,
                    self._contract_snapshot_sha256,
                )
            )
            recovered += 1
        return recovered


class TokenLimitExceeded(ValueError):
    pass


class MockRequestPreflight:
    """Deterministic code-point budget for the non-production Mock identity."""

    def __init__(self, contract: EvaluationContract) -> None:
        self._contract = contract

    def __call__(self, requests: tuple[ModelRequest, ...]) -> None:
        if not requests:
            raise ValueError("challenge must contain at least one request")
        if len(requests[0].student_prompt) > self._contract.student_prompt_tokens:
            raise TokenLimitExceeded("student prompt exceeds the Mock token limit")

        generation_tokens = int(
            self._contract.evaluation_identity["generation_settings"]["max_tokens"]
        )
        for request in requests:
            messages = PromptEnvelope.from_request(request).to_messages()
            rendered_tokens = sum(len(message["content"]) for message in messages)
            if rendered_tokens > self._contract.max_rendered_input_tokens:
                raise TokenLimitExceeded("rendered Mock input exceeds the token limit")
            if rendered_tokens + generation_tokens > self._contract.model_context_tokens:
                raise TokenLimitExceeded("rendered Mock input exceeds the context window")


def _artifacts_match_contract(
    artifacts: ChallengeArtifacts,
    contract: EvaluationContract,
) -> bool:
    identity = contract.evaluation_identity
    return (
        artifacts.public.challenge_id == contract.challenge_id
        and artifacts.public.status == contract.catalog_status
        and artifacts.public.dataset_sha256 == identity.get("dataset_sha256")
        and artifacts.public.selection_sha256 == identity.get("selection_sha256")
        and artifacts.public.task == identity.get("task")
        and artifacts.public.response_schema_version
        == identity.get("response_schema_version")
        and identity.get("scorer_version") == SCORER_VERSION
        and identity.get("aggregation_version") == AGGREGATION_VERSION
        and identity.get("prompt_envelope_version") == PROMPT_ENVELOPE_VERSION
    )


class _SubmissionWorkerCore:
    def __init__(
        self,
        *,
        store: SubmissionStore,
        queue: JobQueue,
        contract: EvaluationContract,
        artifacts: ChallengeArtifacts,
        provider: ModelProvider,
        lease_seconds: int,
        request_preflight: Callable[[tuple[ModelRequest, ...]], None],
        require_termination_confirmation: bool,
    ) -> None:
        if queue.routing_key != contract.contract_snapshot_sha256:
            raise ValueError("queue does not match the evaluation contract")
        minimum_visibility = (
            lease_seconds
            + SQLITE_LOCK_TIMEOUT_SECONDS
            + _CLAIM_PROCESSING_BUDGET_SECONDS
            + _VISIBILITY_SAFETY_SECONDS
        )
        if queue.visibility_timeout_seconds < minimum_visibility:
            raise ValueError(
                "queue visibility must cover claim acquisition and exceed the database "
                "lease by five seconds"
            )
        if not _artifacts_match_contract(artifacts, contract):
            raise ValueError("challenge artifacts do not match the evaluation contract")
        self._store = store
        self._queue = queue
        self._contract = contract
        self._artifacts = artifacts
        self._provider = provider
        self._lease_seconds = lease_seconds
        self._request_preflight = request_preflight
        self._require_termination_confirmation = require_termination_confirmation

    def run_once(self) -> bool:
        if (
            self._require_termination_confirmation
            and isinstance(self._provider, OpenAICompatibleProvider)
            and self._provider.has_active_request
        ):
            # Do not claim unrelated work while an ambiguous remote request remains live.
            return False
        self._store.expire_leases(
            evaluation_identity_sha256=self._contract.evaluation_identity_sha256
        )
        delivery = self._queue.receive()
        if delivery is None:
            return False
        message = delivery.message
        if (
            message.evaluation_identity_sha256
            != self._contract.evaluation_identity_sha256
            or message.contract_snapshot_sha256
            != self._contract.contract_snapshot_sha256
        ):
            self._queue.ack(delivery)
            return False
        claim_attempt = self._store.claim_submission(
            message.submission_id,
            evaluation_identity_sha256=self._contract.evaluation_identity_sha256,
            contract_snapshot_sha256=self._contract.contract_snapshot_sha256,
            lease_seconds=self._lease_seconds,
            max_attempts=self._contract.max_attempts,
            max_running_per_user=self._contract.max_running_submissions_per_user,
        )
        if claim_attempt.claim is None:
            if not claim_attempt.retry_later:
                self._queue.ack(delivery)
            return False
        claim = claim_attempt.claim

        if (
            claim.contract_snapshot_json != self._contract.snapshot_json
            or claim.evaluation_identity_sha256
            != self._contract.evaluation_identity_sha256
        ):
            self._handle_failure(delivery, claim, "RUNTIME_MISCONFIGURATION")
            return True
        if self._store.claim_deadline_expired(claim):
            self._handle_failure(delivery, claim, "JOB_DEADLINE")
            return True

        try:
            aggregate = run_challenge(
                self._artifacts,
                self._provider,
                student_prompt=claim.student_prompt,
                request_preflight=self._request_preflight,
                deadline=JobDeadline.from_timestamp(claim.deadline_at),
            )
        except (TokenLimitExceeded, QwenTokenLimitExceeded):
            if not self._store.complete_rejected(claim):
                self._store.expire_leases(
                    evaluation_identity_sha256=self._contract.evaluation_identity_sha256
                )
            self._queue.ack(delivery)
            return True
        except JobDeadlineExceeded:
            self._handle_failure(delivery, claim, "JOB_DEADLINE")
            return True
        except ProviderTimeoutError as error:
            self._handle_failure(
                delivery,
                claim,
                "PROVIDER_TIMEOUT",
                retry_allowed=self._request_retry_allowed(error.termination_confirmed),
            )
            return True
        except TimeoutError:
            self._handle_failure(
                delivery,
                claim,
                "PROVIDER_TIMEOUT",
                retry_allowed=not self._require_termination_confirmation,
            )
            return True
        except ProviderTransportError as error:
            self._handle_failure(
                delivery,
                claim,
                "PROVIDER_TRANSPORT",
                retry_allowed=self._request_retry_allowed(error.termination_confirmed),
            )
            return True
        except EvaluationPreflightError:
            self._handle_failure(delivery, claim, "DATASET_INTEGRITY")
            return True
        except ProviderContractError:
            self._handle_failure(delivery, claim, "RUNTIME_MISCONFIGURATION")
            return True
        except (OSError, ValueError):
            self._handle_failure(delivery, claim, "DATASET_INTEGRITY")
            return True
        except Exception:
            self._handle_failure(delivery, claim, "RUNTIME_MISCONFIGURATION")
            return True

        if self._store.claim_deadline_expired(claim):
            self._handle_failure(delivery, claim, "JOB_DEADLINE")
            return True
        try:
            owner_result = self._contract.owner_result(
                aggregate,
                student_prompt_sha256=claim.student_prompt_sha256,
            )
        except Exception:
            self._handle_failure(delivery, claim, "RUNTIME_MISCONFIGURATION")
            return True
        if not self._store.complete_success(claim, owner_result=owner_result):
            self._handle_failure(delivery, claim, "JOB_DEADLINE")
            return False
        self._queue.ack(delivery)
        return True

    def _handle_failure(
        self,
        delivery: JobDelivery,
        claim: ClaimedSubmission,
        code: str,
        *,
        retry_allowed: bool = True,
    ) -> None:
        if (
            retry_allowed
            and code in self._contract.retryable_failure_codes
            and self._store.retry_submission(
                claim,
                max_attempts=self._contract.max_attempts,
            )
        ):
            self._queue.nack(delivery)
            return
        self._fail(claim, code, retryable=False)
        self._queue.ack(delivery)

    def _fail(self, claim: ClaimedSubmission, code: str, *, retryable: bool) -> bool:
        completed = self._store.complete_failure(
            claim,
            failure_contract_version=self._contract.failure_contract_version,
            code=code,
            retryable=retryable,
        )
        if not completed:
            self._store.expire_leases(
                evaluation_identity_sha256=self._contract.evaluation_identity_sha256
            )
        return completed

    def _request_retry_allowed(self, termination_confirmed: bool) -> bool:
        return not self._require_termination_confirmation or termination_confirmed


class SubmissionWorker(_SubmissionWorkerCore):
    """Mock-only worker kept separate from the attested Qwen runtime."""

    def __init__(
        self,
        *,
        store: SubmissionStore,
        queue: JobQueue,
        contract: EvaluationContract,
        artifacts: ChallengeArtifacts,
        provider: ModelProvider,
    ) -> None:
        if not isinstance(provider, DeterministicMockProvider):
            raise ValueError("the Mock submission slice requires DeterministicMockProvider")
        if not contract.uses_mock_runtime:
            raise ValueError("the deterministic Mock provider requires a mock evaluation identity")
        identity = contract.evaluation_identity
        if (
            identity.get("model_identity") != deterministic_mock_model_identity()
            or identity.get("generation_settings")
            != deterministic_mock_generation_settings()
            or identity.get("tokenizer_identity")
            != deterministic_mock_tokenizer_identity()
        ):
            raise ValueError("challenge artifacts do not match the evaluation contract")
        super().__init__(
            store=store,
            queue=queue,
            contract=contract,
            artifacts=artifacts,
            provider=provider,
            lease_seconds=min(30, contract.job_deadline_seconds),
            request_preflight=MockRequestPreflight(contract),
            require_termination_confirmation=False,
        )


class QwenSubmissionWorker(_SubmissionWorkerCore):
    """Qwen worker that fails closed unless every pinned runtime identity matches."""

    def __init__(
        self,
        *,
        store: SubmissionStore,
        queue: JobQueue,
        contract: EvaluationContract,
        artifacts: ChallengeArtifacts,
        provider: OpenAICompatibleProvider,
        tokenizer_snapshot_path: Path,
        launch_evidence_path: Path,
    ) -> None:
        if contract.contract_version != QWEN_EVALUATION_CONTRACT_VERSION:
            raise ValueError("Qwen worker requires mvp-evaluation-v2")
        if not contract.retry_requires_prior_request_terminated:
            raise ValueError("Qwen retry policy must require prior request termination")
        runtime = attest_qwen_runtime_from_snapshot(
            contract,
            provider,
            tokenizer_snapshot_path=tokenizer_snapshot_path,
            launch_evidence_path=launch_evidence_path,
        )
        request_preflight = QwenTokenizerPreflight(
            contract,
            runtime.tokenizer,
            runtime.tokenizer_identity,
        )
        super().__init__(
            store=store,
            queue=queue,
            contract=contract,
            artifacts=artifacts,
            provider=provider,
            lease_seconds=contract.job_deadline_seconds,
            request_preflight=request_preflight,
            require_termination_confirmation=True,
        )
