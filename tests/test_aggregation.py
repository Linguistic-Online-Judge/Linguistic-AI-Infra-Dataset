import json
from pathlib import Path

import pytest

from linguistic_oj import (
    AGGREGATION_VERSION,
    SCORER_VERSION,
    DependencyScore,
    ParseErrorCode,
    SampleEvaluationOutcome,
    SegmentationScore,
    TaggingScore,
    TaskType,
    TransliterationScore,
    aggregate_challenge,
    score_segmentation,
    score_tags,
    score_transliteration,
)
from linguistic_oj.challenge import (
    ChallengeArtifacts,
    ManifestSample,
    PrivateChallengeManifest,
    PublicChallenge,
    make_challenge_id,
    selection_sha256,
    sha256_file,
)
from linguistic_oj.contracts import RESPONSE_SCHEMA_VERSIONS, TASK_METRICS

TEST_DATASET_PATH = Path(__file__).parents[1] / "pyproject.toml"


def _artifacts(
    task: TaskType | str,
    gold_items: dict[str, int],
    *,
    aggregation_version: str = AGGREGATION_VERSION,
) -> ChallengeArtifacts:
    task_type = TaskType(task)
    samples = tuple(
        ManifestSample(sample_id=sample_id, gold_items=count)
        for sample_id, count in sorted(gold_items.items())
    )
    selection_hash = selection_sha256(samples)
    challenge_id = make_challenge_id("Chinese", "Test", task_type, "v1")
    primary_metric, secondary_metrics = TASK_METRICS[task_type]
    public = PublicChallenge(
        challenge_id=challenge_id,
        title="Test challenge",
        version="v1",
        language="Chinese",
        treebank="Test",
        task=task_type.value,
        sample_count=len(samples),
        primary_metric=primary_metric,
        secondary_metrics=secondary_metrics,
        response_schema_version=RESPONSE_SCHEMA_VERSIONS[task_type],
        scorer_version=SCORER_VERSION,
        aggregation_version=aggregation_version,
        dataset_sha256=sha256_file(TEST_DATASET_PATH),
        selection_sha256=selection_hash,
        security_level="public_reproducible",
        status="draft",
    )
    private = PrivateChallengeManifest(
        challenge_id=challenge_id,
        version="v1",
        task=task_type.value,
        scorer_version=SCORER_VERSION,
        aggregation_version=aggregation_version,
        dataset_sha256=sha256_file(TEST_DATASET_PATH),
        selection_sha256=selection_hash,
        selection_seed=2026,
        samples=samples,
    )
    return ChallengeArtifacts(public=public, private=private, dataset_path=TEST_DATASET_PATH)


def _aggregate(task: TaskType | str, outcomes):
    gold_items = {outcome.sample_id: outcome.gold_items for outcome in outcomes}
    return aggregate_challenge(_artifacts(task, gold_items), outcomes)


def test_segmentation_uses_micro_counts_not_sentence_average() -> None:
    long_perfect = score_segmentation(["a"] * 9, ["a"] * 9)
    short_wrong = score_segmentation(["b"], ["c"])
    outcomes = [
        SampleEvaluationOutcome.scored("long", "segmentation", long_perfect),
        SampleEvaluationOutcome.scored("short", "segmentation", short_wrong),
    ]

    result = _aggregate("segmentation", outcomes)

    assert result.metrics["micro_precision"] == pytest.approx(0.9)
    assert result.metrics["micro_recall"] == pytest.approx(0.9)
    assert result.metrics["micro_f1"] == pytest.approx(0.9)
    assert result.score == pytest.approx(0.9)
    assert result.score != pytest.approx((long_perfect.f1 + short_wrong.f1) / 2)


def test_malformed_segmentation_penalizes_recall_and_counts_error() -> None:
    perfect = score_segmentation(["我", "们"], ["我", "们"])
    outcomes = [
        SampleEvaluationOutcome.scored("valid", TaskType.SEGMENTATION, perfect),
        SampleEvaluationOutcome.malformed(
            "invalid",
            TaskType.SEGMENTATION,
            gold_items=2,
            error_code=ParseErrorCode.INVALID_JSON,
        ),
    ]

    result = _aggregate(TaskType.SEGMENTATION, outcomes)

    assert result.metrics["micro_precision"] == 1.0
    assert result.metrics["micro_recall"] == 0.5
    assert result.metrics["micro_f1"] == pytest.approx(2 / 3)
    assert result.samples_valid == 1
    assert result.samples_invalid == 1
    assert result.errors == {"INVALID_JSON": 1}


def test_all_malformed_responses_produce_zero_score() -> None:
    outcomes = [
        SampleEvaluationOutcome.malformed(
            "first",
            "segmentation",
            gold_items=3,
            error_code=ParseErrorCode.INVALID_JSON,
        ),
        SampleEvaluationOutcome.malformed(
            "second",
            "segmentation",
            gold_items=2,
            error_code=ParseErrorCode.EMPTY_VALUE,
        ),
    ]

    result = _aggregate("segmentation", outcomes)

    assert result.score == 0.0
    assert result.metrics == {
        "micro_precision": 0.0,
        "micro_recall": 0.0,
        "micro_f1": 0.0,
    }
    assert result.samples_valid == 0
    assert result.samples_invalid == 2
    assert result.errors == {"EMPTY_VALUE": 1, "INVALID_JSON": 1}


