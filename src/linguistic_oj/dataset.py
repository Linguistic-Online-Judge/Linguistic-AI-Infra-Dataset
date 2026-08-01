"""Streaming access to the standardized JSONL dataset."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
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


class SelectedSampleSetError(ValueError):
    """Raised when manifest-selected samples cannot be loaded exactly once."""


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


def load_dataset_samples_by_id(
    path: Path,
    sample_ids: Sequence[str],
) -> tuple[DatasetSample, ...]:
    """Load a bounded sample set in manifest order while streaming the dataset."""

    if isinstance(sample_ids, (str, bytes)) or not sample_ids:
        raise SelectedSampleSetError("sample_ids must be a non-empty sequence")
    if any(not isinstance(sample_id, str) or not sample_id for sample_id in sample_ids):
        raise SelectedSampleSetError("sample_ids must contain non-empty strings")
    if len(sample_ids) != len(set(sample_ids)):
        raise SelectedSampleSetError("sample_ids contains duplicate values")

    expected_ids = set(sample_ids)
    selected: dict[str, DatasetSample] = {}
    for sample in iter_dataset_samples(path):
        if sample.id not in expected_ids:
            continue
        if sample.id in selected:
            raise SelectedSampleSetError(
                f"Dataset contains duplicate selected sample ID: {sample.id}"
            )
        selected[sample.id] = sample

    missing_ids = sorted(expected_ids - selected.keys())
    if missing_ids:
        raise SelectedSampleSetError(f"Dataset is missing selected sample IDs: {missing_ids}")
    return tuple(selected[sample_id] for sample_id in sample_ids)
