"""Build deterministic, versioned challenge manifests from standard JSONL data."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    AGGREGATION_VERSION,
    RESPONSE_SCHEMA_VERSIONS,
    SCORER_VERSION,
    TASK_METRICS,
)
from .dataset import DatasetSample, iter_matching_samples
from .responses import UD_UPOS_TAGS, TaskType

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


class ChallengeSecurityLevel(StrEnum):
    PUBLIC_REPRODUCIBLE = "public_reproducible"


class ChallengeStatus(StrEnum):
    DRAFT = "draft"


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
    secondary_metrics: tuple[str, ...]
    response_schema_version: str
    scorer_version: str | None = None
    aggregation_version: str | None = None
    dataset_sha256: str
    selection_sha256: str
    security_level: str
    status: str


class ManifestSample(BaseModel):
    """Immutable sample identity and trusted gold denominator."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    sample_id: str
    gold_items: int = Field(gt=0)


class PrivateChallengeManifest(BaseModel):
    """Server-only sample selection and integrity metadata."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    challenge_id: str
    version: str
    task: str
    scorer_version: str
    aggregation_version: str
    dataset_sha256: str
    selection_sha256: str
    selection_seed: int
    samples: tuple[ManifestSample, ...]

    @property
    def sample_ids(self) -> tuple[str, ...]:
        return tuple(sample.sample_id for sample in self.samples)

    @property
    def gold_items_by_sample_id(self) -> dict[str, int]:
        return {sample.sample_id: sample.gold_items for sample in self.samples}


@dataclass(frozen=True, slots=True)
class ChallengeArtifacts:
    public: PublicChallenge
    private: PrivateChallengeManifest
    dataset_path: Path

    def __post_init__(self) -> None:
        validate_challenge_artifacts(self)


class InsufficientSamplesError(ValueError):
    """Raised when a filtered data pool cannot satisfy the requested count."""


class DuplicateSampleIdError(ValueError):
    """Raised when the filtered source contains a duplicate sample ID."""


class ChallengeExistsError(FileExistsError):
    """Raised when an existing challenge ID has different immutable content."""


class InvalidGoldAnswerError(ValueError):
    """Raised when a selected sample lacks valid gold data for its task."""


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


def selection_sha256(samples: tuple[ManifestSample, ...]) -> str:
    payload = json.dumps(
        [sample.model_dump(mode="json") for sample in samples],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def validated_gold_item_count(sample: DatasetSample, task: TaskType) -> int:
    tokens = sample.answers.get(TaskType.SEGMENTATION.value)
    if not isinstance(tokens, list) or not tokens or any(
        not isinstance(token, str) or not token for token in tokens
    ):
        raise InvalidGoldAnswerError(
            f"Sample {sample.id} must have a non-empty segmentation gold list"
        )
    if task is TaskType.SEGMENTATION:
        return len(tokens)

    answer = sample.answers.get(task.value)
    if not isinstance(answer, list) or not answer:
        raise InvalidGoldAnswerError(
            f"Sample {sample.id} must have a non-empty {task.value} gold list"
        )
    if len(answer) != len(tokens):
        raise InvalidGoldAnswerError(
            f"Sample {sample.id} {task.value} gold count must match segmentation"
        )

    if task is TaskType.DEPENDENCY:
        token_ids: set[int] = set()
        for index, arc in enumerate(answer):
            if not isinstance(arc, list) or len(arc) != 5:
                raise InvalidGoldAnswerError(
                    f"Sample {sample.id} dependency[{index}] must contain five fields"
                )
            token_id, token_form, head_id, head_form, deprel = arc
            if (
                type(token_id) is not int
                or not 0 < token_id <= len(answer)
                or token_id in token_ids
            ):
                raise InvalidGoldAnswerError(
                    f"Sample {sample.id} dependency[{index}] has an invalid token ID"
                )
            if type(head_id) is not int or not 0 <= head_id <= len(answer):
                raise InvalidGoldAnswerError(
                    f"Sample {sample.id} dependency[{index}] has an invalid head ID"
                )
            text_fields = (token_form, head_form, deprel)
            if any(not isinstance(value, str) or not value for value in text_fields):
                raise InvalidGoldAnswerError(
                    f"Sample {sample.id} dependency[{index}] has an invalid text field"
                )
            if token_form != tokens[token_id - 1]:
                raise InvalidGoldAnswerError(
                    f"Sample {sample.id} dependency[{index}] token form is misaligned"
                )
            expected_head_form = "ROOT" if head_id == 0 else tokens[head_id - 1]
            if head_form != expected_head_form:
                raise InvalidGoldAnswerError(
                    f"Sample {sample.id} dependency[{index}] head form is misaligned"
                )
            token_ids.add(token_id)
        if token_ids != set(range(1, len(answer) + 1)):
            raise InvalidGoldAnswerError(
                f"Sample {sample.id} dependency token IDs must be contiguous"
            )
    elif any(not isinstance(item, str) or not item for item in answer):
        raise InvalidGoldAnswerError(
            f"Sample {sample.id} has invalid {task.value} gold items"
        )
    elif task is TaskType.UPOS and any(tag not in UD_UPOS_TAGS for tag in answer):
        raise InvalidGoldAnswerError(f"Sample {sample.id} has an invalid UPOS gold tag")

    return len(answer)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def validate_challenge_artifacts(artifacts: ChallengeArtifacts) -> None:
    """Validate the complete public/private challenge identity and manifest."""

    public = artifacts.public
    private = artifacts.private
    if not isinstance(public, PublicChallenge) or not isinstance(
        private, PrivateChallengeManifest
    ):
        raise TypeError("artifacts must contain PublicChallenge and PrivateChallengeManifest")

    task = TaskType(public.task)
    expected_primary, expected_secondary = TASK_METRICS[task]
    if public.primary_metric != expected_primary or public.secondary_metrics != expected_secondary:
        raise ValueError("public metrics do not match the task contract")
    if public.response_schema_version != RESPONSE_SCHEMA_VERSIONS[task]:
        raise ValueError("response schema version does not match the task contract")
    if public.scorer_version != SCORER_VERSION or private.scorer_version != SCORER_VERSION:
        raise ValueError("challenge scorer version does not match the runtime")
    if (
        public.aggregation_version != AGGREGATION_VERSION
        or private.aggregation_version != AGGREGATION_VERSION
    ):
        raise ValueError("challenge aggregation version does not match the runtime")
    if public.security_level != ChallengeSecurityLevel.PUBLIC_REPRODUCIBLE.value:
        raise ValueError("unsupported challenge security level")
    if public.status != ChallengeStatus.DRAFT.value:
        raise ValueError("unsupported challenge status")

    matching_fields = (
        "challenge_id",
        "version",
        "task",
        "scorer_version",
        "aggregation_version",
        "dataset_sha256",
        "selection_sha256",
    )
    if any(getattr(public, field) != getattr(private, field) for field in matching_fields):
        raise ValueError("public challenge and private manifest do not match")
    if public.challenge_id != make_challenge_id(
        public.language, public.treebank, task, public.version
    ):
        raise ValueError("challenge_id does not match challenge metadata")
    if not _is_sha256(public.dataset_sha256) or not _is_sha256(public.selection_sha256):
        raise ValueError("challenge fingerprints must be lowercase SHA-256 values")

    sample_ids = private.sample_ids
    if (
        not sample_ids
        or any(not sample_id for sample_id in sample_ids)
        or len(sample_ids) != len(set(sample_ids))
    ):
        raise ValueError("private manifest must contain unique samples")
    if sample_ids != tuple(sorted(sample_ids)):
        raise ValueError("private manifest samples must be sorted by sample_id")
    if public.sample_count != len(private.samples):
        raise ValueError("public sample_count does not match the private manifest")
    if selection_sha256(private.samples) != private.selection_sha256:
        raise ValueError("private manifest does not match selection_sha256")
    if not isinstance(artifacts.dataset_path, Path):
        raise TypeError("dataset_path must be a Path")
    if sha256_file(artifacts.dataset_path) != public.dataset_sha256:
        raise ValueError("configured dataset does not match dataset_sha256")


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
    manifest_samples = tuple(
        ManifestSample(
            sample_id=sample.id,
            gold_items=validated_gold_item_count(sample, task_type),
        )
        for sample in selected
    )
    challenge_id = make_challenge_id(language, treebank, task_type, version)
    primary_metric, secondary_metrics = TASK_METRICS[task_type]
    dataset_sha256 = sha256_file(dataset_path)
    selection_hash = selection_sha256(manifest_samples)

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
        response_schema_version=RESPONSE_SCHEMA_VERSIONS[task_type],
        scorer_version=SCORER_VERSION,
        aggregation_version=AGGREGATION_VERSION,
        dataset_sha256=dataset_sha256,
        selection_sha256=selection_hash,
        security_level=ChallengeSecurityLevel.PUBLIC_REPRODUCIBLE.value,
        status=ChallengeStatus.DRAFT.value,
    )
    private = PrivateChallengeManifest(
        challenge_id=challenge_id,
        version=version,
        task=task_type.value,
        scorer_version=SCORER_VERSION,
        aggregation_version=AGGREGATION_VERSION,
        dataset_sha256=dataset_sha256,
        selection_sha256=selection_hash,
        selection_seed=seed,
        samples=manifest_samples,
    )
    return ChallengeArtifacts(public=public, private=private, dataset_path=dataset_path)


def _serialize_json(model: BaseModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return f"{payload}\n"


def load_challenge_artifacts(
    public_path: Path,
    private_path: Path,
    *,
    dataset_path: Path,
) -> ChallengeArtifacts:
    """Load and verify an evaluation-ready challenge against its dataset file."""

    public = PublicChallenge.model_validate_json(public_path.read_text(encoding="utf-8"))
    private = PrivateChallengeManifest.model_validate_json(
        private_path.read_text(encoding="utf-8")
    )
    return ChallengeArtifacts(public=public, private=private, dataset_path=dataset_path)


def _ensure_compatible_existing_file(path: Path, payload: str) -> None:
    if not path.exists():
        return

    try:
        existing_content = json.loads(path.read_text(encoding="utf-8"))
        new_content = json.loads(payload)
    except json.JSONDecodeError:
        existing_content = None
        new_content = object()

    if existing_content != new_content:
        raise ChallengeExistsError(
            f"Challenge file already exists with different content: {path}. "
            "Create a new challenge version instead of overwriting it."
        )


def _write_new_file(path: Path, payload: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def write_challenge(
    artifacts: ChallengeArtifacts,
    *,
    public_dir: Path,
    private_dir: Path,
) -> tuple[Path, Path]:
    validate_challenge_artifacts(artifacts)
    public_path = public_dir / f"{artifacts.public.challenge_id}.json"
    private_path = private_dir / f"{artifacts.private.challenge_id}.json"
    public_payload = _serialize_json(artifacts.public)
    private_payload = _serialize_json(artifacts.private)

    # Check both outputs before writing either one to avoid a partially updated pair.
    _ensure_compatible_existing_file(public_path, public_payload)
    _ensure_compatible_existing_file(private_path, private_payload)
    _write_new_file(public_path, public_payload)
    _write_new_file(private_path, private_payload)
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
    parser.add_argument("--version", required=True)
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