@pytest.mark.parametrize("task", [TaskType.UPOS, TaskType.XPOS])
def test_tagging_micro_accuracy_includes_malformed_denominator(task: TaskType) -> None:
    partial = score_tags(["NOUN", "VERB", "ADJ"], ["NOUN", "AUX", "ADJ"])
    outcomes = [
        SampleEvaluationOutcome.scored("valid", task, partial),
        SampleEvaluationOutcome.malformed(
            "invalid",
            task,
            gold_items=2,
            error_code=ParseErrorCode.LENGTH_MISMATCH,
        ),
    ]

    result = _aggregate(task, outcomes)

    assert result.metrics == {"micro_accuracy": pytest.approx(2 / 5)}
    assert result.errors == {"LENGTH_MISMATCH": 1}


def test_dependency_aggregates_uas_and_las() -> None:
    first = DependencyScore(
        uas=1.0,
        las=0.5,
        correct_heads=2,
        correct_labeled_arcs=1,
        total_arcs=2,
        valid_structure=True,
    )
    second = DependencyScore(
        uas=0.5,
        las=0.5,
        correct_heads=1,
        correct_labeled_arcs=1,
        total_arcs=2,
        valid_structure=True,
    )
    outcomes = [
        SampleEvaluationOutcome.scored("first", "dependency", first),
        SampleEvaluationOutcome.scored("second", "dependency", second),
    ]

    result = _aggregate("dependency", outcomes)

    assert result.metrics["uas"] == pytest.approx(3 / 4)
    assert result.metrics["las"] == pytest.approx(2 / 4)
    assert result.primary_metric == "las"
    assert result.score == pytest.approx(0.5)


def test_transliteration_aggregates_token_and_sentence_metrics() -> None:
    exact = score_transliteration(["wǒ", "men"], ["wǒ", "men"])
    partial = score_transliteration(["nǐ", "hǎo"], ["nǐ", "hao"])
    outcomes = [
        SampleEvaluationOutcome.scored("exact", "transliteration", exact),
        SampleEvaluationOutcome.scored("partial", "transliteration", partial),
        SampleEvaluationOutcome.malformed(
            "invalid",
            "transliteration",
            gold_items=2,
            error_code=ParseErrorCode.WRONG_TYPE,
        ),
    ]

    result = _aggregate("transliteration", outcomes)

    assert result.metrics["token_accuracy"] == pytest.approx(3 / 6)
    assert result.metrics["sentence_exact_match_rate"] == pytest.approx(1 / 3)
    assert result.errors == {"WRONG_TYPE": 1}


def test_outcome_rejects_score_for_wrong_task() -> None:
    score = score_tags(["NOUN"], ["NOUN"])

    with pytest.raises(TypeError, match="requires SegmentationScore"):
        SampleEvaluationOutcome.scored("sample", "segmentation", score)


@pytest.mark.parametrize(
    ("task", "score"),
    [
        (
            "segmentation",
            SegmentationScore(1.0, 1.0, 1.0, 2, 1, 1, True, True),
        ),
        ("upos", TaggingScore(1.0, 2, 1, True)),
        ("dependency", DependencyScore(1.0, 1.0, 1, 2, 1, True)),
        ("transliteration", TransliterationScore(1.0, 2, 1, True, True)),
    ],
)
def test_outcome_rejects_impossible_score_counts(task: str, score) -> None:
    with pytest.raises(ValueError, match="exceeds|satisfy"):
        SampleEvaluationOutcome.scored("sample", task, score)


@pytest.mark.parametrize("invalid_count", [True, 0.5, float("nan")])
def test_outcome_rejects_non_integer_counts(invalid_count) -> None:
    score = TaggingScore(
        accuracy=1.0,
        correct_tags=invalid_count,
        total_tags=1,
        valid_length=True,
    )

    with pytest.raises(ValueError, match="non-negative integer"):
        SampleEvaluationOutcome.scored("sample", "upos", score)


def test_outcome_rejects_non_finite_or_inconsistent_metrics() -> None:
    non_finite = TaggingScore(float("nan"), 1, 1, True)
    inconsistent = TaggingScore(0.5, 1, 1, True)

    with pytest.raises(ValueError, match="finite number"):
        SampleEvaluationOutcome.scored("non-finite", "upos", non_finite)
    with pytest.raises(ValueError, match="does not match"):
        SampleEvaluationOutcome.scored("inconsistent", "upos", inconsistent)


