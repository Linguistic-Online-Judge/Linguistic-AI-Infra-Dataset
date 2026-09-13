"""Opt-in, co-located Qwen acceptance, not a deployment or a benchmark.

Run on the school server with Python 3.11+ and .[postgres,qwen-worker,dev].
--root must be an absolute, non-symlink snapshot containing this script and src/.
The existing tokenizer and operator launch evidence are read-only inputs. No
downloads, SMTP, service management, public registry edits, or production scores.

An existing owner-only directory OUTSIDE Git is required for --output (new file,
0600). Its temporary child holds only two handwritten UPOS samples. The report
contains aggregates and fingerprints, never credentials or request/response text.
The app is HTTP-inprocess TestClient with real cookie auth, NOT a browser/TLS test.

PostgreSQL needs CONNECT and CREATE SCHEMA in the selected existing database.
Only a freshly created schema receives the repository's migration SQL. Cleanup
checks schema OID, owner and a random creation marker, drops its tables together
with RESTRICT, then drops the empty schema with RESTRICT, never CASCADE. The
existing store's short database-wide advisory locks still apply on a shared DB.
Redis needs Streams/Lua access on the supplied existing Unix socket. DB 15 need
NOT be empty: four exact UUID-namespaced keys are reserved/cleaned with a marker.
No SCAN, KEYS, FLUSHDB, or deletion of keys found by a prefix search is used.

Normal failures and SIGINT/SIGTERM attempt all cleanup and return nonzero if any
cleanup is unconfirmed. SIGKILL, process/host failure, lost connections, or changed
ownership cannot guarantee cleanup. The protected report records exact namespace
identifiers for operator inspection; never rerun cleanup against a guessed name.
One worker.run_once(), at most two provider.generate() calls, no retry loop. A
timed-out vLLM request may finish remotely; this script never stops that service.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import re
import secrets
import signal
import socket
import stat
import sys
import tempfile
import uuid
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, getproxies

VLLM_URL = "http://127.0.0.1:8000/v1"
_SCHEMA = re.compile(r"qwen_accept_u[0-9]+_[0-9a-f]{32}")
_CATALOG_FIELDS = {
    "challenge_id",
    "annotation_license",
    "attribution_requirements",
    "source_release",
    "source_commit",
    "source_file_sha256s",
    "share_alike_requirements",
    "underlying_text_rights",
    "status",
    "security_level",
}
_PROMPT = (
    "Assign Universal Dependencies UPOS tags to the supplied tokens in order. "
    "Return only a JSON object with a tags array, one tag per token. No explanation."
)


class CheckFailed(RuntimeError):
    """Only constant, non-sensitive check names belong in this exception."""


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def exact_path(path: Path, *, exists=True) -> Path:
    require(path.is_absolute() and ".." not in path.parts, "absolute_exact_path_required")
    for item in (path, *path.parents):
        require(not item.is_symlink(), "symlink_path_rejected")
    if exists:
        require(path.exists(), "required_path_missing")
    return path


def protected_file(path: Path) -> bytes:
    exact_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "secret_file_not_regular")
        require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, "secret_file_not_private")
        value = handle.read(8193)
    require(len(value) <= 8192, "secret_file_too_large")
    return value


def validate_output(root: Path, output: Path) -> None:
    exact_path(output, exists=False)
    parent = exact_path(output.parent)
    require(parent.is_dir() and not output.exists(), "report_must_be_new")
    require(not output.is_relative_to(root), "report_inside_snapshot")
    require(not any((p / ".git").exists() for p in (parent, *parent.parents)), "report_inside_git")
    info = parent.stat()
    require(
        info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, "report_directory_not_private"
    )


def load_snapshot(root: Path) -> None:
    exact_path(root)
    require(root.is_dir() and (root / "src/linguistic_oj").is_dir(), "snapshot_missing_source")
    require(
        Path(__file__).resolve() == root / "scripts/check_qwen_pipeline.py",
        "script_not_from_requested_snapshot",
    )
    for name, module in tuple(sys.modules.items()):
        if name == "linguistic_oj" or name.startswith("linguistic_oj."):
            require(
                Path(module.__file__).resolve().is_relative_to(root / "src"),
                "application_already_loaded_from_other_snapshot",
            )
    sys.path.insert(0, str(root / "src"))
    package = importlib.import_module("linguistic_oj")
    require(
        Path(package.__file__).resolve().is_relative_to(root / "src"), "wrong_application_snapshot"
    )


def postgres_parameters(args) -> dict:
    from psycopg.conninfo import conninfo_to_dict

    require(
        not any(k.startswith("PG") and v for k, v in os.environ.items()),
        "libpq_environment_must_be_unset",
    )
    if args.postgres_url_file is not None:
        value = protected_file(args.postgres_url_file).decode("utf-8").strip()
        require(value.startswith(("postgresql://", "postgres://")), "postgres_url_required")
        params = conninfo_to_dict(value)
        require(
            set(params) <= {"host", "port", "user", "password", "dbname"},
            "postgres_url_options_rejected",
        )
    else:
        directory = exact_path(args.postgres_socket)
        require(directory.is_dir(), "postgres_socket_directory_required")
        params = {
            "host": str(directory),
            "port": str(args.postgres_port or 5433),
            "user": args.postgres_user,
            "dbname": args.postgres_database or "postgres",
        }
    require(
        all(params.get(k) for k in ("host", "user", "dbname")),
        "explicit_postgres_host_user_database_required",
    )
    host = params["host"]
    require("," not in host, "postgres_multihost_rejected")
    if host.startswith("/"):
        exact_path(Path(host))
        require(Path(host).is_dir(), "postgres_socket_directory_required")
    else:
        require(host in {"127.0.0.1", "::1"}, "postgres_must_be_colocated")
    port = int(params.get("port", "5433"))
    require(1 <= port <= 65535, "postgres_port_invalid")
    params.update(port=port, password=params.get("password", ""), passfile="/dev/null")
    return params


class OwnedPostgres:
    def __init__(self, params, cleanup):
        import psycopg

        self.factory = psycopg.connect
        self.params = params
        self.schema = f"qwen_accept_u{os.getuid()}_{uuid.uuid4().hex}"
        self.marker = "qwen-acceptance-v1:" + secrets.token_hex(32)
        self.proof = None
        self.attempted = False
        self.cleanup = cleanup

    def connect(self, *, scoped=True):
        from linguistic_oj.postgres_migrations import POSTGRES_SESSION_OPTIONS

        require(_SCHEMA.fullmatch(self.schema) is not None, "unsafe_schema_name")
        search = f"{self.schema},pg_catalog,pg_temp" if scoped else "pg_catalog"
        return self.factory(
            **self.params,
            connect_timeout=5,
            options=POSTGRES_SESSION_OPTIONS + f" -c search_path={search}",
        )

    def schema_proof(self, connection):
        return connection.execute(
            "SELECT oid, nspowner, obj_description(oid, 'pg_namespace') "
            "FROM pg_catalog.pg_namespace WHERE nspname = %s",
            (self.schema,),
        ).fetchone()

    def create(self):
        from psycopg import sql

        from linguistic_oj.postgres_migrations import _POSTGRES_MIGRATIONS
        from linguistic_oj.postgres_submission_store import PostgresSubmissionStore

        with self.connect(scoped=False) as connection:
            require(self.schema_proof(connection) is None, "schema_collision")
            self.attempted = True
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            connection.execute(
                sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                    sql.Identifier(self.schema), sql.Literal(self.marker)
                )
            )
            self.proof = self.schema_proof(connection)
        require(self.proof is not None and self.proof[2] == self.marker, "schema_creation_unproven")
        with self.connect() as connection:
            require(self.schema_proof(connection) == self.proof, "schema_ownership_changed")
            require(
                connection.execute("SELECT current_schema()").fetchone() == (self.schema,),
                "wrong_migration_search_path",
            )
            require(
                connection.execute(
                    "SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace = %s",
                    (self.proof[0],),
                ).fetchone()
                == (0,),
                "new_schema_not_empty",
            )
            # Execute the real migration definitions, only on our proven empty schema.
            # Do not patch psycopg.connect or call the production URL-only migrator.
            for version, migration in sorted(_POSTGRES_MIGRATIONS.items()):
                connection.execute(migration)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (%s, %s)",
                    (version, datetime.now(UTC).isoformat()),
                )

        scoped_connect = self.connect

        class IsolatedPostgresStore(PostgresSubmissionStore):
            def _connect(self):
                return scoped_connect()

        # The base URL is validation-only; every connection uses the local factory above.
        store = IsolatedPostgresStore("postgresql:///qwen_acceptance_factory_only")
        store.health_check()
        return store

    def close(self):
        from psycopg import sql

        if self.proof is None:
            if self.attempted:
                with self.connect(scoped=False) as connection:
                    require(self.schema_proof(connection) is None, "schema_creation_unconfirmed")
            self.cleanup["postgres"] = {
                "confirmed": True,
                "schemas_dropped": 0,
                "tables_dropped": 0,
            }
            return
        require(_SCHEMA.fullmatch(self.schema) is not None, "unsafe_schema_cleanup")
        with self.connect(scoped=False) as connection:
            proof = self.schema_proof(connection)
            if proof is None:
                self.cleanup["postgres"] = {
                    "confirmed": True,
                    "schemas_dropped": 0,
                    "tables_dropped": 0,
                }
                return
            require(proof == self.proof and proof[2] == self.marker, "schema_cleanup_proof_changed")
            tables = connection.execute(
                "SELECT relname FROM pg_catalog.pg_class "
                "WHERE relnamespace = %s AND relkind IN ('r', 'p') ORDER BY relname",
                (proof[0],),
            ).fetchall()
            if tables:
                # RESTRICT fails/rolls back if anything outside our tables depends on them.
                connection.execute(
                    sql.SQL("DROP TABLE {} RESTRICT").format(
                        sql.SQL(", ").join(sql.Identifier(self.schema, row[0]) for row in tables)
                    )
                )
            connection.execute(
                sql.SQL("DROP SCHEMA {} RESTRICT").format(sql.Identifier(self.schema))
            )
        with self.connect(scoped=False) as connection:
            require(self.schema_proof(connection) is None, "schema_cleanup_unconfirmed")
        self.cleanup["postgres"] = {
            "confirmed": True,
            "schemas_dropped": 1,
            "tables_dropped": len(tables),
        }


_RESERVE_REDIS = """
for _, key in ipairs(KEYS) do
    if redis.call('EXISTS', key) ~= 0 then return 0 end
