#!/usr/bin/env python3
"""Build the deterministic 18-language V1 draft challenge catalog."""

from __future__ import annotations

import argparse
import json
import os
import stat
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import ValidationError

from linguistic_oj.challenge import (
    LANGUAGE_CODES,
    ChallengeArtifacts,
    ChallengeExistsError,
    PrivateChallengeManifest,
    PublicChallenge,
    build_challenge,
    validated_gold_item_count,
)
from linguistic_oj.challenge_registry import (
    ChallengeContractRegistryDocument,
    ChallengeContractRegistryEntry,
    load_challenge_contract_registry,
    validate_contract_matches_public,
)
from linguistic_oj.dataset import iter_dataset_samples
from linguistic_oj.mvp_contract import EvaluationContract, canonical_json, canonical_sha256
from linguistic_oj.responses import TaskType

PUBLIC_CATALOG_FIELDS = {
    "annotation_license",
    "attribution_requirements",
    "challenge_id",
    "security_level",
    "share_alike_requirements",
    "source_commit",
    "source_file_sha256s",
    "source_release",
    "status",
    "underlying_text_rights",
}

V1_UPOS_MAX_TOKENS_BY_LANGUAGE = {
    language: 512 if language in {"Hebrew", "Japanese", "Spanish", "Swedish"} else 256
    for language in LANGUAGE_CODES
}
V1_JOB_DEADLINE_SECONDS_BY_TASK = {
    TaskType.DEPENDENCY: 900,
}


@dataclass(frozen=True, slots=True)
class ChallengePlan:
    language: str
    dataset_path: Path
    treebank: str
    eligible_samples: int
    task: TaskType
    version: str
    max_tokens: int
    job_deadline_seconds: int


@dataclass(frozen=True, slots=True)
class CatalogBuildResult:
    plans: tuple[ChallengePlan, ...]
    challenge_ids: tuple[str, ...]


V1_SUPPLEMENTAL_CHALLENGES = (
    ("Chinese", "GSDSimp", TaskType.SEGMENTATION, "v2", 256),
    ("German", "HDT", TaskType.XPOS, "v1", 256),
    ("German", "HDT", TaskType.DEPENDENCY, "v1", 1024),
    ("Chinese", "GSDSimp", TaskType.TRANSLITERATION, "v1", 512),
)


def select_largest_eligible_treebank(
    dataset_path: Path,
    *,
    language: str,
    task: TaskType,
    count: int,
) -> tuple[str, int]:
    """Select the largest valid task pool, breaking ties by treebank name."""

    if count <= 0:
        raise ValueError("count must be positive")
    eligible_by_treebank: Counter[str] = Counter()
    saw_sample = False
    for sample in iter_dataset_samples(dataset_path):
        saw_sample = True
        if sample.language != language:
            raise ValueError(f"dataset for {language} contains {sample.language} samples")
        if task.value not in sample.tasks_available:
            continue
        eligible_by_treebank[sample.treebank] += 1
    if not saw_sample:
        raise ValueError(f"dataset for {language} is empty")

    candidates = [
        (treebank, eligible_samples)
        for treebank, eligible_samples in eligible_by_treebank.items()
        if eligible_samples >= count
    ]
    if not candidates:
        raise ValueError(f"no {language} treebank has {count} valid {task.value} samples")
    selected = min(
        candidates,
        key=lambda item: (-item[1], item[0].casefold(), item[0]),
    )
    for sample in iter_dataset_samples(dataset_path):
        if sample.treebank == selected[0] and task.value in sample.tasks_available:
            validated_gold_item_count(sample, task)
    return selected


