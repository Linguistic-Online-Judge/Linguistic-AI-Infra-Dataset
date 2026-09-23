"""Offline request-workbench backup checkpoints and inert restore verification.

Never launches inference, clears pending intents, or installs a restored live instance.
"""

import json
import os
import re
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from .auth_config import _read_protected
from .bounded_executor_state import BoundedExecutorState, read_bounded_state
from .challenge import load_challenge_artifacts
from .challenge_registry import load_challenge_contract_registry
from .executor_state import RecoveryRequired, _check_directory, executor_lock
from .local_dev import _state_lock
from .mvp_contract import canonical_sha256
from .qwen_runtime import QwenLaunchEvidence, TokenizerIdentity
from .request_development import STATE_CHILD, load_profile, state_binding

SETTINGS_VERSION = 'qwen-request-backup-settings-v1'
SETTINGS_FIELDS = {'version', 'source_root', 'data_root', 'state_dir', 'execution_profile',
                   'registry', 'tokenizer_snapshot', 'launch_evidence'}


def _protect_temporary_directory(directory):
    if os.name != 'nt':
        _check_directory(directory)
        return
    # Windows ignores POSIX mode bits. Restrict only this newly-created temporary tree;
    # inherited broad ACLs must not be accepted or bypassed by protected-file readers.
    script = (
        "$ErrorActionPreference='Stop'; "
        "$me=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
        "$acl=[System.Security.AccessControl.DirectorySecurity]::new(); "
        "$acl.SetAccessRuleProtection($true,$false); "
        "$inherit=[System.Security.AccessControl.InheritanceFlags]"
        "'ContainerInherit,ObjectInherit'; "
        "foreach($id in @($me.Value,'S-1-5-18','S-1-5-32-544')) { "
        "$sid=[System.Security.Principal.SecurityIdentifier]::new($id); "
        "$rule=[System.Security.AccessControl.FileSystemAccessRule]::new("
        "$sid,'FullControl',$inherit,'None','Allow'); $acl.AddAccessRule($rule) }; "
        "$directory=[System.IO.DirectoryInfo]::new($env:LOJ_REQUEST_RESTORE_TMP); "
        "$directory.SetAccessControl($acl)"
    )
    subprocess.run([str(Path(os.environ['SystemRoot']) /
        'System32/WindowsPowerShell/v1.0/powershell.exe'), '-NoProfile', '-NonInteractive',
        '-Command', script], env={**os.environ, 'LOJ_REQUEST_RESTORE_TMP': str(directory)},
        capture_output=True, timeout=15, check=True)


def safe_archive_name(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
            or PurePosixPath(name).as_posix() != name or name == '.'):
        raise ValueError('unsafe archive member')
    return name


def validate_execution_inventory(names):
    required = {'execution-profile.json', 'backup-settings.json', f'{STATE_CHILD}/state.json'}
    if not required <= set(names):
        raise ValueError('request checkpoint inventory is incomplete')
    for name in names:
        safe_archive_name(name)
        if name not in required | {'uncertain-inference.json'} and not re.fullmatch(
            STATE_CHILD + r'/recoveries/[0-9a-f]{64}\.json', name
        ):
            raise ValueError('unexpected request checkpoint member')


def backup_settings(settings):
    return {'version': SETTINGS_VERSION, 'source_root': settings['release_root'],
        'data_root': settings['data_root'], 'state_dir': settings['state_dir'],
        'execution_profile': settings['profile'], 'registry': settings['registry'],
        'tokenizer_snapshot': settings['tokenizer_snapshot'],
        'launch_evidence': settings['launch_evidence']}


def validate_settings(value):
    if (not isinstance(value, dict) or set(value) != SETTINGS_FIELDS
            or value['version'] != SETTINGS_VERSION):
        raise ValueError('invalid request backup settings')
    safe_archive_name(value['registry'])
    for key in SETTINGS_FIELDS - {'version', 'registry'}:
        if not isinstance(value[key], str) or not (
            PurePosixPath(value[key]).is_absolute() or PureWindowsPath(value[key]).is_absolute()
        ):
            raise ValueError('backup paths must be explicit and absolute')
    return value


