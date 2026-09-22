"""Versioned request execution for the private, same-process Qwen development workbench.

Offline profile/state preparation never starts a model or migrates a live instance.
Legacy/public executors continue to reject experimental execution providers.
"""

import argparse
import json
import os
import re
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import MappingProxyType

from .auth_config import _read_protected
from .bounded_executor_state import BoundedExecutorState, read_bounded_state
from .challenge_registry import load_challenge_contract_registry
from .executor_state import (
    ExecutorBusy,
    RecoveryRequired,
    _check_directory,
    _sync_directory,
    executor_lock,
)
from .local_dev import _state_lock
from .mvp_contract import EvaluationContract, canonical_sha256
from .providers import GenerationSettings, ModelIdentity, OpenAICompatibleProvider
from .qwen_runtime import (
    QwenTokenizerPreflight,
    attest_qwen_runtime_from_snapshot,
    validate_qwen_evaluation_contract,
    verify_qwen_runtime,
)
from .request_submission_executor import RequestSubmissionExecutor
from .sample_scheduler import ScheduledProvider

VERSION = 'qwen-request-development-v1'
STATE_CHILD = 'request-executor'
BASE_URL = 'http://127.0.0.1:8000/v1'


@dataclass(frozen=True, repr=False)
class ExecutionProfile:
    capacity: int
    max_jobs: int
    source_hashes: object
    contracts: object
    sha256: str


def profile_document(contracts, *, capacity=32, max_jobs=4):
    if type(capacity) is not int or capacity not in (16, 32):
        raise ValueError('development request capacity must be16 or32')
    if type(max_jobs) is not int or max_jobs not in (4, 8, 16):
        raise ValueError('active job capacity must be4,8 or16')
    if not contracts:
        raise ValueError('execution profile requires registered contracts')
    entries = {}
    for key, contract in contracts.items():
        validate_qwen_evaluation_contract(contract)
        if contract.evaluation_identity['model_identity']['model'] != 'Qwen/Qwen3.5-9B':
            raise ValueError('execution profile requires the fixed Qwen model')
        value = json.loads(contract.snapshot_json)
        value['limits']['worker_model_concurrency'] = capacity
        derived = EvaluationContract.from_mapping(value)
        entries[key] = {'source_contract_sha256': contract.contract_snapshot_sha256,
                        'execution_contract': json.loads(derived.snapshot_json)}
    return {'version': VERSION, 'request_slots': capacity, 'max_active_jobs': max_jobs,
            'contracts': entries}


def load_profile(path, contracts):
    value = json.loads(_read_protected(path, max_bytes=4 * 1024 * 1024))
    if not isinstance(value, dict) or set(value) != {
        'version', 'request_slots', 'max_active_jobs', 'contracts'
    } or value['version'] != VERSION:
        raise ValueError('invalid request execution profile')
    expected = profile_document(contracts, capacity=value['request_slots'],
                                max_jobs=value['max_active_jobs'])
    # Only the model concurrency limit may differ. No deadline, prompt, score or quota edits.
    if canonical_sha256(value) != canonical_sha256(expected):
        raise ValueError('execution profile differs from its frozen source contracts')
    derived = {key: EvaluationContract.from_mapping(item['execution_contract'])
               for key, item in value['contracts'].items()}
    return ExecutionProfile(value['request_slots'], value['max_active_jobs'],
        MappingProxyType({key: item.contract_snapshot_sha256 for key, item in contracts.items()}),
        MappingProxyType(derived), canonical_sha256(value))


def write_profile(path, contracts, *, capacity=32, max_jobs=4):
    _check_directory(path.parent)
    value = profile_document(contracts, capacity=capacity, max_jobs=max_jobs)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    _sync_directory(path.parent)
    return canonical_sha256(value)


def state_binding(profile, marker):
    instance = marker.get('instance', '')
    if (marker.get('kind') != 'linguistic-oj-private-qwen18-v1'
            or not isinstance(instance, str) or not re.fullmatch('[0-9a-f]{32}', instance)
            or marker.get('database') != 'loj_dev18_' + instance[:16]
            or not isinstance(marker.get('owner'), str) or not marker['owner']
            or marker.get('contracts') != dict(profile.source_hashes)):
        raise ValueError('request profile does not bind the existing development instance')
    return canonical_sha256({'version': VERSION, 'profile': profile.sha256,
        'instance': instance, 'owner': marker['owner'], 'database': marker['database'],
        'namespace': 'loj-qwen-dev:' + instance, 'model_endpoint': BASE_URL})


def initialize_state(state_dir, profile):
    _check_directory(state_dir)
    with _state_lock(state_dir):
        if (state_dir / 'uncertain-inference.json').exists():
            raise RecoveryRequired('legacy inference must be reconciled first')
        marker = json.loads(_read_protected(state_dir / 'instance.json', max_bytes=1048576))
        binding = state_binding(profile, marker)
        child = state_dir / STATE_CHILD
        child.mkdir(mode=0o700)  # Never replace/reinitialize existing state or a recovery archive.
        _sync_directory(state_dir)
        with executor_lock(child, create=True):
            BoundedExecutorState.initialize(child, binding, max_inflight=profile.capacity)
    return binding


