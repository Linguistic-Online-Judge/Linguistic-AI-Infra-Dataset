"""Production API composition for the Qwen v2 Redis submission partition."""

from __future__ import annotations

import argparse
import importlib
import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from fastapi import FastAPI

from .admin import install_admin_routes
from .api import Authenticate, create_app
from .auth import install_auth_routes
from .auth_config import build_auth_service
from .challenge import PublicChallenge
from .challenge_registry import (
    ChallengeContractRegistry,
    load_challenge_contract_registry,
    validate_contract_matches_public,
)
from .connection_config import resolve_connection_url
from .mvp_contract import EvaluationContract
from .qwen_runtime import validate_qwen_evaluation_contract
from .redis_job_queue import RedisJobQueue
from .submission_jobs import (
    QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS,
    OutboxDispatcher,
)
from .submission_store import SubmissionStoreProtocol
from .submission_store_factory import build_submission_store


@dataclass(frozen=True, slots=True)
class QwenApiRuntime:
    app: FastAPI
    contracts: Mapping[str, EvaluationContract]
    dispatchers: Mapping[str, OutboxDispatcher]
    queues: Mapping[str, RedisJobQueue]
    public_challenges: Mapping[str, PublicChallenge]
    runtime_availability: Mapping[str, bool]
    store: SubmissionStoreProtocol

    @property
    def registry(self) -> ChallengeContractRegistry:
        return ChallengeContractRegistry(self.public_challenges, self.contracts)

    def _only(self, values: Mapping[str, object], name: str):
        if len(values) != 1:
            raise RuntimeError(f"{name} is only available for a single-challenge runtime")
        return next(iter(values.values()))

    @property
    def contract(self) -> EvaluationContract:
        return cast(EvaluationContract, self._only(self.contracts, "contract"))

    @property
    def dispatcher(self) -> OutboxDispatcher:
        return cast(OutboxDispatcher, self._only(self.dispatchers, "dispatcher"))

    @property
    def queue(self) -> RedisJobQueue:
        return cast(RedisJobQueue, self._only(self.queues, "queue"))


def _resolve_contract_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _load_runtime_registry(
    root: Path,
    *,
    registry_path: Path | None,
    contract_paths: Sequence[Path] | None,
) -> tuple[dict[str, EvaluationContract], dict[str, PublicChallenge]]:
    root = root.resolve()
    if registry_path is not None and contract_paths is not None:
        raise ValueError("registry_path and contract_paths are mutually exclusive")
    default_registry = root / "config" / "challenge_contract_registry_v1.json"
    if registry_path is not None or (contract_paths is None and default_registry.exists()):
        registry = load_challenge_contract_registry(
            root,
            default_registry if registry_path is None else registry_path,
        )
        contracts = dict(registry.contracts)
        public_challenges = dict(registry.public_challenges)
    else:
        if contract_paths is not None and not contract_paths:
            raise ValueError("contract_paths must not be empty")
        paths = (
            (root / "config" / "mvp_evaluation_v2.json",)
            if contract_paths is None
            else tuple(_resolve_contract_path(root, path) for path in contract_paths)
        )
        contracts = {}
        public_challenges = {}
        for path in paths:
            contract = EvaluationContract.from_path(path)
            if contract.challenge_id in contracts:
                raise ValueError(f"duplicate challenge ID: {contract.challenge_id}")
            contracts[contract.challenge_id] = contract
            public_path = root / "challenges" / "public" / f"{contract.challenge_id}.json"
            if not public_path.exists():
                raise ValueError(
                    f"Qwen contract has no public challenge descriptor: {contract.challenge_id}"
                )
            public = PublicChallenge.model_validate_json(
                public_path.read_text(encoding="utf-8")
            )
            validate_contract_matches_public(contract, public)
            public_challenges[public.challenge_id] = public

    for contract in contracts.values():
        validate_qwen_evaluation_contract(contract)
    return contracts, public_challenges


