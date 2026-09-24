import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from urllib.error import HTTPError, URLError

import pytest

from linguistic_oj import auth_config, executor_state, providers
from linguistic_oj.executor_state import (
    ExecutorBusy,
    ExecutorState,
    GuardedQwenProvider,
    RecoveryRequired,
    executor_lock,
    read_state,
)
from linguistic_oj.model_inputs import SegmentationModelInput
from linguistic_oj.providers import ModelIdentity, ModelRequest, ProviderTransportError
from linguistic_oj.qwen_executor import run_serial
from linguistic_oj.responses import TaskType

BINDING = "a" * 64


@pytest.fixture(autouse=True)
def windows_fixture_permissions(monkeypatch):
    # Linux CI checks actual 0600 permissions. Windows fixtures exercise crash/lock semantics.
    if os.name == "nt":
        monkeypatch.setattr(auth_config, "_check_windows_acl", lambda path: None)


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / "executor"
    path.mkdir(mode=0o700)
    with executor_lock(path, create=True):
        ExecutorState.initialize(path, BINDING)
    return path


def request():
    return ModelRequest(task=TaskType.SEGMENTATION, language="Test", treebank="Tiny",
                        student_prompt="private fixture prompt", model_input=SegmentationModelInput(
                            text="AB"))


def provider(state, endpoint="http://127.0.0.1:8000/v1"):
    return GuardedQwenProvider(
        executor_state=state, challenge_id="fixture-segmentation", base_url=endpoint,
        identity=ModelIdentity(model="Qwen/Qwen3.5-9B", revision="a" * 40,
                               runtime="vllm", runtime_version="fixture"),
    )