def _audit_files(directory, state):
    result = {}
    for entry in directory.iterdir():
        if entry.name not in {'.executor.lock', 'state.json', 'recoveries'}:
            raise ValueError('unrecognized executor evidence must be inspected before backup')
    archive = directory / 'recoveries'
    if archive.exists():
        _check_directory(archive)
        for path in archive.iterdir():
            if not re.fullmatch('[0-9a-f]{64}\\.json', path.name):
                raise ValueError('invalid recovery audit filename')
            audit = json.loads(_read_protected(path, max_bytes=65536))
            if (not isinstance(audit, dict)
                    or audit.get('binding_sha256') != state['binding_sha256']
                    or audit.get('recovery_id') != path.stem
                    or not isinstance(audit.get('pending'), dict) or not audit['pending']
                    or canonical_sha256({'binding': state['binding_sha256'],
                        'capacity': state['max_inflight'], 'pending': audit['pending']})
                    != path.stem
                    or not isinstance(audit.get('evidence'), dict)
                    or audit['evidence'].get('all_prior_requests_terminated') is not True
                    or audit['evidence'].get('pending_request_ids') != sorted(audit['pending'])
                    or not re.fullmatch('[0-9a-f]{64}', audit.get('evidence_sha256', ''))):
                raise ValueError('invalid archived recovery audit')
            result[f'{STATE_CHILD}/recoveries/{path.name}'] = path
    return result


def inspect_state(state_root, profile, marker):
    directory = state_root / STATE_CHILD
    state = read_bounded_state(directory)
    if (state['binding_sha256'] != state_binding(profile, marker)
            or state['max_inflight'] != profile.capacity):
        raise ValueError('request ledger does not match the profile and instance')
    audits = _audit_files(directory, state)
    legacy = state_root / 'uncertain-inference.json'
    if legacy.exists():
        _read_protected(legacy, max_bytes=65536)
    summary = {'profile_sha256': profile.sha256, 'binding_sha256': state['binding_sha256'],
        'request_slots': profile.capacity, 'max_active_jobs': profile.max_jobs,
        'pending_requests': len(state['pending']), 'blocked': state['blocked'],
        'recovery_audits': len(audits), 'last_recovery': state['last_recovery'],
        'legacy_uncertain_marker': legacy.exists(),
        'requires_reconciliation': bool(state['pending'] or state['blocked'] or legacy.exists())}
    files = {f'{STATE_CHILD}/state.json': directory / 'state.json', **audits}
    if legacy.exists():
        files['uncertain-inference.json'] = legacy
    return state, summary, files


def validate_launch_assets(profile, snapshot, evidence):
    launch = QwenLaunchEvidence.from_path(evidence)
    if launch.model_snapshot_path != snapshot.resolve() or launch.max_num_seqs != profile.capacity:
        raise ValueError('backup model launch evidence differs from the request profile')
    tokenizers = {}
    for contract in profile.contracts.values():
        expected = TokenizerIdentity.from_mapping(
            contract.evaluation_identity['tokenizer_identity'])
        if expected not in tokenizers:
            tokenizers[expected] = TokenizerIdentity.from_snapshot(snapshot,
                repository=expected.repository, revision=expected.revision,
                add_generation_prompt=expected.add_generation_prompt,
                enable_thinking=expected.enable_thinking)
        actual = tokenizers[expected]
        if (actual != expected or launch.max_model_len != contract.model_context_tokens
                or not launch.language_model_only or launch.runtime_version
                != contract.evaluation_identity['model_identity']['runtime_version']):
            raise ValueError('backup runtime/tokenizer artifacts do not match the contracts')


@contextmanager
def capture(instance, settings_path):
    settings = validate_settings(json.loads(_read_protected(settings_path, max_bytes=65536)))
    paths = {key: Path(settings[key]).resolve(strict=True)
             for key in SETTINGS_FIELDS - {'version', 'registry'}}
    if (paths['state_dir'] != instance.state.resolve()
            or any(not paths[key].is_relative_to(instance.root)
                   for key in ('source_root', 'data_root'))):
        raise ValueError('request backup settings do not select this owned instance')
    _check_directory(instance.state)
    # These locks exclude every supported workbench/executor entry for the entire dump.
    # No application/model process is stopped by this command.
    with _state_lock(instance.state), executor_lock(instance.state / STATE_CHILD):
        marker = json.loads(_read_protected(instance.state / 'instance.json', max_bytes=1048576))
        if marker != instance.marker:
            raise ValueError('instance marker changed before the checkpoint')
        registry = load_challenge_contract_registry(paths['source_root'],
                                                    Path(settings['registry']))
        profile = load_profile(paths['execution_profile'], registry.contracts)
        _, summary, files = inspect_state(instance.state, profile, marker)
        validate_launch_assets(profile, paths['tokenizer_snapshot'], paths['launch_evidence'])
        files['execution-profile.json'] = paths['execution_profile']
        files['backup-settings.json'] = settings_path
        yield {'settings': settings, 'paths': paths, 'profile': profile,
               'launch_snapshot_declared': json.loads(paths['launch_evidence'].read_text())[
                   'model_snapshot_path'],
               'summary': summary, 'files': files}


