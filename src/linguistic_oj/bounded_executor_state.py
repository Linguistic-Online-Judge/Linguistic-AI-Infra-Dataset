"""Isolated bounded-executor prototype: one durable record for every in-flight request.

Not a production entry point or a migration of the existing serial executor state.
The caller must hold executor_lock for the lifetime of this shared state object.
"""

import argparse
import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Event, RLock

from .auth_config import _read_protected
from .executor_state import (
    ExecutorBusy,
    RecoveryRequired,
    _check_directory,
    _now,
    _sync_directory,
    _write_atomic,
    executor_lock,
)
from .mvp_contract import canonical_sha256
from .providers import OpenAICompatibleProvider, ProviderContractError, ProviderRequestError

VERSION = 'qwen-bounded-executor-prototype-v1'


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _request_id(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) is not None


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def _valid_request_context(value):
    return (isinstance(value, dict) and set(value) == {
        'submission_sha256', 'sample_id_sha256', 'sample_position', 'request_sha256'}
        and all(_sha(value[name]) for name in (
            'submission_sha256', 'sample_id_sha256', 'request_sha256'))
        and type(value['sample_position']) is int and 1 <= value['sample_position'] <= 1000000)


def read_bounded_state(directory):
    _check_directory(directory)
    state = json.loads(_read_protected(directory / 'state.json', max_bytes=65536))
    if (not isinstance(state, dict) or set(state) != {
        'version', 'binding_sha256', 'max_inflight', 'pending', 'blocked', 'last_recovery'
    } or state['version'] != VERSION or not _sha(state['binding_sha256'])
            or type(state['max_inflight']) is not int or not 1 <= state['max_inflight'] <= 32
            or type(state['blocked']) is not bool or not isinstance(state['pending'], dict)
            or len(state['pending']) > state['max_inflight']):
        raise ValueError('invalid bounded executor state')
    for key, record in state['pending'].items():
        if (not _request_id(key) or not isinstance(record, dict)
                or set(record) not in ({'challenge_id', 'started_at'},
                                      {'challenge_id', 'started_at', 'request_context'})
                or not _text(record['challenge_id']) or not _text(record['started_at'])
                or ('request_context' in record
                    and not _valid_request_context(record['request_context']))):
            raise ValueError('invalid pending request record')
    last = state['last_recovery']
    if last is not None:
        if not _sha(last):
            raise ValueError('invalid recovery reference')
        _check_directory(directory / 'recoveries')
        audit = json.loads(_read_protected(directory / 'recoveries' / (last + '.json'),
                                          max_bytes=65536))
        if (not isinstance(audit, dict) or audit.get('binding_sha256') != state['binding_sha256']
                or audit.get('recovery_id') != last
                or not isinstance(audit.get('pending'), dict) or not audit['pending']
                or canonical_sha256({'binding': state['binding_sha256'],
                    'capacity': state['max_inflight'], 'pending': audit['pending']}) != last
                or not isinstance(audit.get('evidence'), dict)
                or audit['evidence'].get('all_prior_requests_terminated') is not True
                or audit['evidence'].get('pending_request_ids') != sorted(audit['pending'])
                or not _sha(audit.get('evidence_sha256'))):
            raise ValueError('bounded executor recovery audit mismatch')
    return state


