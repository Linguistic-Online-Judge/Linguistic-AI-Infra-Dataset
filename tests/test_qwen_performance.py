import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest

from linguistic_oj import auth_config
from linguistic_oj import qwen_performance as perf
from linguistic_oj.challenge import build_challenge, write_challenge
from linguistic_oj.executor_state import ExecutorState, RecoveryRequired, executor_lock
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.providers import (
    GenerationSettings,
    ModelGeneration,
    ModelIdentity,
    OpenAICompatibleProvider,
    ProviderTransportError,
)
from linguistic_oj.qwen_runtime import (
    QwenRuntimeAttestation,
    QwenRuntimeAttestationError,
    TokenizerIdentity,
    verify_qwen_runtime,
)

ROOT = Path(__file__).parents[1]


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    if os.name == "nt":
        monkeypatch.setattr(auth_config, "_check_windows_acl", lambda path: None)
    data = tmp_path / "samples.jsonl"
    data.write_text("".join(json.dumps({
        "id": f"private-sample-{i}", "language": "English", "treebank": "Perf",
        "text": "Birds fly .", "tasks_available": ["upos"],
        "answers": {"segmentation": ["Birds", "fly", "."], "upos": ["NOUN", "VERB", "PUNCT"]},
    }) + "\n" for i in range(2)), encoding="utf-8")
    artifacts = build_challenge(data, language="English", treebank="Perf", task="upos",
                                count=2, seed=2026, version="performance-fixture")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    (snapshot / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": "performance-fixture-template"}), encoding="utf-8")
    token_identity = TokenizerIdentity.from_snapshot(
        snapshot, repository="Qwen/Qwen3.5-9B",
        revision="c202236235762e1c871ad0ccb60c8ee5ba337b9a",
    )
    config = json.loads((ROOT / "config/mvp_evaluation_v2.json").read_text(encoding="utf-8"))
    public = artifacts.public.model_dump(mode="json")
    for field in config["catalog"]:
        config["catalog"][field] = public[field]
    config["evaluation_identity"].update(
        challenge_id=artifacts.public.challenge_id, dataset_sha256=artifacts.public.dataset_sha256,
        selection_sha256=artifacts.public.selection_sha256,
        tokenizer_identity=token_identity.to_dict(),
    )
    config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        config["evaluation_identity"])
    contract = EvaluationContract.from_mapping(config)

    class Tokenizer:
        chat_template = "performance-fixture-template"

        def encode(self, text, **kwargs):
            return [1] * 4

        def apply_chat_template(self, messages, **kwargs):
            assert "answers" not in json.dumps(messages)
            return [1] * 32

    tokenizer = Tokenizer()
    monkeypatch.setattr(perf, "load_huggingface_tokenizer", lambda path: tokenizer)
    cases, phases = perf.prepare_cases(artifacts, contract, tokenizer, token_identity,
                                      "Return JSON.", 2)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    with executor_lock(state_dir, create=True):
        ExecutorState.initialize(state_dir, "a" * 64)
    return SimpleNamespace(cases=cases, contract=contract, snapshot=snapshot, state_dir=state_dir,
                           tokenizer=tokenizer, token_identity=token_identity, phases=phases,
                           artifacts=artifacts)


class FakeProvider:
    has_active_request = False

    def generate(self, request):
        return ModelGeneration('{"tags":["NOUN","VERB","PUNCT"]}', 6, "stop", 32)


def test_measurement_contains_lengths_phases_and_scores_but_no_private_text(experiment):
    result = perf.measure_batch(experiment.cases, [FakeProvider()], repetitions=2)
    assert result["planned_requests"] == result["completed_requests"] == 4
    assert result["not_started_requests"] == result["format_invalid_count"] == 0
    assert result["output_tokens_per_batch_second"] > 0
    assert [row["sample_position"] for row in result["rows"]] == [1, 2, 1, 2]
    for row in result["rows"]:
        assert row["input_tokens_local"] == row["input_tokens_reported"] == 32
        assert row["output_tokens"] == 6
        assert row["score_statistics"]["accuracy"] == 1
        assert row["completion_seconds"] >= row["client_queue_wait_seconds"]
    serialized = json.dumps(result)
    for private_text in ("Birds", "private-sample", "Return JSON.", "raw_text", '"tags"'):
        assert private_text not in serialized


def test_missing_usage_is_unknown_instead_of_zero(experiment):
    class Missing(FakeProvider):
        def generate(self, request):
            return ModelGeneration('{"tags":["NOUN","VERB","PUNCT"]}')

    report = perf.measure_batch(experiment.cases, [Missing()])
    assert report["missing_output_token_counts"] == 2
    assert report["output_tokens_per_batch_second"] is None
    assert report["rows"][0]["input_tokens_reported"] is None


