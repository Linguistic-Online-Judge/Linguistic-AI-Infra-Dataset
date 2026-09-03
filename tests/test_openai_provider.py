import json
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import linguistic_oj.providers as providers_module
from linguistic_oj.model_inputs import (
    DependencyModelInput,
    DependencyTokenInput,
    SegmentationModelInput,
    TaggingModelInput,
)
from linguistic_oj.providers import (
    PROMPT_ENVELOPE_VERSION,
    GenerationSettings,
    ModelIdentity,
    ModelRequest,
    OpenAICompatibleProvider,
    PromptEnvelope,
    ProviderContractError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from linguistic_oj.responses import TaskType

REVISION = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"


class _FakeModelHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_payload: object = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"tokens":["A","B"]}'},
            }
        ],
        "usage": {"completion_tokens": 7},
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

    def do_GET(self) -> None:  # noqa: N802
        response_body = json.dumps({"data": [{"id": "Qwen/Qwen3.5-4B"}]}).encode(
            "utf-8"
        )
        self.send_response(200)
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
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"tokens":["A","B"]}'},
            }
        ],
        "usage": {"completion_tokens": 7},
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


def _tagging_request() -> ModelRequest:
    return ModelRequest(
        task=TaskType.XPOS,
        language="German",
        treebank="HDT",
        student_prompt="Tag every token.",
        model_input=TaggingModelInput(tokens=("A", "B")),
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
    assert generation.generated_token_count == 7
    assert generation.finish_reason == "stop"
    assert recorded["path"] == "/v1/chat/completions"
    assert recorded["authorization"] == "Bearer test-key"
    assert payload["model"] == "Qwen/Qwen3.5-4B"
    assert payload["temperature"] == 0.0
    assert payload["top_p"] == 1.0
    assert payload["max_tokens"] == 256
    assert payload["seed"] == 2026
    assert payload["stream"] is False
    assert payload["add_generation_prompt"] is True
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "response_format" not in payload
    assert "structured_outputs" not in payload


def test_openai_provider_allows_missing_generation_metadata(
    fake_model_service: str,
) -> None:
    _FakeModelHandler.response_payload = {
        "choices": [{"message": {"content": '{"tokens":["A","B"]}'}}]
    }
    provider = OpenAICompatibleProvider(base_url=fake_model_service, identity=_identity())

    generation = provider.generate(_request())

    assert generation.generated_token_count is None
    assert generation.finish_reason is None


def test_openai_provider_can_request_exact_xpos_regex(
    fake_model_service: str,
) -> None:
    _FakeModelHandler.response_payload = {
        "choices": [{"message": {"content": '{"tags":["NN","PROAV"]}'}}]
    }
    provider = OpenAICompatibleProvider(
        base_url=fake_model_service,
        identity=_identity(),
        structured_json=True,
    )

    provider.generate(_tagging_request())

    structured_outputs = _FakeModelHandler.requests[0]["payload"]["structured_outputs"]
    pattern = structured_outputs["regex"]
    assert re.fullmatch(pattern, '{"tags":["NN","PROAV"]}') is not None
    assert re.fullmatch(pattern, '{"tags":["NN"]}') is None
    assert re.fullmatch(pattern, '{"tags":["NN","PAV"]}') is None
    assert re.fullmatch(pattern, '{ "tags": ["NN","PROAV"] }') is None


def test_openai_provider_rejects_structured_response_outside_xpos_inventory(
    fake_model_service: str,
) -> None:
    _FakeModelHandler.response_payload = {
        "choices": [{"message": {"content": '{"tags":["NN","PAV"]}'}}]
    }
    provider = OpenAICompatibleProvider(
        base_url=fake_model_service,
        identity=_identity(),
        structured_json=True,
    )

    with pytest.raises(ProviderContractError, match="structured output constraint"):
        provider.generate(_tagging_request())


def test_openai_provider_rejects_xpos_response_outside_exact_regex(
    fake_model_service: str,
) -> None:
    _FakeModelHandler.response_payload = {
        "choices": [{"message": {"content": '{ "tags": ["NN","PROAV"] }'}}]
    }
    provider = OpenAICompatibleProvider(
        base_url=fake_model_service,
        identity=_identity(),
        structured_json=True,
    )

    with pytest.raises(ProviderContractError, match="structured output constraint"):
        provider.generate(_tagging_request())


def test_openai_provider_structured_schema_does_not_apply_scorer_semantics(
    fake_model_service: str,
) -> None:
    _FakeModelHandler.response_payload = {
        "choices": [{"message": {"content": '{ "tags": ["INVALID","NOUN"] }'}}]
    }
    provider = OpenAICompatibleProvider(
        base_url=fake_model_service,
        identity=_identity(),
        structured_json=True,
    )
    request = ModelRequest(
        task=TaskType.UPOS,
        language="Test",
        treebank="Tiny",
        student_prompt="Tag every token.",
        model_input=TaggingModelInput(tokens=("A", "B")),
    )

    generation = provider.generate(request)

    assert generation.raw_text == '{ "tags": ["INVALID","NOUN"] }'
    structured_outputs = _FakeModelHandler.requests[0]["payload"]["structured_outputs"]
    assert "whitespace_pattern" not in structured_outputs


def test_openai_provider_structured_dependency_schema_does_not_apply_graph_checks(
    fake_model_service: str,
) -> None:
    content = (
        '{"arcs":[{"token_id":1.0,"head_id":0,"deprel":"root"},'
        '{"token_id":1,"head_id":9,"deprel":"dep"}]}'
    )
    _FakeModelHandler.response_payload = {
        "choices": [{"message": {"content": content}}]
    }
    provider = OpenAICompatibleProvider(
        base_url=fake_model_service,
        identity=_identity(),
        structured_json=True,
    )
    request = ModelRequest(
        task=TaskType.DEPENDENCY,
        language="Test",
        treebank="Tiny",
        student_prompt="Parse every token.",
        model_input=DependencyModelInput(
            tokens=(
                DependencyTokenInput(token_id=1, form="A"),
                DependencyTokenInput(token_id=2, form="B"),
            )
        ),
    )

    assert provider.generate(request).raw_text == content


def test_xpos_inventory_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        providers_module._XPOS_TAG_INVENTORIES[("German", "HDT")] = ("PAV",)


def test_openai_provider_requires_boolean_structured_json() -> None:
    with pytest.raises(TypeError, match="structured_json"):
        OpenAICompatibleProvider(
            base_url="http://127.0.0.1:8000/v1",
            identity=_identity(),
            structured_json=1,  # type: ignore[arg-type]
        )


def test_openai_provider_structured_mode_is_read_only() -> None:
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        identity=_identity(),
    )

    with pytest.raises(AttributeError):
        provider.structured_json = True  # type: ignore[misc]
    with pytest.raises(AttributeError, match="configuration is frozen"):
        provider._structured_json = True
    with pytest.raises(AttributeError, match="configuration is frozen"):
        del provider._structured_json
    with pytest.raises(AttributeError, match="configuration is frozen"):
        del provider._config_frozen
    vars(provider)["_structured_json"] = True
    vars(provider)["_config_frozen"] = False
    assert provider.structured_json is False
    with pytest.raises(AttributeError, match="configuration is frozen"):
        provider._structured_json = True


