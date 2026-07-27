"""Deterministic scoring functions for supported linguistic tasks.

All public scores are in the inclusive range [0.0, 1.0]. Response parsing is a
separate concern: these functions receive already parsed values and never ask an
LLM to judge or repair an answer.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SegmentationScore:
    precision: float
    recall: float
    f1: float
    correct_tokens: int
    predicted_tokens: int
    gold_tokens: int
    exact_match: bool
    valid_surface: bool


@dataclass(frozen=True)
class TaggingScore:
    accuracy: float
    correct_tags: int
    total_tags: int
    valid_length: bool


@dataclass(frozen=True)
class DependencyArc:
    token_id: int
    head_id: int
    deprel: str


@dataclass(frozen=True)
class DependencyScore:
    uas: float
    las: float
    correct_heads: int
    correct_labeled_arcs: int
    total_arcs: int
    valid_structure: bool


@dataclass(frozen=True)
class ExactMatchScore:
    score: float
    exact_match: bool


@dataclass(frozen=True)
class TransliterationScore:
    token_accuracy: float
    correct_tokens: int
    total_tokens: int
    sentence_exact_match: bool
    valid_length: bool


def _validate_tokens(tokens: Sequence[str], name: str) -> None:
    for index, token in enumerate(tokens):
        if not isinstance(token, str):
            raise TypeError(f"{name}[{index}] must be a string")
        if not token:
            raise ValueError(f"{name}[{index}] must not be empty")


def _token_spans(tokens: Sequence[str]) -> tuple[str, set[tuple[int, int]]]:
    cursor = 0
    spans: set[tuple[int, int]] = set()
    pieces: list[str] = []
    for token in tokens:
        start = cursor
        cursor += len(token)
        spans.add((start, cursor))
        pieces.append(token)
    return "".join(pieces), spans


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def score_segmentation(
    gold_tokens: Sequence[str], predicted_tokens: Sequence[str]
) -> SegmentationScore:
    """Score segmentation with exact token spans over a shared surface string.

    A prediction that changes, removes, or inserts sentence characters has an
    invalid surface and receives zero. This prevents a model from gaining credit
    by rewriting the source sentence instead of segmenting it.
    """

    _validate_tokens(gold_tokens, "gold_tokens")
    _validate_tokens(predicted_tokens, "predicted_tokens")

    gold_surface, gold_spans = _token_spans(gold_tokens)
    predicted_surface, predicted_spans = _token_spans(predicted_tokens)
    valid_surface = gold_surface == predicted_surface
    correct = len(gold_spans & predicted_spans) if valid_surface else 0
    precision = _safe_ratio(correct, len(predicted_tokens))
    recall = _safe_ratio(correct, len(gold_tokens))
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    exact_match = valid_surface and list(gold_tokens) == list(predicted_tokens)
    if not gold_tokens and not predicted_tokens:
        precision = recall = f1 = 1.0
        exact_match = True

    return SegmentationScore(
        precision=precision,
        recall=recall,
        f1=f1,
        correct_tokens=correct,
        predicted_tokens=len(predicted_tokens),
        gold_tokens=len(gold_tokens),
        exact_match=exact_match,
        valid_surface=valid_surface,
    )


def score_tags(gold_tags: Sequence[str], predicted_tags: Sequence[str]) -> TaggingScore:
    """Score an aligned UPOS or XPOS tag sequence.

    Tagging tasks use platform-provided tokenization. A response with a missing
    or extra tag is malformed and receives zero rather than a partial score.
    """

    _validate_tokens(gold_tags, "gold_tags")
    _validate_tokens(predicted_tags, "predicted_tags")
    valid_length = len(gold_tags) == len(predicted_tags)
    if not valid_length:
        return TaggingScore(
            accuracy=0.0,
            correct_tags=0,
            total_tags=len(gold_tags),
            valid_length=False,
        )

    correct = sum(
        gold == predicted for gold, predicted in zip(gold_tags, predicted_tags, strict=True)
    )
    accuracy = _safe_ratio(correct, len(gold_tags))
    if not gold_tags:
        accuracy = 1.0

    return TaggingScore(
        accuracy=accuracy,
        correct_tags=correct,
        total_tags=len(gold_tags),
        valid_length=True,
    )


def _index_arcs(arcs: Sequence[DependencyArc], name: str) -> dict[int, DependencyArc]:
    indexed: dict[int, DependencyArc] = {}
    for arc in arcs:
        if not isinstance(arc, DependencyArc):
            raise TypeError(f"{name} entries must be DependencyArc instances")
        if arc.token_id <= 0:
            raise ValueError(f"{name} token_id must be positive")
        if arc.head_id < 0:
            raise ValueError(f"{name} head_id must be non-negative")
        if not arc.deprel:
            raise ValueError(f"{name} deprel must not be empty")
        if arc.token_id in indexed:
            raise ValueError(f"{name} contains duplicate token_id {arc.token_id}")
        indexed[arc.token_id] = arc
    return indexed


def score_dependencies(
    gold_arcs: Sequence[DependencyArc], predicted_arcs: Sequence[DependencyArc]
) -> DependencyScore:
    """Compute unlabeled and labeled attachment scores by token ID.

    Predictions must contain exactly the same token IDs as the gold parse.
    Missing or extra IDs indicate malformed output and receive zero for both
    metrics. Dependency relation subtypes are compared exactly.
    """

    gold_by_id = _index_arcs(gold_arcs, "gold_arcs")
    predicted_by_id = _index_arcs(predicted_arcs, "predicted_arcs")
    valid_structure = gold_by_id.keys() == predicted_by_id.keys()
    total = len(gold_by_id)
    if not valid_structure:
        return DependencyScore(
            uas=0.0,
            las=0.0,
            correct_heads=0,
            correct_labeled_arcs=0,
            total_arcs=total,
            valid_structure=False,
        )

    correct_heads = 0
    correct_labeled_arcs = 0
    for token_id, gold in gold_by_id.items():
        predicted = predicted_by_id[token_id]
        head_matches = gold.head_id == predicted.head_id
        correct_heads += head_matches
        correct_labeled_arcs += head_matches and gold.deprel == predicted.deprel

    uas = _safe_ratio(correct_heads, total)
    las = _safe_ratio(correct_labeled_arcs, total)
    if not total:
        uas = las = 1.0

    return DependencyScore(
        uas=uas,
        las=las,
        correct_heads=correct_heads,
        correct_labeled_arcs=correct_labeled_arcs,
        total_arcs=total,
        valid_structure=True,
    )


def score_exact_match(gold: str, predicted: str) -> ExactMatchScore:
    """Compare strings after canonical Unicode NFC normalization only."""

    if not isinstance(gold, str) or not isinstance(predicted, str):
        raise TypeError("gold and predicted values must be strings")
    exact_match = unicodedata.normalize("NFC", gold) == unicodedata.normalize("NFC", predicted)
    return ExactMatchScore(score=float(exact_match), exact_match=exact_match)


def score_transliteration(
    gold_transliterations: Sequence[str], predicted_transliterations: Sequence[str]
) -> TransliterationScore:
    """Score aligned token transliterations and full-sentence correctness.

    The platform supplies source tokenization for this task. A missing or extra
    transliteration is malformed and receives zero. Comparison performs Unicode
    NFC normalization without case folding or punctuation replacement.
    """

    _validate_tokens(gold_transliterations, "gold_transliterations")
    _validate_tokens(predicted_transliterations, "predicted_transliterations")
    valid_length = len(gold_transliterations) == len(predicted_transliterations)
    if not valid_length:
        return TransliterationScore(
            token_accuracy=0.0,
            correct_tokens=0,
            total_tokens=len(gold_transliterations),
            sentence_exact_match=False,
            valid_length=False,
        )

    matches = [
        unicodedata.normalize("NFC", gold) == unicodedata.normalize("NFC", predicted)
        for gold, predicted in zip(gold_transliterations, predicted_transliterations, strict=True)
    ]
    correct = sum(matches)
    accuracy = _safe_ratio(correct, len(gold_transliterations))
    if not gold_transliterations:
        accuracy = 1.0

    return TransliterationScore(
        token_accuracy=accuracy,
        correct_tokens=correct,
        total_tokens=len(gold_transliterations),
        sentence_exact_match=all(matches),
        valid_length=True,
    )