def build_qwen_api(
    *,
    root: Path,
    challenge_registry_path: Path | None = None,
    database_path: Path | None = None,
    postgres_database_url: str | None = None,
    redis_url: str,
    authenticate: Authenticate | None = None,
    auth_config_file: Path | None = None,
    registry_path: Path | None = None,
    contract_paths: Sequence[Path] | None = None,
    namespace: str = "linguistic-oj",
    runtime_available_challenge_ids: Collection[str] | None = None,
    allow_draft_submissions: bool = False,
    environment: Literal["development", "test", "production"] = "production",
) -> QwenApiRuntime:
    """Compose one API route and Redis partition per registered Qwen contract."""

    if environment == "production" and database_path is not None:
        raise ValueError("production Qwen API requires PostgreSQL persistence")
    if (authenticate is None) == (auth_config_file is None):
        raise ValueError("configure exactly one of auth_config_file or authenticate")
    if environment == "production" and auth_config_file is None:
        raise ValueError("production Qwen API requires auth_config_file; callbacks are forbidden")
    if challenge_registry_path is not None:
        if registry_path is not None:
            raise ValueError('registry_path and challenge_registry_path are mutually exclusive')
        registry_path = challenge_registry_path
    contracts, public_challenges = _load_runtime_registry(
        root,
        registry_path=registry_path,
        contract_paths=contract_paths,
    )
    if runtime_available_challenge_ids is None:
        available_ids = set(contracts) if environment != "production" else set()
    else:
        available_ids = set(runtime_available_challenge_ids)
        unknown_ids = available_ids - set(contracts)
        if unknown_ids:
            raise ValueError(
                f"runtime availability contains unknown challenges: {sorted(unknown_ids)}"
            )
    runtime_availability = {
        challenge_id: challenge_id in available_ids for challenge_id in contracts
    }
    store = build_submission_store(
        database_path=database_path,
        postgres_database_url=postgres_database_url,
    )
    auth_service = (
        None if auth_config_file is None else build_auth_service(store, auth_config_file)
    )
    if auth_service is not None:
        # Refuse unbound legacy accounts before queue recovery or any serving-side effects.
        # Schema migration alone is not trusted account enrollment; never auto-link old users.
        auth_service.health_check()
        authenticate = auth_service.authenticate
    queues = {
        challenge_id: RedisJobQueue(
            redis_url=redis_url,
            routing_key=contract.contract_snapshot_sha256,
            visibility_timeout_seconds=(
                contract.job_deadline_seconds + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS
            ),
            namespace=namespace,
        )
        for challenge_id, contract in contracts.items()
    }
    dispatchers = {
        challenge_id: OutboxDispatcher(store, queues[challenge_id], contract)
        for challenge_id, contract in contracts.items()
    }

    def readiness_check() -> None:
        if auth_service is not None:
            auth_service.health_check()
        else:
            store.health_check()
        for queue in queues.values():
            queue.health_check()

    assert authenticate is not None
    app = create_app(
        store=store,
        dispatcher=dispatchers,
        contract=contracts,
        authenticate=authenticate,
        readiness_check=readiness_check,
        public_challenges=public_challenges,
        runtime_availability=runtime_availability,
        allow_draft_submissions=allow_draft_submissions,
        environment=environment,
    )
    if auth_service is not None:
        install_auth_routes(app, auth_service)
        install_admin_routes(app)
    return QwenApiRuntime(
        app=app,
        contracts=MappingProxyType(contracts),
        dispatchers=MappingProxyType(dispatchers),
        queues=MappingProxyType(queues),
        public_challenges=MappingProxyType(public_challenges),
        runtime_availability=MappingProxyType(runtime_availability),
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
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--database", type=Path, help="SQLite database path")
    storage.add_argument("--postgres-database-url", help="PostgreSQL database URL")
    storage.add_argument("--postgres-database-url-file", type=Path)
    redis = parser.add_mutually_exclusive_group(required=True)
    redis.add_argument("--redis-url")
    redis.add_argument("--redis-url-file", type=Path)
    routing = parser.add_mutually_exclusive_group()
    routing.add_argument(
        "--registry", "--challenge-registry",
        type=Path,
        help="challenge contract registry; defaults to config/challenge_contract_registry_v1.json",
    )
    routing.add_argument(
        "--contract",
        dest="contracts",
        type=Path,
        action="append",
        help="repeatable evaluation contract path for deployments without a registry",
    )
    authentication = parser.add_mutually_exclusive_group(required=True)
    authentication.add_argument("--auth-config-file", type=Path)
    authentication.add_argument(
        "--authenticate",
        help="development/test only: dotted callback in module:attribute form",
    )
    parser.add_argument("--namespace", default="linguistic-oj")
    parser.add_argument(
        "--runtime-available-challenge",
        dest="runtime_available_challenges",
        action="append",
        help="repeat for each challenge with an attested running worker",
    )
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
    if args.environment == "production" and args.auth_config_file is None:
        parser.error("production Qwen API requires --auth-config-file; callbacks are forbidden")
    try:
        if args.database is None:
            args.postgres_database_url = resolve_connection_url(
                'postgres', inline_url=args.postgres_database_url,
                credential_file=args.postgres_database_url_file,
                production=args.environment == 'production')
        args.redis_url = resolve_connection_url(
            'redis', inline_url=args.redis_url, credential_file=args.redis_url_file,
            production=args.environment == 'production')
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
        database_path=args.database,
        postgres_database_url=args.postgres_database_url,
        redis_url=args.redis_url,
        authenticate=None if args.authenticate is None else _load_authenticate(args.authenticate),
        auth_config_file=args.auth_config_file,
        registry_path=args.registry,
        contract_paths=args.contracts,
        namespace=args.namespace,
        runtime_available_challenge_ids=args.runtime_available_challenges,
        allow_draft_submissions=args.allow_draft_submissions,
        environment=args.environment,
    )
    uvicorn.run(runtime.app, host=args.host, port=args.port, access_log=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
