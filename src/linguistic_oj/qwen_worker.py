"""Production entry point for the runtime-attested Qwen Worker."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from time import sleep

from .challenge import load_challenge_artifacts
from .challenge_registry import load_challenge_contract_registry
from .postgres_migrations import resolve_postgres_url
from .providers import GenerationSettings, ModelIdentity, OpenAICompatibleProvider
from .qwen_runtime import validate_qwen_evaluation_contract
from .redis_job_queue import RedisJobQueue, resolve_redis_url
from .submission_jobs import QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS, QwenSubmissionWorker
from .submission_store_factory import build_submission_store


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the runtime-attested Qwen submission worker."
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
    parser.add_argument("--challenge-id", required=True)
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--database", type=Path, help="SQLite database path")
    storage.add_argument("--postgres-database-url", help="PostgreSQL database URL")
    storage.add_argument("--postgres-database-url-file", type=Path)
    redis = parser.add_mutually_exclusive_group(required=True)
    redis.add_argument("--redis-url")
    redis.add_argument("--redis-url-file", type=Path)
    parser.add_argument("--public-challenge", type=Path, required=True)
    parser.add_argument("--private-challenge", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--vllm-base-url", required=True)
    parser.add_argument("--tokenizer-snapshot", type=Path, required=True)
    parser.add_argument("--launch-evidence", type=Path, required=True)
    parser.add_argument("--consumer-name")
    parser.add_argument("--namespace", default="linguistic-oj")
    parser.add_argument(
        "--environment",
        choices=("development", "test", "production"),
        default="production",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-sleep-seconds", type=float, default=0.25)
    args = parser.parse_args(arguments)
    if args.idle_sleep_seconds <= 0:
        parser.error("--idle-sleep-seconds must be positive")
    if args.environment == "production" and args.database is not None:
        parser.error("production Qwen Worker requires PostgreSQL persistence")
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
            allow_inline_credentials=False,
        )
    except ValueError as error:
        parser.error(str(error))
    return args


def build_worker(args: argparse.Namespace) -> QwenSubmissionWorker:
    """Build one fail-closed worker from deployment-owned paths and endpoints."""

    if args.environment == "production" and args.database is not None:
        raise ValueError("production Qwen Worker requires PostgreSQL persistence")
    registry = load_challenge_contract_registry(args.root, args.challenge_registry)
    public = registry.public_challenges.get(args.challenge_id)
    if public is None:
        raise ValueError(f"challenge is not registered: {args.challenge_id}")
    contract = registry.contracts.get(args.challenge_id)
    if contract is None:
        raise ValueError(f"challenge has no evaluation contract: {args.challenge_id}")
    validate_qwen_evaluation_contract(contract)
    artifacts = load_challenge_artifacts(
        args.public_challenge,
        args.private_challenge,
        dataset_path=args.dataset,
    )
    if artifacts.public != public:
        raise ValueError("configured public challenge does not match the registry")
    identity = contract.evaluation_identity
    model_identity = identity.get("model_identity")
    generation_settings = identity.get("generation_settings")
    if not isinstance(model_identity, dict) or not isinstance(generation_settings, dict):
        raise ValueError("Qwen evaluation contract lacks model configuration")
    provider = OpenAICompatibleProvider(
        base_url=args.vllm_base_url,
        identity=ModelIdentity(**model_identity),
        settings=GenerationSettings(**generation_settings),
        timeout_seconds=contract.provider_request_timeout_seconds,
        max_response_body_bytes=contract.provider_response_body_bytes,
    )
    queue = RedisJobQueue(
        redis_url=args.redis_url,
        routing_key=contract.contract_snapshot_sha256,
        visibility_timeout_seconds=(
            contract.job_deadline_seconds + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS
        ),
        consumer_name=args.consumer_name,
        namespace=args.namespace,
    )
    return QwenSubmissionWorker(
        store=build_submission_store(
            database_path=args.database,
            postgres_database_url=args.postgres_database_url,
        ),
        queue=queue,
        contract=contract,
        artifacts=artifacts,
        provider=provider,
        tokenizer_snapshot_path=args.tokenizer_snapshot,
        launch_evidence_path=args.launch_evidence,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    worker = build_worker(args)
    if args.once:
        worker.run_once()
        return 0
    while True:
        if not worker.run_once():
            sleep(args.idle_sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