def test_unknown_task_is_not_a_student_error() -> None:
    with pytest.raises(ValueError, match="platform configuration error"):
        SampleEvaluationOutcome.malformed(
            "sample",
            "upos",
            gold_items=1,
            error_code=ParseErrorCode.UNKNOWN_TASK,
        )


def test_structurally_invalid_score_must_be_malformed() -> None:
    invalid = TaggingScore(
        accuracy=0.0,
        correct_tags=0,
        total_tags=2,
        valid_length=False,
    )

    with pytest.raises(ValueError, match="represented as malformed"):
        SampleEvaluationOutcome.scored("sample", "upos", invalid)


def test_aggregate_rejects_duplicate_sample_ids() -> None:
    score = score_tags(["NOUN"], ["NOUN"])
    outcomes = [
        SampleEvaluationOutcome.scored("same", "upos", score),
        SampleEvaluationOutcome.scored("same", "upos", score),
    ]

    with pytest.raises(ValueError, match="duplicate sample_id"):
        _aggregate("upos", outcomes)


def test_aggregate_rejects_mixed_tasks_and_empty_input() -> None:
    tag_score = score_tags(["NOUN"], ["NOUN"])
    transliteration_score = TransliterationScore(
        token_accuracy=1.0,
        correct_tokens=1,
        total_tokens=1,
        sentence_exact_match=True,
        valid_length=True,
    )
    mixed = [
        SampleEvaluationOutcome.scored("tag", "upos", tag_score),
        SampleEvaluationOutcome.scored("translit", "transliteration", transliteration_score),
    ]

    with pytest.raises(ValueError, match="match the challenge task"):
        _aggregate("upos", mixed)
    with pytest.raises(ValueError, match="must not be empty"):
        aggregate_challenge(_artifacts("upos", {"sample": 1}), [])


def test_aggregate_requires_exact_manifest_coverage() -> None:
    score = score_tags(["NOUN"], ["NOUN"])
    outcomes = [SampleEvaluationOutcome.scored("included", "upos", score)]

    with pytest.raises(ValueError, match=r"missing=\['omitted'\]"):
        aggregate_challenge(
            _artifacts("upos", {"included": 1, "omitted": 1}),
            outcomes,
        )
    with pytest.raises(ValueError, match=r"extra=\['included'\]"):
        aggregate_challenge(
            _artifacts("upos", {"different": 1}),
            outcomes,
        )


def test_challenge_artifacts_reject_version_mismatch() -> None:
    with pytest.raises(ValueError, match="aggregation version"):
        _artifacts("upos", {"sample": 1}, aggregation_version="2.0")


def test_runtime_contract_mappings_are_immutable() -> None:
    with pytest.raises(TypeError):
        TASK_METRICS[TaskType.UPOS] = ("other", ())  # type: ignore[index]
    with pytest.raises(TypeError):
        RESPONSE_SCHEMA_VERSIONS[TaskType.UPOS] = "other"  # type: ignore[index]


def test_challenge_artifacts_reject_wrong_dataset() -> None:
    artifacts = _artifacts("upos", {"sample": 1})

    with pytest.raises(ValueError, match="configured dataset"):
        ChallengeArtifacts(
            public=artifacts.public,
            private=artifacts.private,
            dataset_path=Path(__file__),
        )


def test_aggregate_rejects_gold_denominator_mismatch() -> None:
    outcome = SampleEvaluationOutcome.malformed(
        "sample",
        "upos",
        gold_items=1,
        error_code=ParseErrorCode.INVALID_JSON,
    )

    with pytest.raises(ValueError, match="gold_items does not match"):
        aggregate_challenge(
            _artifacts("upos", {"sample": 100}),
            [outcome],
        )


def test_result_serializes_stable_report_fields() -> None:
    score = SegmentationScore(
        precision=1.0,
        recall=1.0,
        f1=1.0,
        correct_tokens=1,
        predicted_tokens=1,
        gold_tokens=1,
        exact_match=True,
        valid_surface=True,
    )
    outcome = SampleEvaluationOutcome.scored("sample", "segmentation", score)

    payload = _aggregate("segmentation", [outcome]).to_dict()

    assert payload["aggregation_version"] == "1.0"
    assert payload["scorer_version"] == "1.0"
    assert payload["dataset_sha256"] == sha256_file(TEST_DATASET_PATH)
    assert len(payload["selection_sha256"]) == 64
    assert payload["task"] == "segmentation"
    assert payload["primary_metric"] == "micro_f1"
    assert payload["score"] == 1.0
    json.dumps(payload, allow_nan=False)


def test_result_metric_and_error_mappings_are_immutable() -> None:
    outcome = SampleEvaluationOutcome.malformed(
        "sample",
        "upos",
        gold_items=1,
        error_code=ParseErrorCode.INVALID_JSON,
    )
    result = _aggregate("upos", [outcome])

    with pytest.raises(TypeError):
        result.metrics["micro_accuracy"] = 1.0  # type: ignore[index]
    with pytest.raises(TypeError):
        result.errors["INVALID_JSON"] = 0  # type: ignore[index]
