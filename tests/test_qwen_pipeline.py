"""Offline acceptance-harness tests. No SSH, PostgreSQL, Redis or model service is contacted."""

import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from linguistic_oj.mvp_contract import EvaluationContract
from scripts import check_qwen_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture(tmp_path):
    return pipeline.build_fixture(ROOT, tmp_path, "a" * 32)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline test attempted network access")

    monkeypatch.setattr("linguistic_oj.providers.urlopen", forbidden)
    monkeypatch.setattr(pipeline, "build_opener", forbidden)
    monkeypatch.setattr(pipeline.os, "getuid", lambda: 0, raising=False)


def test_handwritten_fixture_preserves_frozen_model_and_limits(fixture):
    artifacts, contract, metadata = fixture
    original = EvaluationContract.from_path(ROOT / "config/mvp_evaluation_v2.json")
    rows = [json.loads(line) for line in artifacts.dataset_path.read_text().splitlines()]
    assert [row["text"] for row in rows] == ["Cats sleep .", "Dogs run ."]
    assert all(row["answers"]["upos"] == ["NOUN", "VERB", "PUNCT"] for row in rows)
    assert sum(sample.gold_items for sample in artifacts.private.samples) == 6
    assert contract.challenge_id.startswith("en-qwenacceptance-upos-accept-")
    assert contract.challenge_id != original.challenge_id
    assert not contract.external_activation_ready
    changed = json.loads(contract.snapshot_json)
    source = json.loads(original.snapshot_json)
    for key in source.keys() - {"catalog", "evaluation_identity", "leaderboard_partition"}:
        assert changed[key] == source[key]
    for key in source["evaluation_identity"].keys() - {
        "challenge_id",
        "dataset_sha256",
        "selection_sha256",
    }:
        assert changed["evaluation_identity"][key] == source["evaluation_identity"][key]
    assert artifacts.public.annotation_license == "unrecorded"
    assert metadata["samples"] == 2 and metadata["gold_items"] == 6
    assert "Cats" not in json.dumps(metadata)


def cli(tmp_path):
    return [
        "--root",
        str(ROOT),
        "--postgres-socket",
        str(tmp_path),
        "--postgres-user",
        "acceptance",
        "--redis-socket",
        str(tmp_path / "redis.sock"),
        "--tokenizer-snapshot",
        str(tmp_path / "model"),
        "--launch-evidence",
        str(tmp_path / "evidence.json"),
        "--output",
        str(tmp_path / "report.json"),
    ]


def test_cli_requires_operator_inputs_and_never_accepts_password_or_schema(tmp_path):
    args = pipeline.parse_args(cli(tmp_path))
    assert args.redis_db == 15 and args.postgres_port is None
    for option in ("--launch-evidence", "--tokenizer-snapshot", "--redis-socket"):
        values = cli(tmp_path)
        index = values.index(option)
        del values[index : index + 2]
        with pytest.raises(SystemExit):
            pipeline.parse_args(values)
    for extra in (
        ["--postgres-password", "secret"],
        ["--schema", "public"],
        ["--namespace", "linguistic-oj"],
        ["--redis-db", "16"],
        ["--postgres-port", "0"],
        ["--vllm-base-url", "http://elsewhere"],
    ):
        with pytest.raises(SystemExit):
            pipeline.parse_args(cli(tmp_path) + extra)


@pytest.mark.parametrize("bad", [Path("relative"), ROOT / ".." / "danger"])
def test_exact_paths_reject_relative_and_traversal(bad):
    with pytest.raises(pipeline.CheckFailed):
        pipeline.exact_path(bad, exists=False)


def test_exact_paths_reject_symlink_components(monkeypatch, tmp_path):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p.name == "current" or original(p))
    with pytest.raises(pipeline.CheckFailed, match="symlink"):
        pipeline.exact_path(tmp_path / "current" / "snapshot", exists=False)