def _extract_inert(archive_path, destination):
    """Copy verified regular members only; never execute archived Python or restore lock inodes."""
    with tarfile.open(archive_path) as archive:
        for item in archive.getmembers():
            safe_archive_name(item.name)
            if not item.isfile():
                raise ValueError('unsafe archive member')
            target = destination / item.name
            parent = destination
            for part in PurePosixPath(item.name).parts[:-1]:
                parent = parent / part
                # On Windows inherit the explicit root ACL. Python's mode700 creates an
                # OWNER RIGHTS ACE instead, which protected-file readers intentionally reject.
                parent.mkdir(exist_ok=True, mode=0o777 if os.name == 'nt' else 0o700)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output, archive.extractfile(item) as source:
                while block := source.read(1024 * 1024):
                    output.write(block)


@contextmanager
def restored_checkpoint(directory, report):
    """Restore to a fresh private verification directory, leaving the original state untouched."""
    with tempfile.TemporaryDirectory(prefix='loj-request-restore-') as temporary:
        root = Path(temporary).resolve()
        _protect_temporary_directory(root)
        source, state, assets = (root / name for name in ('source', 'state', 'assets'))
        for path in (source, state, assets):
            path.mkdir(mode=0o777 if os.name == 'nt' else 0o700)
        _extract_inert(directory / 'source.tar.gz', source)
        source_manifest = json.loads((source / 'acceptance-source.json').read_text())
        archived_sources = {name: value for name, value in report['source_files'].items()
                            if name != 'acceptance-source.json'}
        if source_manifest['files'] != archived_sources:
            raise ValueError('source archive differs from its release manifest')
        validate_execution_inventory(report['execution_files'])
        _extract_inert(directory / 'request-execution.tar.gz', state)
        _extract_inert(directory / 'evaluation-assets.tar.gz', assets)
        settings = validate_settings(json.loads(_read_protected(state / 'backup-settings.json')))
        if settings['registry'] != report['registry']:
            raise ValueError('backup registry mismatch')
        registry = load_challenge_contract_registry(source, Path(settings['registry']))
        profile = load_profile(state / 'execution-profile.json', registry.contracts)
        for key in profile.contracts:
            public = registry.public_challenges[key]
            datasets = list((assets / 'datasets').glob(public.language + '_*.jsonl'))
            if len(datasets) != 1:
                raise ValueError('backup dataset selection is ambiguous or missing')
            artifacts = load_challenge_artifacts(source / 'challenges/public' / (key + '.json'),
                assets / 'manifests' / (key + '.json'), dataset_path=datasets[0])
            if artifacts.public != public:
                raise ValueError('backup dataset/manifest differs from the registry')
        marker = json.loads((directory / 'instance.json').read_text(encoding='utf-8'))
        if marker['instance'] != report['instance'] or marker['database'] != report['database']:
            raise ValueError('backup marker mismatch')
        ledger, observed, _ = inspect_state(state, profile, marker)
        if observed != report['request_execution']:
            raise ValueError('request checkpoint summary mismatch')
        # Preserve the original launch path in evidence; verify bytes without rewriting it.
        launch = json.loads((assets / 'runtime-evidence/qwen-launch.json').read_text())
        if (not isinstance(launch, dict) or set(launch) != {
            'schema_version', 'model_snapshot_path', 'max_model_len', 'max_num_seqs',
            'runtime_version', 'language_model_only'
        } or launch['schema_version'] != 'linguistic-oj-vllm-launch-v1'
                or launch['model_snapshot_path'] != report['launch_snapshot_declared']
                or launch['max_num_seqs'] != profile.capacity):
            raise ValueError('archived launch configuration mismatch')
        tokenizers = {}
        for contract in profile.contracts.values():
            expected = TokenizerIdentity.from_mapping(
                contract.evaluation_identity['tokenizer_identity'])
            if expected not in tokenizers:
                tokenizers[expected] = TokenizerIdentity.from_snapshot(assets / 'tokenizer',
                    repository=expected.repository, revision=expected.revision,
                    add_generation_prompt=expected.add_generation_prompt,
                    enable_thinking=expected.enable_thinking)
            actual = tokenizers[expected]
            if (actual != expected or launch['max_model_len'] != contract.model_context_tokens
                    or launch['language_model_only'] is not True or launch['runtime_version']
                    != contract.evaluation_identity['model_identity']['runtime_version']):
                raise ValueError('archived tokenizer/runtime mismatch')
        with executor_lock(state / STATE_CHILD, create=True):
            executor = BoundedExecutorState(state / STATE_CHILD, ledger['binding_sha256'])
            try:
                if observed['legacy_uncertain_marker']:
                    raise RecoveryRequired('legacy intent also requires reconciliation')
                executor.require_clean()
                blocked = False
            except RecoveryRequired:
                blocked = True
            if blocked != observed['requires_reconciliation']:
                raise ValueError('restored request safety gate differs from the checkpoint')
            if executor.snapshot() != ledger:
                raise ValueError('verification changed the request ledger')
            yield profile, registry, {**observed, 'safety_gate_preserved': True,
                'model_requests_sent': 0, 'automatic_start_allowed': False}
