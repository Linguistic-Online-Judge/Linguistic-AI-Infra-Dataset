"""Production-only, single-host serial Qwen executor with durable recovery barriers."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import signal
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import getproxies

from .auth_config import _read_protected
from .challenge import load_challenge_artifacts
from .challenge_registry import load_challenge_contract_registry
from .connection_config import resolve_connection_url
from .executor_state import (
    ExecutorBusy,
    ExecutorState,
    GuardedQwenProvider,
    RecoveryRequired,
    executor_lock,
    read_state,
)
from .mvp_contract import canonical_sha256
from .providers import GenerationSettings, ModelIdentity
from .qwen_runtime import validate_qwen_evaluation_contract
from .redis_job_queue import RedisJobQueue
from .sample_cache import VerifiedSelectionCache
from .submission_jobs import QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS, QwenSubmissionWorker
from .submission_store_factory import build_submission_store

LOGGER = logging.getLogger("linguistic_oj.executor")


def _absolute_path(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("executor deployment paths must be explicit absolute paths")
    return Path(value)


def _connection_target(url):
    """Credential rotation must not change binding; different destinations must."""
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    return {"scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port,
            "username": unquote(parsed.username or ""),
            "path": unquote(parsed.path),
            "query": {key: query[key] for key in ("host", "port", "db") if key in query}}


@dataclass(repr=False)
class ExecutorPlan:
    state_dir: Path
    contracts: dict
    artifacts: dict
    postgres_url: str
    redis_url: str
    namespace: str
    base_url: str
    tokenizer_snapshot: Path
    launch_evidence: Path
    binding_sha256: str


def load_plan(config_path: Path) -> ExecutorPlan:
    # Multi-task deployment paths exceed the smaller authentication-file budget.
    config = json.loads(_read_protected(config_path, max_bytes=1048576))
    if not isinstance(config, dict) or set(config) != {
        "version", "root", "registry", "state_dir", "postgres_database_url_file",
        "redis_url_file", "namespace", "vllm_base_url", "tokenizer_snapshot",
        "launch_evidence", "artifacts"
    } or config["version"] != "qwen-serial-executor-v1":
        raise ValueError("invalid production executor configuration")
    root = _absolute_path(config["root"])
    state_dir = _absolute_path(config["state_dir"])
    tokenizer = _absolute_path(config["tokenizer_snapshot"])
    evidence = _absolute_path(config["launch_evidence"])
    registry_path = config["registry"]
    if (not isinstance(registry_path, str)
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", registry_path)
            or Path(registry_path).is_absolute() or ".." in Path(registry_path).parts
            or not (root / registry_path).resolve().is_relative_to(root.resolve())):
        raise ValueError("executor registry must stay inside the release root")
    namespace = config["namespace"]
    if not isinstance(namespace, str) or re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", namespace) is None:
        raise ValueError("invalid executor queue namespace")
    # This composition is deliberately co-located with the pinned school model.
    base_url = config["vllm_base_url"]
    if base_url != "http://127.0.0.1:8000/v1":
        raise ValueError("production executor requires the co-located fixed Qwen endpoint")
    if any(key in getproxies() for key in ("http", "https", "all")):
        raise ValueError("unset model HTTP proxy settings for the co-located executor")
    registry = load_challenge_contract_registry(root, Path(registry_path))
    paths = config["artifacts"]
    if (not isinstance(paths, dict) or not paths or set(paths) - set(registry.contracts)):
        raise ValueError("declare a non-empty set of registered production artifacts")
    contracts, artifacts = {}, {}
    for key in sorted(paths):
        contract = registry.contracts[key]
        validate_qwen_evaluation_contract(contract)
        if not contract.external_activation_ready:
            raise ValueError("executor contains a challenge not eligible for public activation")
        if contract.evaluation_identity["model_identity"]["model"] != "Qwen/Qwen3.5-9B":
            raise ValueError("production executor requires Qwen3.5-9B")
        entry = paths[key]
        if not isinstance(entry, dict) or set(entry) != {
            "public_challenge", "private_challenge", "dataset"
        }:
            raise ValueError("invalid executor artifact paths")
        selected = load_challenge_artifacts(
            _absolute_path(entry["public_challenge"]),
            _absolute_path(entry["private_challenge"]),
            dataset_path=_absolute_path(entry["dataset"]),
        )
        if selected.public != registry.public_challenges[key]:
            raise ValueError("executor artifacts differ from the registered public descriptor")
        contracts[key], artifacts[key] = contract, selected
    models = {canonical_sha256(c.evaluation_identity["model_identity"]) for c in contracts.values()}
    if len(models) != 1:
        raise ValueError("all serial tasks must bind the same model identity")
    pg = resolve_connection_url(
        "postgres", inline_url=None,
        credential_file=_absolute_path(config["postgres_database_url_file"]), production=True,
    )
    redis = resolve_connection_url(
        "redis", inline_url=None,
        credential_file=_absolute_path(config["redis_url_file"]), production=True,
    )
    binding = canonical_sha256({
        "contracts": {key: c.contract_snapshot_sha256 for key, c in contracts.items()},
        "postgres": _connection_target(pg), "redis": _connection_target(redis),
        "namespace": namespace, "model_endpoint": base_url,
    })
    return ExecutorPlan(state_dir, contracts, artifacts, pg, redis, namespace,
                        base_url, tokenizer, evidence, binding)


def build_workers(plan: ExecutorPlan, state: ExecutorState, resources: ExitStack, stop: Event):
    state.require_clean()
    store = build_submission_store(database_path=None, postgres_database_url=plan.postgres_url)
    store.health_check()
    workers = {}
    selection_cache = VerifiedSelectionCache()
    for key, contract in plan.contracts.items():
        if stop.is_set():
            break
        queue = RedisJobQueue(
            redis_url=plan.redis_url, routing_key=contract.contract_snapshot_sha256,
            visibility_timeout_seconds=(contract.job_deadline_seconds
                                        + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS),
            namespace=plan.namespace, consumer_name="serial-production",
        )
        resources.callback(queue.close)
        queue.health_check()
        identity = contract.evaluation_identity
        provider = GuardedQwenProvider(
            executor_state=state, challenge_id=key, base_url=plan.base_url,
            identity=ModelIdentity(**identity["model_identity"]),
            settings=GenerationSettings(**identity["generation_settings"]),
            timeout_seconds=contract.provider_request_timeout_seconds,
            max_response_body_bytes=contract.provider_response_body_bytes,
        )
        workers[key] = QwenSubmissionWorker(
            store=store, queue=queue, contract=contract, artifacts=plan.artifacts[key],
            provider=provider, tokenizer_snapshot_path=plan.tokenizer_snapshot,
            launch_evidence_path=plan.launch_evidence,
            selection_cache=selection_cache,
        )
    return workers


def run_serial(workers, state: ExecutorState, stop: Event, *, once=False, idle_seconds=0.25):
    """Round-robin, one complete claimed job at a time; stop drains the current job."""
    if not math.isfinite(idle_seconds) or idle_seconds <= 0:
        raise ValueError("idle interval must be positive and finite")
    while not stop.is_set():
        state.require_clean()
        did_work = False
        for key, worker in workers.items():
            if stop.is_set():
                return
            state.require_clean()
            worked = worker.run_once()
            did_work = worked or did_work
            # Workers classify provider failures into results; still stop the entire executor.
            state.require_clean()
            if worked:
                LOGGER.info("executor_job_complete challenge=%s", key)
        if once:
            return
        if not did_work:
            stop.wait(idle_seconds)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "init", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        if name == "run":
            sub.add_argument("--once", action="store_true")
    sub = commands.add_parser("status")
    sub.add_argument("--state-dir", type=Path, required=True)
    sub = commands.add_parser("recover")
    sub.add_argument("--state-dir", type=Path, required=True)
    sub.add_argument("--expected-operation-id", required=True)
    sub.add_argument("--evidence-file", type=Path, required=True)
    args = parser.parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.command == "status":
            busy = False
            try:
                with executor_lock(args.state_dir):
                    pass
            except ExecutorBusy:
                busy = True
            state = read_state(args.state_dir)
            print(json.dumps({**state, "process_lock_held": busy,
                              "observation_only": True}))
            return 0
        if args.command == "recover":
            with executor_lock(args.state_dir):
                state = read_state(args.state_dir)
                ExecutorState(args.state_dir, state["binding_sha256"]).recover(
                    args.expected_operation_id, args.evidence_file,
                )
            LOGGER.info("executor_recovery_recorded")
            return 0
        plan = load_plan(args.config)
        if args.command == "check":
            print(json.dumps({"binding_sha256": plan.binding_sha256,
                              "challenge_count": len(plan.contracts),
                              "model_attested": False, "production_started": False}))
            return 0
        with executor_lock(plan.state_dir, create=args.command == "init"):
            if args.command == "init":
                ExecutorState.initialize(plan.state_dir, plan.binding_sha256)
                LOGGER.info("executor_state_initialized")
                return 0
            state = ExecutorState(plan.state_dir, plan.binding_sha256)
            state.require_clean()
            stop = Event()
            previous = {}
            try:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous[sig] = signal.signal(sig, lambda *args: stop.set())
                with ExitStack() as resources:
                    workers = build_workers(plan, state, resources, stop)
                    LOGGER.info("executor_started challenges=%s", len(workers))
                    run_serial(workers, state, stop, once=args.once)
                    LOGGER.info("executor_stopped")
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        return 0
    except RecoveryRequired:
        LOGGER.error("executor_blocked termination_confirmation_required")
        return 75
    except ExecutorBusy:
        LOGGER.error("executor_blocked state_in_use")
        return 75
    except (OSError, ValueError, TypeError, KeyError):
        # No exception text: dependency errors can include credentials or private file contents.
        LOGGER.error("executor_configuration_or_storage_invalid")
        return 78
    except Exception:
        LOGGER.error("executor_runtime_failed")
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