def test_output_never_overwrites_or_enters_git(tmp_path):
    existing = tmp_path / "existing.json"
    existing.write_text("operator-owned")
    with pytest.raises(pipeline.CheckFailed, match="new"):
        pipeline.validate_output(ROOT, existing)
    assert existing.read_text() == "operator-owned"
    snapshot = tmp_path / "snapshot"
    (snapshot / "runtime").mkdir(parents=True)
    with pytest.raises(pipeline.CheckFailed, match="snapshot"):
        pipeline.validate_output(snapshot, snapshot / "runtime" / "acceptance-report.json")
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(pipeline.CheckFailed, match="git"):
        pipeline.validate_output(ROOT, tmp_path / "new.json")


def test_output_requires_owner_only_directory(monkeypatch, tmp_path):
    actual = Path.stat

    def shared(path, *args, **kwargs):
        info = actual(path, *args, **kwargs)
        if path == tmp_path:
            return SimpleNamespace(st_mode=info.st_mode | 0o077, st_uid=0)
        return info

    monkeypatch.setattr(Path, "stat", shared)
    with pytest.raises(pipeline.CheckFailed, match="private"):
        pipeline.validate_output(ROOT, tmp_path / "new.json")


def test_report_creation_uses_exclusive_private_fd(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "validate_output", lambda *args: None)
    original = pipeline.os.open
    opened = []

    def record(path, flags, mode):
        opened.append((flags, mode))
        return original(path, flags, mode)

    monkeypatch.setattr(pipeline.os, "open", record)
    args = SimpleNamespace(root=ROOT, output=tmp_path / "report")
    with pipeline.protected_report(args) as output:
        output.write("aggregate-only")
    assert opened[0][0] & pipeline.os.O_EXCL and opened[0][1] == 0o600
    with pytest.raises(FileExistsError), pipeline.protected_report(args):
        pass


@pytest.mark.parametrize(
    "mode,uid,links", [(0o100644, 0, 1), (0o100600, 1, 1), (0o100600, 0, 2), (0o010600, 0, 1)]
)
def test_secret_file_rejects_shared_foreign_hardlinked_and_nonregular(
    monkeypatch, tmp_path, mode, uid, links
):
    path = tmp_path / "private-url"
    path.write_bytes(b"postgresql:///not-printed")
    monkeypatch.setattr(
        pipeline.os, "fstat", lambda fd: SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=links)
    )
    with pytest.raises(pipeline.CheckFailed):
        pipeline.protected_file(path)


def test_secret_file_is_bounded_and_cli_errors_do_not_echo_credentials(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "private-url"
    path.write_bytes(b"x" * 8193)
    monkeypatch.setattr(
        pipeline.os, "fstat", lambda fd: SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1)
    )
    with pytest.raises(pipeline.CheckFailed, match="too_large"):
        pipeline.protected_file(path)
    with pytest.raises(SystemExit):
        pipeline.parse_args(cli(tmp_path) + ["--postgres-password", "never-echo-this-secret"])
    assert "never-echo-this-secret" not in capsys.readouterr().err


def test_snapshot_cannot_import_another_root(tmp_path):
    (tmp_path / "src/linguistic_oj").mkdir(parents=True)
    with pytest.raises(pipeline.CheckFailed, match="script_not_from"):
        pipeline.load_snapshot(tmp_path)