class BoundedExecutorState:
    def __init__(self, directory, binding_sha256):
        self.directory = directory
        self.binding_sha256 = binding_sha256
        self._lock = RLock()
        self._fault_signal = Event()
        self._live = set()
        self._poisoned = False
        first = self.snapshot()
        self.max_inflight = first['max_inflight']
        self._startup_pending = bool(first['pending'])
        if self._startup_pending or first['blocked']:
            self._fault_signal.set()

    @property
    def dispatch_faulted(self):
        """Nonblocking fault observation; never a replacement for the durable dispatch gate."""
        return self._fault_signal.is_set()

    @classmethod
    def initialize(cls, directory, binding_sha256, *, max_inflight=1):
        _check_directory(directory)
        if not _sha(binding_sha256) or type(max_inflight) is not int or not 1 <= max_inflight <= 32:
            raise ValueError('invalid bounded executor initialization')
        if any(path.name != '.executor.lock' for path in directory.iterdir()):
            raise ValueError('initialization requires a new empty dedicated directory')
        _write_atomic(directory / 'state.json', {'version': VERSION,
            'binding_sha256': binding_sha256, 'max_inflight': max_inflight,
            'pending': {}, 'blocked': False, 'last_recovery': None})
        return cls(directory, binding_sha256)

    def snapshot(self):
        with self._lock:
            try:
                state = read_bounded_state(self.directory)
                if (state['binding_sha256'] != self.binding_sha256
                        or state['max_inflight'] != getattr(
                            self, 'max_inflight', state['max_inflight'])):
                    raise ValueError('bounded executor binding or capacity changed')
                return state
            except BaseException:
                self._fault_signal.set()
                raise

    def _write(self, state):
        previous = self._poisoned
        self._poisoned = True
        try:
            _write_atomic(self.directory / 'state.json', state)
        except BaseException:
            self._fault_signal.set()
            raise
        self._poisoned = previous

    def require_dispatchable(self):
        with self._lock:
            state = self.snapshot()
            if (self._fault_signal.is_set() or self._poisoned or self._startup_pending
                    or state['blocked'] or set(state['pending']) != self._live):
                self._fault_signal.set()
                raise RecoveryRequired('bounded executor requires termination reconciliation')

    def require_clean(self):
        with self._lock:
            self.require_dispatchable()
            if self.snapshot()['pending']:
                raise RecoveryRequired('bounded executor still has in-flight requests')

    @contextmanager
    def claim_guard(self):
        """Serialize queue/DB admission against an incident; never hold across generation."""
        with self._lock:
            self.require_dispatchable()
            yield

    def begin(self, challenge_id, *, request_context=None):
        if not _text(challenge_id):
            raise ValueError('invalid request challenge identity')
        if request_context is not None and not _valid_request_context(request_context):
            raise ValueError('invalid request correlation context')
        with self._lock:
            self.require_dispatchable()
            state = self.snapshot()
            if len(state['pending']) >= self.max_inflight:
                raise ExecutorBusy('bounded executor request slots are full')
            operation = uuid.uuid4().hex
            state['pending'][operation] = {'challenge_id': challenge_id, 'started_at': _now()}
            if request_context is not None:
                state['pending'][operation]['request_context'] = dict(request_context)
            self._write(state)
            self._live.add(operation)
            return operation

    def finish(self, operation):
        with self._lock:
            state = self.snapshot()
            if operation not in self._live or operation not in state['pending']:
                self._poisoned = True
                self._fault_signal.set()
                raise RecoveryRequired('request completion does not match live ownership')
            if self._fault_signal.is_set() or self._poisoned or state['blocked']:
                # Drain callers, but retain conservative recovery evidence after any incident.
                # In particular, an earlier replace/fsync failure may have partially committed.
                self._live.remove(operation)
                return
            del state['pending'][operation]
            self._write(state)
            self._live.remove(operation)

    def block(self, operation):
        with self._lock:
            self._poisoned = True
            self._fault_signal.set()
            state = self.snapshot()
            if operation not in self._live or operation not in state['pending']:
                raise RecoveryRequired('uncertain request identity changed')
            state['blocked'] = True
            self._write(state)

    def recover_all(self, expected_pending_sha256, evidence_file):
        """Offline operator reconciliation; never replays a request or updates a submission."""
        with self._lock:
            if self._live:
                raise RecoveryRequired('stop the old executor before recovery')
            state = self.snapshot()
            pending_hash = canonical_sha256(state['pending'])
            if not state['pending'] or expected_pending_sha256 != pending_hash:
                raise ValueError('pending request set does not match recovery')
            raw = _read_protected(evidence_file)
            evidence = json.loads(raw)
            if (not isinstance(evidence, dict) or set(evidence) != {
                'binding_sha256', 'pending_request_ids', 'all_prior_requests_terminated',
                'confirmed_by', 'evidence_reference'
            } or evidence['binding_sha256'] != self.binding_sha256
                    or evidence['pending_request_ids'] != sorted(state['pending'])
                    or evidence['all_prior_requests_terminated'] is not True
                    or not _text(evidence['confirmed_by'])
                    or not _text(evidence['evidence_reference'])):
                raise ValueError('explicit evidence for the entire pending set is required')
            recovery_id = canonical_sha256({'binding': self.binding_sha256,
                'capacity': self.max_inflight, 'pending': state['pending']})
            archive = self.directory / 'recoveries'
            if not archive.exists():
                archive.mkdir(mode=0o700)
                _sync_directory(self.directory)
            _check_directory(archive)
            audit_path = archive / (recovery_id + '.json')
            audit = {'recovery_id': recovery_id, 'binding_sha256': self.binding_sha256,
                     'pending': state['pending'], 'evidence': evidence,
                     'evidence_sha256': hashlib.sha256(raw.encode()).hexdigest()}
            if audit_path.exists():
                if json.loads(_read_protected(audit_path, max_bytes=65536)) != audit:
                    raise ValueError('recovery evidence differs from durable audit')
            else:
                _write_atomic(audit_path, audit)
            self._write({**state, 'pending': {}, 'blocked': False, 'last_recovery': recovery_id})
            self._startup_pending = False
            self._poisoned = False
            self._fault_signal.clear()


