import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from linguistic_oj.model_inputs import SegmentationModelInput, TaggingModelInput
from linguistic_oj.providers import (
    PROMPT_ENVELOPE_VERSION,
    GenerationSettings,
    ModelIdentity,
    ModelRequest,
    OpenAICompatibleProvider,
    PromptEnvelope,
    ProviderContractError,
    ProviderTransportError,
)
from linguistic_oj.responses import TaskType

REVISION = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"


class _FakeModelHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_payload: object = {
        "choices": [{"message": {"content": '{"tokens":["A","B"]}'}}]
    }
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(content_length))
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": payload,
            }
        )
        response_body = json.dumps(self.response_payload).encode("utf-8")
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.fixture
def fake_model_service() -> Iterator[str]:
    _FakeModelHandler.response_status = 200
    _FakeModelHandler.response_payload = {
        "choices": [{"message": {"content": '{"tokens":["A","B"]}'}}]
    }
    _FakeModelHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _identity() -> ModelIdentity:
    return ModelIdentity(
        model="Qwen/Qwen3.5-4B",
        revision=REVISION,
        runtime="vllm",
        runtime_version="0.27.1",
    )


def _request() -> ModelRequest:
    return ModelRequest(
        task=TaskType.SEGMENTATION,
        language="Test",
        treebank="Tiny",
        student_prompt="Segment carefully.",
        model_input=SegmentationModelInput(text="AB"),
    )


def test_prompt_envelope_is_versioned_gold_free_and_deterministic() -> None:
    tainted = TaggingModelInput(tokens=("A", "B")).model_copy(
        update={"answers": {"upos": ["NOUN", "VERB"]}, "sample_id": "secret"}
    )
    request = ModelRequest(
        task=TaskType.UPOS,
        language="Test",
        treebank="Tiny",
        student_prompt="Tag the tokens.",
        model_input=tainted,
    )

    envelope = PromptEnvelope.from_request(request)
    first = envelope.to_messages()
    second = envelope.to_messages()
    payload = json.loads(first[1]["content"])

    assert first == second
    assert payload["envelope_version"] == PROMPT_ENVELOPE_VERSION
    assert payload["input"] == {"tokens": ["A", "B"]}
    assert payload["required_output_schema"]["additionalProperties"] is False
    assert "answers" not in first[1]["content"]
    assert "sample_id" not in first[1]["content"]
    assert "secret" not in first[1]["content"]


def test_model_identity_requires_a_pinned_revision() -> None:
    with pytest.raises(ValueError, match="commit SHA"):
        ModelIdentity(
            model="Qwen/Qwen3.5-4B",
            revision="main",
            runtime="vllm",
            runtime_version="0.27.1",
        )


@pytest.mark.parametrize(
    "settings",
    [
        GenerationSettings(),
        GenerationSettings(temperature=0.2, top_p=0.9, max_tokens=64, seed=0),
    ],
)
def test_generation_settings_are_serializable(settings: GenerationSettings) -> None:
    assert json.loads(json.dumps(settings.to_dict())) == settings.to_dict()


def test_openai_provider_sends_fixed_contract_and_returns_raw_content(
    fake_model_service: str,
) -> None:
    settings = GenerationSettings(max_tokens=256)
    provider = OpenAICompatibleProvider(
        base_url=fake_model_service,
        identity=_identity(),
        settings=settings,
        timeout_seconds=5,
        api_key="test-key",
    )

    generation = provider.generate(_request())

    recorded = _FakeModelHandler.requests[0]
    payload = recorded["payload"]
    assert generation.raw_text == '{"tokens":["A","B"]}'
    assert recorded["path"] == "/v1/chat/completions"
    assert recorded["authorization"] == "Bearer test-key"
    assert payload["model"] == "Qwen/Qwen3.5-4B"
    assert payload["temperature"] == 0.0
    assert payload["top_p"] == 1.0
    assert payload["max_tokens"] == 256
    assert payload["seed"] == 2026
    assert payload["stream"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "response_format" not in payload


def test_openai_provider_rejects_malformed_service_payload(fake_model_service: str) -> None:
    _FakeModelHandler.response_payload = {"choices": []}
    provider = OpenAICompatibleProvider(base_url=fake_model_service, identity=_identity())

    with pytest.raises(ProviderContractError, match="message.content"):
        provider.generate(_request())


def test_openai_provider_sanitizes_http_failure(fake_model_service: str) -> None:
    _FakeModelHandler.response_status = 503
    _FakeModelHandler.response_payload = {"private": "service details"}
    provider = OpenAICompatibleProvider(base_url=fake_model_service, identity=_identity())

    with pytest.raises(ProviderTransportError, match="HTTP 503") as error:
        provider.generate(_request())

    assert "service details" not in str(error.value)
