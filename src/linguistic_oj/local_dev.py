"""Loopback-only, persistent local workbench; never calls a model or external mail."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import uuid
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from .admin import install_admin_routes
from .api import APIError, create_app
from .auth import AuthService, install_auth_routes
from .challenge import build_challenge
from .mvp_contract import EvaluationContract, canonical_sha256
from .providers import (
    DeterministicMockProvider,
    deterministic_mock_generation_settings,
    deterministic_mock_model_identity,
    deterministic_mock_tokenizer_identity,
)
from .submission_jobs import InMemoryJobQueue, OutboxDispatcher, SubmissionWorker
from .submission_store import SubmissionStore

LOCAL_PASSWORD = "Local-only-passphrase-2026!"
_ACCOUNTS = (
    ("alice@example.test", "LocalAlice", "user"),
    ("bob@example.test", "LocalBob", "user"),
    ("admin@example.test", "LocalAdmin", "admin"),
)
_TASKS = (
    ("English", "upos"),
    ("English", "xpos"),
    ("English", "dependency"),
    ("Chinese", "segmentation"),
    ("Chinese", "transliteration"),
)


def _fixture_bytes() -> bytes:
    rows = []
    for language, sentences in (
        ("English", (("Cats", "sleep", "."), ("Dogs", "run", "."))),
        ("Chinese", (("\u732b", "\u7761", "\u3002"), ("\u72d7", "\u8dd1", "\u3002"))),
    ):
        for index, tokens in enumerate(sentences):
            rows.append({
                "id": f"local-{language.lower()}-{index}",
                "language": language,
                "treebank": "LocalPractice",
                "text": "".join(tokens),
                "tasks_available": [task for lang, task in _TASKS if lang == language],
                "answers": {
                    "segmentation": tokens,
                    "upos": ["NOUN", "VERB", "PUNCT"],
                    "xpos": ["NNS", "VBP", "."],
                    "dependency": [
                        [1, tokens[0], 2, tokens[1], "nsubj"],
                        [2, tokens[1], 0, "ROOT", "root"],
                        [3, tokens[2], 2, tokens[1], "punct"],
                    ],
                    "transliteration": (
                        ["mao", "shui", "\u3002"] if index == 0
                        else ["gou", "pao", "\u3002"]
                    ),
                },
            })
    return ("\n".join(json.dumps(row, ensure_ascii=True, sort_keys=True) for row in rows)
            + "\n").encode("utf-8")


def _state_directory(root: Path, value: Path | None) -> Path:
    path = root / "runtime/local-development" if value is None else value
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    runtime = (root / "runtime").resolve()
    if resolved == runtime or not resolved.is_relative_to(runtime):
        raise ValueError("local state must be a dedicated directory below project runtime/")
    return resolved


def _write_once(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as output:
            output.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(
                "local fixture/version differs; use a new isolated state directory"
            ) from None


def build_local_app(
    root: Path,
    *,
    port: int = 8080,
    state_dir: Path | None = None,
) -> FastAPI:
    """Build the actual web/API stack with tiny handcrafted, non-benchmark fixtures."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    root = root.resolve()
    state_dir = _state_directory(root, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = state_dir / "local-environment.json"
    if not marker.exists() and any(
        entry.name != ".server.lock" for entry in state_dir.iterdir()
    ):
        raise ValueError("refusing to use a nonempty, unmarked local state directory")
    _write_once(marker, b'{"environment":"loopback-mock","version":1}\n')
    dataset = state_dir / "handwritten-fixtures-v1.jsonl"
    _write_once(dataset, _fixture_bytes())
    store = SubmissionStore(state_dir / "accounts-and-runs.sqlite3")
    mailbox: deque[dict[str, str]] = deque(maxlen=100)
    mail_lock = Lock()

    def capture_mail(recipient: str, purpose: str, link: str) -> None:
        with mail_lock:
            mailbox.appendleft({
                "id": uuid.uuid4().hex,
                "recipient": recipient,
                "purpose": purpose,
                "link": link,
                "created_at": datetime.now(UTC).isoformat(),
            })

    auth = AuthService(
        store, public_origin=f"http://127.0.0.1:{port}", mailer=capture_mail, development=True
    )
    for email, handle, role in _ACCOUNTS:
        if store.auth_account(email) is None:
            auth.provision_development_account(email, LOCAL_PASSWORD, handle, role)

    contracts = {}
    public = {}
    dispatchers = {}
    workers = {}
    for language, task in _TASKS:
        artifacts = build_challenge(
            dataset, language=language, treebank="LocalPractice", task=task,
            count=2, seed=2026, version="local-dev-v1",
        )
        config = json.loads((root / "config/mvp_evaluation.json").read_text(encoding="utf-8"))
        config["contract_version"] = "mock-evaluation-v1"
        config["catalog"].update(artifacts.public.model_dump(mode="json", include={
            "challenge_id", "annotation_license", "attribution_requirements", "source_release",
            "source_commit", "source_file_sha256s", "share_alike_requirements",
            "underlying_text_rights", "status", "security_level",
        }))
        config["evaluation_identity"].update({
            "challenge_id": artifacts.public.challenge_id,
            "contract_version": "mock-evaluation-v1",
            "dataset_sha256": artifacts.public.dataset_sha256,
            "selection_sha256": artifacts.public.selection_sha256,
            "task": task,
            "response_schema_version": artifacts.public.response_schema_version,
            "model_identity": deterministic_mock_model_identity(),
            "generation_settings": deterministic_mock_generation_settings(),
            "tokenizer_identity": deterministic_mock_tokenizer_identity(),
        })
        # Local browser regression runs need more room than the frozen public contract.
        config["limits"]["submissions_per_user_per_challenge_per_24h"] = 1000
        config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
            config["evaluation_identity"]
        )
        contract = EvaluationContract.from_mapping(config)
        queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
        challenge_id = contract.challenge_id
        contracts[challenge_id] = contract
        public[challenge_id] = artifacts.public
        dispatchers[challenge_id] = OutboxDispatcher(store, queue, contract)
        workers[challenge_id] = SubmissionWorker(
            store=store, queue=queue, contract=contract, artifacts=artifacts,
            provider=DeterministicMockProvider(),
        )

    worker_status = {"running": False, "failed": False}

    def ready() -> None:
        auth.health_check()
        if not worker_status["running"] or worker_status["failed"]:
            raise RuntimeError("local worker is unavailable")

    app = create_app(
        store=store, dispatcher=dispatchers, contract=contracts, authenticate=auth.authenticate,
        readiness_check=ready, public_challenges=public, allow_draft_submissions=True,
        environment="development",
    )
    install_auth_routes(app, auth)
    install_admin_routes(app)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        stop = asyncio.Event()

        async def consume() -> None:
            try:
                while not stop.is_set():
                    for worker in workers.values():
                        await asyncio.to_thread(worker.run_once)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.1)
                    except TimeoutError:
                        pass
            except Exception:
                worker_status["failed"] = True
                logging.getLogger(__name__).error("local_mock_worker_failed")

        async with original_lifespan(application):
            task = asyncio.create_task(consume())
            worker_status["running"] = True
            try:
                yield
            finally:
                stop.set()
                await task
                worker_status["running"] = False

    app.router.lifespan_context = lifespan
    app.state.local_store = store
    app.state.local_workers = workers
    app.state.local_state_directory = state_dir

    def development_guard(request: Request) -> None:
        if request.headers.getlist("x-loj-development") != ["1"] or (
            request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise APIError(403, code="LOCAL_ACCESS_REQUIRED", message="Local tools only")

    @app.get("/v1/development", include_in_schema=False)
    def development(request: Request) -> JSONResponse:
        development_guard(request)
        return JSONResponse({
            "evaluation_mode": "mock",
            "mail_delivery": "local",
            "accounts": [
                {"email": email, "public_handle": handle, "role": role, "password": LOCAL_PASSWORD}
                for email, handle, role in _ACCOUNTS
            ],
        }, headers={"Cache-Control": "no-store"})

    @app.get("/v1/development/mail", include_in_schema=False)
    def mail(request: Request) -> JSONResponse:
        development_guard(request)
        with mail_lock:
            items = list(mailbox)
        return JSONResponse({"items": items}, headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        })

    return app


@contextmanager
def _state_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".server.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("this local state directory is already in use") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the loopback-only local Mock workbench.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args(arguments)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        import uvicorn
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", args.port))
            except OSError:
                raise RuntimeError(
                    f"port {args.port} is unavailable; no other process was stopped"
                ) from None
            root = args.root.resolve()
            state_dir = _state_directory(root, args.state_dir)
            with _state_lock(state_dir):
                app = build_local_app(root, port=args.port, state_dir=state_dir)
                listener.listen(128)
                print(f"Local workbench: http://127.0.0.1:{args.port}/", flush=True)
                print("MOCK scores only. Test mail stays in the local testing panel.", flush=True)
                server = uvicorn.Server(uvicorn.Config(
                    app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False,
                ))
                server.run(sockets=[listener])
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Local startup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
