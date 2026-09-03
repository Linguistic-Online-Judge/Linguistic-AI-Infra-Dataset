import hashlib
import json
from pathlib import Path

import pytest

import linguistic_oj.qwen_runtime as qwen_runtime_module
from linguistic_oj.model_inputs import TaggingModelInput
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.providers import (
    GenerationSettings,
    ModelIdentity,
    ModelRequest,
    OpenAICompatibleProvider,
)
from linguistic_oj.qwen_runtime import (
    AttestedQwenRuntime,
    QwenRuntimeAttestationError,
    QwenTokenizerPreflight,
    QwenTokenLimitExceeded,
    TokenizerIdentity,
    attest_qwen_runtime_from_snapshot,
    verify_qwen_runtime,
)
from linguistic_oj.responses import TaskType

ROOT = Path(__file__).parents[1]
REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
TEMPLATE = "<|im_start|>{{ messages }}<|im_end|>"


def _tokenizer_identity() -> TokenizerIdentity:
    return TokenizerIdentity.from_mapping(
        {
            "repository": "Qwen/Qwen3.5-9B",
            "revision": REVISION,
            "tokenizer_config_sha256": "1" * 64,
            "tokenizer_json_sha256": "2" * 64,
            "chat_template_sha256": hashlib.sha256(TEMPLATE.encode()).hexdigest(),
            "counting_method": "hf-apply-chat-template-tokenize-v1",
            "add_generation_prompt": True,
            "enable_thinking": False,
        }
    )


def _contract(*, tokenizer_identity: TokenizerIdentity | None = None) -> EvaluationContract:
    config_name = (
        "mvp_evaluation_v2.json" if tokenizer_identity is not None else "mvp_evaluation.json"
    )
    mapping = json.loads((ROOT / "config" / config_name).read_text(encoding="utf-8"))
    identity = mapping["evaluation_identity"]
    if tokenizer_identity is not None:
        identity["tokenizer_identity"] = tokenizer_identity.to_dict()
    mapping["leaderboard_partition"]["expected_sha256"] = canonical_sha256(identity)
    return EvaluationContract.from_mapping(mapping)


def _request(student_prompt: str = "Return JSON.") -> ModelRequest:
    return ModelRequest(
        task=TaskType.UPOS,
        language="English",
        treebank="EWT",
        student_prompt=student_prompt,
        model_input=TaggingModelInput(tokens=("A", "B")),
    )


class _FakeTokenizer:
    chat_template = TEMPLATE

    def __init__(self, *, prompt_tokens: int = 3, rendered_tokens: int = 100) -> None:
        self.prompt_tokens = prompt_tokens
        self.rendered_tokens = rendered_tokens
        self.rendered_messages = []

    def encode(self, text: str, *, add_special_tokens: bool):
        assert add_special_tokens is False
        return list(range(self.prompt_tokens))

    def apply_chat_template(
        self,
        conversation,
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ):
        self.rendered_messages.append(conversation)
        assert tokenize is True
        assert add_generation_prompt is True
        assert enable_thinking is False
        return list(range(self.rendered_tokens))


class _BatchEncodingTokenizer(_FakeTokenizer):
    def apply_chat_template(
        self,
        conversation,
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ):
        return {
            "input_ids": super().apply_chat_template(
                conversation,
                tokenize=tokenize,
                add_generation_prompt=add_generation_prompt,
                enable_thinking=enable_thinking,
            ),
            "attention_mask": [1] * self.rendered_tokens,
        }


def test_qwen_preflight_counts_every_fully_rendered_request() -> None:
    identity = _tokenizer_identity()
    tokenizer = _FakeTokenizer()
    preflight = QwenTokenizerPreflight(_contract(tokenizer_identity=identity), tokenizer, identity)

    preflight((_request(), _request()))

    assert len(tokenizer.rendered_messages) == 2
    assert all(messages[-1]["role"] == "user" for messages in tokenizer.rendered_messages)


def test_qwen_preflight_accepts_transformers_batch_encoding() -> None:
    identity = _tokenizer_identity()
    tokenizer = _BatchEncodingTokenizer()
    preflight = QwenTokenizerPreflight(_contract(tokenizer_identity=identity), tokenizer, identity)

    preflight((_request(),))


