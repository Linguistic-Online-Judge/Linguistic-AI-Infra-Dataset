"""Synchronous offline orchestration from safe model input to aggregate score."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
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
) -> ChallengeAggregateResult:
    """Run one complete challenge without returning partial results."""

    if not isinstance(artifacts, ChallengeArtifacts):
        raise TypeError("artifacts must be ChallengeArtifacts")
    if not isinstance(provider, ModelProvider):
        raise TypeError("provider must implement ModelProvider")
    if not isinstance(student_prompt, str) or not student_prompt.strip():
        raise ValueError("student_prompt must be a non-empty string")

    prepared_samples = _prepare_samples(artifacts)
    task = TaskType(artifacts.public.task)
    outcomes: list[SampleEvaluationOutcome] = []
    for prepared in prepared_samples:
        request = ModelRequest(
            task=task,
            language=artifacts.public.language,
            treebank=artifacts.public.treebank,
            student_prompt=student_prompt,
            model_input=prepared.model_input,
        )
        generation = provider.generate(request)
        if not isinstance(generation, ModelGeneration):
            raise ProviderContractError("provider must return ModelGeneration")
        outcomes.append(
            evaluate_raw_response(
                sample=prepared.dataset_sample,
                manifest_sample=prepared.manifest_sample,
                task=task,
                model_input=prepared.model_input,
                raw_response=generation.raw_text,
            )
        )
    return aggregate_challenge(artifacts, outcomes)


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
        result = run_challenge(
            artifacts,
            provider,
            student_prompt=student_prompt,
        )
        report = result.to_dict()
        if isinstance(provider, OpenAICompatibleProvider):
            report["model_identity"] = provider.identity.to_dict()
            report["generation_settings"] = provider.settings.to_dict()
            report["prompt_envelope_version"] = PROMPT_ENVELOPE_VERSION
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
