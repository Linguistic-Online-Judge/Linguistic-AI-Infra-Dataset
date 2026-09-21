import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

import linguistic_oj.bounded_executor_state as module
from linguistic_oj import auth_config, providers
from linguistic_oj.bounded_dispatch import run_bounded
from linguistic_oj.bounded_executor_state import (
    BoundedExecutorState,
    BoundedGuardedProvider,
    read_bounded_state,
)
from linguistic_oj.executor_state import (
    ExecutorBusy,
    ExecutorState,
    RecoveryRequired,
    executor_lock,
)
from linguistic_oj.model_inputs import SegmentationModelInput
from linguistic_oj.mvp_contract import canonical_sha256
from linguistic_oj.providers import ModelIdentity, ModelRequest, ProviderTransportError
from linguistic_oj.responses import TaskType

BINDING = 'b' * 64


@pytest.fixture(autouse=True)
def permissions(monkeypatch):
    if os.name == 'nt':
        monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / 'bounded'
    path.mkdir(mode=0o700)
    with executor_lock(path, create=True):
        BoundedExecutorState.initialize(path, BINDING, max_inflight=2)
    return path


def make_provider(state):
    return BoundedGuardedProvider(executor_state=state, challenge_id='fixture',
        base_url='http://127.0.0.1:8000/v1', identity=ModelIdentity(
            model='fixture', revision='a' * 40, runtime='fixture', runtime_version='fixture'))


def request():
    return ModelRequest(task=TaskType.SEGMENTATION, language='Test', treebank='Fixture',
        student_prompt='private prompt', model_input=SegmentationModelInput(text='AB'))


def proof(directory, state, pending):
    path = directory.parent / 'proof.json'
    path.write_text(json.dumps({'binding_sha256': state.binding_sha256,
        'pending_request_ids': sorted(pending), 'all_prior_requests_terminated': True,
        'confirmed_by': 'fixture-operator', 'evidence_reference': 'terminated fake requests'}))
    path.chmod(0o600)
    return path


def test_state_versions_capacity_binding_and_exclusive_lock(directory):
    with executor_lock(directory):
        with pytest.raises(ExecutorBusy), executor_lock(directory):
            pytest.fail('duplicate executor acquired the lock')
        with pytest.raises(ValueError):
            ExecutorState(directory, BINDING)
        with pytest.raises(ValueError):
            BoundedExecutorState(directory, 'a' * 64)
        with pytest.raises(ValueError):
            BoundedExecutorState.initialize(directory, BINDING)
        state = BoundedExecutorState(directory, BINDING)
        first, second = state.begin('first'), state.begin('second')
        with pytest.raises(ExecutorBusy):
            state.begin('third')
        state.finish(second)
        assert set(state.snapshot()['pending']) == {first}
        state.finish(first)
        state.require_clean()


def test_parallel_updates_do_not_lose_each_others_pending_records(directory):
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)
        barrier = Barrier(2)

        def run(index):
            for _ in range(8):
                operation = state.begin(str(index))
                barrier.wait(timeout=5)
                assert len(state.snapshot()['pending']) == 2
                barrier.wait(timeout=5)
                state.finish(operation)
                barrier.wait(timeout=5)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, index) for index in range(2)]
            for future in futures:
                future.result(timeout=30)
        state.require_clean()


def test_disk_failure_blocks_send_and_new_dispatch(directory, monkeypatch):
    attempts = []
    monkeypatch.setattr(providers, 'urlopen', lambda *args, **kwargs: attempts.append(1))
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)

        def fail(*args):
            raise OSError('write failed')

        monkeypatch.setattr(module, '_write_atomic', fail)
        with pytest.raises(OSError):
            make_provider(state).generate(request())
        with pytest.raises(RecoveryRequired):
            state.require_dispatchable()
    assert attempts == []


@pytest.mark.parametrize('confirmed', [False, True])
def test_transport_failure_is_durable_and_other_request_can_drain(
    directory, monkeypatch, confirmed,
):
    def fail(*args, **kwargs):
        assert len(read_bounded_state(directory)['pending']) == 2
        if confirmed:
            raise HTTPError('http://fixture', 503, 'fixture', None, None)
        raise URLError('disconnected')

    monkeypatch.setattr(providers, 'urlopen', fail)
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)
        other = state.begin('already-running')
        with pytest.raises(ProviderTransportError):
            make_provider(state).generate(request())
        state.finish(other)
        if confirmed:
            state.require_clean()
        else:
            assert len(state.snapshot()['pending']) == 2
            assert state.snapshot()['blocked']
            with pytest.raises(RecoveryRequired):
                state.begin('must-not-start')
            with pytest.raises(RecoveryRequired):
                BoundedExecutorState(directory, BINDING).require_dispatchable()
    assert 'private prompt' not in (directory / 'state.json').read_text()