def test_qwen_preflight_rejects_before_provider_for_either_token_limit() -> None:
    identity = _tokenizer_identity()
    contract = _contract(tokenizer_identity=identity)

    with pytest.raises(QwenTokenLimitExceeded, match="student prompt"):
        QwenTokenizerPreflight(
            contract,
            _FakeTokenizer(prompt_tokens=contract.student_prompt_tokens + 1),
            identity,
        )((_request(),))

    with pytest.raises(QwenTokenLimitExceeded, match="rendered"):
        QwenTokenizerPreflight(
            contract,
            _FakeTokenizer(rendered_tokens=contract.max_rendered_input_tokens + 1),
            identity,
        )((_request(),))


def test_qwen_preflight_fails_closed_without_pinned_identity() -> None:
    with pytest.raises(QwenRuntimeAttestationError, match="no pinned tokenizer"):
        QwenTokenizerPreflight(_contract(), _FakeTokenizer(), _tokenizer_identity())


def test_tokenizer_snapshot_attestation_hashes_exact_artifacts(tmp_path: Path) -> None:
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": TEMPLATE}),
        encoding="utf-8",
    )
    (tmp_path / "tokenizer.json").write_bytes(b"pinned tokenizer")

    identity = TokenizerIdentity.from_snapshot(
        tmp_path,
        repository="Qwen/Qwen3.5-9B",
        revision=REVISION,
    )

    assert identity.tokenizer_config_sha256 == hashlib.sha256(
        (tmp_path / "tokenizer_config.json").read_bytes()
    ).hexdigest()
    assert identity.tokenizer_json_sha256 == hashlib.sha256(b"pinned tokenizer").hexdigest()
    assert identity.chat_template_sha256 == hashlib.sha256(TEMPLATE.encode()).hexdigest()


def test_runtime_attestation_derives_identity_from_local_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "qwen9b"
    snapshot_path.mkdir()
    (snapshot_path / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": TEMPLATE}),
        encoding="utf-8",
    )
    (snapshot_path / "tokenizer.json").write_bytes(b"pinned tokenizer")
    tokenizer_identity = TokenizerIdentity.from_snapshot(
        snapshot_path,
        repository="Qwen/Qwen3.5-9B",
        revision=REVISION,
    )
    contract = _contract(tokenizer_identity=tokenizer_identity)
    model_identity = ModelIdentity(
        model="Qwen/Qwen3.5-9B",
        revision=REVISION,
        runtime="vllm",
        runtime_version="0.27.1+cu129",
    )
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        identity=model_identity,
        settings=GenerationSettings(max_tokens=256),
        timeout_seconds=contract.provider_request_timeout_seconds,
        max_response_body_bytes=contract.provider_response_body_bytes,
    )
    launch_evidence_path = tmp_path / "qwen-launch.json"
    launch_evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "linguistic-oj-vllm-launch-v1",
                "model_snapshot_path": str(snapshot_path.resolve()),
                "runtime_version": model_identity.runtime_version,
                "max_model_len": contract.model_context_tokens,
                "max_num_seqs": contract.worker_model_concurrency,
                "language_model_only": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        provider,
        "served_model_ids",
        lambda: frozenset({model_identity.model}),
    )
    monkeypatch.setattr(
        qwen_runtime_module,
        "load_huggingface_tokenizer",
        lambda path: _FakeTokenizer(),
    )

    runtime = attest_qwen_runtime_from_snapshot(
        contract,
        provider,
        tokenizer_snapshot_path=snapshot_path,
        launch_evidence_path=launch_evidence_path,
    )

    assert runtime.attestation.model_identity == model_identity
    assert runtime.tokenizer_identity == tokenizer_identity
    structured_provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        identity=model_identity,
        settings=GenerationSettings(max_tokens=256),
        timeout_seconds=contract.provider_request_timeout_seconds,
        max_response_body_bytes=contract.provider_response_body_bytes,
        structured_json=True,
    )
    with pytest.raises(QwenRuntimeAttestationError, match="not declared"):
        verify_qwen_runtime(contract, structured_provider, runtime.attestation)
    with pytest.raises(TypeError, match="attest_qwen_runtime_from_snapshot"):
        AttestedQwenRuntime(
            tokenizer=_FakeTokenizer(),
            tokenizer_identity=tokenizer_identity,
            attestation=runtime.attestation,
            _construction_token=object(),
        )

    launch_evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "linguistic-oj-vllm-launch-v1",
                "model_snapshot_path": str(snapshot_path.resolve()),
                "runtime_version": model_identity.runtime_version,
                "max_model_len": 8192,
                "max_num_seqs": 1,
                "language_model_only": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(QwenRuntimeAttestationError, match="context length"):
        attest_qwen_runtime_from_snapshot(
            contract,
            provider,
            tokenizer_snapshot_path=snapshot_path,
            launch_evidence_path=launch_evidence_path,
        )
