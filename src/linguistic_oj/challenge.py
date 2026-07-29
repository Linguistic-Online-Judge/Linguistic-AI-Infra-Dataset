"""Build deterministic, versioned challenge manifests from standard JSONL data."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .dataset import DatasetSample, iter_matching_samples
from .responses import TaskType

LANGUAGE_CODES = {
    "Arabic": "ar",
    "Chinese": "zh",
    "Danish": "da",
    "Dutch": "nl",
    "English": "en",
    "French": "fr",
    "German": "de",
    "Hebrew": "he",
    "Hindi": "hi",
    "Hungarian": "hu",
    "Italian": "it",
    "Japanese": "ja",
    "Korean": "ko",
    "Portuguese": "pt",
    "Russian": "ru",
    "Spanish": "es",
    "Swedish": "sv",
    "Thai": "th",
}

TASK_METRICS = {
    TaskType.SEGMENTATION: ("micro_f1", []),
    TaskType.UPOS: ("micro_accuracy", []),
    TaskType.XPOS: ("micro_accuracy", []),
    TaskType.DEPENDENCY: ("las", ["uas"]),
    TaskType.TRANSLITERATION: (
        "token_accuracy",
        ["sentence_exact_match_rate"],
    ),
}


class PublicChallenge(BaseModel):
    """Safe metadata that may be returned to students or committed to Git."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    challenge_id: str
    title: str
    version: str
    language: str
    treebank: str
    task: str
    sample_count: int
    primary_metric: str
    secondary_metrics: list[str]
    response_schema_version: str


class PrivateChallengeManifest(BaseModel):
    """Server-only sample selection and integrity metadata."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    challenge_id: str
    version: str
    dataset_sha256: str
    selection_sha256: str
    selection_seed: int
    sample_ids: list[str]


@dataclass(frozen=True, slots=True)
class ChallengeArtifacts:
    public: PublicChallenge
    private: PrivateChallengeManifest


class InsufficientSamplesError(ValueError):
    """Raised when a filtered data pool cannot satisfy the requested count."""


class DuplicateSampleIdError(ValueError):
    """Raised when the filtered source contains a duplicate sample ID."""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise ValueError(f"Cannot create slug from value: {value!r}")
    return slug


def make_challenge_id(
    language: str,
    treebank: str,
    task: TaskType,
    version: str,
) -> str:
    language_code = LANGUAGE_CODES.get(language, _slugify(language))
    return "-".join([language_code, _slugify(treebank), _slugify(task.value), _slugify(version)])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _selection_hash(sample_ids: list[str]) -> str:
    payload = "\n".join(sample_ids).encode()
    return hashlib.sha256(payload).hexdigest()


def _select_samples(
    dataset_path: Path,
    *,
    language: str,
    treebank: str,
    task: TaskType,
    count: int,
    seed: int,
) -> list[DatasetSample]:
    if count <= 0:
        raise ValueError("count must be positive")

    random_source = random.Random(seed)
    reservoir: list[DatasetSample] = []
    seen_ids: set[str] = set()
    matching_count = 0

    for sample in iter_matching_samples(
        dataset_path,
        language=language,
        treebank=treebank,
        task=task.value,
    ):
        if sample.id in seen_ids:
            raise DuplicateSampleIdError(f"Duplicate sample ID: {sample.id}")
        seen_ids.add(sample.id)

        matching_count += 1
        if len(reservoir) < count:
            reservoir.append(sample)
            continue

        replacement_index = random_source.randrange(matching_count)
        if replacement_index < count:
            reservoir[replacement_index] = sample

    if matching_count < count:
        raise InsufficientSamplesError(
            f"Requested {count} samples but only {matching_count} match "
            f"{language}/{treebank}/{task.value}"
        )

    return sorted(reservoir, key=lambda sample: sample.id)


def build_challenge(
    dataset_path: Path,
    *,
    language: str,
    treebank: str,
    task: TaskType | str,
    count: int,
    seed: int,
    version: str,
) -> ChallengeArtifacts:
    """Create deterministic public metadata and a private sample manifest."""

    task_type = TaskType(task)
    selected = _select_samples(
        dataset_path,
        language=language,
        treebank=treebank,
        task=task_type,
        count=count,
        seed=seed,
    )
    sample_ids = [sample.id for sample in selected]
    challenge_id = make_challenge_id(language, treebank, task_type, version)
    primary_metric, secondary_metrics = TASK_METRICS[task_type]

    public = PublicChallenge(
        challenge_id=challenge_id,
        title=f"{language} {treebank} {task_type.value} challenge",
        version=version,
        language=language,
        treebank=treebank,
        task=task_type.value,
        sample_count=count,
        primary_metric=primary_metric,
        secondary_metrics=secondary_metrics,
        response_schema_version=f"{task_type.value}-v1",
    )
    private = PrivateChallengeManifest(
        challenge_id=challenge_id,
        version=version,
        dataset_sha256=sha256_file(dataset_path),
        selection_sha256=_selection_hash(sample_ids),
        selection_seed=seed,
        sample_ids=sample_ids,
    )
    return ChallengeArtifacts(public=public, private=private)


def _write_json(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    path.write_text(f"{payload}\n", encoding="utf-8")


def write_challenge(
    artifacts: ChallengeArtifacts,
    *,
    public_dir: Path,
    private_dir: Path,
) -> tuple[Path, Path]:
    public_path = public_dir / f"{artifacts.public.challenge_id}.json"
    private_path = private_dir / f"{artifacts.private.challenge_id}.json"
    _write_json(public_path, artifacts.public)
    _write_json(private_path, artifacts.private)
    return public_path, private_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a deterministic challenge set.")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("Standard_Dataset/standard_dataset.jsonl"),
    )
    parser.add_argument("--language", required=True)
    parser.add_argument("--treebank", required=True)
    parser.add_argument("--task", required=True, choices=[task.value for task in TaskType])
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--public-dir", type=Path, default=Path("challenges/public"))
    parser.add_argument(
        "--private-dir",
        type=Path,
        default=Path("runtime/private/challenges"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    artifacts = build_challenge(
        args.dataset,
        language=args.language,
        treebank=args.treebank,
        task=args.task,
        count=args.count,
        seed=args.seed,
        version=args.version,
    )
    public_path, private_path = write_challenge(
        artifacts,
        public_dir=args.public_dir,
        private_dir=args.private_dir,
    )
    print(f"Challenge: {artifacts.public.challenge_id}")
    print(f"Public description: {public_path}")
    print(f"Private manifest: {private_path}")


if __name__ == "__main__":
    main()
