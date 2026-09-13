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
    validate_public_challenge(public)
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
        "annotation_license": public.annotation_license,
        "attribution_requirements": public.attribution_requirements,
        "source_release": public.source_release,
        "source_commit": public.source_commit,
        "source_file_sha256s": [
            source.model_dump(mode="json") for source in public.source_file_sha256s
        ],
        "share_alike_requirements": public.share_alike_requirements,
        "underlying_text_rights": public.underlying_text_rights,
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
        "annotation_license": contract.catalog.get("annotation_license"),
        "attribution_requirements": contract.catalog.get("attribution_requirements"),
        "source_release": contract.catalog.get("source_release"),
        "source_commit": contract.catalog.get("source_commit"),
        "source_file_sha256s": contract.catalog.get("source_file_sha256s"),
        "share_alike_requirements": contract.catalog.get("share_alike_requirements"),
        "underlying_text_rights": contract.catalog.get("underlying_text_rights"),
    }
    if actual != expected:
        raise ValueError(
            f"evaluation contract does not match public challenge {public.challenge_id}"
        )


def load_challenge_contract_registry(
    root: Path,
    registry_path: Path,
) -> ChallengeContractRegistry:
    """Load safe public metadata and optional executable contracts from references."""

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
    evaluation_identities: set[str] = set()
    for index, entry in enumerate(document.entries):
        public_path = _project_path(
            root,
            entry.public_descriptor_path,
            f"entries[{index}].public_descriptor_path",
        )
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
        contract = EvaluationContract.from_path(contract_path)
        validate_contract_matches_public(contract, public)
        if contract.evaluation_identity_sha256 in evaluation_identities:
            raise ValueError(
                f"duplicate evaluation identity: {contract.evaluation_identity_sha256}"
            )
        evaluation_identities.add(contract.evaluation_identity_sha256)
        contracts[public.challenge_id] = contract

    if not contracts:
        raise ValueError("challenge contract registry must contain an executable contract")
    return ChallengeContractRegistry(
        public_challenges=MappingProxyType(public_challenges),
        contracts=MappingProxyType(contracts),
    )
