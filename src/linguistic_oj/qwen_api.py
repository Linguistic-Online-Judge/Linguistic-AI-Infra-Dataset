"""Production API composition for the Qwen v2 Redis submission partition."""

from __future__ import annotations

import argparse
import importlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from fastapi import FastAPI

from .api import Authenticate, create_app
from .challenge_registry import ChallengeContractRegistry, load_challenge_contract_registry
from .postgres_migrations import resolve_postgres_url
from .qwen_runtime import validate_qwen_evaluation_contract
from .redis_job_queue import RedisJobQueue, resolve_redis_url
from .submission_jobs import (
    QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS,
    OutboxDispatcher,
)
from .submission_store import SubmissionStoreProtocol
from .submission_store_factory import build_submission_store


@dataclass(frozen=True, slots=True)
class QwenApiRuntime:
    app: FastAPI
    registry: ChallengeContractRegistry
    dispatchers: Mapping[str, OutboxDispatcher]
    queues: Mapping[str, RedisJobQueue]
    store: SubmissionStoreProtocol

    def __post_init__(self) -> None:
        object.__setattr__(self, "dispatchers", MappingProxyType(dict(self.dispatchers)))
        object.__setattr__(self, "queues", MappingProxyType(dict(self.queues)))


def build_qwen_api(
    *,
    root: Path,
    challenge_registry_path: Path,
    database_path: Path | None = None,
    postgres_database_url: str | None = None,
    redis_url: str,
    authenticate: Authenticate,
    namespace: str = "linguistic-oj",
    allow_draft_submissions: bool = False,
    environment: Literal["development", "test", "production"] = "production",
) -> QwenApiRuntime:
    """Compose one API with an isolated Redis queue for each Qwen contract."""

    if environment == "production" and database_path is not None:
        raise ValueError("production Qwen API requires PostgreSQL persistence")
    registry = load_challenge_contract_registry(root, challenge_registry_path)
    for contract in registry.contracts.values():
        validate_qwen_evaluation_contract(contract)
    store = build_submission_store(
        database_path=database_path,
        postgres_database_url=postgres_database_url,
    )
    queues = {
        challenge_id: RedisJobQueue(
            redis_url=redis_url,
            routing_key=contract.contract_snapshot_sha256,
            visibility_timeout_seconds=(
                contract.job_deadline_seconds + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS
            ),
            namespace=namespace,
        )
        for challenge_id, contract in registry.contracts.items()
    }
    dispatchers = {
        challenge_id: OutboxDispatcher(store, queues[challenge_id], contract)
        for challenge_id, contract in registry.contracts.items()
    }

    def readiness_check() -> None:
        store.health_check()
        for queue in queues.values():
            queue.health_check()

    app = create_app(
        store=store,
        registry=registry,
        dispatchers=dispatchers,
        authenticate=authenticate,
        readiness_check=readiness_check,
        allow_draft_submissions=allow_draft_submissions,
        environment=environment,
    )
    return QwenApiRuntime(
        app=app,
        registry=registry,
        dispatchers=dispatchers,
        queues=queues,
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
        help="deployment root containing registry-referenced files",
    )
    parser.add_argument(
        "--challenge-registry",
        type=Path,
        required=True,
        help="root-relative challenge registry path",
    )
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--database", type=Path, help="SQLite database path")
    storage.add_argument("--postgres-database-url", help="PostgreSQL database URL")
    storage.add_argument("--postgres-database-url-file", type=Path)
    redis = parser.add_mutually_exclusive_group(required=True)
    redis.add_argument("--redis-url")
    redis.add_argument("--redis-url-file", type=Path)
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
    if args.environment == "production" and args.database is not None:
        parser.error("production Qwen API requires PostgreSQL persistence")
    try:
        if args.database is None:
            args.postgres_database_url = resolve_postgres_url(
                inline_url=args.postgres_database_url,
                credential_file=args.postgres_database_url_file,
                allow_inline_credentials=args.environment != "production",
            )
        args.redis_url = resolve_redis_url(
            inline_url=args.redis_url,
            credential_file=args.redis_url_file,
            allow_inline_credentials=args.environment != "production",
        )
    except ValueError as error:
        parser.error(str(error))
    return args


def _load_authenticate(reference: str) -> Authenticate:
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("--authenticate must use module:attribute form")
    callback = getattr(importlib.import_module(module_name), attribute_name)
    if not callable(callback):
        raise TypeError("configured authentication callback must be callable")
    return cast(Authenticate, callback)


def _configure_safe_request_logging() -> None:
    logger = logging.getLogger("linguistic_oj.http")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    try:
        import uvicorn
    except ImportError as error:
        raise RuntimeError("install the api extra to run the Qwen API") from error
    _configure_safe_request_logging()
    runtime = build_qwen_api(
        root=args.root,
        challenge_registry_path=args.challenge_registry,
        database_path=args.database,
        postgres_database_url=args.postgres_database_url,
        redis_url=args.redis_url,
        authenticate=_load_authenticate(args.authenticate),
        namespace=args.namespace,
        allow_draft_submissions=args.allow_draft_submissions,
        environment=args.environment,
    )
    uvicorn.run(runtime.app, host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
