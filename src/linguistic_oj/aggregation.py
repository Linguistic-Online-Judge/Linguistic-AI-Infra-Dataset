"""Challenge-level aggregation for deterministic per-sample scores."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .contracts import AGGREGATION_VERSION, SCORER_VERSION, TASK_METRICS
from .evaluation import (
    DependencyScore,
    SegmentationScore,
    TaggingScore,
    TransliterationScore,
)
from .responses import ParseErrorCode, TaskType

if TYPE_CHECKING:
    from .challenge import ChallengeArtifacts

ScoreResult = SegmentationScore | TaggingScore | DependencyScore | TransliterationScore


def _expected_score_type(task: TaskType) -> type[ScoreResult]:
    if task is TaskType.SEGMENTATION:
        return SegmentationScore
    if task in {TaskType.UPOS, TaskType.XPOS}:
        return TaggingScore
    if task is TaskType.DEPENDENCY:
        return DependencyScore
    return TransliterationScore


def _gold_items(score: ScoreResult) -> int:
    if isinstance(score, SegmentationScore):
        return score.gold_tokens
    if isinstance(score, TaggingScore):
        return score.total_tags
    if isinstance(score, DependencyScore):
        return score.total_arcs
    if isinstance(score, TransliterationScore):
        return score.total_tokens
    raise TypeError(f"Unsupported score type: {type(score).__name__}")


def _validate_structural_score(score: ScoreResult) -> None:
    if isinstance(score, SegmentationScore):
        if type(score.exact_match) is not bool or type(score.valid_surface) is not bool:
            raise ValueError("segmentation flags must be boolean")
        if not score.valid_surface and (score.correct_tokens or score.exact_match):
            raise ValueError("invalid segmentation surface cannot contain correct tokens")
    if isinstance(score, TaggingScore) and not score.valid_length:
        raise ValueError("Invalid tagging structure must be represented as malformed")
    if isinstance(score, DependencyScore) and not score.valid_structure:
        raise ValueError("Invalid dependency structure must be represented as malformed")
    if isinstance(score, TransliterationScore) and not score.valid_length:
        raise ValueError("Invalid transliteration structure must be represented as malformed")

    if isinstance(score, TaggingScore) and type(score.valid_length) is not bool:
        raise ValueError("valid_length must be boolean")
    if isinstance(score, DependencyScore) and type(score.valid_structure) is not bool:
        raise ValueError("valid_structure must be boolean")
    if isinstance(score, TransliterationScore):
        if type(score.valid_length) is not bool or type(score.sentence_exact_match) is not bool:
            raise ValueError("transliteration flags must be boolean")


def _require_nonnegative_int(name: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_metric(name: str, value: float, expected: float) -> None:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    if not math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(f"{name} does not match its score counts")


def _validate_score_counts(score: ScoreResult) -> None:
    if isinstance(score, SegmentationScore):
        _require_nonnegative_int("correct_tokens", score.correct_tokens)
        _require_nonnegative_int("predicted_tokens", score.predicted_tokens)
        _require_nonnegative_int("gold_tokens", score.gold_tokens)
        if score.correct_tokens > min(score.predicted_tokens, score.gold_tokens):
            raise ValueError("correct_tokens exceeds a segmentation denominator")
        precision = _ratio(score.correct_tokens, score.predicted_tokens)
        recall = _ratio(score.correct_tokens, score.gold_tokens)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        _require_metric("precision", score.precision, precision)
        _require_metric("recall", score.recall, recall)
        _require_metric("f1", score.f1, f1)
    elif isinstance(score, TaggingScore):
        _require_nonnegative_int("correct_tags", score.correct_tags)
        _require_nonnegative_int("total_tags", score.total_tags)
        if score.correct_tags > score.total_tags:
            raise ValueError("correct_tags exceeds total_tags")
        _require_metric("accuracy", score.accuracy, _ratio(score.correct_tags, score.total_tags))
    elif isinstance(score, DependencyScore):
        _require_nonnegative_int("correct_heads", score.correct_heads)
        _require_nonnegative_int("correct_labeled_arcs", score.correct_labeled_arcs)
        _require_nonnegative_int("total_arcs", score.total_arcs)
        if not 0 <= score.correct_labeled_arcs <= score.correct_heads <= score.total_arcs:
            raise ValueError("dependency counts must satisfy LAS <= UAS <= total_arcs")
        _require_metric("uas", score.uas, _ratio(score.correct_heads, score.total_arcs))
        _require_metric("las", score.las, _ratio(score.correct_labeled_arcs, score.total_arcs))
    else:
        _require_nonnegative_int("correct_tokens", score.correct_tokens)
        _require_nonnegative_int("total_tokens", score.total_tokens)
        if score.correct_tokens > score.total_tokens:
            raise ValueError("correct_tokens exceeds total_tokens")
        _require_metric(
            "token_accuracy",
            score.token_accuracy,
            _ratio(score.correct_tokens, score.total_tokens),
        )
        if score.sentence_exact_match != (score.correct_tokens == score.total_tokens):
            raise ValueError("sentence_exact_match does not match transliteration counts")


@dataclass(frozen=True, slots=True)
class SampleEvaluationOutcome:
    """One sample that either has a score or a deterministic parse error."""

    sample_id: str
    task: TaskType
    gold_items: int
    score: ScoreResult | None = None
    error_code: ParseErrorCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id:
            raise ValueError("sample_id must not be empty")
        if type(self.gold_items) is not int or self.gold_items <= 0:
            raise ValueError("gold_items must be positive")
        if not isinstance(self.task, TaskType):
            raise TypeError("task must be a TaskType")
        if self.error_code is not None and not isinstance(self.error_code, ParseErrorCode):
            raise TypeError("error_code must be a ParseErrorCode")
        if self.error_code is ParseErrorCode.UNKNOWN_TASK:
            raise ValueError("UNKNOWN_TASK is a platform configuration error, not model output")
        if (self.score is None) == (self.error_code is None):
            raise ValueError("Outcome must contain exactly one score or error_code")
        if self.score is not None:
            expected_type = _expected_score_type(self.task)
            if not isinstance(self.score, expected_type):
                raise TypeError(
                    f"{self.task.value} requires {expected_type.__name__}, "
                    f"received {type(self.score).__name__}"
                )
            if _gold_items(self.score) != self.gold_items:
                raise ValueError("gold_items does not match the score denominator")
            _validate_structural_score(self.score)
            _validate_score_counts(self.score)

    @classmethod
    def scored(
        cls,
        sample_id: str,
        task: TaskType | str,
        score: ScoreResult,
    ) -> SampleEvaluationOutcome:
        task_type = TaskType(task)
        return cls(
            sample_id=sample_id,
            task=task_type,
            gold_items=_gold_items(score),
            score=score,
        )

    @classmethod
    def malformed(
        cls,
        sample_id: str,
        task: TaskType | str,
        *,
        gold_items: int,
        error_code: ParseErrorCode,
    ) -> SampleEvaluationOutcome:
        return cls(
            sample_id=sample_id,
            task=TaskType(task),
            gold_items=gold_items,
            error_code=error_code,
        )

    @property
    def is_valid(self) -> bool:
        return self.score is not None


@dataclass(frozen=True, slots=True)
class ChallengeAggregateResult:
    challenge_id: str
    task: TaskType
    scorer_version: str
    aggregation_version: str
    dataset_sha256: str
    selection_sha256: str
    samples_total: int
    samples_valid: int
    samples_invalid: int
    primary_metric: str
    score: float
    metrics: Mapping[str, float]
    errors: Mapping[str, int]

    def __post_init__(self) -> None:
        if not self.challenge_id:
            raise ValueError("challenge_id must not be empty")
        if not isinstance(self.task, TaskType):
            raise TypeError("task must be a TaskType")
        if self.scorer_version != SCORER_VERSION:
            raise ValueError("scorer_version does not match the runtime")
        if self.aggregation_version != AGGREGATION_VERSION:
            raise ValueError("aggregation_version does not match the runtime")
        for name, fingerprint in (
            ("dataset_sha256", self.dataset_sha256),
            ("selection_sha256", self.selection_sha256),
        ):
            if len(fingerprint) != 64 or any(
                character not in "0123456789abcdef" for character in fingerprint
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 value")
        for name, value in (
            ("samples_total", self.samples_total),
            ("samples_valid", self.samples_valid),
            ("samples_invalid", self.samples_invalid),
        ):
            _require_nonnegative_int(name, value)
        if self.samples_total != self.samples_valid + self.samples_invalid:
            raise ValueError("sample counts do not add up")
        if self.samples_total == 0:
            raise ValueError("samples_total must be positive")

        metrics = dict(self.metrics)
        expected_metrics = {TASK_METRICS[self.task][0], *TASK_METRICS[self.task][1]}
        if set(metrics) != expected_metrics or self.primary_metric != TASK_METRICS[self.task][0]:
            raise ValueError("result metrics do not match the task metric declaration")
        for name, value in metrics.items():
            _require_metric(name, value, value)
        _require_metric("score", self.score, metrics[self.primary_metric])

        errors = dict(self.errors)
        for code, count in errors.items():
            if not isinstance(code, str) or not code:
                raise ValueError("error codes must be non-empty strings")
            if type(count) is not int or count <= 0:
                raise ValueError("error counts must be positive integers")
        if sum(errors.values()) != self.samples_invalid:
            raise ValueError("error counts must equal samples_invalid")

        object.__setattr__(self, "metrics", MappingProxyType(metrics))
        object.__setattr__(self, "errors", MappingProxyType(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "task": self.task.value,
            "scorer_version": self.scorer_version,
            "aggregation_version": self.aggregation_version,
            "dataset_sha256": self.dataset_sha256,
            "selection_sha256": self.selection_sha256,
            "samples_total": self.samples_total,
            "samples_valid": self.samples_valid,
            "samples_invalid": self.samples_invalid,
            "primary_metric": self.primary_metric,
            "score": self.score,
            "metrics": dict(self.metrics),
            "errors": dict(self.errors),
        }


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _aggregate_segmentation(outcomes: Sequence[SampleEvaluationOutcome]) -> dict[str, float]:
    correct = 0
    predicted = 0
    gold = 0
    for outcome in outcomes:
        gold += outcome.gold_items
        if isinstance(outcome.score, SegmentationScore):
            correct += outcome.score.correct_tokens
            predicted += outcome.score.predicted_tokens

    precision = _ratio(correct, predicted)
    recall = _ratio(correct, gold)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": f1,
    }


def _aggregate_tagging(outcomes: Sequence[SampleEvaluationOutcome]) -> dict[str, float]:
    correct = sum(
        outcome.score.correct_tags
        for outcome in outcomes
        if isinstance(outcome.score, TaggingScore)
    )
    total = sum(outcome.gold_items for outcome in outcomes)
    return {"micro_accuracy": _ratio(correct, total)}


def _aggregate_dependency(outcomes: Sequence[SampleEvaluationOutcome]) -> dict[str, float]:
    correct_heads = 0
    correct_labeled_arcs = 0
    total = sum(outcome.gold_items for outcome in outcomes)
    for outcome in outcomes:
        if isinstance(outcome.score, DependencyScore):
            correct_heads += outcome.score.correct_heads
            correct_labeled_arcs += outcome.score.correct_labeled_arcs
    return {
        "uas": _ratio(correct_heads, total),
        "las": _ratio(correct_labeled_arcs, total),
    }


def _aggregate_transliteration(
    outcomes: Sequence[SampleEvaluationOutcome],
) -> dict[str, float]:
    correct_tokens = 0
    exact_sentences = 0
    total_tokens = sum(outcome.gold_items for outcome in outcomes)
    for outcome in outcomes:
        if isinstance(outcome.score, TransliterationScore):
            correct_tokens += outcome.score.correct_tokens
            exact_sentences += outcome.score.sentence_exact_match
    return {
        "token_accuracy": _ratio(correct_tokens, total_tokens),
        "sentence_exact_match_rate": _ratio(exact_sentences, len(outcomes)),
    }


def aggregate_challenge(
    artifacts: ChallengeArtifacts,
    outcomes: Sequence[SampleEvaluationOutcome],
) -> ChallengeAggregateResult:
    """Aggregate per-sample counts into one deterministic challenge result."""

    from .challenge import ChallengeArtifacts, validate_challenge_artifacts

    if not isinstance(artifacts, ChallengeArtifacts):
        raise TypeError("artifacts must be ChallengeArtifacts")
    validate_challenge_artifacts(artifacts)
    if not outcomes:
        raise ValueError("outcomes must not be empty")

    task_type = TaskType(artifacts.public.task)
    expected_sample_ids = artifacts.private.sample_ids
    expected_gold_items = artifacts.private.gold_items_by_sample_id
    sample_ids = [outcome.sample_id for outcome in outcomes]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("outcomes contain duplicate sample_id values")
    if any(outcome.task is not task_type for outcome in outcomes):
        raise ValueError("all outcomes must match the challenge task")
    if set(sample_ids) != set(expected_sample_ids):
        missing = sorted(set(expected_sample_ids) - set(sample_ids))
        extra = sorted(set(sample_ids) - set(expected_sample_ids))
        raise ValueError(
            f"outcomes do not cover the challenge manifest: missing={missing}, extra={extra}"
        )
    for outcome in outcomes:
        if outcome.gold_items != expected_gold_items[outcome.sample_id]:
            raise ValueError(
                f"Outcome {outcome.sample_id} gold_items does not match the private manifest"
            )

    if task_type is TaskType.SEGMENTATION:
        metrics = _aggregate_segmentation(outcomes)
    elif task_type in {TaskType.UPOS, TaskType.XPOS}:
        metrics = _aggregate_tagging(outcomes)
    elif task_type is TaskType.DEPENDENCY:
        metrics = _aggregate_dependency(outcomes)
    else:
        metrics = _aggregate_transliteration(outcomes)

    errors = Counter(
        outcome.error_code.value for outcome in outcomes if outcome.error_code is not None
    )
    primary_metric = TASK_METRICS[task_type][0]
    declared_metrics = {primary_metric, *TASK_METRICS[task_type][1]}
    if set(metrics) != declared_metrics:
        raise RuntimeError("aggregated metrics do not match the task metric declaration")
    samples_valid = sum(outcome.is_valid for outcome in outcomes)
    return ChallengeAggregateResult(
        challenge_id=artifacts.public.challenge_id,
        task=task_type,
        scorer_version=SCORER_VERSION,
        aggregation_version=AGGREGATION_VERSION,
        dataset_sha256=artifacts.public.dataset_sha256,
        selection_sha256=artifacts.public.selection_sha256,
        samples_total=len(outcomes),
        samples_valid=samples_valid,
        samples_invalid=len(outcomes) - samples_valid,
        primary_metric=primary_metric,
        score=metrics[primary_metric],
        metrics=metrics,
        errors=dict(sorted(errors.items())),
    )
