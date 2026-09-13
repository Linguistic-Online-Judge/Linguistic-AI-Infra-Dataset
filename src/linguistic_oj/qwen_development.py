"""Private, co-located Qwen workbench: 18 real languages, persistent isolated state.

This is an explicit developer-only composition, not a production activation path.
It never launches a model, rewrites a frozen contract, or falls back to Mock scores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import socket
import sys
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlencode
from urllib.request import getproxies

from fastapi import Request
from starlette.responses import JSONResponse

from .admin import install_admin_routes
from .api import APIError, create_app
from .auth import AuthService, install_auth_routes
from .challenge import LANGUAGE_CODES, load_challenge_artifacts
from .challenge_registry import load_challenge_contract_registry
from .local_dev import _ACCOUNTS, LOCAL_PASSWORD, _state_lock
from .postgres_migrations import migrate_postgres
from .postgres_submission_store import PostgresSubmissionStore
from .providers import GenerationSettings, ModelIdentity, OpenAICompatibleProvider
from .redis_job_queue import RedisJobQueue
from .submission_jobs import (
    QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS,
    OutboxDispatcher,
    QwenSubmissionWorker,
)

REQUIRED_LANGUAGES = frozenset(LANGUAGE_CODES)
INSTANCE_KIND = "linguistic-oj-private-qwen18-v1"


def load_development_catalog(
    root: Path, data_root: Path,
    registry_path: Path = Path("config/challenge_contract_registry_v1.json"),
):
    registry = load_challenge_contract_registry(
        root, registry_path
    )
    languages = {
        registry.public_challenges[key].language
        for key, contract in registry.contracts.items()
        if contract.evaluation_identity["task"] == "upos"
    }
    if languages != REQUIRED_LANGUAGES:
        raise ValueError("real development requires an executable UPOS task in all 18 languages")
    artifacts = {}
    for key, contract in registry.contracts.items():
        if contract.evaluation_identity["model_identity"]["model"] != "Qwen/Qwen3.5-9B":
            raise ValueError("the development workbench only accepts Qwen3.5-9B")
        public = registry.public_challenges[key]
        matches = list(
            (data_root / "Standard_Dataset/by_language").glob(f"{public.language}_*.jsonl")
        )
        if len(matches) != 1:
            raise ValueError(f"expected exactly one dataset for {public.language}")
        selected = load_challenge_artifacts(
            root / "challenges/public" / f"{key}.json",
            data_root / "runtime/private/challenges" / f"{key}.json",
            dataset_path=matches[0],
        )
        if selected.public != public:
            raise ValueError("configured development artifacts differ from the loaded registry")
        artifacts[key] = selected
    return registry, artifacts


def prepare_store(
    state_dir: Path,
    *,
    pg_socket: Path,
    pg_port: int,
    initialize: bool,
    contract_hashes: dict[str, str],
):
    """Only initialize/reopen an explicitly marked, dedicated development database."""
    import pwd

    import psycopg
    from psycopg import sql

    if any(key.startswith("PG") and value for key, value in os.environ.items()):
        raise ValueError(
            "unset implicit PostgreSQL environment variables for this development service"
        )
    marker_file = state_dir / "instance.json"
    owner = pwd.getpwuid(os.geteuid()).pw_name
    if not marker_file.exists():
        if not initialize:
            raise ValueError(
                "first startup requires --initialize in an empty private state directory"
            )
        if any(p.name != ".server.lock" for p in state_dir.iterdir()):
            raise ValueError("refusing to initialize an unmarked nonempty state directory")
        marker = {
            "kind": INSTANCE_KIND,
            "instance": uuid.uuid4().hex,
            "owner": owner,
            "contracts": contract_hashes,
        }
        marker["database"] = "loj_dev18_" + marker["instance"][:16]
        with marker_file.open("x", encoding="utf-8") as output:
            json.dump(marker, output, indent=2, sort_keys=True)
        marker_file.chmod(0o600)
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    if (
        marker.get("kind") != INSTANCE_KIND
        or marker.get("owner") != owner
        or not re.fullmatch(r"[0-9a-f]{32}", marker.get("instance", ""))
        or marker.get("database") != "loj_dev18_" + marker["instance"][:16]
        or marker.get("contracts") != contract_hashes
    ):
        raise ValueError("development instance ownership or contract snapshot changed")
    passfile = state_dir / "empty.pgpass"
    if not passfile.exists():
        with passfile.open("x"):
            pass
        passfile.chmod(0o600)
    expected_comment = INSTANCE_KIND + ":" + marker["instance"]
    with psycopg.connect(
        host=str(pg_socket),
        port=pg_port,
        user=owner,
        dbname="postgres",
        password="",
        passfile=str(passfile),
        connect_timeout=5,
        autocommit=True,
    ) as connection:
        row = connection.execute(
            "SELECT pg_get_userbyid(datdba), shobj_description(oid,'pg_database') "
            "FROM pg_database WHERE datname=%s",
            (marker["database"],),
        ).fetchone()
        if row is None:
            if not initialize:
                raise ValueError(
                    "development database is missing; explicit initialization required"
                )
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(marker["database"]),
                    sql.Identifier(owner),
                )
            )
            connection.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(marker["database"]),
                    sql.Literal(expected_comment),
                )
            )
        elif row != (owner, expected_comment):
            raise ValueError("refusing to use a database without matching development ownership")
    query = urlencode({"host": str(pg_socket), "port": pg_port, "passfile": str(passfile)})
    url = f"postgresql://{quote(owner, safe='')}@/{marker['database']}?{query}"
    if initialize:
        migrate_postgres(url, applied_at=datetime.now(UTC).isoformat())
    store = PostgresSubmissionStore(url)
    store.health_check()
    return store, marker


def build_qwen_development(args):
    if (args.state_dir / "uncertain-inference.json").exists():
        raise ValueError("confirm termination of the previous model request before restarting")
    root = args.root.resolve()
    registry, artifacts = load_development_catalog(
        root, args.data_root.resolve(),
        getattr(args, "registry", Path("config/challenge_contract_registry_v1.json")),
    )
    hashes = {key: value.contract_snapshot_sha256 for key, value in registry.contracts.items()}
    store, marker = prepare_store(
        args.state_dir,
        pg_socket=args.postgres_socket.resolve(),
        pg_port=args.postgres_port,
        initialize=args.initialize,
        contract_hashes=hashes,
    )
    mailbox = deque(maxlen=100)
    mail_lock = Lock()

    def capture_mail(recipient, purpose, link):
        with mail_lock:
            mailbox.appendleft(
                {
                    "id": uuid.uuid4().hex,
                    "recipient": recipient,
                    "purpose": purpose,
                    "link": link,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )

    origin = f"http://127.0.0.1:{args.port}"
    auth = AuthService(
        store,
        public_origin=origin,
        mailer=capture_mail,
        development=True,
        development_cookie_name="loj_qwen_dev_session",
    )
    for email, handle, role in _ACCOUNTS:
        if store.auth_account(email) is None:
            auth.provision_development_account(email, LOCAL_PASSWORD, handle, role)
    workers, dispatchers, queues, providers = {}, {}, {}, {}
    try:
        for key, contract in registry.contracts.items():
            identity = contract.evaluation_identity
            provider = OpenAICompatibleProvider(
                base_url="http://127.0.0.1:8000/v1",
                identity=ModelIdentity(**identity["model_identity"]),
                settings=GenerationSettings(**identity["generation_settings"]),
                timeout_seconds=contract.provider_request_timeout_seconds,
                max_response_body_bytes=contract.provider_response_body_bytes,
            )
            providers[key] = provider
            redis_url = f"unix://{quote(str(args.redis_socket.resolve()), safe='/')}?db=15"
            queue = RedisJobQueue(
                redis_url=redis_url,
                routing_key=contract.contract_snapshot_sha256,
                namespace="loj-qwen-dev:" + marker["instance"],
                consumer_name="serial-development",
                visibility_timeout_seconds=contract.job_deadline_seconds
                + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS,
            )
            queues[key] = queue
            workers[key] = QwenSubmissionWorker(
                store=store,
                queue=queue,
                contract=contract,
                artifacts=artifacts[key],
                provider=provider,
                tokenizer_snapshot_path=args.tokenizer_snapshot.resolve(),
                launch_evidence_path=args.launch_evidence.resolve(),
            )
            dispatchers[key] = OutboxDispatcher(store, queue, contract)
    except Exception:
        for queue in queues.values():
            queue.close()
        raise

    status = {"running": False, "failed": False}

    def ready():
        store.health_check()
        if not status["running"] or status["failed"]:
            raise RuntimeError("serial Qwen worker is unavailable")
        for queue in queues.values():
            queue.health_check()

    app = create_app(
        store=store,
        dispatcher=dispatchers,
        contract=registry.contracts,
        public_challenges=registry.public_challenges,
        authenticate=auth.authenticate,
        readiness_check=ready,
        allow_draft_submissions=True,
        environment="development",
        runtime_availability=dict.fromkeys(workers, True),
    )
    install_auth_routes(app, auth)
    install_admin_routes(app)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        stop = asyncio.Event()

        async def consume():
            # One loop across all queues: never launch 22 concurrent GPU workers.
            try:
                while not stop.is_set():
                    for key, worker in workers.items():
                        if stop.is_set():
                            break
                        await asyncio.to_thread(worker.run_once)
                        if providers[key].has_active_request:
                            with (args.state_dir / "uncertain-inference.json").open("x") as warning:
                                json.dump(
                                    {
                                        "challenge_id": key,
                                        "reason": "termination_unconfirmed",
                                        "created_at": datetime.now(UTC).isoformat(),
                                    },
                                    warning,
                                )
                            raise RuntimeError("previous model request termination is unconfirmed")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.25)
                    except TimeoutError:
                        pass
            except Exception:
                status["failed"] = True
                application.state.admin_context["runtime_availability"].update(
                    dict.fromkeys(workers, False)
                )
                logging.getLogger(__name__).error("qwen_development_worker_failed")

        try:
            async with original_lifespan(application):
                status["running"] = True
                task = asyncio.create_task(consume())
                try:
                    yield
                finally:
                    stop.set()
                    await task
                    status["running"] = False
        finally:
            for queue in queues.values():
                queue.close()

    app.router.lifespan_context = lifespan

    def guard(request):
        if request.headers.getlist("x-loj-development") != ["1"] or (
            request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise APIError(
                403, code="LOCAL_ACCESS_REQUIRED", message="Private developer tools only"
            )

    @app.get("/v1/development", include_in_schema=False)
    def development(request: Request):
        guard(request)
        return JSONResponse(
            {
                "evaluation_mode": "qwen",
                "mail_delivery": "local",
                "model": "Qwen/Qwen3.5-9B",
                "languages": sorted(REQUIRED_LANGUAGES),
                "language_count": len(REQUIRED_LANGUAGES),
                "executable_challenges": len(workers),
                "catalog_challenges": len(registry.public_challenges),
                "accounts": [
                    {
                        "email": email,
                        "public_handle": handle,
                        "role": role,
                        "password": LOCAL_PASSWORD,
                    }
                    for email, handle, role in _ACCOUNTS
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/development/mail", include_in_schema=False)
    def mail(request: Request):
        guard(request)
        with mail_lock:
            items = list(mailbox)
        return JSONResponse({"items": items}, headers={"Cache-Control": "no-store"})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in (
        "root",
        "data-root",
        "state-dir",
        "postgres-socket",
        "redis-socket",
        "tokenizer-snapshot",
        "launch-evidence",
    ):
        parser.add_argument("--" + field, required=True, type=Path)
    parser.add_argument("--postgres-port", type=int, default=5433)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--registry", type=Path,
                        default=Path("config/challenge_contract_registry_v1.json"))
    args = parser.parse_args()
    if os.name != "posix":
        parser.error(
            "run beside Qwen on the Linux server; forward the app port, not the model port"
        )
    if not 1 <= args.port <= 65535 or not 1 <= args.postgres_port <= 65535:
        parser.error("ports must be in 1..65535")
    try:
        import uvicorn

        if any(value for key, value in getproxies().items() if key != "no"):
            raise ValueError(
                "proxy environment variables must not redirect the local model service"
            )
        args.state_dir = args.state_dir.resolve()
        if not args.state_dir.is_dir() or args.state_dir.stat().st_mode & 0o077:
            raise ValueError("state-dir must be an existing owner-only directory")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", args.port))
            listener.listen(128)
            with _state_lock(args.state_dir):
                app = build_qwen_development(args)
                print(f"Private Qwen18 workbench: http://127.0.0.1:{args.port}/", flush=True)
                uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host="127.0.0.1",
                        port=args.port,
                        access_log=False,
                        proxy_headers=False,
                    )
                ).run(sockets=[listener])
        return 0
    except Exception as error:
        print(f"Qwen development startup failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