@pytest.fixture
def postgres_driver(monkeypatch):
    """Minimal driver seam: SQL statements are recorded, never executed on a server."""
    calls = []
    proof = [None]
    tables = ["auth_credentials", "results", "schema_migrations", "users"]

    class Connection:
        def __init__(self, options):
            self.schema = options.split("search_path=")[1].split(",")[0]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=()):
            query = str(query)
            calls.append((self.schema, query, params))
            row = None
            rows = []
            if "obj_description" in query:
                row = proof[0]
            elif query.startswith("COMMENT ON SCHEMA"):
                proof[0] = (1234, 100, query.split(" IS '")[1][:-1])
            elif query == "SELECT current_schema()":
                row = (self.schema,)
            elif "SELECT count(*)" in query:
                row = (0,)
            elif "SELECT relname" in query:
                rows = [(name,) for name in tables]
            elif query.startswith("DROP SCHEMA"):
                proof[0] = None
            return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: rows)

    connect = Mock(side_effect=lambda **kwargs: Connection(kwargs["options"]))
    driver = SimpleNamespace(
        connect=connect,
        IntegrityError=type("IntegrityError", (Exception,), {}),
        sql=SimpleNamespace(
            SQL=str,
            Identifier=lambda *names: ".".join(
                '"' + name.replace('"', '""') + '"' for name in names
            ),
            Literal=lambda value: "'" + value.replace("'", "''") + "'",
        ),
    )
    monkeypatch.setitem(sys.modules, "psycopg", driver)
    monkeypatch.setattr(
        "linguistic_oj.postgres_submission_store.PostgresSubmissionStore.health_check",
        lambda self: None,
    )
    return SimpleNamespace(driver=driver, calls=calls, proof=proof, tables=tables)


def test_pg_factory_and_migrations_are_scoped_without_global_patching(postgres_driver):
    from linguistic_oj.postgres_migrations import (
        _POSTGRES_MIGRATIONS,
        POSTGRES_SESSION_OPTIONS,
    )
    from linguistic_oj.postgres_submission_store import PostgresSubmissionStore

    original = postgres_driver.driver.connect
    cleanup = {}
    pg = pipeline.OwnedPostgres({"host": "/private/socket", "dbname": "shared"}, cleanup)
    store = pg.create()
    assert isinstance(store, PostgresSubmissionStore)
    with store._connect():
        pass
    assert postgres_driver.driver.connect is original
    for call in original.call_args_list:
        options = call.kwargs["options"]
        assert options.startswith(POSTGRES_SESSION_OPTIONS)
        assert options.endswith("search_path=pg_catalog") or options.endswith(
            f"search_path={pg.schema},pg_catalog,pg_temp"
        )
        assert "public" not in options
    for migration in _POSTGRES_MIGRATIONS.values():
        assert any(
            schema == pg.schema and query == migration for schema, query, _ in postgres_driver.calls
        )
    pg.close()
    assert cleanup["postgres"] == {"confirmed": True, "schemas_dropped": 1, "tables_dropped": 4}
    drops = [query for _, query, _ in postgres_driver.calls if query.startswith("DROP")]
    assert len(drops) == 2 and all(
        "RESTRICT" in query and "CASCADE" not in query for query in drops
    )
    assert all(pg.schema in query and '"public"' not in query for query in drops)


def test_pg_collision_and_changed_proof_never_drop_shared_schema(postgres_driver):
    pg = pipeline.OwnedPostgres({}, {})
    postgres_driver.proof[0] = (500, 1, "existing-operator-schema")
    with pytest.raises(pipeline.CheckFailed, match="collision"):
        pg.create()
    pg.close()
    assert not any(query.startswith("DROP") for _, query, _ in postgres_driver.calls)
    postgres_driver.proof[0] = None
    pg.create()
    postgres_driver.proof[0] = (pg.proof[0] + 1, pg.proof[1], pg.marker)
    with pytest.raises(pipeline.CheckFailed, match="proof_changed"):
        pg.close()
    assert not any(query.startswith("DROP") for _, query, _ in postgres_driver.calls)
    pg.schema = "public"
    with pytest.raises(pipeline.CheckFailed, match="unsafe_schema"):
        pg.close()


