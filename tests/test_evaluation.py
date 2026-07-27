import pytest

from linguistic_oj import (
    DependencyArc,
    score_dependencies,
    score_exact_match,
    score_segmentation,
    score_tags,
    score_transliteration,
)


def test_segmentation_perfect_match() -> None:
    result = score_segmentation(["我", "没有", "问题", "。"], ["我", "没有", "问题", "。"])

    assert result.f1 == 1.0
    assert result.exact_match is True
    assert result.valid_surface is True


def test_segmentation_uses_token_spans() -> None:
    result = score_segmentation(["研究", "生命", "起源"], ["研究生", "命", "起源"])

    assert result.correct_tokens == 1
    assert result.precision == pytest.approx(1 / 3)
    assert result.recall == pytest.approx(1 / 3)
    assert result.f1 == pytest.approx(1 / 3)


def test_segmentation_changed_surface_receives_zero() -> None:
    result = score_segmentation(["hello", "world"], ["hello", "there"])

    assert result.f1 == 0.0
    assert result.valid_surface is False


def test_segmentation_rejects_empty_token() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        score_segmentation(["a"], ["", "a"])


def test_tag_accuracy() -> None:
    result = score_tags(["PRON", "VERB", "NOUN"], ["PRON", "AUX", "NOUN"])

    assert result.accuracy == pytest.approx(2 / 3)
    assert result.correct_tags == 2
    assert result.valid_length is True


def test_tag_length_mismatch_is_malformed() -> None:
    result = score_tags(["PRON", "VERB"], ["PRON"])

    assert result.accuracy == 0.0
    assert result.valid_length is False


def test_dependency_uas_and_las() -> None:
    gold = [
        DependencyArc(1, 2, "nsubj"),
        DependencyArc(2, 0, "root"),
        DependencyArc(3, 2, "obj"),
    ]
    predicted = [
        DependencyArc(1, 2, "nsubj"),
        DependencyArc(2, 0, "root:custom"),
        DependencyArc(3, 1, "obj"),
    ]

    result = score_dependencies(gold, predicted)

    assert result.uas == pytest.approx(2 / 3)
    assert result.las == pytest.approx(1 / 3)
    assert result.valid_structure is True


def test_dependency_token_id_mismatch_is_malformed() -> None:
    gold = [DependencyArc(1, 0, "root")]
    predicted = [DependencyArc(2, 0, "root")]

    result = score_dependencies(gold, predicted)

    assert result.uas == 0.0
    assert result.las == 0.0
    assert result.valid_structure is False


def test_dependency_rejects_duplicate_token_ids() -> None:
    arcs = [DependencyArc(1, 0, "root"), DependencyArc(1, 0, "root")]

    with pytest.raises(ValueError, match="duplicate token_id"):
        score_dependencies(arcs, arcs)


def test_exact_match_normalizes_canonical_unicode() -> None:
    result = score_exact_match("wǒ", "wo\u030c")

    assert result.score == 1.0
    assert result.exact_match is True


def test_exact_match_does_not_strip_or_fold_case() -> None:
    assert score_exact_match("Test", "test").score == 0.0
    assert score_exact_match("test", "test ").score == 0.0


def test_transliteration_reports_token_and_sentence_metrics() -> None:
    result = score_transliteration(
        ["rán'ér", ",", "zhèyàng"],
        ["rán'ér", ",", "zěnyàng"],
    )

    assert result.token_accuracy == pytest.approx(2 / 3)
    assert result.correct_tokens == 2
    assert result.sentence_exact_match is False
    assert result.valid_length is True


def test_transliteration_normalizes_unicode_per_token() -> None:
    result = score_transliteration(["wǒ"], ["wo\u030c"])

    assert result.token_accuracy == 1.0
    assert result.sentence_exact_match is True


def test_transliteration_length_mismatch_is_malformed() -> None:
    result = score_transliteration(["wǒ", "men"], ["wǒmen"])

    assert result.token_accuracy == 0.0
    assert result.sentence_exact_match is False
    assert result.valid_length is False
