import hashlib
import io
import tarfile
from types import SimpleNamespace

import pytest

from scripts.qwen_dev_ops import backup, validate_marker, verify_archive


def test_operations_refuse_unrelated_database_or_owner():
    marker = {'kind': 'linguistic-oj-private-qwen18-v1', 'owner': 'operator',
              'instance': 'a' * 32, 'database': 'loj_dev18_' + 'a' * 16, 'contracts': {}}
    validate_marker(marker, 'operator')
    for change in [{'database': 'linguistic_oj'}, {'owner': 'other'},
                   {'instance': '../elsewhere'}, {'kind': 'unknown'}]:
        with pytest.raises(ValueError):
            validate_marker({**marker, **change}, 'operator')


def make_archive(path, name, content):
    with tarfile.open(path, 'w:gz') as archive:
        item = tarfile.TarInfo(name)
        item.size = len(content)
        archive.addfile(item, io.BytesIO(content))


def test_restore_archive_integrity_and_inventory(tmp_path):
    archive = tmp_path / 'source.tar.gz'
    make_archive(archive, 'src/app.py', b'original')
    expected = {'src/app.py': hashlib.sha256(b'original').hexdigest()}
    verify_archive(archive, expected)
    make_archive(archive, 'src/app.py', b'changed')
    with pytest.raises(ValueError, match='content mismatch'):
        verify_archive(archive, expected)
    with pytest.raises(ValueError, match='inventory mismatch'):
        verify_archive(archive, {})


def test_restore_rejects_parent_paths_without_extracting(tmp_path):
    archive = tmp_path / 'source.tar.gz'
    make_archive(archive, '../outside', b'content')
    with pytest.raises(ValueError, match='unsafe'):
        verify_archive(archive, {'../outside': hashlib.sha256(b'content').hexdigest()})
    assert not (tmp_path / 'outside').exists()


@pytest.mark.parametrize('profile_flag', [False, True])
def test_serial_backup_cannot_omit_request_profile_or_recovery_ledger(tmp_path, profile_flag):
    argv = ['python', '-m', 'linguistic_oj.qwen_development']
    if profile_flag:
        argv.extend(['--execution-profile', '/private/profile.json'])
    else:
        (tmp_path / 'request-executor').mkdir()
    instance = SimpleNamespace(state=tmp_path, application=lambda: (123, argv))
    with pytest.raises(ValueError, match='profile-and-ledger-aware'):
        backup(instance)
    assert not (tmp_path / 'backups').exists()