def test_pg_cleanup_restrict_failure_cannot_fall_back_to_cascade(postgres_driver):
    pg = pipeline.OwnedPostgres({}, {})
    pg.create()
    original = pg.factory

    def dependent(**kwargs):
        connection = original(**kwargs)
        execute = connection.execute

        def guarded(query, params=()):
            if str(query).startswith("DROP TABLE"):
                raise RuntimeError("external dependency prevents RESTRICT")
            return execute(query, params)

        connection.execute = guarded
        return connection

    pg.factory = dependent
    with pytest.raises(RuntimeError, match="dependency"):
        pg.close()
    assert postgres_driver.proof[0] == pg.proof
    assert not any(query.startswith("DROP SCHEMA") for _, query, _ in postgres_driver.calls)


def test_pg_unproven_creation_never_claims_cleanup_or_drops(postgres_driver):
    pg = pipeline.OwnedPostgres({}, {})
    pg.attempted = True
    postgres_driver.proof[0] = (1234, 1, "unconfirmed")
    with pytest.raises(pipeline.CheckFailed, match="unconfirmed"):
        pg.close()
    assert not pg.cleanup
    assert not any(query.startswith("DROP") for _, query, _ in postgres_driver.calls)


@pytest.mark.parametrize(
    "params",
    [
        {"host": "remote.example", "user": "a", "dbname": "shared"},
        {"host": "127.0.0.1", "user": "a", "dbname": "shared", "options": "-c search_path=public"},
        {"host": "127.0.0.1", "user": "a", "dbname": "shared", "service": "production"},
        {"user": "a", "dbname": "shared"},
    ],
)
def test_secret_url_rejects_remote_and_connection_overrides(monkeypatch, tmp_path, params):
    monkeypatch.setitem(
        sys.modules, "psycopg.conninfo", SimpleNamespace(conninfo_to_dict=lambda value: params)
    )
    monkeypatch.setattr(pipeline, "protected_file", lambda path: b"postgresql:///secret")
    monkeypatch.setattr(pipeline.os, "environ", {})
    with pytest.raises(pipeline.CheckFailed):
        pipeline.postgres_parameters(SimpleNamespace(postgres_url_file=tmp_path / "secret"))


def test_libpq_environment_is_rejected_without_connection(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", SimpleNamespace(conninfo_to_dict=Mock()))
    monkeypatch.setattr(pipeline.os, "environ", {"PGOPTIONS": "-c search_path=public"})
    with pytest.raises(pipeline.CheckFailed, match="environment"):
        pipeline.postgres_parameters(SimpleNamespace(postgres_url_file=tmp_path / "secret"))


@pytest.fixture
def redis_driver(monkeypatch):
    data = {"linguistic-oj:production": b"keep", "other-tenant": b"keep-too"}
    evaluations = []

    class Client:
        def eval(self, script, count, *args):
            keys, marker = args[:count], args[count]
            evaluations.append((script, keys))
            if script == pipeline._RESERVE_REDIS:
                if any(key in data for key in keys):
                    return 0
                data[keys[3]] = marker.encode()
                data[keys[0]] = b"stream"
                return 1
            assert script == pipeline._CLEAN_REDIS
            if data.get(keys[3]) != marker.encode():
                return -1
            removed = sum(key in data for key in keys)
            for key in keys:
                data.pop(key, None)
            return removed

        def get(self, key):
            return data.get(key)

        def exists(self, *keys):
            return sum(key in data for key in keys)

        def close(self):
            pass

    class Queue:
        def __init__(self, **kwargs):
            self.stream_name = f"{kwargs['namespace']}:jobs:{{{kwargs['routing_key']}}}"
            self._active_key = self.stream_name + ":active"
            self._receipt_key = self.stream_name + ":receipts"

        def close(self):
            pass

    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **kw: Client())
    monkeypatch.setattr("linguistic_oj.redis_job_queue.RedisJobQueue", Queue)
    return SimpleNamespace(data=data, evaluations=evaluations)


