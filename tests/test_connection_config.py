import os
from pathlib import Path

import pytest

from linguistic_oj import auth_config
from linguistic_oj.connection_config import (
    resolve_connection_url,
    validate_postgres_connection_url,
    validate_redis_connection_url,
)
from linguistic_oj.qwen_api import parse_args


@pytest.fixture(autouse=True)
def clear_pg_environment(monkeypatch):
    for key in os.environ:
        if key.startswith('PG'):
            monkeypatch.delenv(key)


@pytest.mark.parametrize('url', [
    'postgresql://operator@127.0.0.1:5433/judge',
    'postgresql://operator@/judge?host=%2Fprivate%2Fpg&port=5433&passfile=%2Fprivate%2Fempty',
    'postgresql://operator@db.example/judge?sslmode=verify-full',
])
def test_explicit_postgres_targets_and_existing_socket_shape(url):
    assert validate_postgres_connection_url(url) == url


@pytest.mark.parametrize('url', [
    'postgresql:///judge',
    'postgresql://localhost/judge?host=/other',
    'postgresql://localhost/judge?hostaddr=203.0.113.1',
    'postgresql://localhost/judge?dbname=other',
    'postgresql://localhost/judge?user=other',
    'postgresql://localhost/judge?service=other',
    'postgresql://localhost/judge?options=-c%20statement_timeout=0',
    'postgresql://localhost,db.example/judge',
    'postgresql://operator@/judge?host=/private/pg,/other&port=5433',
    'postgresql://operator@/judge?host=/private/pg&port=0',
    'postgresql://localhost/judge?sslmode=require&sslmode=disable',
    'postgresql://db.example/judge',
    'postgresql://localhost/judge#ignored',
    'postgresql://localhost/judge?host=%00',
    'postgresql://localhost/judge?sslrootcert=%0afile',
])
def test_postgres_overrides_are_rejected(url):
    with pytest.raises(ValueError):
        validate_postgres_connection_url(url)


def test_later_environment_change_is_rejected(monkeypatch):
    from linguistic_oj.postgres_submission_store import PostgresSubmissionStore

    store = PostgresSubmissionStore('postgresql://localhost/judge')
    monkeypatch.setenv('PGSERVICE', 'another-service')
    with pytest.raises(ValueError, match='PG'):
        store._connect()


@pytest.mark.parametrize('url', [
    'redis://localhost:6379/15', 'rediss://queue.example:6380/0',
    'unix:///private/redis.sock?db=15&protocol=3',
])
def test_explicit_redis_targets(url):
    assert validate_redis_connection_url(url) == url


@pytest.mark.parametrize('url', [
    'redis://queue.example/0', 'http://localhost/0', 'redis:///0',
    'redis://localhost/0?host=queue.example',
    'redis://localhost/0?decode_responses=true',
    'redis://localhost/0?socket_timeout=0', 'rediss://queue.example/0?ssl_cert_reqs=none',
    'redis://localhost/0?db=1', 'unix:///socket?db=1&db=2',
    'unix://other/socket?db=15', 'redis://localhost/0?protocol=4',
    'unix:///private/%00redis.sock', 'redis://localhost//1',
])
def test_redis_overrides_are_rejected(url):
    with pytest.raises(ValueError):
        validate_redis_connection_url(url)


def test_production_cli_uses_private_files_not_password_arguments(tmp_path, monkeypatch):
    if os.name == 'nt':
        monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)
    pg = tmp_path / 'postgres.url'
    redis = tmp_path / 'redis.url'
    pg.write_text('postgresql://operator:fixture-password@localhost/judge\n', encoding='utf-8')
    redis.write_text('redis://:fixture-password@localhost/15\n', encoding='utf-8')
    pg.chmod(0o600)
    redis.chmod(0o600)
    args = parse_args(['--root', str(tmp_path), '--postgres-database-url-file', str(pg),
                       '--redis-url-file', str(redis), '--auth-config-file',
                       str(tmp_path / 'auth.json')])
    assert args.postgres_database_url == pg.read_text().strip()
    assert args.redis_url == redis.read_text().strip()
    for kind, value in [('postgres', args.postgres_database_url), ('redis', args.redis_url)]:
        with pytest.raises(ValueError, match='private URL file') as error:
            resolve_connection_url(kind, inline_url=value, credential_file=None, production=True)
        assert 'fixture-password' not in str(error.value)
        assert resolve_connection_url(kind, inline_url=value, credential_file=None,
                                      production=False) == value
    with pytest.raises(ValueError):
        resolve_connection_url('postgres', inline_url=None,
                               credential_file=Path(tmp_path / 'missing'), production=True)