def test_ambiguous_failure_stops_assignment_and_preserves_batch_barrier(experiment):
    class Failed(FakeProvider):
        def generate(self, request):
            raise ProviderTransportError("private error details", termination_confirmed=False)

    with executor_lock(experiment.state_dir):
        state = ExecutorState(experiment.state_dir, "a" * 64)
        result = perf.guarded_experiment(state, experiment.cases, [Failed()],
                                         repetitions=3, warmup_requests=0)
        assert result["measurement"]["started_requests"] == 1
        assert result["measurement"]["not_started_requests"] == 5
        assert result["measurement"]["termination_unconfirmed"]
        with pytest.raises(RecoveryRequired):
            state.require_clean()
        assert "private error details" not in json.dumps(result)


def test_successful_warmup_is_separate_and_barrier_clears(experiment):
    with executor_lock(experiment.state_dir):
        state = ExecutorState(experiment.state_dir, "a" * 64)
        result = perf.guarded_experiment(state, experiment.cases, [FakeProvider()],
                                         repetitions=2, warmup_requests=1)
        assert result["warmup"]["planned_requests"] == 1
        assert result["measurement"]["planned_requests"] == 4
        state.require_clean()


def test_stop_event_prevents_new_assignments(experiment):
    stop = Event()

    class StopAfterFirst(FakeProvider):
        def generate(self, request):
            stop.set()
            return super().generate(request)

    result = perf.measure_batch(experiment.cases, [StopAfterFirst()], stop_event=stop)
    assert result["started_requests"] == result["completed_requests"] == 1
    assert result["not_started_requests"] == 1
    assert not result["termination_unconfirmed"]


def test_calibration_capacity_does_not_relax_production_attestation(experiment, tmp_path):
    contract = experiment.contract
    before = contract.snapshot_json
    model = ModelIdentity(**contract.evaluation_identity["model_identity"])
    evidence = tmp_path / "launch.json"
    evidence.write_text(json.dumps({
        "schema_version": "linguistic-oj-vllm-launch-v1",
        "model_snapshot_path": str(experiment.snapshot.resolve()),
        "runtime_version": model.runtime_version, "max_model_len": contract.model_context_tokens,
        "max_num_seqs": 2, "language_model_only": True,
    }), encoding="utf-8")
    evidence.chmod(0o600)
    _, _, token_identity, launch = perf.verify_experiment_runtime(
        contract, experiment.snapshot, evidence, 2)
    assert launch.max_num_seqs == 2
    with pytest.raises(ValueError, match="capacity"):
        perf.verify_experiment_runtime(contract, experiment.snapshot, evidence, 4)
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1", identity=model,
        settings=GenerationSettings(**contract.evaluation_identity["generation_settings"]),
        timeout_seconds=contract.provider_request_timeout_seconds,
        max_response_body_bytes=contract.provider_response_body_bytes,
    )
    with pytest.raises(QwenRuntimeAttestationError, match="concurrency"):
        verify_qwen_runtime(contract, provider, QwenRuntimeAttestation(
            model, token_identity, contract.model_context_tokens, 2, True))
    assert contract.snapshot_json == before