def test_redis_deletes_only_exact_owned_keys_in_nonempty_database(redis_driver, fixture, tmp_path):
    _, contract, _ = fixture
    cleanup = {}
    redis = pipeline.OwnedRedis(tmp_path / "private.sock", 15, contract, cleanup)
    redis.create(contract)
    redis_driver.data[redis.keys[1]] = b"active"
    redis_driver.data[redis.keys[2]] = b"receipts"
    redis_driver.data[redis.namespace + ":unowned-sibling"] = b"keep"
    redis.close()
    assert cleanup["redis"] == {"confirmed": True, "keys_deleted": 4}
    assert len(redis_driver.data) == 3
    assert redis_driver.data["linguistic-oj:production"] == b"keep"
    assert all(keys == redis.keys for _, keys in redis_driver.evaluations)
    assert not re.search(r"FLUSH|SCAN|redis.call\('KEYS'", pipeline._CLEAN_REDIS)


def test_redis_collision_marker_change_and_partial_init(
    redis_driver, fixture, tmp_path, monkeypatch
):
    _, contract, _ = fixture
    redis = pipeline.OwnedRedis(tmp_path / "private.sock", 15, contract, {})
    redis_driver.data[redis.keys[0]] = b"existing"
    with pytest.raises(pipeline.CheckFailed, match="collision"):
        redis.create(contract)
    with pytest.raises(pipeline.CheckFailed, match="not_owned"):
        redis.close()
    assert redis_driver.data[redis.keys[0]] == b"existing"
    redis = pipeline.OwnedRedis(tmp_path / "private.sock", 15, contract, {})
    monkeypatch.setattr(
        "linguistic_oj.redis_job_queue.RedisJobQueue",
        Mock(side_effect=RuntimeError("constructor failure")),
    )
    with pytest.raises(RuntimeError):
        redis.create(contract)
    redis.close()
    assert not any(key in redis_driver.data for key in redis.keys)
    redis = pipeline.OwnedRedis(tmp_path / "private.sock", 15, contract, {})
    with pytest.raises(RuntimeError):
        redis.create(contract)
    redis_driver.data[redis.keys[3]] = b"not-ours"
    with pytest.raises(pipeline.CheckFailed, match="not_owned"):
        redis.close()
    assert redis_driver.data[redis.keys[0]] == b"stream"


def test_missing_launch_fails_without_creating_evidence(fixture, tmp_path):
    _, contract, _ = fixture
    args = SimpleNamespace(tokenizer_snapshot=tmp_path, launch_evidence=tmp_path / "missing.json")
    with pytest.raises(pipeline.CheckFailed, match="missing"):
        pipeline.check_launch(args, contract)
    assert not args.launch_evidence.exists()


@pytest.mark.parametrize("wrong", ["runtime", "tokenizer", "live_root", "live_context"])
def test_wrong_existing_attestation_fails_closed(fixture, tmp_path, monkeypatch, wrong):
    from linguistic_oj.qwen_runtime import QwenLaunchEvidence, TokenizerIdentity

    _, contract, _ = fixture
    args = SimpleNamespace(tokenizer_snapshot=tmp_path, launch_evidence=tmp_path / "operator.json")
    args.launch_evidence.write_text("operator-owned-existing-test-stub")
    expected = TokenizerIdentity.from_mapping(contract.evaluation_identity["tokenizer_identity"])
    launch = QwenLaunchEvidence(
        tmp_path, "wrong" if wrong == "runtime" else "0.27.1+cu129", 4096, 1, True
    )
    monkeypatch.setattr(QwenLaunchEvidence, "from_path", lambda path: launch)
    monkeypatch.setattr(
        TokenizerIdentity,
        "from_snapshot",
        lambda *a, **kw: None if wrong == "tokenizer" else expected,
    )
    response = Mock()
    response.read.return_value = json.dumps(
        {
            "data": [
                {
                    "id": "Qwen/Qwen3.5-9B",
                    "root": "wrong" if wrong == "live_root" else str(tmp_path),
                    "max_model_len": 8192 if wrong == "live_context" else 4096,
                }
            ]
        }
    ).encode()
    opened = Mock()
    opened.__enter__ = Mock(return_value=response)
    opened.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        pipeline, "build_opener", lambda *a: SimpleNamespace(open=lambda *a, **kw: opened)
    )
    with pytest.raises(pipeline.CheckFailed):
        pipeline.check_launch(args, contract)
    assert args.launch_evidence.read_text() == "operator-owned-existing-test-stub"