def test_crashed_process_leaves_all_requests_and_requires_exact_full_recovery(directory):
    code = """
import os, sys
from pathlib import Path
from linguistic_oj import auth_config
from linguistic_oj.executor_state import executor_lock
from linguistic_oj.bounded_executor_state import BoundedExecutorState
if os.name == 'nt':
    auth_config._check_windows_acl = lambda path: None
path = Path(sys.argv[1])
with executor_lock(path):
    state = BoundedExecutorState(path, 'b' * 64)
    state.begin('first')
    state.begin('second')
    os._exit(23)
"""
    result = subprocess.run([sys.executable, '-c', code, str(directory)], timeout=15)
    assert result.returncode == 23
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)
        pending = state.snapshot()['pending']
        assert len(pending) == 2
        with pytest.raises(RecoveryRequired):
            state.require_dispatchable()
        evidence = proof(directory, state, pending)
        value = json.loads(evidence.read_text())
        value['pending_request_ids'].pop()
        evidence.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            state.recover_all(canonical_sha256(pending), evidence)
        evidence = proof(directory, state, pending)
        with pytest.raises(ValueError):
            state.recover_all('c' * 64, evidence)
        state.recover_all(canonical_sha256(pending), evidence)
        state.require_clean()
        assert len(list((directory / 'recoveries').glob('*.json'))) == 1
        with pytest.raises(ValueError):
            state.recover_all(canonical_sha256(pending), evidence)


def test_recovery_interruption_after_audit_is_resumable_without_overwrite(directory, monkeypatch):
    with executor_lock(directory):
        original = BoundedExecutorState(directory, BINDING)
        original.begin('fixture')
        state = BoundedExecutorState(directory, BINDING)
        pending = state.snapshot()['pending']
        evidence = proof(directory, state, pending)
        write = module._write_atomic

        def fail_clear(path, value):
            if path.name == 'state.json':
                raise OSError('interrupted clear')
            write(path, value)

        monkeypatch.setattr(module, '_write_atomic', fail_clear)
        with pytest.raises(OSError):
            state.recover_all(canonical_sha256(pending), evidence)
        audit = next((directory / 'recoveries').glob('*.json'))
        previous = audit.read_bytes()
        monkeypatch.setattr(module, '_write_atomic', write)
        restarted = BoundedExecutorState(directory, BINDING)
        restarted.recover_all(canonical_sha256(pending), evidence)
        assert audit.read_bytes() == previous
        restarted.require_clean()


def test_dispatcher_failure_stops_new_jobs_and_drains_other_lane(directory):
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)
        entered, blocked, release = Event(), Event(), Event()
        calls, errors = [], []

        def first():
            operation = state.begin('uncertain')
            assert entered.wait(5)
            state.block(operation)
            blocked.set()
            return True

        def second():
            operation = state.begin('draining')
            calls.append('started')
            entered.set()
            assert release.wait(5)
            state.finish(operation)
            calls.append('finished')
            return True

        def run():
            try:
                run_bounded([{'task': SimpleNamespace(run_once=first,
                                                       _provider=make_provider(state))},
                             {'task': SimpleNamespace(run_once=second,
                                                       _provider=make_provider(state))}],
                            state, Event(), model_capacity=2)
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run)
        thread.start()
        try:
            assert blocked.wait(5)
            assert thread.is_alive()
        finally:
            release.set()
        thread.join(5)
        assert not thread.is_alive() and isinstance(errors[0], RecoveryRequired)
        assert calls == ['started', 'finished']
        assert len(state.snapshot()['pending']) == 2


def test_partial_finish_write_failure_cannot_erase_the_remaining_recovery_barrier(
    directory, monkeypatch,
):
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)
        first, second = state.begin('first'), state.begin('second')
        write = module._write_atomic

        def fail_after_replace(path, value):
            write(path, value)
            raise OSError('directory fsync failure after replacement')

        monkeypatch.setattr(module, '_write_atomic', fail_after_replace)
        with pytest.raises(OSError):
            state.finish(first)
        monkeypatch.setattr(module, '_write_atomic', write)
        state.finish(second)
        assert set(state.snapshot()['pending']) == {second}
        with pytest.raises(RecoveryRequired):
            BoundedExecutorState(directory, BINDING).require_dispatchable()


def test_normal_stop_drains_all_claimed_jobs_without_new_jobs(directory):
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)
        both = Barrier(3)
        stop, release = Event(), Event()
        calls, errors = [], []

        def worker(index):
            def run_once():
                calls.append(index)
                both.wait(timeout=5)
                assert release.wait(5)
                # A claimed job may still start its next sample after graceful stop.
                operation = state.begin(str(index))
                state.finish(operation)
                return True
            return SimpleNamespace(run_once=run_once, _provider=make_provider(state))

        def execute():
            try:
                run_bounded([{'task': worker(0)}, {'task': worker(1)}],
                            state, stop, model_capacity=2)
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=execute)
        thread.start()
        try:
            both.wait(timeout=5)
            stop.set()
            assert thread.is_alive()
        finally:
            release.set()
        thread.join(5)
        assert not thread.is_alive() and not errors and sorted(calls) == [0, 1]
        state.require_clean()


