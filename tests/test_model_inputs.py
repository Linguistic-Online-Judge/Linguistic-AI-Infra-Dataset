import pytest
from pydantic import ValidationError

from linguistic_oj.dataset import DatasetSample
from linguistic_oj.model_inputs import (
    DependencyModelInput,
    DependencyTokenInput,
    SegmentationModelInput,
    TaggingModelInput,
    TransliterationModelInput,
    build_model_input,
    canonicalize_model_input,
    response_expectations,
)


def _sample() -> DatasetSample:
    return DatasetSample.model_validate(
        {
            "id": "sample-1",
            "language": "Test",
            "treebank": "Tiny",
            "text": "A B",
            "answers": {
                "segmentation": ["A", "B"],
                "upos": ["NOUN", "VERB"],
                "xpos": ["N", "V"],
                "dependency": [
                    [1, "A", 2, "B", "nsubj"],
                    [2, "B", 0, "ROOT", "root"],
                ],
                "transliteration": ["a", "b"],
            },
            "tasks_available": [
                "segmentation",
                "upos",
                "xpos",
                "dependency",
                "transliteration",
            ],
        }
    )


def test_safe_inputs_have_exact_gold_free_shapes() -> None:
    sample = _sample()

    segmentation = build_model_input(sample, "segmentation")
    upos = build_model_input(sample, "upos")
    xpos = build_model_input(sample, "xpos")
    dependency = build_model_input(sample, "dependency")
    transliteration = build_model_input(sample, "transliteration")

    assert segmentation.model_dump(mode="json") == {"text": "AB"}
    assert upos.model_dump(mode="json") == {"tokens": ["A", "B"]}
    assert xpos.model_dump(mode="json") == {"tokens": ["A", "B"]}
    assert dependency.model_dump(mode="json") == {
        "tokens": [
            {"token_id": 1, "form": "A"},
            {"token_id": 2, "form": "B"},
        ]
    }
    assert transliteration.model_dump(mode="json") == {
        "text": "A B",
        "tokens": ["A", "B"],
    }


def test_model_inputs_are_deeply_immutable_and_forbid_extra_fields() -> None:
    model_input = TaggingModelInput(tokens=("A", "B"))

    with pytest.raises(ValidationError):
        model_input.tokens = ("changed",)  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaggingModelInput(tokens=("A",), answers={})  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="valid tuple"):
        TaggingModelInput(tokens=["A"])  # type: ignore[arg-type]


def test_dependency_input_requires_contiguous_ids() -> None:
    with pytest.raises(ValidationError, match="contiguous"):
        DependencyModelInput(
            tokens=(
                DependencyTokenInput(token_id=1, form="A"),
                DependencyTokenInput(token_id=3, form="B"),
            )
        )


def test_canonical_input_strips_unchecked_copy_fields_and_rejects_subclasses() -> None:
    tainted = TaggingModelInput(tokens=("A",)).model_copy(
        update={"answers": {"upos": ["NOUN"]}, "sample_id": "secret"}
    )

    canonical = canonicalize_model_input("upos", tainted)

    assert canonical is not tainted
    assert canonical.model_dump(mode="json") == {"tokens": ["A"]}
    assert not hasattr(canonical, "answers")
    assert not hasattr(canonical, "sample_id")

    class LeakyTaggingInput(TaggingModelInput):
        answers: dict

    leaky = LeakyTaggingInput(tokens=("A",), answers={"upos": ["NOUN"]})
    with pytest.raises(TypeError, match="wrong model input"):
        canonicalize_model_input("upos", leaky)


def test_response_expectations_come_from_safe_input() -> None:
    segmentation = SegmentationModelInput(text="AB")
    tagging = TaggingModelInput(tokens=("A", "B"))
    dependency = DependencyModelInput(
        tokens=(
            DependencyTokenInput(token_id=1, form="A"),
            DependencyTokenInput(token_id=2, form="B"),
        )
    )
    transliteration = TransliterationModelInput(text="AB", tokens=("A", "B"))

    assert response_expectations("segmentation", segmentation) == (None, None)
    assert response_expectations("upos", tagging) == (2, None)
    assert response_expectations("dependency", dependency) == (2, (1, 2))
    assert response_expectations("transliteration", transliteration) == (2, None)

    with pytest.raises(TypeError, match="wrong model input"):
        response_expectations("upos", segmentation)