end
redis.call('SET', KEYS[4], ARGV[1])
redis.call('XGROUP', 'CREATE', KEYS[1], ARGV[2], '0-0', 'MKSTREAM')
return 1
"""
_CLEAN_REDIS = """
if redis.call('GET', KEYS[4]) ~= ARGV[1] then return -1 end
return redis.call('DEL', KEYS[1], KEYS[2], KEYS[3], KEYS[4])
"""


class OwnedRedis:
    def __init__(self, socket_path, database, contract, cleanup):
        from redis import Redis

        self.namespace = "qwen-acceptance:" + uuid.uuid4().hex
        self.routing_key = contract.contract_snapshot_sha256
        stream = f"{self.namespace}:jobs:{{{self.routing_key}}}"
        self.keys = (stream, stream + ":active", stream + ":receipts", stream + ":owner")
        self.marker = secrets.token_hex(32)
        self.url = f"unix://{quote(str(socket_path), safe='/')}?db={database}"
        self.client = Redis.from_url(
            self.url, socket_connect_timeout=5, socket_timeout=5, decode_responses=False
        )
        self.queue = None
        self.attempted = False
        self.cleanup = cleanup

    def create(self, contract):
        from linguistic_oj.redis_job_queue import RedisJobQueue
        from linguistic_oj.submission_jobs import QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS

        self.attempted = True
        require(
            self.client.eval(_RESERVE_REDIS, 4, *self.keys, self.marker, "submission-workers-v1")
            == 1,
            "redis_namespace_collision",
        )
        self.queue = RedisJobQueue(
            redis_url=self.url,
            routing_key=self.routing_key,
            namespace=self.namespace,
            consumer_name="qwen-acceptance",
            visibility_timeout_seconds=(
                contract.job_deadline_seconds + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS
            ),
        )
        require(
            self.queue.stream_name == self.keys[0]
            and self.queue._active_key == self.keys[1]
            and self.queue._receipt_key == self.keys[2],
            "redis_queue_key_contract_changed",
        )
        return self.queue

    def close(self):
        try:
            if self.queue is not None:
                self.queue.close()
            count = 0
            if self.attempted:
                marker = self.client.get(self.keys[3])
                if marker == self.marker.encode():
                    count = self.client.eval(_CLEAN_REDIS, 4, *self.keys, self.marker)
                    require(0 <= count <= 4, "redis_cleanup_proof_changed")
                    require(self.client.exists(*self.keys) == 0, "redis_cleanup_unconfirmed")
                else:
                    require(self.client.exists(*self.keys) == 0, "redis_cleanup_not_owned")
            self.cleanup["redis"] = {"confirmed": True, "keys_deleted": count}
        finally:
            self.client.close()


def build_fixture(root: Path, directory: Path, run_id: str):
    from linguistic_oj.challenge import ChallengeArtifacts, build_challenge
    from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256

    dataset = directory / "handwritten-upos.jsonl"
    rows = [
        {
            "id": f"handwritten-{index}",
            "language": "English",
            "treebank": "QwenAcceptance",
            "text": " ".join(tokens),
            "tasks_available": ["upos"],
            "answers": {"segmentation": tokens, "upos": ["NOUN", "VERB", "PUNCT"]},
        }
        for index, tokens in enumerate((["Cats", "sleep", "."], ["Dogs", "run", "."]))
    ]
    with dataset.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    artifacts = build_challenge(
        dataset,
        language="English",
        treebank="QwenAcceptance",
        task="upos",
        count=2,
        seed=2026,
        version="accept-" + run_id,
    )
    public = artifacts.public.model_copy(
        update={
            "benchmark_limitations": "Two handwritten acceptance samples; not a benchmark.",
        }
    )
    artifacts = ChallengeArtifacts(public, artifacts.private, dataset)
    source = exact_path(root / "config/mvp_evaluation_v2.json").read_bytes()
    config = json.loads(source)
    base = EvaluationContract.from_mapping(config)
    require(
        base.contract_version == "mvp-evaluation-v2"
        and base.evaluation_identity["model_identity"]["model"] == "Qwen/Qwen3.5-9B"
        and base.model_context_tokens == 4096
        and base.provider_request_timeout_seconds == 120
        and base.provider_response_body_bytes == 32768
        and base.evaluation_identity["generation_settings"]["max_tokens"] == 256,
        "unexpected_frozen_qwen_contract",
    )
    config["catalog"].update(public.model_dump(mode="json", include=_CATALOG_FIELDS))
    config["evaluation_identity"].update(
        {
            "challenge_id": public.challenge_id,
            "dataset_sha256": public.dataset_sha256,
            "selection_sha256": public.selection_sha256,
        }
    )
    config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        config["evaluation_identity"]
    )
    contract = EvaluationContract.from_mapping(config)
    return (
        artifacts,
        contract,
        {
            "source_contract_file_sha256": hashlib.sha256(source).hexdigest(),
            "source_contract_snapshot_sha256": base.contract_snapshot_sha256,
            "contract_snapshot_sha256": contract.contract_snapshot_sha256,
            "evaluation_identity_sha256": contract.evaluation_identity_sha256,
            "dataset_sha256": public.dataset_sha256,
            "selection_sha256": public.selection_sha256,
            "model_identity_sha256": canonical_sha256(
                contract.evaluation_identity["model_identity"]
            ),
            "tokenizer_identity_sha256": canonical_sha256(
                contract.evaluation_identity["tokenizer_identity"]
            ),
            "tokenizer_artifact_sha256s": {
                key: value
                for key, value in contract.evaluation_identity["tokenizer_identity"].items()
                if key.endswith("sha256")
            },
            "model": "Qwen/Qwen3.5-9B",
            "samples": 2,
            "gold_items": 6,
            "draft_override": True,
            "external_activation_ready": contract.external_activation_ready,
        },
    )


def build_provider(contract):
    from linguistic_oj.providers import (
        GenerationSettings,
        ModelIdentity,
        OpenAICompatibleProvider,
        ProviderContractError,
    )

    class BoundedQwenProvider(OpenAICompatibleProvider):
        generation_calls = 0

        def generate(self, request, /, *, timeout_seconds=None):
            if self.generation_calls >= 2:
                raise ProviderContractError("acceptance generation budget exhausted")
            self.generation_calls += 1
            return super().generate(request, timeout_seconds=timeout_seconds)

    return BoundedQwenProvider(
        base_url=VLLM_URL,
        identity=ModelIdentity(**contract.evaluation_identity["model_identity"]),
        settings=GenerationSettings(**contract.evaluation_identity["generation_settings"]),
        timeout_seconds=contract.provider_request_timeout_seconds,
        max_response_body_bytes=contract.provider_response_body_bytes,
    )


def check_launch(args, contract):
    from linguistic_oj.qwen_runtime import QwenLaunchEvidence, TokenizerIdentity

    exact_path(args.tokenizer_snapshot)
    exact_path(args.launch_evidence)
    require(args.launch_evidence.stat().st_size <= 8192, "launch_evidence_too_large")
    launch = QwenLaunchEvidence.from_path(args.launch_evidence)
    expected = TokenizerIdentity.from_mapping(contract.evaluation_identity["tokenizer_identity"])
    actual = TokenizerIdentity.from_snapshot(
        args.tokenizer_snapshot,
        repository=expected.repository,
        revision=expected.revision,
        add_generation_prompt=expected.add_generation_prompt,
        enable_thinking=expected.enable_thinking,
    )
    require(actual == expected, "tokenizer_artifacts_mismatch")
    require(
        launch.model_snapshot_path == args.tokenizer_snapshot
        and launch.max_model_len == contract.model_context_tokens
        and launch.max_num_seqs == contract.worker_model_concurrency
        and launch.language_model_only
        and launch.runtime_version
        == contract.evaluation_identity["model_identity"]["runtime_version"],
        "operator_launch_evidence_mismatch",
    )

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise CheckFailed("model_metadata_redirect_rejected")

    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(
        Request(VLLM_URL + "/models"), timeout=contract.provider_request_timeout_seconds
    ) as response:
        body = response.read(contract.provider_response_body_bytes + 1)
    require(len(body) <= contract.provider_response_body_bytes, "model_metadata_too_large")
    models = [
        item
        for item in json.loads(body)["data"]
        if item.get("id") == contract.evaluation_identity["model_identity"]["model"]
    ]
    require(
        len(models) == 1
        and isinstance(models[0].get("root"), str)
        and Path(models[0]["root"]).is_absolute()
        and Path(models[0]["root"]).resolve() == args.tokenizer_snapshot.resolve()
        and models[0].get("max_model_len") == contract.model_context_tokens,
        "live_model_root_or_context_mismatch",
    )


def checked(response, status=200, code=None):
    require(response.status_code == status, "unexpected_http_status")
    value = response.json()
    if code is not None:
        require(value.get("error", {}).get("code") == code, "unexpected_http_error_code")
    return value


def exercise_http(store, queue, worker, contract, artifacts, report, *, queue_probe):
    from fastapi.testclient import TestClient

    from linguistic_oj.admin import install_admin_routes
    from linguistic_oj.api import create_app
    from linguistic_oj.auth import AuthService, install_auth_routes
    from linguistic_oj.submission_jobs import OutboxDispatcher

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{reservation.getsockname()[1]}"
        headers = {"Origin": origin, "X-LOJ-CSRF": "1"}
        messages = []
        auth = AuthService(
            store,
            public_origin=origin,
            development=True,
            mailer=lambda *message: messages.append(message),
        )
        dispatcher = OutboxDispatcher(store, queue, contract)

        def ready():
            auth.health_check()
            queue.health_check()

        def app(override):
            application = create_app(
                store=store,
                dispatcher=dispatcher,
                contract=contract,
                authenticate=auth.authenticate,
                readiness_check=ready,
                public_challenges={contract.challenge_id: artifacts.public},
                runtime_availability={contract.challenge_id: True},
                environment="test",
                allow_draft_submissions=override,
            )
            install_auth_routes(application, auth)
            install_admin_routes(application)
            return application

        application = app(True)
        payload = {"challenge_id": contract.challenge_id, "student_prompt": _PROMPT}
        idem = "acceptance-" + uuid.uuid4().hex
        with (
            TestClient(
                application, base_url=origin, headers=headers, client=("127.0.0.1", 50001)
            ) as owner,
            TestClient(
                application, base_url=origin, headers=headers, client=("127.0.0.1", 50002)
            ) as other,
        ):
            checked(owner.get("/health/ready"))
            checked(owner.get("/v1/submissions"), 401, "AUTH_INVALID_TOKEN")
            users = []
            for index, client in enumerate((owner, other)):
                email = f"accept-{uuid.uuid4().hex}@example.test"
                password = secrets.token_urlsafe(32)
                handle = "Accept" + str(index) + uuid.uuid4().hex[:12]
                checked(client.post("/v1/auth/register", json={"email": email}), 202)
                require(len(messages) == 1, "registration_mail_not_captured")
                recipient, purpose, link = messages.pop()
                require(recipient == email and purpose == "verify-email", "wrong_captured_mail")
                token = parse_qs(urlsplit(link).fragment.partition("?")[2])["token"][0]
                verification = {"token": token, "password": password, "public_handle": handle}
                checked(
                    client.post("/v1/auth/login", json={"email": email, "password": password}),
                    401,
                    "AUTH_INVALID_CREDENTIALS",
                )
                checked(client.post("/v1/auth/verify-email", json=verification))
                checked(
                    client.post("/v1/auth/verify-email", json=verification),
                    401,
                    "AUTH_INVALID_TOKEN",
                )
                login = client.post("/v1/auth/login", json={"email": email, "password": password})
                user = checked(login)["user"]
                cookie = login.headers.get("set-cookie", "").lower()
                require(
                    "httponly" in cookie
                    and "samesite=strict" in cookie
                    and "path=/" in cookie
                    and "secure" not in cookie
                    and client.cookies.get(auth.cookie_name),
                    "development_cookie_missing",
                )
                require(
                    user["role"] == "user" and user["public_handle"] == handle,
                    "unexpected_registered_account",
                )
                require(
                    checked(client.get("/v1/auth/session"))["user"] == user,
                    "session_account_mismatch",
                )
                require(
                    checked(client.get("/v1/users/me"))["user_id"] == user["user_id"],
                    "current_user_mismatch",
                )
                require(store.auth_account(email).user_id == user["user_id"], "auth_owner_mismatch")
                users.append(user)
            require(users[0]["user_id"] != users[1]["user_id"], "accounts_not_distinct")
            report["checks"]["registration_verification_replay_login_cookie"] = True
            report["stage"] = "http_submission_guards"
            common = {"Idempotency-Key": idem}
            checked(
                owner.post("/v1/submissions", json=payload, headers=common),
                409,
                "AUTH_ACCOUNT_CHANGED",
            )
            checked(
                owner.post(
                    "/v1/submissions",
                    json=payload,
                    headers={**common, "X-LOJ-Expected-User": users[1]["user_id"]},
                ),
                409,
                "AUTH_ACCOUNT_CHANGED",
            )
            submit_headers = {**common, "X-LOJ-Expected-User": users[0]["user_id"]}
            checked(
                owner.post(
                    "/v1/submissions", json=payload, headers={**submit_headers, "X-LOJ-CSRF": "0"}
                ),
                403,
                "AUTH_CSRF_REJECTED",
            )
            checked(
                owner.post(
                    "/v1/submissions",
                    json=payload,
                    headers={**submit_headers, "Origin": "http://127.0.0.1:1"},
                ),
                403,
                "AUTH_CSRF_REJECTED",
            )
            with TestClient(
                app(False), base_url=origin, headers=headers, client=("127.0.0.1", 50003)
            ) as gated:
                gated.cookies.update(owner.cookies)
                checked(
                    gated.post("/v1/submissions", json=payload, headers=submit_headers),
                    409,
                    "CHALLENGE_NOT_OPEN",
                )
            require(
                checked(owner.get("/v1/submissions"))["items"] == [], "guard_created_submission"
            )
            report["checks"]["expected_user_csrf_origin_rights_gate"] = True
            report["stage"] = "postgres_outbox_redis"
            submission = checked(
                owner.post("/v1/submissions", json=payload, headers=submit_headers), 202
            )
            sid = submission["submission_id"]
            require(
                submission["status"] == "queued"
                and submission["evaluation_identity_sha256"] == contract.evaluation_identity_sha256,
                "wrong_submission_partition",
            )
            stored = store.submission_for_owner(sid, users[0]["user_id"])
            require(
                stored is not None and stored.user_id == users[0]["user_id"],
                "submission_owner_not_persisted",
            )
            replay = checked(
                owner.post("/v1/submissions", json=payload, headers=submit_headers), 202
            )
            require(replay["submission_id"] == sid, "idempotency_replay_duplicated")
            checked(
                owner.post(
                    "/v1/submissions",
                    json={**payload, "student_prompt": _PROMPT + " "},
                    headers=submit_headers,
                ),
                409,
                "IDEMPOTENCY_CONFLICT",
            )
            checked(owner.get(f"/v1/submissions/{sid}/result"), 409, "RESULT_NOT_READY")
            queue_probe(sid)
            report["checks"]["postgres_outbox_redis_published"] = True
            report["stage"] = "qwen_worker_once"
            require(worker.run_once(), "worker_did_not_consume")
            report["stage"] = "owner_result_history_leaderboard"
            result = checked(owner.get(f"/v1/submissions/{sid}/result"))
            if result.get("outcome") == "succeeded":
                report["result"] = {
                    key: result[key]
                    for key in ("score", "samples_total", "samples_valid", "samples_invalid")
                }
            require(
                result.get("outcome") == "succeeded"
                and result["samples_total"] == 2
                and result["samples_valid"] == 2
                and result["samples_invalid"] == 0
                and result["score"] == 1.0
                and result["metrics"]["micro_accuracy"] == 1.0,
                "handwritten_upos_score_failed",
            )
            require(
                set(result) == set(contract.owner_result_fields) | {"outcome"}
                and result["model_identity"] == contract.evaluation_identity["model_identity"]
                and result["generation_settings"]
                == contract.evaluation_identity["generation_settings"]
                and result["dataset_sha256"] == artifacts.public.dataset_sha256
                and result["selection_sha256"] == artifacts.public.selection_sha256
                and result["student_prompt_sha256"] == hashlib.sha256(_PROMPT.encode()).hexdigest(),
                "owner_result_identity_mismatch",
            )
            history = checked(owner.get("/v1/submissions"))
            require(
                len(history["items"]) == 1
                and history["items"][0]["submission_id"] == sid
                and history["items"][0]["status"] == "succeeded",
                "owner_history_mismatch",
            )
            for suffix in ("", "/result"):
                checked(other.get(f"/v1/submissions/{sid}{suffix}"), 404, "SUBMISSION_NOT_FOUND")
            require(
                checked(other.get("/v1/submissions"))["items"] == [], "history_cross_account_leak"
            )
            leaderboard = checked(
                other.get("/v1/leaderboards/" + contract.evaluation_identity_sha256)
            )
            require(len(leaderboard["items"]) == 1, "leaderboard_row_count")
            row = leaderboard["items"][0]
            require(
                set(row) == set(contract.public_leaderboard_fields)
                and row["public_handle"] == users[0]["public_handle"]
                and row["rank"] == 1
                and row["score"] == result["score"]
                and row["samples_total"] == 2
                and row["evaluation_identity_sha256"] == contract.evaluation_identity_sha256,
                "leaderboard_partition_mismatch",
            )
            report["stage"] = "administrator_teaching_and_admissions"
            admin_path = "/v1/admin/challenges/" + contract.challenge_id
            checked(owner.get(admin_path), 403, "ADMIN_REQUIRED")
            store.set_account_role(users[0]["user_id"], "admin")
            detail = checked(owner.get(admin_path))
            content = {
                "title": "Isolated teaching acceptance",
                "summary": "Synthetic integration check, not a published benchmark.",
                "instructions": "Assign one UPOS label per supplied token.",
                "zero_shot_prompt": _PROMPT,
                "few_shot_prompt": _PROMPT + " Preserve token order.",
            }
            draft = checked(
                owner.put(
                    admin_path + "/teaching-draft",
                    headers=submit_headers,
                    json={"expected_revision": detail["revision"], "content": content},
                )
            )
            teaching_path = "/v1/challenges/" + contract.challenge_id + "/teaching"
            require(checked(other.get(teaching_path))["content"] is None, "draft_leaked")
            published = checked(
                owner.post(
                    admin_path + "/publish",
                    headers=submit_headers,
                    json={"expected_revision": draft["revision"]},
                )
            )
            require(
                checked(other.get(teaching_path))["content"] == content,
                "published_teaching_mismatch",
            )
            closed = checked(
                owner.post(
                    admin_path + "/admissions",
                    headers=submit_headers,
                    json={"expected_revision": published["revision"], "closed": True},
                )
            )
            checked(
                owner.post(
                    "/v1/submissions",
                    json=payload,
                    headers={
                        **submit_headers,
                        "Idempotency-Key": idem + "-paused",
                    },
                ),
                409,
                "CHALLENGE_PAUSED",
            )
            require(
                checked(owner.post("/v1/submissions", json=payload, headers=submit_headers), 202)[
                    "submission_id"
                ]
                == sid,
                "pause_broke_idempotent_replay",
            )
            require(
                checked(owner.get(f"/v1/submissions/{sid}/result")) == result,
                "pause_changed_existing_result",
            )
            checked(
                owner.post(
                    admin_path + "/admissions",
                    headers=submit_headers,
                    json={"expected_revision": closed["revision"], "closed": False},
                )
            )
            store.set_account_role(users[0]["user_id"], "user")
            checked(owner.get(admin_path), 403, "ADMIN_REQUIRED")
            report["checks"]["postgres_admin_publish_pause_replay_resume_role"] = True
            # Same idempotency key, different real account: create only, never evaluate again.
            second = checked(
                other.post(
                    "/v1/submissions",
                    json=payload,
                    headers={**common, "X-LOJ-Expected-User": users[1]["user_id"]},
                ),
                202,
            )
            require(
                second["submission_id"] != sid and second["status"] == "queued",
                "cross_account_idempotency_collision",
            )
            require(
                store.submission_for_owner(second["submission_id"], users[1]["user_id"])
                is not None,
                "second_submission_owner_mismatch",
            )
            checked(
                owner.get(f"/v1/submissions/{second['submission_id']}"), 404, "SUBMISSION_NOT_FOUND"
            )
            old_cookie = owner.cookies.get(auth.cookie_name)
            checked(owner.post("/v1/auth/logout", json={}))
            checked(owner.get("/v1/submissions"), 401, "AUTH_INVALID_TOKEN")
            checked(
                owner.get(
                    "/v1/submissions",
                    headers={
                        "Cookie": f"{auth.cookie_name}={old_cookie}",
                    },
                ),
                401,
                "AUTH_INVALID_TOKEN",
            )
            report["checks"].update(
                owner_result_history_leaderboard=True,
                owner_only_access=True,
                per_account_idempotency=True,
                logout_revokes_cookie=True,
            )
            report["submissions"] = {"created": 2, "evaluated": 1, "unevaluated_for_guard_check": 1}


@contextmanager
def temporary_fixture(parent, report):
    temporary = tempfile.TemporaryDirectory(prefix="qwen-acceptance-", dir=parent)
    report["cleanup"]["temporary_fixture"] = {"confirmed": False}
    report["temporary_directory_name"] = Path(temporary.name).name
    try:
        yield Path(temporary.name)
    finally:
        temporary.cleanup()
        report["cleanup"]["temporary_fixture"] = {"confirmed": not Path(temporary.name).exists()}


def execute(args, report, *, checkpoint=lambda: None):
    from linguistic_oj.submission_jobs import QwenSubmissionWorker

    cleanup = report["cleanup"]
    with temporary_fixture(args.output.parent, report) as directory:
        report["stage"] = "handwritten_contract"
        artifacts, contract, metadata = build_fixture(args.root, Path(directory), uuid.uuid4().hex)
        report["metadata"] = metadata
        report["stage"] = "existing_launch_and_model_metadata"
        check_launch(args, contract)
        report["checks"]["existing_launch_tokenizer_and_live_models_metadata"] = True
        provider = build_provider(contract)
        params = postgres_parameters(args)
        # A private empty regular file disables implicit ~/.pgpass reads without
        # libpq's repeated warnings about /dev/null not being a regular file.
        passfile = directory / "empty.pgpass"
        descriptor = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        params["passfile"] = str(passfile)
        postgres = OwnedPostgres(params, cleanup)
        redis = None
        try:
            report["isolation"] = {
                "postgres_schema": postgres.schema,
                "redis_database": args.redis_db,
            }
            report["stage"] = "new_postgres_schema_bootstrap"
            cleanup["postgres"] = {"confirmed": False}
            checkpoint()
            store = postgres.create()
            report["stage"] = "new_redis_namespace"
            redis = OwnedRedis(args.redis_socket, args.redis_db, contract, cleanup)
            report["isolation"].update(redis_namespace=redis.namespace, redis_keys=list(redis.keys))
            cleanup["redis"] = {"confirmed": False}
            checkpoint()
            queue = redis.create(contract)
            report["stage"] = "qwen_worker_attestation"
            worker = QwenSubmissionWorker(
                store=store,
                queue=queue,
                contract=contract,
                artifacts=artifacts,
                provider=provider,
                tokenizer_snapshot_path=args.tokenizer_snapshot,
                launch_evidence_path=args.launch_evidence,
            )
            report["checks"]["real_qwen_worker_attestation"] = True

            def queue_probe(sid):
                with store._connect() as connection:
                    rows = connection.execute(
                        "SELECT submission_id, published_at IS NOT NULL FROM submission_outbox"
                    ).fetchall()
                require(rows == [(sid, True)], "durable_outbox_not_published_once")
                entries = redis.client.xrange(queue.stream_name)
                require(
                    len(entries) == 1
                    and entries[0][1]
                    == {
                        b"submission_id": sid.encode(),
                        b"evaluation_identity_sha256": contract.evaluation_identity_sha256.encode(),
                        b"contract_snapshot_sha256": contract.contract_snapshot_sha256.encode(),
                    },
                    "redis_delivery_not_matching_postgres",
                )

            report["stage"] = "http_registration"
            exercise_http(
                store, queue, worker, contract, artifacts, report, queue_probe=queue_probe
            )
            require(
                provider.generation_calls == 2 and not provider.has_active_request,
                "generation_budget_or_termination_failed",
            )
            require(
                redis.client.xpending(queue.stream_name, "submission-workers-v1")["pending"] == 0,
                "redis_worker_ack_unconfirmed",
            )
            report["checks"]["redis_worker_ack"] = True
        finally:
            report["generation_calls"] = provider.generation_calls
            report["provider_request_still_active"] = provider.has_active_request
            # Independent cleanup attempts: a Redis failure must not skip schema cleanup.
            for name, resource in (("redis", redis), ("postgres", postgres)):
                if resource is not None:
                    try:
                        resource.close()
                    except (Exception, KeyboardInterrupt):
                        cleanup[name] = {"confirmed": False}
    require(all(item["confirmed"] for item in cleanup.values()), "cleanup_unconfirmed")


def parse_args(arguments=None):
    class PrivateArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(2, "Invalid acceptance arguments; use --help. Do not pass credentials.\n")

    parser = PrivateArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, required=True)
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--postgres-url-file", type=Path)
    storage.add_argument("--postgres-socket", type=Path)
    parser.add_argument("--postgres-port", type=int)
    parser.add_argument("--postgres-user")
    parser.add_argument("--postgres-database")
    parser.add_argument("--redis-socket", type=Path, required=True)
    parser.add_argument("--redis-db", type=int, choices=range(16), default=15)
    parser.add_argument("--tokenizer-snapshot", type=Path, required=True)
    parser.add_argument("--launch-evidence", type=Path, required=True)
    parser.add_argument("--output", "--report", dest="output", type=Path, required=True)
    args = parser.parse_args(arguments)
    if args.postgres_socket is not None and not args.postgres_user:
        parser.error("Unix-socket mode requires --postgres-user")
    if args.postgres_url_file is not None and any(
        (
            args.postgres_port is not None,
            args.postgres_user is not None,
            args.postgres_database is not None,
        )
    ):
        parser.error("URL-file mode cannot be combined with PostgreSQL connection overrides")
    if args.postgres_port is not None and not 1 <= args.postgres_port <= 65535:
        parser.error("PostgreSQL port must be between 1 and 65535")
    return args


@contextmanager
def protected_report(args):
    validate_output(args.root, args.output)
    fd = os.open(
        args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        yield output


def main(arguments=None):
    args = parse_args(arguments)
    report = {
        "schema_version": "qwen-isolated-acceptance-v1",
        "passed": False,
        "stage": "preflight",
        "transport": "HTTP-inprocess-TestClient",
        "browser_verified": False,
        "environment": "isolated-test",
        "mail_delivery": "development-memory-capture",
        "production_scores_written": False,
        "benchmark": False,
        "checks": {},
        "generation_calls": 0,
        "generation_call_limit": 2,
        "cleanup": {
            name: {"confirmed": True, "not_created": True}
            for name in ("postgres", "redis", "temporary_fixture")
        },
        "attestation_scope": "operator-evidence+local-tokenizer-hashes+live-model-metadata",
        "started_at": datetime.now(UTC).isoformat(),
    }
    previous_logging = logging.root.manager.disable
    try:
        require(os.name == "posix", "run_on_colocated_linux_server")
        load_snapshot(args.root)
        with protected_report(args) as output, ExitStack() as signals:

            def interrupted(signum, frame):
                # A second signal must not abort the first signal's resource cleanup.
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                raise KeyboardInterrupt

            def checkpoint():
                output.seek(0)
                json.dump(report, output, indent=2, sort_keys=True, allow_nan=False)
                output.write("\n")
                output.truncate()
                output.flush()
                os.fsync(output.fileno())

            for signum in (signal.SIGINT, signal.SIGTERM):
                prior = signal.signal(signum, interrupted)
                signals.callback(signal.signal, signum, prior)
            logging.disable(logging.CRITICAL)
            try:
                checkpoint()
                # Forbid urllib proxy routing without mutating the existing provider's globals.
                require(
                    not any(v for k, v in getproxies().items() if k != "no"),
                    "proxy_environment_must_be_unset",
                )
                exact_path(args.redis_socket)
                require(
                    stat.S_ISSOCK(args.redis_socket.stat().st_mode), "redis_unix_socket_required"
                )
                with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                    execute(args, report, checkpoint=checkpoint)
                report["passed"] = True
                report["stage"] = "complete"
            except (Exception, KeyboardInterrupt) as error:
                report["failure"] = (
                    str(error) if isinstance(error, CheckFailed) else type(error).__name__
                )
            finally:
                report["finished_at"] = datetime.now(UTC).isoformat()
                checkpoint()
        print(
            "Qwen isolated acceptance passed."
            if report["passed"]
            else "Qwen isolated acceptance failed; inspect the protected aggregate report."
        )
        return 0 if report["passed"] else 1
    except (Exception, KeyboardInterrupt):
        print(
            "Qwen acceptance preflight/report failed; no diagnostic secrets are printed.",
            file=sys.stderr,
        )
        return 1
    finally:
        logging.disable(previous_logging)


if __name__ == "__main__":
    raise SystemExit(main())