def require_compatible_outstanding(store, contracts):
    allowed = {contract.contract_snapshot_sha256 for contract in contracts.values()}
    if store.outstanding_contract_hashes() - allowed:
        raise ValueError('drain submissions from the previous execution profile before switching')


def require_legacy_safe(state_dir):
    """Serial fallback must not bypass an incident left by request-level execution."""
    child = state_dir / STATE_CHILD
    if child.exists():
        with executor_lock(child):
            value = read_bounded_state(child)
            BoundedExecutorState(child, value['binding_sha256']).require_clean()


class RequestDevelopmentRuntime:
    def __init__(self, executor, state, resources, stop):
        self.executor, self.state = executor, state
        self._resources, self.stop = resources, stop
        self._running = Event()
        self.availability = executor.availability

    def run(self):
        self._running.set()
        try:
            report = self.executor.run()
            if not self.stop.is_set():
                raise RuntimeError('request executor exited unexpectedly')
            return report
        finally:
            self._running.clear()

    def close(self):
        if self._running.is_set():
            raise RuntimeError('drain request execution before releasing its state lock')
        self._resources.close()


def build_runtime(*, profile, marker, state_dir, store, queues, artifacts,
                  tokenizer_snapshot, launch_evidence, model_healthy, selection_cache):
    resources = ExitStack()
    try:
        binding = state_binding(profile, marker)
        directory = state_dir / STATE_CHILD
        resources.enter_context(executor_lock(directory))
        state = BoundedExecutorState(directory, binding)
        if state.max_inflight != profile.capacity:
            raise ValueError('persistent capacity differs from the selected execution profile')
        state.require_clean()
        require_compatible_outstanding(store, profile.contracts)
        preflights, slots, attested = {}, [dict() for _ in range(profile.capacity)], None
        for key, contract in profile.contracts.items():
            identity = contract.evaluation_identity
            settings = {'base_url': BASE_URL,
                'identity': ModelIdentity(**identity['model_identity']),
                'settings': GenerationSettings(**identity['generation_settings']),
                'timeout_seconds': contract.provider_request_timeout_seconds,
                'max_response_body_bytes': contract.provider_response_body_bytes}
            # Attest unchanged HTTP requests through the normal pinned runtime path. This
            # factory alone constructs the correlation-bound slots from those same settings.
            plain = OpenAICompatibleProvider(**settings)
            if attested is None:
                attested = attest_qwen_runtime_from_snapshot(contract, plain,
                    tokenizer_snapshot_path=tokenizer_snapshot,
                    launch_evidence_path=launch_evidence)
            verify_qwen_runtime(contract, plain, attested.attestation)
            preflights[key] = QwenTokenizerPreflight(contract, attested.tokenizer,
                                                    attested.tokenizer_identity)
            for slot in slots:
                slot[key] = ScheduledProvider(executor_state=state, challenge_id=key, **settings)
        stop = Event()
        executor = RequestSubmissionExecutor(store=store, queues=queues,
            contracts=profile.contracts, artifacts=artifacts, preflights=preflights, slots=slots,
            state=state, model_capacity=profile.capacity, max_jobs=profile.max_jobs, stop=stop,
            selection_cache=selection_cache, model_healthy=model_healthy)
        return RequestDevelopmentRuntime(executor, state, resources, stop)
    except BaseException:
        resources.close()
        raise


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'check', 'init'):
        sub = commands.add_parser(name)
        sub.add_argument('--root', type=Path, required=True)
        sub.add_argument('--registry', type=Path, required=True)
        sub.add_argument('--profile', type=Path, required=True)
        if name == 'prepare':
            sub.add_argument('--request-slots', type=int, choices=(16, 32), default=32)
            sub.add_argument('--max-active-jobs', type=int, choices=(4, 8, 16), default=4)
        if name == 'init':
            sub.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        registry = load_challenge_contract_registry(args.root.resolve(), args.registry)
        if args.command == 'prepare':
            digest = write_profile(args.profile.absolute(), registry.contracts,
                capacity=args.request_slots, max_jobs=args.max_active_jobs)
            print(json.dumps({'profile_sha256': digest, 'services_started': False}))
            return 0
        profile = load_profile(args.profile, registry.contracts)
        binding = (initialize_state(args.state_dir.absolute(), profile)
                   if args.command == 'init' else None)
        print(json.dumps({'profile_sha256': profile.sha256, 'binding_sha256': binding,
            'request_slots': profile.capacity, 'max_active_jobs': profile.max_jobs,
            'contract_count': len(profile.contracts), 'model_attested': False,
            'services_started': False}))
        return 0
    except (ExecutorBusy, RecoveryRequired):
        print('request_development_reconciliation_or_exclusive_access_required')
        return 75
    except Exception:
        print('request_development_configuration_or_storage_invalid')
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
