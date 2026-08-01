"""Gold-free, task-specific inputs that may cross the model-provider boundary."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .dataset import DatasetSample
from .responses import TaskType

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
NonEmptyStringTuple = Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]


class ModelInputError(ValueError):
    """Raised when trusted dataset data cannot produce a safe model input."""


class StrictModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SegmentationModelInput(StrictModelInput):
    text: NonEmptyString


class TaggingModelInput(StrictModelInput):
    tokens: NonEmptyStringTuple


class DependencyTokenInput(StrictModelInput):
    token_id: Annotated[int, Field(gt=0)]
    form: NonEmptyString


class DependencyModelInput(StrictModelInput):
    tokens: Annotated[tuple[DependencyTokenInput, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_contiguous_token_ids(self) -> Self:
        token_ids = tuple(token.token_id for token in self.tokens)
        if token_ids != tuple(range(1, len(self.tokens) + 1)):
            raise ValueError("dependency token IDs must be contiguous and start at 1")
        return self


class TransliterationModelInput(StrictModelInput):
    text: NonEmptyString
    tokens: NonEmptyStringTuple


SafeModelInput = (
    SegmentationModelInput
    | TaggingModelInput
    | DependencyModelInput
    | TransliterationModelInput
)


def _fixed_tokens(sample: DatasetSample) -> tuple[str, ...]:
    tokens = sample.answers.get(TaskType.SEGMENTATION.value)
    if not isinstance(tokens, list) or not tokens or any(
        not isinstance(token, str) or not token for token in tokens
    ):
        raise ModelInputError(
            f"Sample {sample.id} must have a non-empty segmentation token list"
        )
    return tuple(tokens)


def model_input_matches_task(task: TaskType, model_input: SafeModelInput) -> bool:
    if task is TaskType.SEGMENTATION:
        return type(model_input) is SegmentationModelInput
    if task in {TaskType.UPOS, TaskType.XPOS}:
        return type(model_input) is TaggingModelInput
    if task is TaskType.DEPENDENCY:
        return type(model_input) is DependencyModelInput
    return type(model_input) is TransliterationModelInput


def canonicalize_model_input(
    task: TaskType | str,
    model_input: SafeModelInput,
) -> SafeModelInput:
    """Rebuild an exact DTO so unchecked copies cannot carry hidden fields."""

    task_type = TaskType(task)
    if not model_input_matches_task(task_type, model_input):
        raise TypeError(f"{task_type.value} received the wrong model input type")
    if type(model_input) is SegmentationModelInput:
        return SegmentationModelInput(text=model_input.text)
    if type(model_input) is TaggingModelInput:
        return TaggingModelInput(tokens=tuple(model_input.tokens))
    if type(model_input) is DependencyModelInput:
        tokens = tuple(
            DependencyTokenInput(token_id=token.token_id, form=token.form)
            for token in model_input.tokens
        )
        return DependencyModelInput(tokens=tokens)
    if type(model_input) is TransliterationModelInput:
        return TransliterationModelInput(
            text=model_input.text,
            tokens=tuple(model_input.tokens),
        )
    raise TypeError(f"Unsupported model input type: {type(model_input).__name__}")


def build_model_input(sample: DatasetSample, task: TaskType | str) -> SafeModelInput:
    """Construct a new safe DTO without serializing the source sample."""

    if not isinstance(sample, DatasetSample):
        raise TypeError("sample must be a DatasetSample")
    task_type = TaskType(task)
    if task_type is TaskType.SEGMENTATION:
        # Concatenation removes gold boundaries while matching the scorer's surface.
        return SegmentationModelInput(text="".join(_fixed_tokens(sample)))

    tokens = _fixed_tokens(sample)
    if task_type in {TaskType.UPOS, TaskType.XPOS}:
        return TaggingModelInput(tokens=tokens)
    if task_type is TaskType.DEPENDENCY:
        dependency_tokens = tuple(
            DependencyTokenInput(token_id=index, form=form)
            for index, form in enumerate(tokens, start=1)
        )
        return DependencyModelInput(tokens=dependency_tokens)
    return TransliterationModelInput(text=sample.text, tokens=tokens)


def response_expectations(
    task: TaskType | str,
    model_input: SafeModelInput,
) -> tuple[int | None, tuple[int, ...] | None]:
    """Derive parser context exclusively from the safe provider input."""

    task_type = TaskType(task)
    model_input = canonicalize_model_input(task_type, model_input)
    if type(model_input) is SegmentationModelInput:
        return None, None
    if type(model_input) is DependencyModelInput:
        token_ids = tuple(token.token_id for token in model_input.tokens)
        return len(model_input.tokens), token_ids
    return len(model_input.tokens), None
