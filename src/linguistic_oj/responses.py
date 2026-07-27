"""Strict response contracts and parsing for model-generated task output."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
NonEmptyStringList = Annotated[list[NonEmptyString], Field(min_length=1)]
UD_UPOS_TAGS = frozenset(
    {
        "ADJ",
        "ADP",
        "ADV",
        "AUX",
        "CCONJ",
        "DET",
        "INTJ",
        "NOUN",
        "NUM",
        "PART",
        "PRON",
        "PROPN",
        "PUNCT",
        "SCONJ",
        "SYM",
        "VERB",
        "X",
    }
)


class TaskType(StrEnum):
    SEGMENTATION = "segmentation"
    UPOS = "upos"
    XPOS = "xpos"
    DEPENDENCY = "dependency"
    TRANSLITERATION = "transliteration"


class StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SegmentationResponse(StrictResponse):
    tokens: NonEmptyStringList


class TaggingResponse(StrictResponse):
    tags: NonEmptyStringList


class DependencyArcResponse(StrictResponse):
    token_id: Annotated[int, Field(gt=0)]
    head_id: Annotated[int, Field(ge=0)]
    deprel: NonEmptyString


class DependencyResponse(StrictResponse):
    arcs: Annotated[list[DependencyArcResponse], Field(min_length=1)]


class TransliterationResponse(StrictResponse):
    transliterations: NonEmptyStringList


ModelResponse = (
    SegmentationResponse | TaggingResponse | DependencyResponse | TransliterationResponse
)


class ParseErrorCode(StrEnum):
    UNKNOWN_TASK = "UNKNOWN_TASK"
    INVALID_JSON = "INVALID_JSON"
    TOP_LEVEL_NOT_OBJECT = "TOP_LEVEL_NOT_OBJECT"
    MISSING_FIELD = "MISSING_FIELD"
    EXTRA_FIELD = "EXTRA_FIELD"
    WRONG_TYPE = "WRONG_TYPE"
    EMPTY_VALUE = "EMPTY_VALUE"
    INVALID_VALUE = "INVALID_VALUE"
    INVALID_TAG = "INVALID_TAG"
    DUPLICATE_TOKEN_ID = "DUPLICATE_TOKEN_ID"
    LENGTH_MISMATCH = "LENGTH_MISMATCH"
    TOKEN_ID_MISMATCH = "TOKEN_ID_MISMATCH"
    INVALID_HEAD_ID = "INVALID_HEAD_ID"


@dataclass(frozen=True, slots=True)
class ResponseParseError:
    code: ParseErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class ResponseParseResult:
    task: TaskType | None
    value: ModelResponse | None = None
    error: ResponseParseError | None = None

    @property
    def is_valid(self) -> bool:
        return self.value is not None and self.error is None


RESPONSE_MODELS: dict[TaskType, type[StrictResponse]] = {
    TaskType.SEGMENTATION: SegmentationResponse,
    TaskType.UPOS: TaggingResponse,
    TaskType.XPOS: TaggingResponse,
    TaskType.DEPENDENCY: DependencyResponse,
    TaskType.TRANSLITERATION: TransliterationResponse,
}


def response_json_schema(task: TaskType | str) -> dict:
    """Return the JSON Schema used in prompts and API documentation."""

    task_type = TaskType(task)
    return RESPONSE_MODELS[task_type].model_json_schema()


def _failure(
    code: ParseErrorCode, message: str, task: TaskType | None = None
) -> ResponseParseResult:
    return ResponseParseResult(
        task=task,
        error=ResponseParseError(code=code, message=message),
    )


def _validation_failure(error: ValidationError, task: TaskType) -> ResponseParseResult:
    first_error = error.errors(include_url=False)[0]
    error_type = first_error["type"]
    location = ".".join(str(part) for part in first_error["loc"])
    message = first_error["msg"]

    if error_type == "missing":
        code = ParseErrorCode.MISSING_FIELD
    elif error_type == "extra_forbidden":
        code = ParseErrorCode.EXTRA_FIELD
    elif error_type in {"list_type", "string_type", "int_type"}:
        code = ParseErrorCode.WRONG_TYPE
    elif error_type in {"string_too_short", "too_short"}:
        code = ParseErrorCode.EMPTY_VALUE
    else:
        code = ParseErrorCode.INVALID_VALUE

    detail = f"{location}: {message}" if location else message
    return _failure(code, detail, task)


def _sequence_length(value: ModelResponse) -> int:
    if isinstance(value, SegmentationResponse):
        return len(value.tokens)
    if isinstance(value, TaggingResponse):
        return len(value.tags)
    if isinstance(value, DependencyResponse):
        return len(value.arcs)
    return len(value.transliterations)


def parse_model_response(
    task: TaskType | str,
    raw_response: str,
    *,
    expected_count: int | None = None,
    expected_token_ids: Sequence[int] | None = None,
) -> ResponseParseResult:
    """Parse one raw model response without coercion or LLM-based repair.

    ``expected_count`` applies to tagging, dependency, and transliteration.
    Segmentation deliberately permits any positive token count because choosing
    token boundaries is the task itself.
    """

    try:
        task_type = TaskType(task)
    except ValueError:
        return _failure(ParseErrorCode.UNKNOWN_TASK, f"Unsupported task: {task}")

    if not isinstance(raw_response, str):
        return _failure(
            ParseErrorCode.WRONG_TYPE,
            "Raw model response must be a string",
            task_type,
        )
    if expected_count is not None and expected_count < 0:
        raise ValueError("expected_count must be non-negative")

    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as error:
        return _failure(
            ParseErrorCode.INVALID_JSON,
            f"Invalid JSON at line {error.lineno}, column {error.colno}",
            task_type,
        )

    if not isinstance(payload, dict):
        return _failure(
            ParseErrorCode.TOP_LEVEL_NOT_OBJECT,
            "Top-level JSON value must be an object",
            task_type,
        )

    model = RESPONSE_MODELS[task_type]
    try:
        value = model.model_validate(payload)
    except ValidationError as error:
        return _validation_failure(error, task_type)

    if task_type is TaskType.UPOS and isinstance(value, TaggingResponse):
        invalid_tags = sorted(set(value.tags) - UD_UPOS_TAGS)
        if invalid_tags:
            return _failure(
                ParseErrorCode.INVALID_TAG,
                f"Unknown UPOS tags: {', '.join(invalid_tags)}",
                task_type,
            )

    if isinstance(value, DependencyResponse):
        token_ids = [arc.token_id for arc in value.arcs]
        if len(token_ids) != len(set(token_ids)):
            return _failure(
                ParseErrorCode.DUPLICATE_TOKEN_ID,
                "Dependency response contains duplicate token_id values",
                task_type,
            )
        if expected_token_ids is not None:
            expected_id_set = set(expected_token_ids)
            if set(token_ids) != expected_id_set:
                return _failure(
                    ParseErrorCode.TOKEN_ID_MISMATCH,
                    "Dependency token IDs do not match the provided input tokens",
                    task_type,
                )
            invalid_heads = sorted(
                {arc.head_id for arc in value.arcs if arc.head_id not in expected_id_set | {0}}
            )
            if invalid_heads:
                return _failure(
                    ParseErrorCode.INVALID_HEAD_ID,
                    f"Unknown dependency head IDs: {invalid_heads}",
                    task_type,
                )

    if (
        task_type is not TaskType.SEGMENTATION
        and expected_count is not None
        and _sequence_length(value) != expected_count
    ):
        return _failure(
            ParseErrorCode.LENGTH_MISMATCH,
            f"Expected {expected_count} items, received {_sequence_length(value)}",
            task_type,
        )

    return ResponseParseResult(task=task_type, value=value)