@contextmanager
def parallel_http_model():
    gate, lock = Barrier(2), Lock()
    stats = {"active": 0, "peak": 0, "requests": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "answers" not in json.dumps(payload)
            with lock:
                stats["active"] += 1
                stats["peak"] = max(stats["peak"], stats["active"])
                stats["requests"] += 1
            gate.wait(timeout=5)
            with lock:
                stats["active"] -= 1
            body = json.dumps({"choices": [{"message": {
                "content": '{"tags":["NOUN","VERB","PUNCT"]}'}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 6, "prompt_tokens": 32}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", stats
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_http_clients_reach_bounded_parallelism_and_parse_usage(experiment):
    with parallel_http_model() as (url, stats):
        providers = [OpenAICompatibleProvider(
            base_url=url, identity=ModelIdentity(**experiment.contract.evaluation_identity[
                "model_identity"]),
        ) for _ in range(2)]
        report = perf.measure_batch(experiment.cases, providers, repetitions=3)
    assert stats["peak"] == 2
    assert stats["requests"] == report["completed_requests"] == 6
    assert report["rows"][0]["input_tokens_reported"] == 32


def test_comparison_rejects_changed_inputs_and_incomplete_results(experiment):
    measurement = perf.measure_batch(experiment.cases, [FakeProvider()])
    baseline = {
        "schema_version": "qwen-performance-v1", "status": "completed",
        "real_model_requests_started": True, "source_contract_sha256": "a" * 64,
        "request_set_sha256": "b" * 64, "model_identity": {}, "generation_settings": {},
        "tokenizer_identity": {}, "samples_per_repetition": 2, "repetitions": 1,
        "warmup_requests": 0, "client_concurrency": 1, "declared_server_max_num_seqs": 1,
        "measurement": measurement, "gpu": {"samples": []},
    }
    second = {**baseline, "client_concurrency": 2, "declared_server_max_num_seqs": 2}
    comparison = perf.compare_reports([baseline, second])
    assert comparison["rows"][1]["throughput_vs_baseline"] == 1
    assert comparison["rows"][1]["changed_score_statistics"] == 0
    assert not comparison["same_gpu_and_driver_observed"]
    assert not comparison["classroom_capacity_verified"]
    for change in ({"request_set_sha256": "c" * 64}, {"status": "incomplete"}):
        with pytest.raises(ValueError):
            perf.compare_reports([baseline, {**second, **change}])


def test_output_refuses_source_tree_and_existing_directories(tmp_path):
    with pytest.raises(ValueError):
        perf.validate_output(ROOT / "benchmarks/new-private-report", tmp_path / "state")
    with pytest.raises(ValueError):
        perf.validate_output(tmp_path, tmp_path / "state")
    perf.validate_output(tmp_path / "new-report", tmp_path / "state")


def test_gpu_unavailable_is_explicit(monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(perf.subprocess, "run", unavailable)
    gpu = perf.GPUSampler()
    gpu.started = 0
    gpu._loop()
    assert gpu.report()["error"] == "gpu_telemetry_unavailable"
    assert gpu.report()["samples"] == []


def test_cli_dry_run_and_explicit_run_keep_outputs_private_and_non_overwriting(
    experiment, tmp_path, monkeypatch,
):
    monkeypatch.setattr(perf, "getproxies", lambda: {})
    calls = []

    class Client(FakeProvider):
        def __init__(self, **kwargs):
            self.base_url = kwargs["base_url"]
            self.identity = kwargs["identity"]

        def served_model_ids(self):
            calls.append("models")
            return {self.identity.model}

        def generate(self, request):
            calls.append("generate")
            return super().generate(request)

    @contextmanager
    def no_gpu(index):
        yield SimpleNamespace(report=lambda: {"error": "fixture", "samples": []})

    monkeypatch.setattr(perf, "OpenAICompatibleProvider", Client)
    monkeypatch.setattr(perf, "GPUSampler", no_gpu)
    contract_file = tmp_path / "contract.json"
    contract_file.write_text(experiment.contract.snapshot_json, encoding="utf-8")
    write_challenge(experiment.artifacts, public_dir=tmp_path / "public",
                    private_dir=tmp_path / "private")
    key = experiment.contract.challenge_id
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    evidence = tmp_path / "launch.json"
    evidence.write_text(json.dumps({
        "schema_version": "linguistic-oj-vllm-launch-v1",
        "model_snapshot_path": str(experiment.snapshot.resolve()),
        "runtime_version": experiment.contract.evaluation_identity["model_identity"][
            "runtime_version"], "max_model_len": 4096, "max_num_seqs": 1,
        "language_model_only": True,
    }), encoding="utf-8")
    evidence.chmod(0o600)
    state = tmp_path / "cli-state"
    state.mkdir(mode=0o700)
    output = tmp_path / "report"
    args = ["--contract", str(contract_file),
            "--public-challenge", str(tmp_path / "public" / f"{key}.json"),
            "--private-challenge", str(tmp_path / "private" / f"{key}.json"),
            "--dataset", str(experiment.artifacts.dataset_path), "--prompt-file", str(prompt),
            "--tokenizer-snapshot", str(experiment.snapshot), "--launch-evidence", str(evidence),
            "--state-dir", str(state), "--output", str(output),
            "--sample-limit", "2", "--repetitions", "2"]
    assert perf.main(args) == 0
    assert calls == [] and not output.exists() and not (state / "state.json").exists()
    assert perf.main([*args, "--run-real-qwen", "--initialize-state"]) == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert calls.count("generate") == report["planned_model_requests"] == 5
    assert report["measurement"]["completed_requests"] == 4
    assert report["status"] == "completed" and report["platform_scores_written"] is False
    assert report["application_queue_wait_seconds"] is None
    original = (output / "report.json").read_bytes()
    assert perf.main([*args, "--run-real-qwen"]) == 78
    assert (output / "report.json").read_bytes() == original
    assert calls.count("generate") == 5