def plan_v1_challenges(
    dataset_dir: Path,
    *,
    languages: Sequence[str] = tuple(LANGUAGE_CODES),
    task: TaskType = TaskType.UPOS,
    count: int = 50,
) -> tuple[ChallengePlan, ...]:
    if not dataset_dir.is_dir():
        raise FileNotFoundError(f"dataset directory not found: {dataset_dir}")
    if (
        isinstance(languages, (str, bytes))
        or not languages
        or len(languages) != len(set(languages))
    ):
        raise ValueError("languages must be a non-empty unique sequence")

    plans = []
    for language in languages:
        if language not in LANGUAGE_CODES:
            raise ValueError(f"unsupported V1 language: {language}")
        matches = sorted(dataset_dir.glob(f"{language}_*.jsonl"))
        if len(matches) != 1:
            raise ValueError(f"expected exactly one dataset file for {language}")
        treebank, eligible_samples = select_largest_eligible_treebank(
            matches[0],
            language=language,
            task=task,
            count=count,
        )
        plans.append(
            ChallengePlan(
                language=language,
                dataset_path=matches[0],
                treebank=treebank,
                eligible_samples=eligible_samples,
                task=task,
                version="v1",
                max_tokens=(
                    V1_UPOS_MAX_TOKENS_BY_LANGUAGE[language]
                    if task is TaskType.UPOS
                    else 256
                ),
                job_deadline_seconds=V1_JOB_DEADLINE_SECONDS_BY_TASK.get(task, 300),
            )
        )
    return tuple(plans)


def plan_v1_supplemental_challenges(
    dataset_dir: Path,
    *,
    count: int = 50,
) -> tuple[ChallengePlan, ...]:
    """Validate the frozen task-family exemplars against strict eligibility."""

    plans = []
    for language, treebank, task, version, max_tokens in V1_SUPPLEMENTAL_CHALLENGES:
        matches = sorted(dataset_dir.glob(f"{language}_*.jsonl"))
        if len(matches) != 1:
            raise ValueError(f"expected exactly one dataset file for {language}")
        eligible_samples = 0
        for sample in iter_dataset_samples(matches[0]):
            if sample.language != language:
                raise ValueError(f"dataset for {language} contains {sample.language} samples")
            if sample.treebank != treebank or task.value not in sample.tasks_available:
                continue
            validated_gold_item_count(sample, task)
            eligible_samples += 1
        if eligible_samples < count:
            raise ValueError(
                f"{language} {treebank} has only {eligible_samples} valid {task.value} samples"
            )
        plans.append(
            ChallengePlan(
                language=language,
                dataset_path=matches[0],
                treebank=treebank,
                eligible_samples=eligible_samples,
                task=task,
                version=version,
                max_tokens=max_tokens,
                job_deadline_seconds=V1_JOB_DEADLINE_SECONDS_BY_TASK.get(task, 300),
            )
        )
    return tuple(plans)


def _contract_mapping(
    template: dict[str, object],
    artifacts: ChallengeArtifacts,
    *,
    max_tokens: int,
    job_deadline_seconds: int,
) -> tuple[dict[str, object], EvaluationContract]:
    config = json.loads(json.dumps(template))
    public = artifacts.public
    catalog = config["catalog"]
    identity = config["evaluation_identity"]
    limits = config["limits"]
    job_policy = config["job_policy"]
    partition = config["leaderboard_partition"]
    if not isinstance(catalog, dict) or not isinstance(identity, dict) or not isinstance(
        limits, dict
    ) or not isinstance(job_policy, dict) or not isinstance(partition, dict):
        raise ValueError("evaluation contract template has invalid sections")

    generation_settings = identity.get("generation_settings")
    model_context_tokens = limits.get("model_context_tokens")
    if not isinstance(generation_settings, dict) or type(model_context_tokens) is not int:
        raise ValueError("evaluation contract template has invalid token settings")
    if type(max_tokens) is not int or max_tokens <= 0 or max_tokens >= model_context_tokens:
        raise ValueError("max_tokens must fit within the model context")
    generation_settings["max_tokens"] = max_tokens
    limits["max_rendered_input_tokens"] = model_context_tokens - max_tokens
    if type(job_deadline_seconds) is not int or job_deadline_seconds <= 0:
        raise ValueError("job_deadline_seconds must be a positive integer")
    job_policy["job_deadline_seconds"] = job_deadline_seconds

    catalog.update(public.model_dump(mode="json", include=PUBLIC_CATALOG_FIELDS))
    identity.update(
        {
            "aggregation_version": public.aggregation_version,
            "challenge_id": public.challenge_id,
            "dataset_sha256": public.dataset_sha256,
            "response_schema_version": public.response_schema_version,
            "scorer_version": public.scorer_version,
            "selection_sha256": public.selection_sha256,
            "task": public.task,
        }
    )
    partition["expected_sha256"] = canonical_sha256(identity)
    contract = EvaluationContract.from_mapping(config)
    validate_contract_matches_public(contract, public)
    return config, contract


