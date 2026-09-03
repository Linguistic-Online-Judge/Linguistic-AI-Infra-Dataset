"""Synchronous offline orchestration from safe model input to aggregate score."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from .aggregation import ChallengeAggregateResult, SampleEvaluationOutcome, aggregate_challenge
from .challenge import (
    ChallengeArtifacts,
    ManifestSample,
    load_challenge_artifacts,
    validate_challenge_artifacts,
    validated_gold_item_count,
)
from .dataset import DatasetSample, load_dataset_samples_by_id
from .evaluation import (
    DependencyArc,
    score_dependencies,
    score_segmentation,
    score_tags,
    score_transliteration,
)
from .model_inputs import (
    SafeModelInput,
    build_model_input,
    canonicalize_model_input,
    response_expectations,
)
from .providers import (
    PROMPT_ENVELOPE_VERSION,
    STRUCTURED_OUTPUT_CONTRACT_VERSION,
    DeterministicMockProvider,
    GenerationSettings,
    ModelGeneration,
    ModelIdentity,
    ModelProvider,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderContractError,
)
from .responses import (
    DependencyResponse,
    ParseErrorCode,
    SegmentationResponse,
    TaggingResponse,
    TaskType,
    TransliterationResponse,
    parse_model_response,
)


class EvaluationPreflightError(ValueError):
    """Raised before provider calls when challenge data is inconsistent."""


class RunnerInvariantError(RuntimeError):
    """Raised when trusted platform components violate their internal contract."""


class JobDeadlineExceeded(TimeoutError):
    """Raised when the complete evaluation job has no time remaining."""


GENERATION_DIAGNOSTICS_VERSION = "generation-diagnostics-v1"


@dataclass(slots=True)
class ChallengeRunDiagnostics:
    """Aggregate-only model runtime observations for one challenge run."""

    _request_count: int = 0
    _request_latency_seconds_total: float = 0.0
    _request_latency_seconds_min: float | None = None
    _request_latency_seconds_max: float | None = None
    _generated_token_count_observed: int = 0
    _generated_token_count_missing: int = 0
    _generated_tokens_total: int = 0
    _generated_tokens_max: int | None = None
    _response_utf8_bytes_total: int = 0
    _response_utf8_bytes_min: int | None = None
    _response_utf8_bytes_max: int | None = None
    _finish_reasons: Counter[str] = field(default_factory=Counter)
    _parse_errors: Counter[str] = field(default_factory=Counter)
    _execution_seconds: float | None = None

    def record(
        self,
        generation: ModelGeneration,
        outcome: SampleEvaluationOutcome,
        *,
        request_latency_seconds: float,
    ) -> None:
        if self._execution_seconds is not None:
            raise RuntimeError("cannot record diagnostics after run completion")
        if not isinstance(generation, ModelGeneration):
            raise TypeError("generation must be ModelGeneration")
        if not isinstance(outcome, SampleEvaluationOutcome):
            raise TypeError("outcome must be SampleEvaluationOutcome")
        if (
            type(request_latency_seconds) not in {int, float}
            or not math.isfinite(request_latency_seconds)
            or request_latency_seconds < 0
        ):
            raise ValueError("request_latency_seconds must be finite and non-negative")

        latency = float(request_latency_seconds)
        response_bytes = len(generation.raw_text.encode("utf-8"))
        self._request_count += 1
        self._request_latency_seconds_total += latency
        self._request_latency_seconds_min = (
            latency
            if self._request_latency_seconds_min is None
            else min(self._request_latency_seconds_min, latency)
        )
        self._request_latency_seconds_max = (
            latency
            if self._request_latency_seconds_max is None
            else max(self._request_latency_seconds_max, latency)
        )
        self._response_utf8_bytes_total += response_bytes
        self._response_utf8_bytes_min = (
            response_bytes
            if self._response_utf8_bytes_min is None
            else min(self._response_utf8_bytes_min, response_bytes)
        )
        self._response_utf8_bytes_max = (
            response_bytes
            if self._response_utf8_bytes_max is None
            else max(self._response_utf8_bytes_max, response_bytes)
        )

        if generation.generated_token_count is None:
            self._generated_token_count_missing += 1
        else:
            self._generated_token_count_observed += 1
            self._generated_tokens_total += generation.generated_token_count
            self._generated_tokens_max = (
                generation.generated_token_count
                if self._generated_tokens_max is None
                else max(self._generated_tokens_max, generation.generated_token_count)
            )
        self._finish_reasons[generation.finish_reason or "missing"] += 1
        if outcome.error_code is not None:
            self._parse_errors[outcome.error_code.value] += 1

    def complete(self, *, execution_seconds: float) -> None:
        if self._execution_seconds is not None:
            raise RuntimeError("run diagnostics are already complete")
        if (
            type(execution_seconds) not in {int, float}
            or not math.isfinite(execution_seconds)
            or execution_seconds < 0
        ):
            raise ValueError("execution_seconds must be finite and non-negative")
        self._execution_seconds = float(execution_seconds)

    def to_dict(self) -> dict[str, Any]:
        observed_tokens = self._generated_token_count_observed
        return {
            "schema_version": GENERATION_DIAGNOSTICS_VERSION,
            "execution_seconds": self._execution_seconds,
            "request_count": self._request_count,
            "request_latency_seconds": {
                "total": self._request_latency_seconds_total,
                "minimum": self._request_latency_seconds_min,
                "maximum": self._request_latency_seconds_max,
                "mean": (
                    self._request_latency_seconds_total / self._request_count
                    if self._request_count
                    else None
                ),
            },
            "generated_tokens": {
                "observed_count": observed_tokens,
                "missing_count": self._generated_token_count_missing,
                "total": self._generated_tokens_total,
                "maximum": self._generated_tokens_max,
                "mean_observed": (
                    self._generated_tokens_total / observed_tokens if observed_tokens else None
                ),
            },
            "response_utf8_bytes": {
                "total": self._response_utf8_bytes_total,
                "minimum": self._response_utf8_bytes_min,
                "maximum": self._response_utf8_bytes_max,
                "mean": (
                    self._response_utf8_bytes_total / self._request_count
                    if self._request_count
                    else None
                ),
            },
            "finish_reasons": dict(sorted(self._finish_reasons.items())),
            "parse_errors": dict(sorted(self._parse_errors.items())),
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class JobDeadline:
    """One absolute UTC deadline shared by every sample and retry attempt."""

    expires_at: datetime
    clock: Callable[[], datetime] = _utc_now

    def __post_init__(self) -> None:
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be a timezone-aware datetime")
        if not callable(self.clock):
            raise TypeError("clock must be callable")

    @classmethod
    def from_timestamp(
        cls,
        value: str,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> JobDeadline:
        if not isinstance(value, str):
            raise TypeError("deadline timestamp must be a string")
        try:
            expires_at = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("deadline timestamp must be ISO 8601") from None
        return cls(expires_at=expires_at, clock=clock)

    def remaining_seconds(self) -> float:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise RuntimeError("deadline clock must return a timezone-aware datetime")
        return (self.expires_at - now).total_seconds()

    def require_remaining(self) -> float:
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise JobDeadlineExceeded("evaluation job deadline expired")
        return remaining


@dataclass(frozen=True, slots=True)
class _PreparedSample:
    manifest_sample: ManifestSample
    dataset_sample: DatasetSample
    model_input: SafeModelInput


def _require_gold_list(sample: DatasetSample, task: TaskType) -> list[Any]:
    gold = sample.answers.get(task.value)
    if not isinstance(gold, list):
        raise RunnerInvariantError(f"Sample {sample.id} gold data is not a list")
    return gold


def _score_parsed_response(
    sample: DatasetSample,
    task: TaskType,
    value: SegmentationResponse | TaggingResponse | DependencyResponse | TransliterationResponse,
):
    gold = _require_gold_list(sample, task)
    if task is TaskType.SEGMENTATION:
        if not isinstance(value, SegmentationResponse):
            raise RunnerInvariantError("segmentation parser returned the wrong response type")
        return score_segmentation(gold, value.tokens)
    if task in {TaskType.UPOS, TaskType.XPOS}:
        if not isinstance(value, TaggingResponse):
            raise RunnerInvariantError("tagging parser returned the wrong response type")
        return score_tags(gold, value.tags)
    if task is TaskType.DEPENDENCY:
        if not isinstance(value, DependencyResponse):
            raise RunnerInvariantError("dependency parser returned the wrong response type")
        gold_arcs = tuple(
            DependencyArc(token_id=arc[0], head_id=arc[2], deprel=arc[4]) for arc in gold
        )
        predicted_arcs = tuple(
            DependencyArc(
                token_id=arc.token_id,
                head_id=arc.head_id,
                deprel=arc.deprel,
            )
            for arc in value.arcs
        )
        return score_dependencies(gold_arcs, predicted_arcs)
    if not isinstance(value, TransliterationResponse):
        raise RunnerInvariantError("transliteration parser returned the wrong response type")
    return score_transliteration(gold, value.transliterations)


def evaluate_raw_response(
    *,
    sample: DatasetSample,
    manifest_sample: ManifestSample,
    task: TaskType | str,
    model_input: SafeModelInput,
    raw_response: str,
) -> SampleEvaluationOutcome:
    """Parse and score one raw response without exposing parser details downstream."""

    task_type = TaskType(task)
    if not isinstance(sample, DatasetSample):
        raise TypeError("sample must be a DatasetSample")
    if not isinstance(manifest_sample, ManifestSample):
        raise TypeError("manifest_sample must be a ManifestSample")
    if not isinstance(raw_response, str):
        raise ProviderContractError("provider raw response must be a string")
    if sample.id != manifest_sample.sample_id:
        raise EvaluationPreflightError("dataset sample does not match the manifest sample")
    try:
        model_input = canonicalize_model_input(task_type, model_input)
    except (TypeError, ValueError) as error:
        raise EvaluationPreflightError(
            "model input does not match the challenge task"
        ) from error

    gold_items = validated_gold_item_count(sample, task_type)
    if gold_items != manifest_sample.gold_items:
        raise EvaluationPreflightError(
            f"Sample {sample.id} gold denominator does not match the manifest"
        )

    expected_count, expected_token_ids = response_expectations(task_type, model_input)
    parsed = parse_model_response(
        task_type,
        raw_response,
        expected_count=expected_count,
        expected_token_ids=expected_token_ids,
    )
    if parsed.error is not None:
        if parsed.error.code is ParseErrorCode.UNKNOWN_TASK:
            raise RunnerInvariantError("known task was rejected by the response parser")
        return SampleEvaluationOutcome.malformed(
            sample.id,
            task_type,
            gold_items=manifest_sample.gold_items,
            error_code=parsed.error.code,
        )
    if parsed.value is None or parsed.task is not task_type:
        raise RunnerInvariantError("response parser returned an inconsistent success result")

    score = _score_parsed_response(sample, task_type, parsed.value)
    return SampleEvaluationOutcome.scored(sample.id, task_type, score)


def _prepare_samples(artifacts: ChallengeArtifacts) -> tuple[_PreparedSample, ...]:
    validate_challenge_artifacts(artifacts)
    task = TaskType(artifacts.public.task)
    dataset_samples = load_dataset_samples_by_id(
        artifacts.dataset_path,
        artifacts.private.sample_ids,
    )
    prepared: list[_PreparedSample] = []
    for manifest_sample, dataset_sample in zip(
        artifacts.private.samples,
        dataset_samples,
        strict=True,
    ):
        if dataset_sample.id != manifest_sample.sample_id:
            raise EvaluationPreflightError("dataset sample order does not match the manifest")
        if dataset_sample.language != artifacts.public.language:
            raise EvaluationPreflightError(
                f"Sample {dataset_sample.id} language does not match the challenge"
            )
        if dataset_sample.treebank != artifacts.public.treebank:
            raise EvaluationPreflightError(
                f"Sample {dataset_sample.id} treebank does not match the challenge"
            )
        if task.value not in dataset_sample.tasks_available:
            raise EvaluationPreflightError(
                f"Sample {dataset_sample.id} does not declare task {task.value}"
            )
        gold_items = validated_gold_item_count(dataset_sample, task)
        if gold_items != manifest_sample.gold_items:
            raise EvaluationPreflightError(
                f"Sample {dataset_sample.id} gold denominator does not match the manifest"
            )
        prepared.append(
            _PreparedSample(
                manifest_sample=manifest_sample,
                dataset_sample=dataset_sample,
                model_input=build_model_input(dataset_sample, task),
            )
        )
    return tuple(prepared)


def run_challenge(
    artifacts: ChallengeArtifacts,
    provider: ModelProvider,
    *,
    student_prompt: str,
    request_preflight: Callable[[tuple[ModelRequest, ...]], None] | None = None,
    deadline: JobDeadline | None = None,
    diagnostics: ChallengeRunDiagnostics | None = None,
) -> ChallengeAggregateResult:
    """Run one complete challenge without returning partial results."""

    if not isinstance(artifacts, ChallengeArtifacts):
        raise TypeError("artifacts must be ChallengeArtifacts")
    if not isinstance(provider, ModelProvider):
        raise TypeError("provider must implement ModelProvider")
    if not isinstance(student_prompt, str) or not student_prompt.strip():
        raise ValueError("student_prompt must be a non-empty string")
    if deadline is not None and not isinstance(deadline, JobDeadline):
        raise TypeError("deadline must be a JobDeadline")
    if diagnostics is not None and not isinstance(diagnostics, ChallengeRunDiagnostics):
        raise TypeError("diagnostics must be ChallengeRunDiagnostics")

    execution_started = monotonic() if diagnostics is not None else None
    prepared_samples = _prepare_samples(artifacts)
    task = TaskType(artifacts.public.task)
    requests = tuple(
        ModelRequest(
            task=task,
            language=artifacts.public.language,
            treebank=artifacts.public.treebank,
            student_prompt=student_prompt,
            model_input=prepared.model_input,
        )
        for prepared in prepared_samples
    )
    if request_preflight is not None:
        request_preflight(requests)
    if deadline is not None:
        deadline.require_remaining()

    outcomes: list[SampleEvaluationOutcome] = []
    for prepared, request in zip(prepared_samples, requests, strict=True):
        timeout_seconds = deadline.require_remaining() if deadline is not None else None
        request_started = monotonic() if diagnostics is not None else None
        generation = provider.generate(request, timeout_seconds=timeout_seconds)
        request_finished = monotonic() if diagnostics is not None else None
        if not isinstance(generation, ModelGeneration):
            raise ProviderContractError("provider must return ModelGeneration")
        if deadline is not None:
            deadline.require_remaining()
        outcome = evaluate_raw_response(
            sample=prepared.dataset_sample,
            manifest_sample=prepared.manifest_sample,
            task=task,
            model_input=prepared.model_input,
            raw_response=generation.raw_text,
        )
        outcomes.append(outcome)
        if diagnostics is not None:
            if request_started is None or request_finished is None:
                raise RunnerInvariantError("diagnostic timer was not initialized")
            diagnostics.record(
                generation,
                outcome,
                request_latency_seconds=request_finished - request_started,
            )
    aggregate = aggregate_challenge(artifacts, outcomes)
    if diagnostics is not None:
        if execution_started is None:
            raise RunnerInvariantError("execution timer was not initialized")
        diagnostics.complete(execution_seconds=monotonic() - execution_started)
    return aggregate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a challenge with a model provider.")
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--provider", choices=["mock", "openai"], default="mock")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--model-revision")
    parser.add_argument("--runtime-version")
    parser.add_argument("--api-key-env")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument(
        "--structured-json",
        action="store_true",
        help="Calibration-only dynamic JSON schema; changes the evaluation protocol.",
    )
    return parser.parse_args()


def _provider_from_args(args: argparse.Namespace) -> ModelProvider:
    if args.provider == "mock":
        return DeterministicMockProvider()

    required = {
        "base_url": args.base_url,
        "model": args.model,
        "model_revision": args.model_revision,
        "runtime_version": args.runtime_version,
    }
    missing = sorted(name.replace("_", "-") for name, value in required.items() if not value)
    if missing:
        raise ValueError(f"openai provider requires: {', '.join(missing)}")

    api_key = None
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)
        if not api_key:
            raise ValueError("configured API key environment variable is empty")

    return OpenAICompatibleProvider(
        base_url=args.base_url,
        identity=ModelIdentity(
            model=args.model,
            revision=args.model_revision,
            runtime="vllm",
            runtime_version=args.runtime_version,
        ),
        settings=GenerationSettings(max_tokens=args.max_tokens),
        timeout_seconds=args.timeout_seconds,
        api_key=api_key,
        structured_json=args.structured_json,
    )


def main() -> None:
    args = parse_args()
    try:
        student_prompt = args.prompt_file.read_text(encoding="utf-8")
        artifacts = load_challenge_artifacts(
            args.public,
            args.private,
            dataset_path=args.dataset,
        )
        provider = _provider_from_args(args)
        diagnostics = (
            ChallengeRunDiagnostics() if isinstance(provider, OpenAICompatibleProvider) else None
        )
        result = run_challenge(
            artifacts,
            provider,
            student_prompt=student_prompt,
            diagnostics=diagnostics,
        )
        report = result.to_dict()
        report["student_prompt_sha256"] = hashlib.sha256(
            student_prompt.encode("utf-8")
        ).hexdigest()
        if isinstance(provider, OpenAICompatibleProvider):
            report["model_identity"] = provider.identity.to_dict()
            report["generation_settings"] = provider.settings.to_dict()
            report["prompt_envelope_version"] = PROMPT_ENVELOPE_VERSION
            if provider.structured_json:
                report["structured_output_contract_version"] = (
                    STRUCTURED_OUTPUT_CONTRACT_VERSION
                )
            if diagnostics is None:
                raise RunnerInvariantError("OpenAI diagnostics were not initialized")
            report["generation_diagnostics"] = diagnostics.to_dict()
        print(
            json.dumps(
                report,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    except Exception as error:
        print(f"Evaluation failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