def test_actual_auth_http_worker_scoring_flow_with_offline_remote_seams(
    fixture, monkeypatch, tmp_path
):
    from linguistic_oj.providers import (
        ModelGeneration,
        OpenAICompatibleProvider,
        ProviderContractError,
    )
    from linguistic_oj.submission_jobs import InMemoryJobQueue, QwenSubmissionWorker
    from linguistic_oj.submission_store import SubmissionStore

    artifacts, contract, _ = fixture
    store = SubmissionStore(tmp_path / "offline.sqlite3")
    queue = InMemoryJobQueue(contract.contract_snapshot_sha256, visibility_timeout_seconds=320)
    queue.health_check = lambda: None
    attested = Mock(return_value=SimpleNamespace(tokenizer=object(), tokenizer_identity=object()))
    monkeypatch.setattr("linguistic_oj.submission_jobs.attest_qwen_runtime_from_snapshot", attested)
    monkeypatch.setattr(
        "linguistic_oj.submission_jobs.QwenTokenizerPreflight", lambda *a: lambda r: None
    )
    generation = Mock(return_value=ModelGeneration(raw_text='{"tags":["NOUN","VERB","PUNCT"]}'))
    monkeypatch.setattr(OpenAICompatibleProvider, "generate", generation)
    provider = pipeline.build_provider(contract)
    worker = QwenSubmissionWorker(
        store=store,
        queue=queue,
        contract=contract,
        artifacts=artifacts,
        provider=provider,
        tokenizer_snapshot_path=tmp_path / "tokenizer",
        launch_evidence_path=tmp_path / "operator",
    )
    probes = []

    def probe(sid):
        with store._connect() as connection:
            rows = connection.execute(
                "SELECT submission_id, published_at IS NOT NULL FROM submission_outbox"
            ).fetchall()
        assert [tuple(row) for row in rows] == [(sid, 1)] and len(queue) == 1
        probes.append(sid)

    report = {"checks": {}}
    pipeline.exercise_http(store, queue, worker, contract, artifacts, report, queue_probe=probe)
    attested.assert_called_once()
    assert len(probes) == 1
    assert provider.generation_calls == generation.call_count == 2
    assert report["result"]["score"] == 1.0 and report["submissions"]["evaluated"] == 1
    assert all(report["checks"].values())
    assert len(queue) == 1  # Second account's idempotency probe is intentionally not evaluated.
    assert not queue._inflight
    with pytest.raises(ProviderContractError, match="budget"):
        provider.generate(generation.call_args.args[0])
    assert generation.call_count == 2
    serialized = json.dumps(report)
    for private in ("example.test", "password", "Cats", "Dogs", "student_prompt", "token="):
        assert private not in serialized


