import json
import os
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from linguistic_oj import auth_config
from linguistic_oj.executor_state import ExecutorBusy, executor_lock
from linguistic_oj.local_dev import _state_lock
from linguistic_oj.request_backup import STATE_CHILD, restored_checkpoint
from scripts import qwen_dev_ops as ops
from scripts.check_request_backup import fixture_files

ROOT = Path(__file__).parents[1]
REAL_WINDOWS_ACL_CHECK = auth_config._check_windows_acl


@pytest.fixture(autouse=True)
def permissions(monkeypatch):
    if os.name == 'nt':
        monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)


@pytest.fixture
def fixture(tmp_path, monkeypatch, request):
    args, marker, profile, settings = fixture_files(ROOT, tmp_path,
                                                   uncertain=getattr(request, 'param', False))

    class Connection:
        def execute(self, query, *args):
            return SimpleNamespace(fetchone=lambda: ('fixture-snapshot',), fetchall=lambda: [])

    class Instance:
        root, state = tmp_path, args.state_dir

        @contextmanager
        def connect(self):
            yield Connection()

        def command(self, command, **kwargs):
            # This unit fixture covers archive semantics only; service tests use real pg_dump.
            Path(command[command.index('--file') + 1]).write_bytes(b'explicit-dump-fixture')

    instance = Instance()
    instance.marker = marker
    monkeypatch.setattr(ops, 'table_fingerprints', lambda connection: {'fixture': {'count': 0}})
    return instance, args, profile, settings


@pytest.mark.parametrize('fixture', [False, True], indirect=True)
def test_backup_and_inert_restore_preserve_full_state_and_recovery_gate(fixture):
    instance, args, _, settings = fixture
    before = {p.relative_to(args.state_dir).as_posix(): ops.digest(p)
              for p in args.state_dir.rglob('*') if p.is_file()}
    result = ops.backup_request(instance, settings)
    directory = Path(result['backup'])
    assert ops.verify_copy(directory, result['manifest_sha256'])['files_verified']
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['kind'] == 'qwen18-backup-v2'
    assert manifest['request_execution']['recovery_audits'] == 1
    assert '.executor.lock' not in ''.join(manifest['execution_files'])
    assert '.server.lock' not in ''.join(manifest['execution_files'])
    with restored_checkpoint(directory, manifest) as (profile, registry, state):
        assert len(registry.contracts) == len(profile.contracts) == 22
        assert state['safety_gate_preserved'] and state['model_requests_sent'] == 0
        assert not state['automatic_start_allowed']
        assert state['requires_reconciliation'] == bool(state['pending_requests'])
    assert before == {p.relative_to(args.state_dir).as_posix(): ops.digest(p)
                      for p in args.state_dir.rglob('*') if p.is_file()}


def test_application_and_executor_ownership_prevent_live_backup(fixture):
    instance, args, _, settings = fixture
    with _state_lock(args.state_dir), pytest.raises(RuntimeError, match='already in use'):
        ops.backup_request(instance, settings)
    with executor_lock(args.state_dir / STATE_CHILD), pytest.raises(ExecutorBusy):
        ops.backup_request(instance, settings)
    assert not (instance.root / 'backups').exists()


def test_profile_mismatch_fails_before_dump(fixture):
    instance, args, _, settings = fixture
    value = json.loads(args.execution_profile.read_text())
    value['max_active_jobs'] = 8
    args.execution_profile.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='ledger does not match'):
        ops.backup_request(instance, settings)
    assert not (instance.root / 'backups').exists()


def test_dump_failure_does_not_publish_a_complete_manifest_and_unlocks(fixture):
    instance, args, _, settings = fixture

    def fail(*args, **kwargs):
        raise OSError('fixture interrupted dump')

    instance.command = fail
    with pytest.raises(OSError):
        ops.backup_request(instance, settings)
    assert list((instance.root / 'backups').rglob('manifest.json')) == []
    with _state_lock(args.state_dir), executor_lock(args.state_dir / STATE_CHILD):
        pass


def test_missing_audit_is_rejected_even_after_archive_hashes_are_updated(fixture):
    instance, _, _, settings = fixture
    directory = Path(ops.backup_request(instance, settings)['backup'])
    manifest = json.loads((directory / 'manifest.json').read_text())
    archive_path = directory / 'request-execution.tar.gz'
    with tarfile.open(archive_path) as archive:
        contents = {member.name: archive.extractfile(member).read()
                    for member in archive.getmembers() if '/recoveries/' not in member.name}
    import io

    with tarfile.open(archive_path, 'w:gz') as archive:
        for name, data in contents.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    manifest['execution_files'] = {k: v for k, v in manifest['execution_files'].items()
                                   if '/recoveries/' not in k}
    manifest['files']['request-execution.tar.gz'] = ops.digest(archive_path)
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        ops.verify_copy(directory)


def test_database_or_external_manifest_corruption_is_rejected(fixture):
    instance, _, _, settings = fixture
    result = ops.backup_request(instance, settings)
    directory = Path(result['backup'])
    with pytest.raises(ValueError, match='manifest fingerprint'):
        ops.verify_copy(directory, 'f' * 64)
    (directory / 'database.dump').write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='backup fingerprint'):
        ops.verify_copy(directory, result['manifest_sha256'])


def test_restored_temporary_files_pass_real_protected_file_checks(fixture, monkeypatch):
    instance, _, _, settings = fixture
    result = ops.backup_request(instance, settings)
    # Fixture-source ACLs can be synthetic; copied restore files must satisfy real OS checks.
    monkeypatch.setattr(auth_config, '_check_windows_acl', REAL_WINDOWS_ACL_CHECK)
    assert ops.verify_copy(Path(result['backup']), result['manifest_sha256'])['files_verified']


def test_legacy_backup_inventory_remains_verifiable(tmp_path):
    files = ('database.dump', 'instance.json', 'source.tar.gz', 'evaluation-assets.tar.gz')
    (tmp_path / 'database.dump').write_bytes(b'legacy fixture')
    (tmp_path / 'instance.json').write_text('{}')
    for name in ('source.tar.gz', 'evaluation-assets.tar.gz'):
        with tarfile.open(tmp_path / name, 'w:gz'):
            pass
    ops.write_json(tmp_path / 'manifest.json', {'kind': 'qwen18-backup-v1',
        'files': {name: ops.digest(tmp_path / name) for name in files},
        'source_files': {}, 'evaluation_files': {}})
    assert ops.verify_copy(tmp_path)['files_verified']