def _json_payload(value: object) -> str:
    return f"{json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)}\n"


def _preflight_model(path: Path, expected: PublicChallenge | PrivateChallengeManifest) -> None:
    if path.is_symlink():
        raise ChallengeExistsError(f"catalog output must not be a symbolic link: {path}")
    if not path.exists():
        return
    model_type = type(expected)
    try:
        existing = model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise ChallengeExistsError(f"existing challenge file is invalid: {path}") from error
    if existing != expected:
        raise ChallengeExistsError(
            f"challenge file has different immutable content: {path}"
        )


def _preflight_contract(path: Path, expected: dict[str, object]) -> None:
    if path.is_symlink():
        raise ChallengeExistsError(f"catalog output must not be a symbolic link: {path}")
    if not path.exists():
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ChallengeExistsError(f"existing evaluation contract is invalid: {path}") from error
    if canonical_json(existing) != canonical_json(expected):
        raise ChallengeExistsError(
            f"evaluation contract has different immutable content: {path}"
        )


def _write_new(
    path: Path,
    payload: str,
    validate_existing: Callable[[], None],
) -> None:
    if path.is_symlink():
        raise ChallengeExistsError(f"catalog output must not be a symbolic link: {path}")
    if path.exists():
        validate_existing()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as output_file:
            output_file.write(payload)
    except FileExistsError:
        validate_existing()


