"""Runtime access to a frozen evaluation contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .aggregation import ChallengeAggregateResult

_CANONICALIZATION_PARAMETERS = {
    "allow_nan": False,
    "ensure_ascii": False,
    "separators": [",", ":"],
    "sort_keys": True,
}


def canonical_json(value: object) -> str:
    """Serialize using the versioned python-json-v1 contract."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class EvaluationContract:
    """Validated immutable snapshot used by API, worker, and leaderboard code."""

    snapshot_json: str
    contract_version: str
    challenge_id: str
    catalog_status: str
    evaluation_identity_sha256: str
    contract_snapshot_sha256: str
    owner_result_fields: tuple[str, ...]
    owner_failure_fields: tuple[str, ...]
    public_leaderboard_fields: tuple[str, ...]
    idempotency_key_pattern: str
    student_prompt_utf8_bytes: int
    student_prompt_tokens: int
    api_request_body_bytes: int
    max_rendered_input_tokens: int
    model_context_tokens: int
    submissions_per_user_per_challenge_per_24h: int
    max_outstanding_submissions_per_user: int
    max_running_submissions_per_user: int
    global_queue_depth: int
    max_attempts: int
    job_deadline_seconds: int
    failure_contract_version: str
    retryable_failure_codes: frozenset[str]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EvaluationContract:
        snapshot_json = canonical_json(dict(value))
        config = _mapping(json.loads(snapshot_json), "contract")
        identity = _mapping(config.get("evaluation_identity"), "evaluation_identity")
        partition = _mapping(config.get("leaderboard_partition"), "leaderboard_partition")
        if partition.get("algorithm") != "sha256":
            raise ValueError("unsupported leaderboard hash algorithm")
        if partition.get("canonicalization") != "python-json-v1":
            raise ValueError("unsupported leaderboard canonicalization")
        if partition.get("canonicalization_parameters") != _CANONICALIZATION_PARAMETERS:
            raise ValueError("leaderboard canonicalization parameters do not match")
        if partition.get("canonical_json_source") != "evaluation_identity":
            raise ValueError("leaderboard source must be evaluation_identity")

        identity_sha256 = canonical_sha256(identity)
        if partition.get("expected_sha256") != identity_sha256:
            raise ValueError("evaluation identity SHA-256 does not match the contract")

        catalog = _mapping(config.get("catalog"), "catalog")
        feedback = _mapping(config.get("feedback"), "feedback")
        idempotency = _mapping(config.get("idempotency"), "idempotency")
        limits = _mapping(config.get("limits"), "limits")
        job_policy = _mapping(config.get("job_policy"), "job_policy")
        failure_contract = _mapping(config.get("failure_contract"), "failure_contract")
        failure_codes = _mapping(failure_contract.get("codes"), "failure_contract.codes")

        contract_version = config.get("contract_version")
        challenge_id = catalog.get("challenge_id")
        catalog_status = catalog.get("status")
        if not all(
            isinstance(item, str) and item
            for item in (contract_version, challenge_id, catalog_status)
        ):
            raise ValueError("contract, challenge, and catalog status must be non-empty")
        if catalog_status not in {"draft", "active"}:
            raise ValueError("unsupported catalog status")
        if identity.get("contract_version") != contract_version:
            raise ValueError("identity contract version does not match")
        if identity.get("challenge_id") != challenge_id:
            raise ValueError("identity challenge does not match")

        key_pattern = idempotency.get("key_ascii_pattern")
        if not isinstance(key_pattern, str) or idempotency.get(
            "key_match_semantics"
        ) != "ascii_fullmatch":
            raise ValueError("unsupported idempotency-key contract")
        re.compile(key_pattern, flags=re.ASCII)

        retryable_codes = frozenset(job_policy.get("retryable_failure_codes", ()))
        declared_retryable = frozenset(
            code
            for code, policy in failure_codes.items()
            if isinstance(policy, dict) and policy.get("retryable") is True
        )
        if retryable_codes != declared_retryable:
            raise ValueError("retryable failure-code declarations do not match")

        return cls(
            snapshot_json=snapshot_json,
            contract_version=contract_version,
            challenge_id=challenge_id,
            catalog_status=catalog_status,
            evaluation_identity_sha256=identity_sha256,
            contract_snapshot_sha256=hashlib.sha256(
                snapshot_json.encode("utf-8")
            ).hexdigest(),
            owner_result_fields=tuple(feedback["owner_result_fields"]),
            owner_failure_fields=tuple(feedback["owner_failure_fields"]),
            public_leaderboard_fields=tuple(feedback["public_leaderboard_fields"]),
            idempotency_key_pattern=key_pattern,
            student_prompt_utf8_bytes=int(limits["student_prompt_utf8_bytes"]),
            student_prompt_tokens=int(limits["student_prompt_tokens"]),
            api_request_body_bytes=int(limits["api_request_body_bytes"]),
            max_rendered_input_tokens=int(limits["max_rendered_input_tokens"]),
            model_context_tokens=int(limits["model_context_tokens"]),
            submissions_per_user_per_challenge_per_24h=int(
                limits["submissions_per_user_per_challenge_per_24h"]
            ),
            max_outstanding_submissions_per_user=int(
                limits["max_outstanding_submissions_per_user"]
            ),
            max_running_submissions_per_user=int(
                limits["max_running_submissions_per_user"]
            ),
            global_queue_depth=int(limits["global_queue_depth"]),
            max_attempts=int(job_policy["max_attempts"]),
            job_deadline_seconds=int(job_policy["job_deadline_seconds"]),
            failure_contract_version=str(failure_contract["version"]),
            retryable_failure_codes=retryable_codes,
        )

    @classmethod
    def from_path(cls, path: Path) -> EvaluationContract:
        return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")))

    @property
    def evaluation_identity(self) -> dict[str, Any]:
        return _mapping(
            json.loads(self.snapshot_json)["evaluation_identity"],
            "evaluation_identity",
        )

    @property
    def uses_mock_runtime(self) -> bool:
        model_identity = _mapping(
            self.evaluation_identity.get("model_identity"),
            "evaluation_identity.model_identity",
        )
        return model_identity.get("runtime") == "mock"

    @property
    def external_activation_ready(self) -> bool:
        config = _mapping(json.loads(self.snapshot_json), "contract")
        catalog = _mapping(config.get("catalog"), "catalog")
        access_policy = _mapping(config.get("access_policy"), "access_policy")
        if access_policy.get("external_activation_requirements") != [
            "recorded_source_release_or_commit",
            "recorded_source_file_sha256s",
            "recorded_attribution_requirements",
            "reviewed_share_alike_requirements",
            "reviewed_underlying_text_rights",
        ]:
            return False
        if catalog.get("status") != "active":
            return False
        source_release = catalog.get("source_release")
        source_commit = catalog.get("source_commit")
        recorded_source = any(
            isinstance(value, str)
            and value.strip() not in {"", "unrecorded", "review_required"}
            for value in (source_release, source_commit)
        )
        if not recorded_source:
            return False
        source_hashes = catalog.get("source_file_sha256s")
        if not isinstance(source_hashes, list) or not source_hashes:
            return False
        paths: set[str] = set()
        for record in source_hashes:
            if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
                return False
            path = record["path"]
            sha256 = record["sha256"]
            if (
                not isinstance(path, str)
                or not path
                or path.strip() != path
                or "\\" in path
            ):
                return False
            pure_path = PurePosixPath(path)
            if (
                pure_path.is_absolute()
                or not pure_path.parts
                or ".." in pure_path.parts
                or ":" in pure_path.parts[0]
                or path in paths
            ):
                return False
            if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
                return False
            paths.add(path)

        unresolved_values = {"", "unrecorded", "review_required"}
        attribution = catalog.get("attribution_requirements")
        if not isinstance(attribution, str) or attribution.strip() in unresolved_values:
            return False
        share_alike = catalog.get("share_alike_requirements")
        if not isinstance(share_alike, str) or share_alike.strip() in unresolved_values:
            return False
        rights = catalog.get("underlying_text_rights")
        return isinstance(rights, str) and rights.strip() not in unresolved_values

    def idempotency_key_is_valid(self, value: str) -> bool:
        return re.fullmatch(self.idempotency_key_pattern, value, flags=re.ASCII) is not None

    def owner_result(
        self,
        aggregate: ChallengeAggregateResult,
        *,
        student_prompt_sha256: str,
    ) -> dict[str, Any]:
        identity = self.evaluation_identity
        report = aggregate.to_dict()
        report.update(
            {
                "generation_settings": identity["generation_settings"],
                "model_identity": identity["model_identity"],
                "prompt_envelope_version": identity["prompt_envelope_version"],
                "student_prompt_sha256": student_prompt_sha256,
            }
        )
        missing = set(self.owner_result_fields) - set(report)
        if missing:
            raise RuntimeError(f"owner result is missing contract fields: {sorted(missing)}")
        return {field: report[field] for field in self.owner_result_fields}


def load_mvp_contract(root: Path) -> EvaluationContract:
    return EvaluationContract.from_path(root / "config" / "mvp_evaluation.json")