def test_dispatcher_rejects_worker_sharing_and_capacity_mismatch(directory):
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)
        worker = SimpleNamespace(run_once=lambda: pytest.fail('invalid plan reached worker'))
        with pytest.raises(ValueError):
            run_bounded([{'task': worker}] * 2, state, Event(), model_capacity=2)
        with pytest.raises(ValueError):
            run_bounded([{'a': worker}, {'a': object()}], state, Event(), model_capacity=1)


def test_dispatcher_rejects_unprotected_shared_or_contract_mismatched_providers(directory):
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)

        def forbidden():
            pytest.fail('invalid provider plan reached a worker')

        with pytest.raises(ValueError, match='shared request ledger'):
            run_bounded([{'a': SimpleNamespace(run_once=forbidden)}],
                        state, Event(), model_capacity=2)
        provider = make_provider(state)
        lanes = [{'a': SimpleNamespace(run_once=forbidden, _provider=provider)} for _ in range(2)]
        with pytest.raises(ValueError, match='shared request ledger'):
            run_bounded(lanes, state, Event(), model_capacity=2)
        worker = SimpleNamespace(run_once=forbidden, _provider=provider,
                                 _contract=SimpleNamespace(worker_model_concurrency=1))
        with pytest.raises(ValueError, match='contract capacity'):
            run_bounded([{'a': worker}], state, Event(), model_capacity=2)


def test_process_kill_with_two_real_http_requests_requires_full_termination_proof(directory):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Lock

    received, release, finished = Event(), Event(), Event()
    lock = Lock()
    calls, exits = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            with lock:
                calls.append(len(read_bounded_state(directory)['pending']))
                if len(calls) == 2:
                    received.set()
            try:
                release.wait(30)
                body = b'{"choices":[{"message":{"content":"{\\"tokens\\":[\\"AB\\"]}"}}]}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    exits.append(1)
                    if len(exits) == 2:
                        finished.set()

    code = '''
import os, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from linguistic_oj import auth_config
from linguistic_oj.executor_state import executor_lock
from linguistic_oj.bounded_executor_state import BoundedExecutorState, BoundedGuardedProvider
from linguistic_oj.model_inputs import SegmentationModelInput
from linguistic_oj.providers import ModelIdentity, ModelRequest
from linguistic_oj.responses import TaskType
if os.name == 'nt':
    auth_config._check_windows_acl = lambda path: None
with executor_lock(Path(sys.argv[1])):
    state = BoundedExecutorState(Path(sys.argv[1]), 'b' * 64)
    def send(index):
        provider = BoundedGuardedProvider(executor_state=state, challenge_id=str(index),
            base_url=sys.argv[2], identity=ModelIdentity(model='fixture', revision='a'*40,
            runtime='fixture', runtime_version='fixture'))
        return provider.generate(ModelRequest(task=TaskType.SEGMENTATION, language='Test',
            treebank='Fixture', student_prompt='private',
            model_input=SegmentationModelInput(text='AB')))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(send, (0, 1)))
'''
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    child = subprocess.Popen([sys.executable, '-c', code, str(directory),
        f'http://127.0.0.1:{server.server_port}/v1'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert received.wait(15), 'child did not send both requests'
        assert calls[-1] == 2
        child.kill()
        child.communicate(timeout=10)
        with executor_lock(directory):
            state = BoundedExecutorState(directory, BINDING)
            with pytest.raises(RecoveryRequired):
                state.require_dispatchable()
            assert not finished.is_set()  # Client exit does not prove remote termination.
            release.set()
            assert finished.wait(10)
            pending = state.snapshot()['pending']
            assert len(pending) == 2
            state.recover_all(canonical_sha256(pending), proof(directory, state, pending))
            state.require_clean()
        assert len(calls) == 2  # Recovery did not resend either request.
    finally:
        release.set()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_offline_cli_observes_busy_state_and_never_recovers_under_live_lock(directory, capsys):
    with executor_lock(directory):
        state = BoundedExecutorState(directory, BINDING)
        state.begin('fixture')
        pending = state.snapshot()['pending']
        evidence = proof(directory, state, pending)
        before = (directory / 'state.json').read_bytes()
        assert module.main(['status', '--state-dir', str(directory)]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report['process_lock_held'] and report['observation_only']
        assert report['pending_sha256'] == canonical_sha256(pending)
        args = ['recover', '--state-dir', str(directory), '--expected-pending-sha256',
                report['pending_sha256'], '--evidence-file', str(evidence)]
        assert module.main(args) == 75
        assert (directory / 'state.json').read_bytes() == before
    assert module.main(args) == 0
    assert module.main(args) == 78