def test_openai_provider_normalizes_unknown_finish_reason(
    fake_model_service: str,
) -> None:
    _FakeModelHandler.response_payload = {
        "choices": [
            {
                "finish_reason": "vendor_specific",
                "message": {"content": '{"tokens":["A","B"]}'},
            }
        ]
    }
    provider = OpenAICompatibleProvider(base_url=fake_model_service, identity=_identity())

    assert provider.generate(_request()).finish_reason == "other"


@pytest.mark.parametrize(
    "response_payload",
    [
        {
            "choices": [
                {
                    "finish_reason": 1,
                    "message": {"content": '{"tokens":["A","B"]}'},
                }
            ]
        },
        {
            "choices": [{"message": {"content": '{"tokens":["A","B"]}'}}],
            "usage": {"completion_tokens": -1},
        },
    ],
)
def test_openai_provider_ignores_invalid_generation_metadata(
    fake_model_service: str,
    response_payload: object,
) -> None:
    _FakeModelHandler.response_payload = response_payload
    provider = OpenAICompatibleProvider(base_url=fake_model_service, identity=_identity())

    generation = provider.generate(_request())

    assert generation.raw_text == '{"tokens":["A","B"]}'
    assert generation.generated_token_count is None
    assert generation.finish_reason is None


def test_openai_provider_reads_served_model_ids(fake_model_service: str) -> None:
    provider = OpenAICompatibleProvider(base_url=fake_model_service, identity=_identity())

    assert provider.served_model_ids() == frozenset({"Qwen/Qwen3.5-4B"})


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
    assert error.value.termination_confirmed is True


