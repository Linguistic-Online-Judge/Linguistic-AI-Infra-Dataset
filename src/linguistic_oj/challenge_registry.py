"""Validated public challenge catalog and evaluation-contract registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from .challenge import PublicChallenge, validate_public_challenge
from .mvp_contract import EvaluationContract


class ChallengeContractRegistryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    public_descriptor_path: str
    evaluation_contract_path: str | None


class ChallengeContractRegistryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: str
    entries: tuple[ChallengeContractRegistryEntry, ...]


@dataclass(frozen=True, slots=True)
class ChallengeContractRegistry:
    public_challenges: Mapping[str, PublicChallenge]
    contracts: Mapping[str, EvaluationContract]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "public_challenges",
            MappingProxyType(dict(self.public_challenges)),
        )
        object.__setattr__(self, "contracts", MappingProxyType(dict(self.contracts)))


def _project_path(root: Path, value: str, name: str) -> Path:
    if not value or value.strip() != value or "\\" in value:
        raise ValueError(f"{name} must be a root-relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or any(":" in part for part in path.parts)
    ):
        raise ValueError(f"{name} must stay below the project root")

    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*path.parts)
    if candidate.is_symlink():
        raise ValueError(f"{name} must not be a symbolic link")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"{name} must stay below the project root")
    return resolved


def validate_contract_matches_public(
    contract: EvaluationContract,
    public: PublicChallenge,
) -> None:
    """Reject a contract that describes a different public challenge."""

    validate_public_challenge(public)
    if public.scorer_version is None or public.aggregation_version is None:
        raise ValueError(
            "an evaluation contract requires public scorer and aggregation versions"
        )
    identity = contract.evaluation_identity
    expected = {
        "challenge_id": public.challenge_id,
        "status": public.status,
        "security_level": public.security_level,
        "dataset_sha256": public.dataset_sha256,
        "selection_sha256": public.selection_sha256,
        "task": public.task,
        "response_schema_version": public.response_schema_version,
        "scorer_version": public.scorer_version,
        "aggregation_version": public.aggregation_version,
    }
    actual = {
        "challenge_id": contract.challenge_id,
        "status": contract.catalog_status,
        "security_level": contract.catalog.get("security_level"),
        "dataset_sha256": identity.get("dataset_sha256"),
        "selection_sha256": identity.get("selection_sha256"),
        "task": identity.get("task"),
        "response_schema_version": identity.get("response_schema_version"),
        "scorer_version": identity.get("scorer_version"),
        "aggregation_version": identity.get("aggregation_version"),
    }
    mismatches = sorted(field for field in expected if actual[field] != expected[field])
    if mismatches:
        raise ValueError(
            "evaluation contract does not match public challenge "
            f"{public.challenge_id}: {', '.join(mismatches)}"
        )


def load_challenge_contract_registry(
    root: Path,
    registry_path: Path,
) -> ChallengeContractRegistry:
    """Load and validate all registry references eagerly for trusted startup."""

    if not isinstance(root, Path) or not isinstance(registry_path, Path):
        raise TypeError("root and registry_path must be Path values")
    if not registry_path.is_absolute():
        registry_path = root / registry_path
    document = ChallengeContractRegistryDocument.model_validate_json(
        registry_path.read_text(encoding="utf-8")
    )
    if document.schema_version != "challenge-contract-registry-v1":
        raise ValueError("unsupported challenge contract registry version")
    if not document.entries:
        raise ValueError("challenge contract registry must not be empty")

    public_challenges: dict[str, PublicChallenge] = {}
    contracts: dict[str, EvaluationContract] = {}
    public_paths: set[Path] = set()
    contract_paths: set[Path] = set()
    for index, entry in enumerate(document.entries):
        public_path = _project_path(
            root,
            entry.public_descriptor_path,
            f"entries[{index}].public_descriptor_path",
        )
        if public_path in public_paths:
            raise ValueError(f"duplicate public descriptor path: {entry.public_descriptor_path}")
        public_paths.add(public_path)

        public = PublicChallenge.model_validate_json(public_path.read_text(encoding="utf-8"))
        validate_public_challenge(public)
        if public.challenge_id in public_challenges:
            raise ValueError(f"duplicate challenge ID: {public.challenge_id}")
        public_challenges[public.challenge_id] = public

        if entry.evaluation_contract_path is None:
            continue
        contract_path = _project_path(
            root,
            entry.evaluation_contract_path,
            f"entries[{index}].evaluation_contract_path",
        )
        if contract_path in contract_paths:
            raise ValueError(
                f"duplicate evaluation contract path: {entry.evaluation_contract_path}"
            )
        contract_paths.add(contract_path)

        contract = EvaluationContract.from_path(contract_path)
        validate_contract_matches_public(contract, public)
        contracts[public.challenge_id] = contract

    return ChallengeContractRegistry(
        public_challenges=public_challenges,
        contracts=contracts,
    )