@pytest.mark.parametrize(
    "failure", ["postgres", "redis", "worker", "http", "interrupt", "cleanup_only"]
)
def test_execute_cleans_every_created_scope_on_failure(fixture, tmp_path, monkeypatch, failure):
    artifacts, contract, metadata = fixture
    closed = []
    directory_names = []

    def build(root, directory, run_id):
        directory_names.append(directory)
        return artifacts, contract, metadata

    class Postgres:
        schema = "qwen_accept_u0_" + "b" * 32

        def __init__(self, params, cleanup):
            self.cleanup = cleanup

        def create(self):
            if failure == "postgres":
                raise RuntimeError("private database error")
            return object()

        def close(self):
            closed.append("postgres")
            self.cleanup["postgres"] = {"confirmed": True}

    class Redis:
        namespace = "qwen-acceptance:test"
        keys = ("only-own-key",)

        def __init__(self, path, db, contract, cleanup):
            self.cleanup = cleanup
            self.client = SimpleNamespace(xpending=lambda *a: {"pending": 0})

        def create(self, contract):
            if failure == "redis":
                raise RuntimeError("private redis error")
            return SimpleNamespace(stream_name="own-stream")

        def close(self):
            closed.append("redis")
            # Test that another cleanup failure still cannot skip PostgreSQL cleanup.
            raise RuntimeError("cleanup disconnected")

    monkeypatch.setattr(pipeline, "build_fixture", build)
    monkeypatch.setattr(pipeline, "check_launch", lambda *a: None)
    monkeypatch.setattr(
        pipeline,
        "build_provider",
        lambda *a: SimpleNamespace(generation_calls=2, has_active_request=False),
    )
    monkeypatch.setattr(pipeline, "postgres_parameters", lambda *a: {})
    monkeypatch.setattr(pipeline, "OwnedPostgres", Postgres)
    monkeypatch.setattr(pipeline, "OwnedRedis", Redis)
    monkeypatch.setattr(
        "linguistic_oj.submission_jobs.QwenSubmissionWorker",
        Mock(side_effect=RuntimeError("attestation failed") if failure == "worker" else None),
    )
    monkeypatch.setattr(
        pipeline,
        "exercise_http",
        Mock(
            side_effect=(
                None
                if failure == "cleanup_only"
                else KeyboardInterrupt()
                if failure == "interrupt"
                else RuntimeError("HTTP failed")
            )
        ),
    )
    args = SimpleNamespace(
        root=ROOT,
        output=tmp_path / "report",
        redis_socket=tmp_path / "redis",
        redis_db=15,
        tokenizer_snapshot=tmp_path,
        launch_evidence=tmp_path,
    )
    report = {"cleanup": {}, "checks": {}}
    with pytest.raises((RuntimeError, KeyboardInterrupt)):
        pipeline.execute(args, report)
    assert closed == (["postgres"] if failure == "postgres" else ["redis", "postgres"])
    assert report["cleanup"]["postgres"]["confirmed"]
    assert report["cleanup"]["temporary_fixture"]["confirmed"]
    assert not any(path.exists() for path in directory_names)
    if failure != "postgres":
        assert not report["cleanup"]["redis"]["confirmed"]


def test_main_failure_report_redacts_dependency_messages(tmp_path, monkeypatch, capsys):
    (tmp_path / "redis.sock").touch()
    real_require = pipeline.require
    monkeypatch.setattr(
        pipeline,
        "require",
        lambda condition, code: (
            None
            if code
            in {
                "run_on_colocated_linux_server",
                "redis_unix_socket_required",
            }
            else real_require(condition, code)
        ),
    )
    monkeypatch.setattr(pipeline, "load_snapshot", lambda root: None)
    monkeypatch.setattr(pipeline, "getproxies", lambda: {})
    monkeypatch.setattr(pipeline, "exact_path", lambda path, **kw: tmp_path)

    @contextmanager
    def output(args):
        with args.output.open("w") as handle:
            yield handle

    monkeypatch.setattr(pipeline, "protected_report", output)
    monkeypatch.setattr(
        pipeline,
        "execute",
        Mock(
            side_effect=RuntimeError(
                "postgresql://user:secret-password@host/db "
                "email@example.test token=raw Cats sleep ."
            )
        ),
    )
    assert pipeline.main(cli(tmp_path)) == 1
    report = json.loads((tmp_path / "report.json").read_text())
    captured = capsys.readouterr()
    text = json.dumps(report) + captured.out + captured.err
    assert report["failure"] == "RuntimeError" and not report["passed"]
    assert report["transport"] == "HTTP-inprocess-TestClient" and not report["browser_verified"]
    for forbidden in ("secret-password", "email@example.test", "token=raw", "Cats sleep"):
        assert forbidden not in text