class BoundedGuardedProvider(OpenAICompatibleProvider):
    """Request-preserving prototype, explicitly rejected by production runtime verification."""
    experimental_execution = True

    def __init__(self, *, executor_state, challenge_id, **kwargs):
        if not isinstance(executor_state, BoundedExecutorState):
            raise TypeError('bounded provider requires a shared BoundedExecutorState')
        super().__init__(**kwargs)
        self.executor_state = executor_state
        self.challenge_id = challenge_id

    def generate(self, request, /, *, timeout_seconds=None):
        return self._generate_guarded(request, timeout_seconds=timeout_seconds)

    def _generate_guarded(self, request, *, timeout_seconds=None, request_context=None):
        operation = self.executor_state.begin(self.challenge_id, request_context=request_context)
        try:
            result = super().generate(request, timeout_seconds=timeout_seconds)
        except (ProviderRequestError, ProviderContractError) as error:
            confirmed = not isinstance(error, ProviderRequestError) or error.termination_confirmed
            if confirmed and not self.has_active_request:
                self.executor_state.finish(operation)
            else:
                self.executor_state.block(operation)
            raise
        except BaseException:
            self.executor_state.block(operation)
            raise
        if self.has_active_request:
            self.executor_state.block(operation)
            raise RecoveryRequired('request termination is not confirmed')
        self.executor_state.finish(operation)
        return result


def main(arguments=None):
    """Observation and offline recovery only; deliberately no model-start/run command."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status')
    status.add_argument('--state-dir', type=Path, required=True)
    recovery = commands.add_parser('recover')
    recovery.add_argument('--state-dir', type=Path, required=True)
    recovery.add_argument('--expected-pending-sha256', required=True)
    recovery.add_argument('--evidence-file', type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        if args.command == 'status':
            busy = False
            try:
                with executor_lock(args.state_dir):
                    state = read_bounded_state(args.state_dir)
            except ExecutorBusy:
                busy = True
                state = read_bounded_state(args.state_dir)
            print(json.dumps({**state, 'process_lock_held': busy, 'observation_only': True,
                              'pending_sha256': canonical_sha256(state['pending'])}))
            return 0
        with executor_lock(args.state_dir):
            snapshot = read_bounded_state(args.state_dir)
            state = BoundedExecutorState(args.state_dir, snapshot['binding_sha256'])
            state.recover_all(args.expected_pending_sha256, args.evidence_file)
        print('bounded_executor_recovery_recorded_no_requests_replayed')
        return 0
    except (ExecutorBusy, RecoveryRequired):
        print('bounded_executor_recovery_blocked')
        return 75
    except (OSError, ValueError, TypeError, KeyError):
        print('bounded_executor_state_or_evidence_invalid')
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
