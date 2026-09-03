import pytest

from linguistic_oj.model_inputs import (
    DependencyModelInput,
    DependencyTokenInput,
    SegmentationModelInput,
    TaggingModelInput,
    TransliterationModelInput,
    response_expectations,
)
from linguistic_oj.providers import (
    DeterministicMockProvider,
    ModelGeneration,
    ModelProvider,
    ModelRequest,
)
from linguistic_oj.responses import TaskType, parse_model_response


def _request(task: TaskType, model_input) -> ModelRequest:
    return ModelRequest(
        task=task,
        language="Test",
        treebank="Tiny",
        student_prompt="Return the required JSON.",
        model_input=model_input,
    )


@pytest.mark.parametrize(
    ("task", "model_input"),
    [
        (TaskType.SEGMENTATION, SegmentationModelInput(text="AB")),
        (TaskType.UPOS, TaggingModelInput(tokens=("A", "B"))),
        (TaskType.XPOS, TaggingModelInput(tokens=("A", "B"))),
        (
            TaskType.DEPENDENCY,
            DependencyModelInput(
                tokens=(
                    DependencyTokenInput(token_id=1, form="A"),
                    DependencyTokenInput(token_id=2, form="B"),
                )
            ),
        ),
        (
            TaskType.TRANSLITERATION,
            TransliterationModelInput(text="AB", tokens=("A", "B")),
        ),
    ],
)
def test_mock_provider_is_deterministic_and_schema_valid(task: TaskType, model_input) -> None:
    provider = DeterministicMockProvider()
    request = _request(task, model_input)

    first = provider.generate(request)
    second = provider.generate(request)
    expected_count, expected_token_ids = response_expectations(task, model_input)
    parsed = parse_model_response(
        task,
        first.raw_text,
        expected_count=expected_count,
        expected_token_ids=expected_token_ids,
    )

    assert first.raw_text == second.raw_text
    assert parsed.is_valid
    assert isinstance(provider, ModelProvider)


def test_model_request_rejects_task_input_mismatch() -> None:
    with pytest.raises(TypeError, match="wrong model input"):
        _request(TaskType.UPOS, SegmentationModelInput(text="AB"))


def test_model_request_exposes_response_schema_without_private_context() -> None:
    tainted = TaggingModelInput(tokens=("A", "B")).model_copy(
        update={"answers": {"upos": ["NOUN", "VERB"]}, "sample_id": "secret"}
    )
    request = _request(TaskType.UPOS, tainted)

    assert set(request.__dataclass_fields__) == {
        "task",
        "language",
        "treebank",
        "student_prompt",
        "model_input",
    }
    assert not hasattr(request.model_input, "answers")
    assert not hasattr(request.model_input, "sample_id")
    assert request.response_schema["additionalProperties"] is False


def test_model_generation_requires_raw_string() -> None:
    with pytest.raises(TypeError, match="raw_text must be a string"):
        ModelGeneration(raw_text=123)  # type: ignore[arg-type]


@pytest.mark.parametrize("generated_token_count", [-1, True, 1.5])
def test_model_generation_rejects_invalid_token_count(generated_token_count) -> None:
    with pytest.raises(ValueError, match="generated_token_count"):
        ModelGeneration(raw_text="{}", generated_token_count=generated_token_count)


def test_model_generation_rejects_unsafe_finish_reason() -> None:
    with pytest.raises(ValueError, match="finish_reason"):
        ModelGeneration(raw_text="{}", finish_reason="provider-private-detail")
