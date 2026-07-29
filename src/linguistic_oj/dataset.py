"""Streaming access to the standardized JSONL dataset."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class DatasetSample(BaseModel):
    """Fields required by challenge construction and later evaluation."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    id: str
    language: str
    treebank: str
    text: str
    answers: dict[str, Any]
    tasks_available: list[str]


class DatasetFormatError(ValueError):
    """Raised when a JSONL line is invalid or does not match the dataset schema."""


def iter_dataset_samples(path: Path) -> Iterator[DatasetSample]:
    """Yield validated samples one line at a time without loading the full file."""

    if not path.is_file():
        raise FileNotFoundError(f"Dataset file not found: {path}")

    with path.open(encoding="utf-8-sig") as dataset_file:
        for line_number, line in enumerate(dataset_file, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                yield DatasetSample.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as error:
                raise DatasetFormatError(
                    f"Invalid dataset sample at {path}:{line_number}: {error}"
                ) from error


def iter_matching_samples(
    path: Path,
    *,
    language: str,
    treebank: str,
    task: str,
) -> Iterator[DatasetSample]:
    """Yield samples matching one language, treebank, and available task."""

    for sample in iter_dataset_samples(path):
        if (
            sample.language == language
            and sample.treebank == treebank
            and task in sample.tasks_available
        ):
            yield sample