def test_openai_provider_enforces_response_limit_before_json_decode(
    fake_model_service: str,
) -> None:
    _FakeModelHandler.response_payload = {
        "choices": [{"message": {"content": "x" * 100}}]
    }
    provider = OpenAICompatibleProvider(
        base_url=fake_model_service,
        identity=_identity(),
        max_response_body_bytes=32,
    )

    with pytest.raises(ProviderContractError, match="too large"):
        provider.generate(_request())


def test_openai_provider_enforces_streaming_response_limit_without_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit: int) -> bytes:
            return b"x" * limit

    monkeypatch.setattr(providers_module, "urlopen", lambda *args, **kwargs: _Response())
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        identity=_identity(),
        max_response_body_bytes=32,
    )

    with pytest.raises(ProviderContractError, match="too large"):
        provider.generate(_request())


def test_openai_provider_clamps_timeout_to_remaining_job_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeout = None

    class _Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit: int) -> bytes:
            return b'{"choices":[{"message":{"content":"{\\"tokens\\":[\\"A\\",\\"B\\"]}"}}]}'

    def fake_urlopen(request, *, timeout):
        nonlocal observed_timeout
        observed_timeout = timeout
        return _Response()

    monkeypatch.setattr(providers_module, "urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        identity=_identity(),
        timeout_seconds=120,
    )

    provider.generate(_request(), timeout_seconds=7.5)

    assert observed_timeout is not None
    assert 0 < observed_timeout <= 7.5


def test_openai_provider_timeout_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request, *, timeout):
        raise TimeoutError

    monkeypatch.setattr(providers_module, "urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        identity=_identity(),
    )

    with pytest.raises(ProviderTimeoutError) as error:
        provider.generate(_request(), timeout_seconds=1)

    assert error.value.termination_confirmed is False
    assert provider.has_active_request is True
    with pytest.raises(ProviderTransportError, match="prior model request"):
        provider.generate(_request(), timeout_seconds=1)


def test_openai_provider_enforces_absolute_deadline_and_keeps_timeout_poisoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def fake_urlopen(request, *, timeout):
        release.wait()
        raise TimeoutError

    monkeypatch.setattr(providers_module, "urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        identity=_identity(),
    )

    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError, match="absolute request deadline"):
        provider.generate(_request(), timeout_seconds=0.02)
    elapsed = time.monotonic() - started
    with pytest.raises(ProviderTransportError, match="prior model request"):
        provider.generate(_request(), timeout_seconds=1)

    release.set()
    deadline = time.monotonic() + 1
    while provider.has_active_request and time.monotonic() < deadline:
        time.sleep(0.01)
    assert elapsed < 0.2
    assert provider.has_active_request is True


def test_openai_provider_does_not_start_a_request_after_caller_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_targets = []

    class _DeferredThread:
        def __init__(self, *, target, daemon) -> None:
            assert daemon is True
            self._target = target

        def start(self) -> None:
            pending_targets.append(self._target)

    def fail_if_opened(*args, **kwargs):
        raise AssertionError("the deferred request must not reach urlopen")

    monkeypatch.setattr(providers_module, "Thread", _DeferredThread)
    monkeypatch.setattr(providers_module, "urlopen", fail_if_opened)
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        identity=_identity(),
    )

    with pytest.raises(ProviderTimeoutError) as error:
        provider.generate(_request(), timeout_seconds=0.01)

    assert error.value.termination_confirmed is True
    pending_targets.pop()()
    assert provider._active_request is None
