"""Production entry point for the runtime-attested Qwen Worker."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from time import sleep

from .challenge import load_challenge_artifacts
from .mvp_contract import load_qwen_worker_contract
from .providers import GenerationSettings, ModelIdentity, OpenAICompatibleProvider
from .redis_job_queue import RedisJobQueue, resolve_redis_url
from .submission_jobs import QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS, QwenSubmissionWorker
from .submission_store import SubmissionStore


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the runtime-attested Qwen submission worker."
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="deployment root containing config/mvp_evaluation_v2.json",
    )
    parser.add_argument("--database", type=Path, required=True)
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
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-sleep-seconds", type=float, default=0.25)
    args = parser.parse_args(arguments)
    if args.idle_sleep_seconds <= 0:
        parser.error("--idle-sleep-seconds must be positive")
    try:
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

    contract = load_qwen_worker_contract(args.root)
    identity = contract.evaluation_identity
    model_identity = identity.get("model_identity")
    generation_settings = identity.get("generation_settings")
    if not isinstance(model_identity, dict) or not isinstance(generation_settings, dict):
        raise ValueError("Qwen evaluation contract lacks model configuration")
    artifacts = load_challenge_artifacts(
        args.public_challenge,
        args.private_challenge,
        dataset_path=args.dataset,
    )
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
        store=SubmissionStore(args.database),
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
