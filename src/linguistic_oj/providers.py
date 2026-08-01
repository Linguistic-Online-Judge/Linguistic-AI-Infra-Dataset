"""Provider-neutral model generation contract and deterministic test provider."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .model_inputs import (
    DependencyModelInput,
    SafeModelInput,
    SegmentationModelInput,
    TaggingModelInput,
    TransliterationModelInput,
    canonicalize_model_input,
    model_input_matches_task,
)
from .responses import TaskType, response_json_schema


class ProviderContractError(RuntimeError):
    """Raised when a provider adapter violates the platform contract."""


@dataclass(frozen=True, slots=True)
class ModelRequest:
    task: TaskType
    language: str
    treebank: str
    student_prompt: str
    model_input: SafeModelInput

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskType):
            raise TypeError("task must be a TaskType")
        for name, value in (
            ("language", self.language),
            ("treebank", self.treebank),
            ("student_prompt", self.student_prompt),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not model_input_matches_task(self.task, self.model_input):
            raise TypeError(f"{self.task.value} received the wrong model input type")
        object.__setattr__(
            self,
            "model_input",
            canonicalize_model_input(self.task, self.model_input),
        )

    @property
    def response_schema(self) -> dict[str, Any]:
        return response_json_schema(self.task)


@dataclass(frozen=True, slots=True)
class ModelGeneration:
    raw_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.raw_text, str):
            raise TypeError("raw_text must be a string")


@runtime_checkable
class ModelProvider(Protocol):
    def generate(self, request: ModelRequest, /) -> ModelGeneration: ...


class DeterministicMockProvider:
    """Generate valid deterministic responses using safe input fields only."""

    def generate(self, request: ModelRequest, /) -> ModelGeneration:
        if not isinstance(request, ModelRequest):
            raise ProviderContractError("request must be a ModelRequest")

        model_input = request.model_input
        if isinstance(model_input, SegmentationModelInput):
            payload: dict[str, Any] = {"tokens": list(model_input.text)}
        elif isinstance(model_input, TaggingModelInput):
            tag = "X" if request.task is TaskType.UPOS else "MOCK"
            payload = {"tags": [tag] * len(model_input.tokens)}
        elif isinstance(model_input, DependencyModelInput):
            payload = {
                "arcs": [
                    {
                        "token_id": token.token_id,
                        "head_id": 0 if token.token_id == 1 else token.token_id - 1,
                        "deprel": "root" if token.token_id == 1 else "dep",
                    }
                    for token in model_input.tokens
                ]
            }
        elif isinstance(model_input, TransliterationModelInput):
            payload = {"transliterations": list(model_input.tokens)}
        else:
            raise ProviderContractError(
                f"Unsupported model input type: {type(model_input).__name__}"
            )

        raw_text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return ModelGeneration(raw_text=raw_text)
