"""Pinned Qwen tokenizer preflight and fail-closed runtime attestation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from .mvp_contract import EvaluationContract
from .providers import (
    ModelIdentity,
    ModelRequest,
    OpenAICompatibleProvider,
    PromptEnvelope,
    ProviderContractError,
    ProviderTimeoutError,
    ProviderTransportError,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_COUNTING_METHOD = "hf-apply-chat-template-tokenize-v1"
_LOCALHOSTS = {"127.0.0.1", "::1", "localhost"}
_RUNTIME_CONSTRUCTION_TOKEN = object()
QWEN_EVALUATION_CONTRACT_VERSION = "mvp-evaluation-v2"


class QwenRuntimeAttestationError(RuntimeError):
    """Raised before queue consumption when the actual runtime does not match."""


class QwenTokenLimitExceeded(ValueError):
    """Raised before provider calls when pinned-tokenizer limits are exceeded."""


@runtime_checkable
class ChatTokenizer(Protocol):
    chat_template: str

    def encode(self, text: str, *, add_special_tokens: bool) -> Sequence[int]: ...

    def apply_chat_template(
        self,
        conversation: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ) -> Sequence[int]: ...


def _required_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_sha256(mapping: Mapping[str, object], key: str) -> str:
    value = _required_text(mapping, key)
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{key} must be a lowercase SHA-256 value")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class TokenizerIdentity:
    repository: str
    revision: str
    tokenizer_config_sha256: str
    tokenizer_json_sha256: str
    chat_template_sha256: str
    counting_method: str
    add_generation_prompt: bool
    enable_thinking: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TokenizerIdentity:
        revision = _required_text(value, "revision")
        if _REVISION.fullmatch(revision) is None:
            raise ValueError("tokenizer revision must be a lowercase commit SHA")
        counting_method = _required_text(value, "counting_method")
        if counting_method != _COUNTING_METHOD:
            raise ValueError("unsupported tokenizer counting method")
        add_generation_prompt = value.get("add_generation_prompt")
        enable_thinking = value.get("enable_thinking")
        if type(add_generation_prompt) is not bool or type(enable_thinking) is not bool:
            raise ValueError("tokenizer template controls must be boolean")
        return cls(
            repository=_required_text(value, "repository"),
            revision=revision,
            tokenizer_config_sha256=_required_sha256(value, "tokenizer_config_sha256"),
            tokenizer_json_sha256=_required_sha256(value, "tokenizer_json_sha256"),
            chat_template_sha256=_required_sha256(value, "chat_template_sha256"),
            counting_method=counting_method,
            add_generation_prompt=add_generation_prompt,
            enable_thinking=enable_thinking,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot_path: Path,
        *,
        repository: str,
        revision: str,
        add_generation_prompt: bool = True,
        enable_thinking: bool = False,
    ) -> TokenizerIdentity:
        if not isinstance(snapshot_path, Path):
            raise TypeError("snapshot_path must be a Path")
        config_path = snapshot_path / "tokenizer_config.json"
        tokenizer_path = snapshot_path / "tokenizer.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise QwenRuntimeAttestationError(
                "tokenizer_config.json is missing or invalid"
            ) from error
        chat_template = config.get("chat_template") if isinstance(config, dict) else None
        if not isinstance(chat_template, str) or not chat_template:
            raise QwenRuntimeAttestationError("tokenizer chat template is missing")
        try:
            tokenizer_json_sha256 = _sha256_file(tokenizer_path)
            tokenizer_config_sha256 = _sha256_file(config_path)
        except OSError as error:
            raise QwenRuntimeAttestationError("tokenizer artifacts are missing") from error
        return cls.from_mapping(
            {
                "repository": repository,
                "revision": revision,
                "tokenizer_config_sha256": tokenizer_config_sha256,
                "tokenizer_json_sha256": tokenizer_json_sha256,
                "chat_template_sha256": hashlib.sha256(
                    chat_template.encode("utf-8")
                ).hexdigest(),
                "counting_method": _COUNTING_METHOD,
                "add_generation_prompt": add_generation_prompt,
                "enable_thinking": enable_thinking,
            }
        )

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "repository": self.repository,
            "revision": self.revision,
            "tokenizer_config_sha256": self.tokenizer_config_sha256,
            "tokenizer_json_sha256": self.tokenizer_json_sha256,
            "chat_template_sha256": self.chat_template_sha256,
            "counting_method": self.counting_method,
            "add_generation_prompt": self.add_generation_prompt,
            "enable_thinking": self.enable_thinking,
        }


@dataclass(frozen=True, slots=True)
class QwenRuntimeAttestation:
    model_identity: ModelIdentity
    tokenizer_identity: TokenizerIdentity
    max_model_len: int
    max_num_seqs: int
    language_model_only: bool

    def __post_init__(self) -> None:
        if not isinstance(self.model_identity, ModelIdentity):
            raise TypeError("model_identity must be a ModelIdentity")
        if not isinstance(self.tokenizer_identity, TokenizerIdentity):
            raise TypeError("tokenizer_identity must be a TokenizerIdentity")
        if type(self.max_model_len) is not int or self.max_model_len <= 0:
            raise ValueError("max_model_len must be a positive integer")
        if type(self.max_num_seqs) is not int or self.max_num_seqs <= 0:
            raise ValueError("max_num_seqs must be a positive integer")
        if type(self.language_model_only) is not bool:
            raise ValueError("language_model_only must be boolean")


@dataclass(frozen=True, slots=True)
class QwenLaunchEvidence:
    """Local launcher evidence read by the worker, never supplied as constructor claims."""

    model_snapshot_path: Path
    runtime_version: str
    max_model_len: int
    max_num_seqs: int
    language_model_only: bool

    @classmethod
    def from_path(cls, path: Path) -> QwenLaunchEvidence:
        if not isinstance(path, Path):
            raise TypeError("launch evidence path must be a Path")
        try:
            metadata = path.lstat()
        except OSError as error:
            raise QwenRuntimeAttestationError("launch evidence is missing or invalid") from error
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise QwenRuntimeAttestationError("launch evidence must be a regular file")
        if os.name == "posix" and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise QwenRuntimeAttestationError("launch evidence must not be shared-writable")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise QwenRuntimeAttestationError("launch evidence is missing or invalid") from error
        if not isinstance(value, dict) or set(value) != {
            "language_model_only",
            "max_model_len",
            "max_num_seqs",
            "model_snapshot_path",
            "runtime_version",
            "schema_version",
        }:
            raise QwenRuntimeAttestationError("launch evidence has an invalid schema")
        if value["schema_version"] != "linguistic-oj-vllm-launch-v1":
            raise QwenRuntimeAttestationError("launch evidence has an unsupported schema")
        model_snapshot_path = value["model_snapshot_path"]
        runtime_version = value["runtime_version"]
        max_model_len = value["max_model_len"]
        max_num_seqs = value["max_num_seqs"]
        language_model_only = value["language_model_only"]
        if not isinstance(model_snapshot_path, str) or not model_snapshot_path:
            raise QwenRuntimeAttestationError("launch evidence has no model snapshot path")
        if not Path(model_snapshot_path).is_absolute():
            raise QwenRuntimeAttestationError("launch evidence snapshot path must be absolute")
        if not isinstance(runtime_version, str) or not runtime_version:
            raise QwenRuntimeAttestationError("launch evidence has no runtime version")
        if type(max_model_len) is not int or max_model_len <= 0:
            raise QwenRuntimeAttestationError("launch evidence has an invalid context length")
        if type(max_num_seqs) is not int or max_num_seqs <= 0:
            raise QwenRuntimeAttestationError("launch evidence has an invalid concurrency")
        if type(language_model_only) is not bool:
            raise QwenRuntimeAttestationError("launch evidence has an invalid model mode")
        return cls(
            model_snapshot_path=Path(model_snapshot_path).resolve(),
            runtime_version=runtime_version,
            max_model_len=max_model_len,
            max_num_seqs=max_num_seqs,
            language_model_only=language_model_only,
        )


class AttestedQwenRuntime:
    """Trusted startup result required by QwenSubmissionWorker."""

    __slots__ = ("attestation", "tokenizer", "tokenizer_identity")

    def __init__(
        self,
        *,
        tokenizer: ChatTokenizer,
        tokenizer_identity: TokenizerIdentity,
        attestation: QwenRuntimeAttestation,
        _construction_token: object,
    ) -> None:
        if _construction_token is not _RUNTIME_CONSTRUCTION_TOKEN:
            raise TypeError("use attest_qwen_runtime_from_snapshot to create a Qwen runtime")
        self.tokenizer = tokenizer
        self.tokenizer_identity = tokenizer_identity
        self.attestation = attestation


def load_huggingface_tokenizer(snapshot_path: Path) -> ChatTokenizer:
    """Load the production tokenizer from a local snapshot without network access."""

    if not isinstance(snapshot_path, Path):
        raise TypeError("snapshot_path must be a Path")
    try:
        from transformers import AutoTokenizer
    except ImportError as error:
        raise QwenRuntimeAttestationError(
            "transformers is required by the Qwen worker runtime"
        ) from error
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            snapshot_path,
            local_files_only=True,
            trust_remote_code=False,
        )
    except Exception as error:
        raise QwenRuntimeAttestationError("pinned tokenizer could not be loaded") from error
    if not isinstance(tokenizer, ChatTokenizer):
        raise QwenRuntimeAttestationError("loaded tokenizer lacks the required chat API")
    return tokenizer


def _attest_qwen_runtime_from_local(
    contract: EvaluationContract,
    provider: OpenAICompatibleProvider,
    *,
    tokenizer: ChatTokenizer,
    tokenizer_snapshot_path: Path,
    launch_evidence_path: Path,
) -> AttestedQwenRuntime:
    """Derive startup evidence from local artifacts, launch evidence, and vLLM."""

    if not isinstance(provider, OpenAICompatibleProvider):
        raise TypeError("Qwen runtime requires OpenAICompatibleProvider")
    if contract.contract_version != QWEN_EVALUATION_CONTRACT_VERSION:
        raise QwenRuntimeAttestationError("Qwen worker requires mvp-evaluation-v2")
    host = urlsplit(provider.base_url).hostname
    if host is None or host.lower() not in _LOCALHOSTS:
        raise QwenRuntimeAttestationError("Qwen provider must be co-located on localhost")
    expected_model = contract.evaluation_identity.get("model_identity")
    if not isinstance(expected_model, dict):
        raise QwenRuntimeAttestationError("contract model identity is invalid")
    try:
        model_identity = ModelIdentity(**expected_model)
    except (TypeError, ValueError) as error:
        raise QwenRuntimeAttestationError("contract model identity is invalid") from error
    snapshot_path = tokenizer_snapshot_path.resolve()
    expected_tokenizer = _contract_tokenizer_identity(contract)
    actual_tokenizer = TokenizerIdentity.from_snapshot(
        snapshot_path,
        repository=expected_tokenizer.repository,
        revision=expected_tokenizer.revision,
        add_generation_prompt=expected_tokenizer.add_generation_prompt,
        enable_thinking=expected_tokenizer.enable_thinking,
    )
    if actual_tokenizer != expected_tokenizer:
        raise QwenRuntimeAttestationError("local tokenizer artifacts do not match the contract")
    if not expected_tokenizer.add_generation_prompt:
        raise QwenRuntimeAttestationError(
            "Qwen runtime requires add_generation_prompt to match vLLM requests"
        )
    if hashlib.sha256(tokenizer.chat_template.encode("utf-8")).hexdigest() != (
        expected_tokenizer.chat_template_sha256
    ):
        raise QwenRuntimeAttestationError("loaded tokenizer template does not match the snapshot")
    launch = QwenLaunchEvidence.from_path(launch_evidence_path)
    if launch.model_snapshot_path != snapshot_path:
        raise QwenRuntimeAttestationError("launch evidence uses a different model snapshot")
    attestation = QwenRuntimeAttestation(
        model_identity=model_identity,
        tokenizer_identity=actual_tokenizer,
        max_model_len=launch.max_model_len,
        max_num_seqs=launch.max_num_seqs,
        language_model_only=launch.language_model_only,
    )
    verify_qwen_runtime(contract, provider, attestation)
    if launch.runtime_version != model_identity.runtime_version:
        raise QwenRuntimeAttestationError("launch runtime version does not match the contract")
    try:
        served_models = provider.served_model_ids()
    except (ProviderContractError, ProviderTimeoutError, ProviderTransportError) as error:
        raise QwenRuntimeAttestationError("could not attest the local vLLM service") from error
    if model_identity.model not in served_models:
        raise QwenRuntimeAttestationError("local vLLM service does not serve the pinned model")
    return AttestedQwenRuntime(
        tokenizer=tokenizer,
        tokenizer_identity=actual_tokenizer,
        attestation=attestation,
        _construction_token=_RUNTIME_CONSTRUCTION_TOKEN,
    )


def attest_qwen_runtime_from_snapshot(
    contract: EvaluationContract,
    provider: OpenAICompatibleProvider,
    *,
    tokenizer_snapshot_path: Path,
    launch_evidence_path: Path,
) -> AttestedQwenRuntime:
    return _attest_qwen_runtime_from_local(
        contract,
        provider,
        tokenizer=load_huggingface_tokenizer(tokenizer_snapshot_path),
        tokenizer_snapshot_path=tokenizer_snapshot_path,
        launch_evidence_path=launch_evidence_path,
    )


class QwenTokenizerPreflight:
    """Count every exact rendered request before the first provider call."""

    def __init__(
        self,
        contract: EvaluationContract,
        tokenizer: ChatTokenizer,
        tokenizer_identity: TokenizerIdentity,
    ) -> None:
        if contract.uses_mock_runtime:
            raise ValueError("Qwen preflight requires a non-Mock contract")
        if not isinstance(tokenizer, ChatTokenizer):
            raise TypeError("tokenizer must implement ChatTokenizer")
        expected = _contract_tokenizer_identity(contract)
        if tokenizer_identity != expected:
            raise QwenRuntimeAttestationError("tokenizer artifacts do not match the contract")
        actual_template_sha256 = hashlib.sha256(
            tokenizer.chat_template.encode("utf-8")
        ).hexdigest()
        if actual_template_sha256 != expected.chat_template_sha256:
            raise QwenRuntimeAttestationError("loaded chat template does not match the contract")
        self._contract = contract
        self._tokenizer = tokenizer
        self._identity = expected

    def __call__(self, requests: tuple[ModelRequest, ...]) -> None:
        if not requests:
            raise ValueError("challenge must contain at least one request")
        generation_settings = self._contract.evaluation_identity.get("generation_settings")
        if not isinstance(generation_settings, dict):
            raise QwenRuntimeAttestationError("generation settings are missing")
        generation_tokens = generation_settings.get("max_tokens")
        if type(generation_tokens) is not int or generation_tokens <= 0:
            raise QwenRuntimeAttestationError("generation token limit is invalid")
        if generation_settings.get("enable_thinking") != self._identity.enable_thinking:
            raise QwenRuntimeAttestationError(
                "generation thinking mode does not match the tokenizer contract"
            )

        for request in requests:
            prompt_tokens = self._tokenizer.encode(
                request.student_prompt,
                add_special_tokens=False,
            )
            if _token_count(prompt_tokens) > self._contract.student_prompt_tokens:
                raise QwenTokenLimitExceeded("student prompt exceeds the Qwen token limit")
            rendered_tokens = self._tokenizer.apply_chat_template(
                list(PromptEnvelope.from_request(request).to_messages()),
                tokenize=True,
                add_generation_prompt=self._identity.add_generation_prompt,
                enable_thinking=self._identity.enable_thinking,
            )
            rendered_count = _token_count(rendered_tokens)
            if rendered_count > self._contract.max_rendered_input_tokens:
                raise QwenTokenLimitExceeded("rendered Qwen input exceeds the token limit")
            if rendered_count + generation_tokens > self._contract.model_context_tokens:
                raise QwenTokenLimitExceeded("rendered Qwen input exceeds the context window")


def verify_qwen_runtime(
    contract: EvaluationContract,
    provider: OpenAICompatibleProvider,
    attestation: QwenRuntimeAttestation,
) -> None:
    """Verify trusted startup evidence before a Qwen worker consumes any job."""

    if contract.uses_mock_runtime:
        raise QwenRuntimeAttestationError("Qwen worker cannot use a Mock contract")
    if not isinstance(provider, OpenAICompatibleProvider):
        raise TypeError("Qwen runtime requires OpenAICompatibleProvider")
    if not isinstance(attestation, QwenRuntimeAttestation):
        raise TypeError("attestation must be a QwenRuntimeAttestation")
    identity = contract.evaluation_identity
    if provider.identity.to_dict() != identity.get("model_identity"):
        raise QwenRuntimeAttestationError("provider model identity does not match the contract")
    if provider.settings.to_dict() != identity.get("generation_settings"):
        raise QwenRuntimeAttestationError("provider generation settings do not match the contract")
    if attestation.model_identity != provider.identity:
        raise QwenRuntimeAttestationError("running model identity does not match the provider")
    if attestation.tokenizer_identity != _contract_tokenizer_identity(contract):
        raise QwenRuntimeAttestationError("running tokenizer identity does not match the contract")
    if attestation.max_model_len != contract.model_context_tokens:
        raise QwenRuntimeAttestationError("runtime context length does not match the contract")
    if attestation.max_num_seqs != contract.worker_model_concurrency:
        raise QwenRuntimeAttestationError("runtime model concurrency does not match the contract")
    if not attestation.language_model_only:
        raise QwenRuntimeAttestationError("runtime must use language-model-only mode")
    if provider.timeout_seconds != contract.provider_request_timeout_seconds:
        raise QwenRuntimeAttestationError("provider timeout does not match the contract")
    if provider.max_response_body_bytes != contract.provider_response_body_bytes:
        raise QwenRuntimeAttestationError("provider response limit does not match the contract")


def _contract_tokenizer_identity(contract: EvaluationContract) -> TokenizerIdentity:
    value = contract.evaluation_identity.get("tokenizer_identity")
    if not isinstance(value, dict):
        raise QwenRuntimeAttestationError("contract has no pinned tokenizer identity")
    try:
        return TokenizerIdentity.from_mapping(value)
    except ValueError as error:
        raise QwenRuntimeAttestationError("contract tokenizer identity is invalid") from error


def _token_count(value: Sequence[int] | Mapping[str, object]) -> int:
    if isinstance(value, Mapping):
        if "input_ids" not in value:
            raise QwenRuntimeAttestationError("tokenizer returned an invalid token sequence")
        value = value["input_ids"]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise QwenRuntimeAttestationError("tokenizer returned an invalid token sequence")
    if any(type(token) is not int or token < 0 for token in value):
        raise QwenRuntimeAttestationError("tokenizer returned an invalid token sequence")
    return len(value)
