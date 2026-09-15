"""Private, bounded Qwen performance experiments; never an online scoring entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from urllib.request import getproxies

from .auth_config import _read_protected
from .challenge import load_challenge_artifacts
from .challenge_registry import validate_contract_matches_public
from .executor_state import (
    ExecutorBusy,
    ExecutorState,
    RecoveryRequired,
    _check_directory,
    _write_atomic,
    executor_lock,
)
from .mvp_contract import EvaluationContract, canonical_sha256
from .providers import (
    GenerationSettings,
    ModelIdentity,
    ModelRequest,
    OpenAICompatibleProvider,
    PromptEnvelope,
    ProviderContractError,
    ProviderRequestError,
)
from .qwen_runtime import (
    QwenLaunchEvidence,
    QwenTokenizerPreflight,
    TokenizerIdentity,
    _token_count,
    load_huggingface_tokenizer,
    validate_qwen_evaluation_contract,
)
from .responses import TaskType
from .runner import _prepare_samples, evaluate_raw_response


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "maximum": None}
    return {"count": len(values), "mean": sum(values) / len(values),
            "p50": values[math.ceil(len(values) * 0.5) - 1],
            "p95": values[math.ceil(len(values) * 0.95) - 1], "maximum": values[-1]}


def validate_output(output, state_dir):
    target, state = output.resolve(), state_dir.resolve()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("output must be a new directory in an existing private parent")
    _check_directory(output.parent)
    if target.is_relative_to(state) or state.is_relative_to(target):
        raise ValueError("output must be separate from persistent experiment state")
    result = subprocess.run(["git", "-C", str(output.parent), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=5, check=False)
    if result.returncode == 0:
        root = Path(result.stdout.strip()).resolve()
        if not target.is_relative_to(root / "runtime"):
            raise ValueError("reports inside a Git checkout must stay below ignored runtime")


@dataclass(frozen=True, repr=False)
class PerformanceCase:
    position: int
    request: ModelRequest
    prepared: object
    input_tokens: int


def prepare_cases(artifacts, contract, tokenizer, tokenizer_identity, prompt, sample_limit):
    start = monotonic()
    validate_contract_matches_public(contract, artifacts.public)
    prepared = _prepare_samples(artifacts)
    if type(sample_limit) is not int or not 1 <= sample_limit <= len(prepared):
        raise ValueError("sample limit must be within the frozen sample selection")
    requests = tuple(ModelRequest(
        task=TaskType(artifacts.public.task), language=artifacts.public.language,
        treebank=artifacts.public.treebank, student_prompt=prompt, model_input=sample.model_input,
    ) for sample in prepared[:sample_limit])
    prepared_at = monotonic()
    QwenTokenizerPreflight(contract, tokenizer, tokenizer_identity)(requests)
    cases = []
    for position, (sample, request) in enumerate(zip(prepared, requests, strict=False), 1):
        count = _token_count(tokenizer.apply_chat_template(
            list(PromptEnvelope.from_request(request).to_messages()), tokenize=True,
            add_generation_prompt=True, enable_thinking=False,
        ))
        cases.append(PerformanceCase(position, request, sample, count))
    return cases, {"prepare_samples_seconds": prepared_at - start,
                   "token_preflight_and_count_seconds": monotonic() - prepared_at}


def verify_experiment_runtime(contract, snapshot, evidence_path, concurrency):
    """Separate calibration checks: report actual declared capacity, never edit a contract."""
    validate_qwen_evaluation_contract(contract)
    identity = contract.evaluation_identity
    model = ModelIdentity(**identity["model_identity"])
    if model.model != "Qwen/Qwen3.5-9B":
        raise ValueError("performance experiment requires the pinned Qwen3.5-9B")
    expected = TokenizerIdentity.from_mapping(identity["tokenizer_identity"])
    actual = TokenizerIdentity.from_snapshot(
        snapshot, repository=expected.repository, revision=expected.revision,
        add_generation_prompt=expected.add_generation_prompt,
        enable_thinking=expected.enable_thinking,
    )
    launch = QwenLaunchEvidence.from_path(evidence_path)
    if (actual != expected or launch.model_snapshot_path != snapshot.resolve()
            or launch.runtime_version != model.runtime_version
            or launch.max_model_len != contract.model_context_tokens
            or not launch.language_model_only):
        raise ValueError("performance runtime evidence does not match pinned configuration")
    if concurrency not in (1, 2, 4) or launch.max_num_seqs < concurrency:
        raise ValueError("declared model capacity is below requested client concurrency")
    tokenizer = load_huggingface_tokenizer(snapshot)
    template_hash = hashlib.sha256(tokenizer.chat_template.encode()).hexdigest()
    if template_hash != expected.chat_template_sha256:
        raise ValueError("loaded tokenizer template does not match pinned files")
    return model, tokenizer, expected, launch


class GPUSampler:
    """Read-only nvidia-smi sampling; missing telemetry is explicit, never fabricated."""

    def __init__(self, index=0):
        self.index = index
        self.stop = Event()
        self.rows = []
        self.error = None
        self.thread = Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stop.is_set():
            try:
                result = subprocess.run([
                    "nvidia-smi", f"--id={self.index}",
                    "--query-gpu=uuid,name,driver_version,memory.total,memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ], capture_output=True, text=True, timeout=2, check=True)
                values = [part.strip() for part in result.stdout.strip().split(",")]
                if len(values) != 6:
                    raise ValueError
                if any(not math.isfinite(float(value)) for value in values[3:]):
                    raise ValueError
                self.rows.append({"elapsed_seconds": monotonic() - self.started,
                                  "uuid": values[0], "name": values[1], "driver": values[2],
                                  "memory_total_mib": float(values[3]),
                                  "memory_used_mib": float(values[4]),
                                  "utilization_percent": float(values[5])})
            except (OSError, ValueError, subprocess.SubprocessError):
                self.error = "gpu_telemetry_unavailable"
                return
            self.stop.wait(1)

    def __enter__(self):
        self.started = monotonic()
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()

    def report(self):
        return {"scope": "whole_device_not_exclusive_process", "error": self.error,
                "samples": self.rows}


def measure_batch(cases, providers, *, repetitions=1, score=evaluate_raw_response, stop_event=None):
    """One provider per slot; stop assigning new cases on failure and drain assigned requests."""
    if not cases or not providers or type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError("non-empty cases/providers and 1..10 repetitions are required")
    work = list(enumerate(case for _ in range(repetitions) for case in cases))
    lock, abort, uncertain = Lock(), stop_event if stop_event is not None else Event(), Event()
    cursor, rows = 0, []
    started = monotonic()

    def consume(provider):
        nonlocal cursor
        while True:
            with lock:
                if abort.is_set() or cursor == len(work):
                    return
                sequence, case = work[cursor]
                cursor += 1
            row = {"sequence": sequence, "sample_position": case.position,
                   "repetition": sequence // len(cases) + 1,
                   "client_queue_wait_seconds": monotonic() - started,
                   "input_tokens_local": case.input_tokens, "status": "failed"}
            request_started = monotonic()
            generation = None
            try:
                generation = provider.generate(case.request)
                row.update(request_seconds=monotonic() - request_started,
                           output_tokens=generation.generated_token_count,
                           input_tokens_reported=generation.prompt_token_count,
                           response_utf8_bytes=len(generation.raw_text.encode("utf-8")),
                           output_sha256=hashlib.sha256(generation.raw_text.encode()).hexdigest(),
                           finish_reason=generation.finish_reason)
                scoring_started = monotonic()
                outcome = score(sample=case.prepared.dataset_sample,
                                manifest_sample=case.prepared.manifest_sample,
                                task=case.request.task, model_input=case.request.model_input,
                                raw_response=generation.raw_text)
                row.update(scoring_seconds=monotonic() - scoring_started, status="completed",
                           score_statistics=asdict(outcome.score) if outcome.score else None,
                           format_error=outcome.error_code.value if outcome.error_code else None)
            except BaseException as error:
                abort.set()
                row["request_seconds"] = row.get("request_seconds", monotonic() - request_started)
                if isinstance(error, ProviderRequestError):
                    if not error.termination_confirmed:
                        uncertain.set()
                    row["error"] = "provider_request_failed"
                elif isinstance(error, ProviderContractError):
                    row["error"] = "provider_contract_failed"
                else:
                    # If generate() did not finish, unknown exceptions cannot prove termination.
                    if generation is None:
                        uncertain.set()
                    row["error"] = "measurement_failed"
            finally:
                if provider.has_active_request:
                    uncertain.set()
                    abort.set()
                row["completion_seconds"] = monotonic() - started
                with lock:
                    rows.append(row)

    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futures = [pool.submit(consume, provider) for provider in providers]
        for future in futures:
            future.result()
    elapsed = monotonic() - started
    rows.sort(key=lambda row: row["sequence"])
    completed = [row for row in rows if row["status"] == "completed"]
    tokens = [row["output_tokens"] for row in completed if row["output_tokens"] is not None]
    return {
        "planned_requests": len(work), "started_requests": len(rows),
        "completed_requests": len(completed), "not_started_requests": len(work) - len(rows),
        "termination_unconfirmed": uncertain.is_set(), "batch_wall_seconds": elapsed,
        "completed_requests_per_second": len(completed) / elapsed if elapsed else None,
        "output_tokens_per_batch_second": (sum(tokens) / elapsed
                                           if elapsed and len(tokens) == len(completed)
                                           and completed else None),
        "missing_output_token_counts": len(completed) - len(tokens),
        "request_seconds": distribution([row["request_seconds"] for row in completed]),
        "client_queue_wait_seconds": distribution(
            [row["client_queue_wait_seconds"] for row in rows]),
        "completion_seconds": distribution([row["completion_seconds"] for row in completed]),
        "scoring_seconds_total": sum(row["scoring_seconds"] for row in completed),
        "format_invalid_count": sum(row["format_error"] is not None for row in completed),
        "rows": rows,
    }


def guarded_experiment(state, cases, providers, *, repetitions, warmup_requests, stop_event=None):
    state.require_clean()
    operation = state.begin("private-performance-experiment")
    warmup = None
    if warmup_requests:
        warmup = measure_batch(cases[:1], providers[:1], stop_event=stop_event)
        if warmup["completed_requests"] != 1 or warmup["termination_unconfirmed"]:
            if not warmup["termination_unconfirmed"]:
                state.finish(operation)
            return {"operation_id": operation, "warmup": warmup, "measurement": None}
    measurement = measure_batch(cases, providers, repetitions=repetitions, stop_event=stop_event)
    if not measurement["termination_unconfirmed"]:
        state.finish(operation)
    return {"operation_id": operation, "warmup": warmup, "measurement": measurement}


def compare_reports(reports):
    if len(reports) < 2:
        raise ValueError("at least two completed reports are required")
    matching = ("source_contract_sha256", "request_set_sha256", "model_identity",
                "generation_settings", "tokenizer_identity", "samples_per_repetition",
                "repetitions", "warmup_requests")
    first = reports[0]
    for report in reports:
        if (report.get("schema_version") != "qwen-performance-v1"
                or report.get("status") != "completed"
                or not report.get("real_model_requests_started")
                or any(key not in report or report[key] != first.get(key) for key in matching)
                or not isinstance(report.get("measurement"), dict)
                or report["measurement"].get("termination_unconfirmed")
                or report["measurement"].get("completed_requests")
                != report["measurement"].get("planned_requests")):
            raise ValueError("reports are incomplete or use different comparison inputs")
        measurement = report["measurement"]
        planned = report["samples_per_repetition"] * report["repetitions"]
        if (type(planned) is not int or planned < 1
                or measurement["planned_requests"] != planned
                or len(measurement.get("rows", [])) != planned
                or [row.get("sequence") for row in measurement["rows"]] != list(range(planned))
                or any(row.get("status") != "completed" for row in measurement["rows"])):
            raise ValueError("sample rows do not match declared completed work")
    baseline = next((report for report in reports if report["client_concurrency"] == 1), None)
    if baseline is None:
        raise ValueError("a single-client baseline is required")
    baseline_rows = baseline["measurement"]["rows"]
    rows, hardware = [], []
    for report in reports:
        result = report["measurement"]
        if len(result["rows"]) != len(baseline_rows):
            raise ValueError("sample observations do not match")
        baseline_rate = baseline["measurement"]["completed_requests_per_second"]
        rows.append({"client_concurrency": report["client_concurrency"],
                     "declared_server_max_num_seqs": report["declared_server_max_num_seqs"],
                     "batch_wall_seconds": result["batch_wall_seconds"],
                     "throughput_vs_baseline": (result["completed_requests_per_second"]
                                                / baseline_rate if baseline_rate else None),
                     "request_seconds": result["request_seconds"],
                     "format_invalid_count": result["format_invalid_count"],
                     "output_tokens_per_batch_second": result["output_tokens_per_batch_second"],
                     "changed_output_digests": sum(
                         row.get("output_sha256") != reference.get("output_sha256")
                         for row, reference in zip(result["rows"], baseline_rows, strict=True)),
                     "changed_score_statistics": sum(
                         row.get("score_statistics") != reference.get("score_statistics")
                         for row, reference in zip(result["rows"], baseline_rows, strict=True))})
        hardware.append({(sample["uuid"], sample["name"], sample["driver"])
                         for sample in report["gpu"]["samples"]})
    return {"schema_version": "qwen-performance-comparison-v1", "rows": rows,
            "same_gpu_and_driver_observed": bool(hardware[0]) and all(
                item == hardware[0] for item in hardware),
            "classroom_capacity_verified": False}


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments and arguments[0] == "compare":
        comparison = argparse.ArgumentParser(
            description="Compare matching private performance runs")
        comparison.add_argument("reports", type=Path, nargs="+")
        args = comparison.parse_args(arguments[1:])
        try:
            reports = [json.loads(_read_protected(path, max_bytes=1048576))
                       for path in args.reports]
            print(json.dumps(compare_reports(reports), ensure_ascii=False, indent=2))
            return 0
        except Exception:
            print("performance_reports_not_comparable")
            return 78
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("contract", "public-challenge", "private-challenge", "dataset",
                  "prompt-file", "tokenizer-snapshot", "launch-evidence", "state-dir", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--sample-limit", type=int, default=6)
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--warmup-requests", type=int, choices=(0, 1), default=1)
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--model-port", type=int, default=8000,
                        help="loopback model port; use a separate port for isolated tests")
    parser.add_argument("--initialize-state", action="store_true")
    parser.add_argument("--run-real-qwen", action="store_true")
    args = parser.parse_args(arguments)
    try:
        if any(key in getproxies() for key in ("http", "https", "all")):
            raise ValueError("implicit model proxy is not supported")
        validate_output(args.output, args.state_dir)
        if args.gpu_index < 0:
            raise ValueError("GPU index must not be negative")
        if not 1024 <= args.model_port <= 65535:
            raise ValueError("model port must be in 1024..65535")
        contract = EvaluationContract.from_path(args.contract)
        model, tokenizer, token_identity, launch = verify_experiment_runtime(
            contract, args.tokenizer_snapshot, args.launch_evidence, args.concurrency,
        )
        start = monotonic()
        artifacts = load_challenge_artifacts(args.public_challenge, args.private_challenge,
                                              dataset_path=args.dataset)
        load_seconds = monotonic() - start
        prompt = args.prompt_file.read_text(encoding="utf-8")
        cases, phases = prepare_cases(artifacts, contract, tokenizer, token_identity,
                                     prompt, args.sample_limit)
        settings = GenerationSettings(**contract.evaluation_identity["generation_settings"])
        providers = [OpenAICompatibleProvider(
            base_url=f"http://127.0.0.1:{args.model_port}/v1", identity=model, settings=settings,
            timeout_seconds=contract.provider_request_timeout_seconds,
            max_response_body_bytes=contract.provider_response_body_bytes,
        ) for _ in range(args.concurrency)]
        request_hash = canonical_sha256([
            PromptEnvelope.from_request(case.request).to_dict() for case in cases])
        report = {
            "schema_version": "qwen-performance-v1", "assurance": "private-calibration-only",
            "source_contract_sha256": contract.contract_snapshot_sha256,
            "request_set_sha256": request_hash, "model_identity": model.to_dict(),
            "generation_settings": settings.to_dict(),
            "tokenizer_identity": token_identity.to_dict(),
            "launch_evidence_sha256": hashlib.sha256(args.launch_evidence.read_bytes()).hexdigest(),
            "declared_server_max_num_seqs": launch.max_num_seqs,
            "model_endpoint": providers[0].base_url, "gpu_index": args.gpu_index,
            "client_concurrency": args.concurrency, "samples_per_repetition": len(cases),
            "repetitions": args.repetitions, "warmup_requests": args.warmup_requests,
            "planned_model_requests": len(cases) * args.repetitions + args.warmup_requests,
            "preparation": {"artifact_load_seconds": load_seconds, **phases},
            "platform_scores_written": False, "application_queue_wait_seconds": None,
            "database_write_seconds": None, "is_classroom_load_test": False,
            "is_pure_decode_token_rate": False, "real_model_requests_started": False,
        }
        if not args.run_real_qwen:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        binding = canonical_sha256({"kind": "qwen-performance-state-v1",
                                    "endpoint": providers[0].base_url, "model": model.to_dict()})
        with executor_lock(args.state_dir, create=args.initialize_state):
            state = (ExecutorState.initialize(args.state_dir, binding) if args.initialize_state
                     else ExecutorState(args.state_dir, binding))
            state.require_clean()
            if model.model not in providers[0].served_model_ids():
                raise ValueError("model alias does not match")
            args.output.mkdir(mode=0o700)
            _write_atomic(args.output / "report.json", {
                **report, "status": "running", "real_model_requests_started": None,
            })
            # Stop assigning cases on normal termination; keep the barrier on forced interruption.
            stop, previous = Event(), {}
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    previous[sig] = signal.signal(sig, lambda *args: stop.set())
                with GPUSampler(args.gpu_index) as gpu:
                    results = guarded_experiment(
                        state, cases, providers, repetitions=args.repetitions,
                        warmup_requests=args.warmup_requests, stop_event=stop,
                    )
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            measurement = results["measurement"]
            complete = (measurement is not None and not measurement["termination_unconfirmed"]
                        and measurement["completed_requests"] == measurement["planned_requests"])
            attempts = sum(result["started_requests"] for result in (
                results["warmup"], measurement) if result is not None)
            report.update(results, gpu=gpu.report(), real_model_requests_started=attempts > 0,
                          status="completed" if complete else "incomplete")
            _write_atomic(args.output / "report.json", report)
            print(json.dumps({"status": report["status"],
                              "report": str(args.output / "report.json"),
                              "operation_id": results["operation_id"]}))
            return 0 if complete else 1
    except RecoveryRequired:
        print("performance_state_requires_recorded_termination_confirmation")
        return 75
    except ExecutorBusy:
        print("performance_state_in_use")
        return 75
    except Exception:
        # Never echo server errors, credential material, prompts or gold data.
        print("performance_preflight_or_run_failed")
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