@contextmanager
def _catalog_lock(root: Path) -> Iterator[None]:
    lock_path = root / "config" / ".v1-catalog.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _reject_symlink_components(root, lock_path.parent, "catalog lock directory")
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    if lock_path.is_symlink():
        raise RuntimeError(f"catalog lock must not be a symbolic link: {lock_path}")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise RuntimeError(f"cannot safely open catalog lock: {lock_path}") from error
    try:
        path_stat = os.stat(lock_path, follow_symlinks=False)
        descriptor_stat = os.fstat(descriptor)
        if stat.S_ISLNK(path_stat.st_mode) or (
            path_stat.st_dev,
            path_stat.st_ino,
        ) != (descriptor_stat.st_dev, descriptor_stat.st_ino):
            raise RuntimeError(f"catalog lock changed while opening: {lock_path}")
        lock_file = os.fdopen(descriptor, "r+b")
        descriptor = -1
    except Exception:
        os.close(descriptor)
        raise
    with lock_file:
        lock_file.seek(0, 2)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("another V1 catalog build is in progress") from error
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_registry(path: Path, document: ChallengeContractRegistryDocument) -> None:
    if path.is_symlink():
        raise ChallengeExistsError(f"registry output must not be a symbolic link: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(_json_payload(document.model_dump(mode="json")))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary = Path(temporary_file.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _relative_project_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(f"catalog output must stay below the project root: {path}") from error


def _reject_symlink_components(root: Path, path: Path, name: str) -> None:
    lexical_root = root.absolute()
    lexical_path = path.absolute()
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as error:
        raise ValueError(f"{name} must stay below the project root") from error
    current = lexical_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{name} must not traverse symbolic links: {current}")


def _build_v1_catalog(
    root: Path,
    *,
    dataset_dir: Path,
    public_dir: Path,
    private_dir: Path,
    contract_template_path: Path,
    contract_dir: Path,
    registry_path: Path,
    languages: Sequence[str] = tuple(LANGUAGE_CODES),
    task: TaskType = TaskType.UPOS,
    count: int = 50,
    seed: int = 2026,
    version: str = "v1",
) -> CatalogBuildResult:
    """Preflight and write one immutable draft challenge per requested language."""

    _relative_project_path(root, public_dir)
    _relative_project_path(root, contract_dir)
    _relative_project_path(root, registry_path)
    _reject_symlink_components(root, public_dir, "public output directory")
    _reject_symlink_components(root, contract_dir, "contract output directory")
    _reject_symlink_components(root, registry_path.parent, "registry output directory")
    if registry_path.is_symlink():
        raise ChallengeExistsError(
            f"registry output must not be a symbolic link: {registry_path}"
        )
    plans = (
        *plan_v1_challenges(
            dataset_dir,
            languages=languages,
            task=task,
            count=count,
        ),
        *plan_v1_supplemental_challenges(dataset_dir, count=count),
    )
    template = json.loads(contract_template_path.read_text(encoding="utf-8"))
    if not isinstance(template, dict):
        raise ValueError("evaluation contract template must be a JSON object")

    generated = []
    generated_ids: set[str] = set()
    for plan in plans:
        artifacts = build_challenge(
            plan.dataset_path,
            language=plan.language,
            treebank=plan.treebank,
            task=plan.task,
            count=count,
            seed=seed,
            version=plan.version,
        )
        contract_mapping, contract = _contract_mapping(
            template,
            artifacts,
            max_tokens=plan.max_tokens,
            job_deadline_seconds=plan.job_deadline_seconds,
        )
        if contract.challenge_id in generated_ids:
            raise ValueError(f"duplicate generated challenge ID: {contract.challenge_id}")
        generated_ids.add(contract.challenge_id)
        public_path = public_dir / f"{contract.challenge_id}.json"
        private_path = private_dir / f"{contract.challenge_id}.json"
        contract_path = contract_dir / f"{contract.challenge_id}.json"
        _preflight_model(public_path, artifacts.public)
        _preflight_model(private_path, artifacts.private)
        _preflight_contract(contract_path, contract_mapping)
        generated.append(
            (
                artifacts,
                contract_mapping,
                contract,
                public_path,
                private_path,
                contract_path,
            )
        )

    existing_registry = None
    if registry_path.exists():
        existing_registry = load_challenge_contract_registry(root, registry_path.resolve())
        registry = ChallengeContractRegistryDocument.model_validate_json(
            registry_path.read_text(encoding="utf-8")
        )
        if registry.schema_version != "challenge-contract-registry-v1":
            raise ValueError("unsupported challenge contract registry version")
        references = [entry.public_descriptor_path for entry in registry.entries]
        if len(references) != len(set(references)):
            raise ValueError("challenge contract registry contains duplicate paths")
        entries = {entry.public_descriptor_path: entry for entry in registry.entries}
    else:
        entries = {}
    existing_identity_owners = (
        {
            contract.evaluation_identity_sha256: challenge_id
            for challenge_id, contract in existing_registry.contracts.items()
        }
        if existing_registry is not None
        else {}
    )
    for _, _, contract, public_path, _, contract_path in generated:
        public_reference = _relative_project_path(root, public_path)
        contract_reference = _relative_project_path(root, contract_path)
        existing_public = (
            existing_registry.public_challenges.get(contract.challenge_id)
            if existing_registry is not None
            else None
        )
        if existing_public is not None and public_reference not in entries:
            raise ChallengeExistsError(
                f"challenge ID already uses another registry path: {contract.challenge_id}"
            )
        identity_owner = existing_identity_owners.get(contract.evaluation_identity_sha256)
        if identity_owner is not None and identity_owner != contract.challenge_id:
            raise ChallengeExistsError(
                "evaluation identity already belongs to another challenge: "
                f"{identity_owner}"
            )
        existing_entry = entries.get(public_reference)
        if (
            existing_entry is not None
            and existing_entry.evaluation_contract_path is not None
            and existing_entry.evaluation_contract_path != contract_reference
        ):
            raise ChallengeExistsError(
                f"challenge already uses another contract path: {contract.challenge_id}"
            )
        entries[public_reference] = ChallengeContractRegistryEntry(
            public_descriptor_path=public_reference,
            evaluation_contract_path=contract_reference,
        )
    registry_document = ChallengeContractRegistryDocument(
        schema_version="challenge-contract-registry-v1",
        entries=tuple(entries[key] for key in sorted(entries)),
    )

    for artifacts, contract_mapping, _, public_path, private_path, contract_path in generated:
        _write_new(
            public_path,
            _json_payload(artifacts.public.model_dump(mode="json")),
            lambda path=public_path, expected=artifacts.public: _preflight_model(
                path, expected
            ),
        )
        _write_new(
            private_path,
            _json_payload(artifacts.private.model_dump(mode="json")),
            lambda path=private_path, expected=artifacts.private: _preflight_model(
                path, expected
            ),
        )
        _write_new(
            contract_path,
            _json_payload(contract_mapping),
            lambda path=contract_path, expected=contract_mapping: _preflight_contract(
                path, expected
            ),
        )
    _write_registry(registry_path, registry_document)
    return CatalogBuildResult(
        plans=plans,
        challenge_ids=tuple(item[0].public.challenge_id for item in generated),
    )


def build_v1_catalog(
    root: Path,
    *,
    dataset_dir: Path,
    public_dir: Path,
    private_dir: Path,
    contract_template_path: Path,
    contract_dir: Path,
    registry_path: Path,
    languages: Sequence[str] = tuple(LANGUAGE_CODES),
    task: TaskType = TaskType.UPOS,
    count: int = 50,
    seed: int = 2026,
    version: str = "v1",
) -> CatalogBuildResult:
    """Build the frozen representative V1 catalog under an exclusive lock."""

    if (
        task is not TaskType.UPOS
        or count != 50
        or seed != 2026
        or version != "v1"
        or tuple(languages) != tuple(LANGUAGE_CODES)
    ):
        raise ValueError(
            "V1 catalog policy requires exactly the ordered 18 languages, UPOS, "
            "count 50, seed 2026, and version v1"
        )
    _relative_project_path(root, public_dir)
    _relative_project_path(root, contract_dir)
    _relative_project_path(root, registry_path)
    _reject_symlink_components(root, public_dir, "public output directory")
    _reject_symlink_components(root, contract_dir, "contract output directory")
    _reject_symlink_components(root, registry_path.parent, "registry output directory")
    with _catalog_lock(root):
        return _build_v1_catalog(
            root,
            dataset_dir=dataset_dir,
            public_dir=public_dir,
            private_dir=private_dir,
            contract_template_path=contract_template_path,
            contract_dir=contract_dir,
            registry_path=registry_path,
            languages=languages,
            task=task,
            count=count,
            seed=seed,
            version=version,
        )


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--count", type=int, choices=(50,), default=50)
    parser.add_argument("--seed", type=int, choices=(2026,), default=2026)
    parser.add_argument("--version", choices=("v1",), default="v1")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    root = args.root.resolve()
    result = build_v1_catalog(
        root,
        dataset_dir=root / "Standard_Dataset" / "by_language",
        public_dir=root / "challenges" / "public",
        private_dir=root / "runtime" / "private" / "challenges",
        contract_template_path=root / "config" / "mvp_evaluation_v2.json",
        contract_dir=root / "config" / "evaluation_contracts" / "v1",
        registry_path=root / "config" / "challenge_contract_registry_v1.json",
        count=args.count,
        seed=args.seed,
        version=args.version,
    )
    print(
        _json_payload(
            {
                "challenge_ids": result.challenge_ids,
                "plans": [
                    {
                        "eligible_samples": plan.eligible_samples,
                        "language": plan.language,
                        "max_tokens": plan.max_tokens,
                        "task": plan.task.value,
                        "treebank": plan.treebank,
                        "version": plan.version,
                    }
                    for plan in result.plans
                ],
            }
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
