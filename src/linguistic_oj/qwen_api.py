"""Production API composition for the Qwen v2 Redis submission partition."""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from fastapi import FastAPI

from .api import Authenticate, create_app
from .mvp_contract import EvaluationContract, load_qwen_worker_contract
from .redis_job_queue import RedisJobQueue
from .submission_jobs import (
    QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS,
    OutboxDispatcher,
)
from .submission_store import SubmissionStore


@dataclass(frozen=True, slots=True)
class QwenApiRuntime:
    app: FastAPI
    contract: EvaluationContract
    dispatcher: OutboxDispatcher
    queue: RedisJobQueue
    store: SubmissionStore


def build_qwen_api(
    *,
    root: Path,
    database_path: Path,
    redis_url: str,
    authenticate: Authenticate,
    namespace: str = "linguistic-oj",
    allow_draft_submissions: bool = False,
    environment: Literal["development", "test", "production"] = "production",
) -> QwenApiRuntime:
    """Compose API, store, and Redis queue for the Qwen v2 contract only."""

    contract = load_qwen_worker_contract(root)
    store = SubmissionStore(database_path)
    queue = RedisJobQueue(
        redis_url=redis_url,
        routing_key=contract.contract_snapshot_sha256,
        visibility_timeout_seconds=(
            contract.job_deadline_seconds + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS
        ),
        namespace=namespace,
    )
    dispatcher = OutboxDispatcher(store, queue, contract)

    def readiness_check() -> None:
        store.health_check()
        queue.health_check()

    app = create_app(
        store=store,
        dispatcher=dispatcher,
        contract=contract,
        authenticate=authenticate,
        readiness_check=readiness_check,
        allow_draft_submissions=allow_draft_submissions,
        environment=environment,
    )
    return QwenApiRuntime(
        app=app,
        contract=contract,
        dispatcher=dispatcher,
        queue=queue,
        store=store,
    )


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Qwen v2 submission API with a Redis Streams outbox."
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="deployment root containing config/mvp_evaluation_v2.json",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument(
        "--authenticate",
        required=True,
        help="dotted callback in module:attribute form",
    )
    parser.add_argument("--namespace", default="linguistic-oj")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--environment",
        choices=("development", "test", "production"),
        default="production",
    )
    parser.add_argument("--allow-draft-submissions", action="store_true")
    args = parser.parse_args(arguments)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def _load_authenticate(reference: str) -> Authenticate:
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("--authenticate must use module:attribute form")
    callback = getattr(importlib.import_module(module_name), attribute_name)
    if not callable(callback):
        raise TypeError("configured authentication callback must be callable")
    return cast(Authenticate, callback)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    try:
        import uvicorn
    except ImportError as error:
        raise RuntimeError("install the api extra to run the Qwen API") from error
    runtime = build_qwen_api(
        root=args.root,
        database_path=args.database,
        redis_url=args.redis_url,
        authenticate=_load_authenticate(args.authenticate),
        namespace=args.namespace,
        allow_draft_submissions=args.allow_draft_submissions,
        environment=args.environment,
    )
    uvicorn.run(runtime.app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
