"""Queue boundary and explicit worker for submission evaluation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from .challenge import ChallengeArtifacts
from .contracts import AGGREGATION_VERSION, SCORER_VERSION
from .mvp_contract import EvaluationContract
from .providers import (
    PROMPT_ENVELOPE_VERSION,
    DeterministicMockProvider,
    ModelProvider,
    ModelRequest,
    PromptEnvelope,
    ProviderContractError,
    ProviderTransportError,
    deterministic_mock_generation_settings,
    deterministic_mock_model_identity,
    deterministic_mock_tokenizer_identity,
)
from .runner import EvaluationPreflightError, run_challenge
from .submission_store import ClaimedSubmission, SubmissionStore


@dataclass(frozen=True, slots=True)
class JobMessage:
    submission_id: str
    evaluation_identity_sha256: str
    contract_snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class JobDelivery:
    delivery_id: int
    message: JobMessage


class JobQueue(Protocol):
    @property
    def routing_key(self) -> str: ...

    def publish(self, message: JobMessage) -> None: ...

    def receive(self) -> JobDelivery | None: ...

    def ack(self, delivery: JobDelivery) -> None: ...

    def nack(self, delivery: JobDelivery) -> None: ...


class InMemoryJobQueue:
    """Process-local at-least-once queue used only by tests and development."""

    def __init__(self, routing_key: str) -> None:
        if not routing_key:
            raise ValueError("routing_key must not be empty")
        self._routing_key = routing_key
        self._messages: deque[JobMessage] = deque()
        self._inflight: dict[int, JobMessage] = {}
        self._next_delivery_id = 1
        self._lock = Lock()

    @property
    def routing_key(self) -> str:
        return self._routing_key

    def publish(self, message: JobMessage) -> None:
        if not isinstance(message, JobMessage):
            raise TypeError("message must be a JobMessage")
        if message.contract_snapshot_sha256 != self._routing_key:
            raise ValueError("message does not match the queue routing key")
        with self._lock:
            self._messages.append(message)

    def receive(self) -> JobDelivery | None:
        with self._lock:
            if not self._messages:
                return None
            delivery_id = self._next_delivery_id
            self._next_delivery_id += 1
            message = self._messages.popleft()
            self._inflight[delivery_id] = message
            return JobDelivery(delivery_id, message)

    def ack(self, delivery: JobDelivery) -> None:
        with self._lock:
            if self._inflight.pop(delivery.delivery_id, None) != delivery.message:
                raise ValueError("delivery is not in flight")

    def nack(self, delivery: JobDelivery) -> None:
        with self._lock:
            if self._inflight.pop(delivery.delivery_id, None) != delivery.message:
                raise ValueError("delivery is not in flight")
            self._messages.append(delivery.message)

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


class SubmissionWorker:
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
        if queue.routing_key != contract.contract_snapshot_sha256:
            raise ValueError("queue does not match the evaluation contract")
        identity = contract.evaluation_identity
        if (
            artifacts.public.challenge_id != contract.challenge_id
            or artifacts.public.status != contract.catalog_status
            or artifacts.public.dataset_sha256 != identity.get("dataset_sha256")
            or artifacts.public.selection_sha256 != identity.get("selection_sha256")
            or artifacts.public.task != identity.get("task")
            or artifacts.public.response_schema_version
            != identity.get("response_schema_version")
            or identity.get("scorer_version") != SCORER_VERSION
            or identity.get("aggregation_version") != AGGREGATION_VERSION
            or identity.get("prompt_envelope_version") != PROMPT_ENVELOPE_VERSION
            or identity.get("model_identity") != deterministic_mock_model_identity()
            or identity.get("generation_settings")
            != deterministic_mock_generation_settings()
            or identity.get("tokenizer_identity")
            != deterministic_mock_tokenizer_identity()
        ):
            raise ValueError("challenge artifacts do not match the evaluation contract")
        self._store = store
        self._queue = queue
        self._contract = contract
        self._artifacts = artifacts
        self._provider = provider
        self._request_preflight = MockRequestPreflight(contract)

    def run_once(self) -> bool:
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
            self._queue.nack(delivery)
            return False
        claim_attempt = self._store.claim_submission(
            message.submission_id,
            evaluation_identity_sha256=self._contract.evaluation_identity_sha256,
            contract_snapshot_sha256=self._contract.contract_snapshot_sha256,
            lease_seconds=min(30, self._contract.job_deadline_seconds),
            max_attempts=self._contract.max_attempts,
            max_running_per_user=self._contract.max_running_submissions_per_user,
        )
        if claim_attempt.claim is None:
            if claim_attempt.retry_later:
                self._queue.nack(delivery)
            else:
                self._queue.ack(delivery)
            return False
        claim = claim_attempt.claim

        if (
            claim.contract_snapshot_json != self._contract.snapshot_json
            or claim.evaluation_identity_sha256
            != self._contract.evaluation_identity_sha256
        ):
            self._fail(claim, "RUNTIME_MISCONFIGURATION")
            self._queue.ack(delivery)
            return True
        if self._store.claim_deadline_expired(claim):
            self._fail(claim, "JOB_DEADLINE")
            self._queue.ack(delivery)
            return True

        try:
            aggregate = run_challenge(
                self._artifacts,
                self._provider,
                student_prompt=claim.student_prompt,
                request_preflight=self._request_preflight,
            )
        except TokenLimitExceeded:
            if not self._store.complete_rejected(claim):
                self._store.expire_leases(
                    evaluation_identity_sha256=self._contract.evaluation_identity_sha256
                )
            self._queue.ack(delivery)
            return True
        except TimeoutError:
            self._fail(claim, "PROVIDER_TIMEOUT")
            self._queue.ack(delivery)
            return True
        except ProviderTransportError:
            self._fail(claim, "PROVIDER_TRANSPORT")
            self._queue.ack(delivery)
            return True
        except EvaluationPreflightError:
            self._fail(claim, "DATASET_INTEGRITY")
            self._queue.ack(delivery)
            return True
        except ProviderContractError:
            self._fail(claim, "RUNTIME_MISCONFIGURATION")
            self._queue.ack(delivery)
            return True
        except (OSError, ValueError):
            self._fail(claim, "DATASET_INTEGRITY")
            self._queue.ack(delivery)
            return True
        except Exception:
            self._fail(claim, "RUNTIME_MISCONFIGURATION")
            self._queue.ack(delivery)
            return True

        if self._store.claim_deadline_expired(claim):
            self._fail(claim, "JOB_DEADLINE")
            self._queue.ack(delivery)
            return True
        try:
            owner_result = self._contract.owner_result(
                aggregate,
                student_prompt_sha256=claim.student_prompt_sha256,
            )
        except Exception:
            self._fail(claim, "RUNTIME_MISCONFIGURATION")
            self._queue.ack(delivery)
            return True
        if not self._store.complete_success(claim, owner_result=owner_result):
            self._fail(claim, "JOB_DEADLINE")
            self._queue.ack(delivery)
            return False
        self._queue.ack(delivery)
        return True

    def _fail(self, claim: ClaimedSubmission, code: str) -> bool:
        retryable = code in self._contract.retryable_failure_codes
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