def evidence(directory, state, operation, *, confirmed=True):
    path = directory.parent / "evidence.json"
    path.write_text(json.dumps({
        "operation_id": operation, "binding_sha256": state.binding_sha256,
        "prior_request_terminated": confirmed, "confirmed_by": "fixture-operator",
        "evidence_reference": "isolated fake request thread has exited",
    }), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_exclusive_ownership_reinitialization_and_changed_binding(directory):
    with executor_lock(directory):
        with pytest.raises(ExecutorBusy):
            with executor_lock(directory):
                pytest.fail("a second executor acquired the same state")
        with pytest.raises(ValueError, match="empty"):
            ExecutorState.initialize(directory, BINDING)
        with pytest.raises(ValueError, match="binding changed"):
            ExecutorState(directory, "b" * 64)
    with executor_lock(directory):
        ExecutorState(directory, BINDING).require_clean()


def test_disk_failure_prevents_network_and_poisons_the_loop(directory, monkeypatch):
    attempts = []
    monkeypatch.setattr(providers, "urlopen", lambda *args, **kwargs: attempts.append(1))
    with executor_lock(directory):
        state = ExecutorState(directory, BINDING)

        def failed_sync(*args):
            raise OSError("fixture disk failure")

        monkeypatch.setattr(executor_state.os, "fsync", failed_sync)
        with pytest.raises(OSError):
            provider(state).generate(request())
        with pytest.raises(RecoveryRequired):
            state.require_clean()
    assert attempts == []


@pytest.mark.parametrize("confirmed", [True, False])
def test_transport_completion_distinguishes_known_http_error_from_disconnect(
    directory, monkeypatch, confirmed,
):
    def fail_request(*args, **kwargs):
        # The write-ahead barrier is already on disk at the actual network boundary.
        assert read_state(directory)["pending"]["challenge_id"] == "fixture-segmentation"
        if confirmed:
            raise HTTPError("http://fixture/v1", 503, "fixture", None, None)
        raise URLError("fixture connection lost")

    monkeypatch.setattr(providers, "urlopen", fail_request)
    with executor_lock(directory):
        state = ExecutorState(directory, BINDING)
        with pytest.raises(ProviderTransportError):
            provider(state).generate(request())
        assert (state.snapshot()["pending"] is None) == confirmed
        if not confirmed:
            with pytest.raises(RecoveryRequired):
                provider(state).generate(request())
    assert "private fixture prompt" not in (directory / "state.json").read_text()


def test_recovery_requires_exact_evidence_and_is_not_replayable(directory):
    with executor_lock(directory):
        state = ExecutorState(directory, BINDING)
        operation = state.begin("fixture")
        original = (directory / "state.json").read_bytes()
        with pytest.raises(ValueError, match="does not match"):
            state.recover("0" * 32, evidence(directory, state, operation))
        with pytest.raises(ValueError, match="explicit matching"):
            state.recover(operation, evidence(directory, state, operation, confirmed=False))
        assert (directory / "state.json").read_bytes() == original
        proof = evidence(directory, state, operation)
        state.recover(operation, proof)
        assert (directory / "recoveries" / (operation + ".json")).is_file()
        state.require_clean()
        with pytest.raises(ValueError, match="does not match"):
            state.recover(operation, proof)


def test_recovery_crash_after_audit_resumes_without_overwriting_proof(directory, monkeypatch):
    with executor_lock(directory):
        state = ExecutorState(directory, BINDING)
        operation = state.begin("fixture")
        proof = evidence(directory, state, operation)
        original_write = executor_state._write_atomic

        def fail_clear(path, document):
            if path.name == "state.json":
                raise OSError("fixture interruption after audit")
            return original_write(path, document)

        monkeypatch.setattr(executor_state, "_write_atomic", fail_clear)
        with pytest.raises(OSError):
            state.recover(operation, proof)
        audit = directory / "recoveries" / (operation + ".json")
        recorded = audit.read_bytes()
        monkeypatch.setattr(executor_state, "_write_atomic", original_write)
        restarted = ExecutorState(directory, BINDING)
        with pytest.raises(RecoveryRequired):
            restarted.require_clean()
        restarted.recover(operation, proof)
        assert audit.read_bytes() == recorded
        restarted.require_clean()


def test_corrupt_state_fails_closed(directory):
    (directory / "state.json").write_text('{"pending":null}', encoding="utf-8")
    with executor_lock(directory), pytest.raises(ValueError):
        ExecutorState(directory, BINDING)


def test_serial_round_robin_stop_and_uncertain_worker_block_every_queue(directory):
    calls = []
    stop = Event()

    class Worker:
        def __init__(self, key, action=lambda: None):
            self.key, self.action = key, action

        def run_once(self):
            calls.append(self.key)
            self.action()
            return True

    with executor_lock(directory):
        state = ExecutorState(directory, BINDING)
        run_serial({"a": Worker("a"), "b": Worker("b")}, state, stop, once=True)
        assert calls == ["a", "b"]
        calls.clear()
        run_serial({"a": Worker("a", stop.set), "b": Worker("b")}, state, stop)
        assert calls == ["a"]
        stop.clear()
        calls.clear()
        with pytest.raises(RecoveryRequired):
            run_serial({"a": Worker("a", lambda: state.begin("a")), "b": Worker("b")},
                       state, stop, once=True)
        assert calls == ["a"]


@contextmanager
def delayed_model(directory):
    received, release, finished = Event(), Event(), Event()
    records = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            records.append(read_state(directory))
            received.set()
            release.wait(15)
            try:
                body = b'{"choices":[{"message":{"content":"{\\"tokens\\":[\\"AB\\"]}"}}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass
            finally:
                finished.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received, release, finished, records
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_process_kill_during_real_http_requires_offline_recovery(directory):
    with delayed_model(directory) as (url, received, release, finished, records):
        code = '''
import os, sys
from pathlib import Path
from linguistic_oj import auth_config
from linguistic_oj.executor_state import ExecutorState, executor_lock, GuardedQwenProvider
from linguistic_oj.model_inputs import SegmentationModelInput
from linguistic_oj.providers import ModelIdentity, ModelRequest
from linguistic_oj.responses import TaskType
if os.name == 'nt':
    auth_config._check_windows_acl = lambda path: None
directory = Path(sys.argv[1])
with executor_lock(directory):
    state = ExecutorState(directory, 'a' * 64)
    provider = GuardedQwenProvider(executor_state=state, challenge_id='fixture',
        base_url=sys.argv[2], identity=ModelIdentity(model='Qwen/Qwen3.5-9B',
            revision='a' * 40, runtime='vllm', runtime_version='fixture'))
    provider.generate(ModelRequest(task=TaskType.SEGMENTATION, language='Test', treebank='Tiny',
        student_prompt='private crash fixture', model_input=SegmentationModelInput(text='AB')))
'''
        child = subprocess.Popen([sys.executable, "-c", code, str(directory), url],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert received.wait(10), "isolated child did not reach the fake model"
            assert records[0]["pending"] is not None
            with pytest.raises(ExecutorBusy):
                with executor_lock(directory):
                    pytest.fail("duplicate executor started while a request was running")
            child.kill()
            child.communicate(timeout=10)
            with executor_lock(directory):
                state = ExecutorState(directory, BINDING)
                with pytest.raises(RecoveryRequired):
                    state.require_clean()
                operation = state.snapshot()["pending"]["operation_id"]
                # This is an actual fixture termination observation, not elapsed-time inference.
                release.set()
                assert finished.wait(5)
                state.recover(operation, evidence(directory, state, operation))
                state.require_clean()
            assert len(records) == 1  # Recovery never replays the killed request.
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_success_clears_barrier_only_after_complete_model_response(directory):
    with delayed_model(directory) as (url, received, release, finished, records):
        release.set()
        with executor_lock(directory):
            state = ExecutorState(directory, BINDING)
            result = provider(state, url).generate(request())
            assert json.loads(result.raw_text) == {"tokens": ["AB"]}
            state.require_clean()
        assert received.is_set() and finished.wait(5)
        assert records[0]["pending"] is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM drain is checked on Linux CI")
def test_actual_executor_sigterm_drains_request_without_starting_another(directory):
    with delayed_model(directory) as (url, received, release, finished, records):
        code = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from linguistic_oj import qwen_executor
from linguistic_oj.executor_state import GuardedQwenProvider
from linguistic_oj.model_inputs import SegmentationModelInput
from linguistic_oj.providers import ModelIdentity, ModelRequest
from linguistic_oj.responses import TaskType
qwen_executor.load_plan = lambda path: SimpleNamespace(
    state_dir=Path(sys.argv[1]), binding_sha256='a' * 64)
def build(plan, state, resources, stop):
    provider = GuardedQwenProvider(executor_state=state, challenge_id='fixture',
        base_url=sys.argv[2], identity=ModelIdentity(model='Qwen/Qwen3.5-9B',
            revision='a' * 40, runtime='vllm', runtime_version='fixture'))
    def run_once():
        provider.generate(ModelRequest(task=TaskType.SEGMENTATION,
            language='Test', treebank='Tiny', student_prompt='signal fixture',
            model_input=SegmentationModelInput(text='AB')))
        return True
    return {'fixture': SimpleNamespace(run_once=run_once)}
qwen_executor.build_workers = build
raise SystemExit(qwen_executor.main(['run', '--config', 'unused-fixture']))
'''
        child = subprocess.Popen([sys.executable, "-c", code, str(directory), url],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert received.wait(10)
            child.send_signal(signal.SIGTERM)
            assert read_state(directory)["pending"] is not None
            release.set()
            _, errors = child.communicate(timeout=10)
            assert child.returncode == 0, errors.decode()
            assert finished.wait(5)
            assert len(records) == 1
            with executor_lock(directory):
                ExecutorState(directory, BINDING).require_clean()
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
