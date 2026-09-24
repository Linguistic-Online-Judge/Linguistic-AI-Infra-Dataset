"""Explicit connection targets and private credential-file inputs for service CLIs."""
from __future__ import annotations

import os
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit


def _parts(url: str, schemes: set[str]):
    if not isinstance(url, str) or not url or url.strip() != url:
        raise ValueError('connection URL must be a non-empty string without outer whitespace')
    if any(ord(char) < 32 or ord(char) == 127 for char in unquote(url)):
        raise ValueError('connection URL contains a control character')
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if parsed.scheme not in schemes or parsed.fragment or (port is not None and port == 0):
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        raise ValueError('invalid connection URL') from None
    if any(len(values) != 1 for values in query.values()):
        raise ValueError('connection URL must not repeat query parameters')
    if ',' in unquote(parsed.netloc.rpartition('@')[2]):
        raise ValueError('connection URL must specify exactly one host')
    return parsed, query


def _loopback(host):
    if host == 'localhost':
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _socket_path(path):
    return (bool(path) and path.startswith('/') and ',' not in path
            and '\\' not in path and '..' not in PurePosixPath(path).parts)


def validate_postgres_connection_url(url: str) -> str:
    parsed, query = _parts(url, {'postgres', 'postgresql'})
    if parsed.path in ('', '/'):
        raise ValueError('database URL must be a PostgreSQL URL with a database name')
    # Keep test variables POSTGRES_TEST_*; only libpq PG* variables can override connections.
    if any(key.startswith('PG') for key in os.environ):
        raise ValueError('implicit libpq PG* connection environment is not supported')
    allowed = {'host', 'port', 'passfile', 'sslmode', 'sslrootcert', 'sslcert', 'sslkey'}
    if set(query) - allowed:
        raise ValueError('unsupported PostgreSQL connection target or query override')
    host = query.get('host', [None])[0]
    if host is not None:
        if parsed.hostname is not None or parsed.port is not None or not _socket_path(host):
            raise ValueError('PostgreSQL query host must be an unambiguous Unix socket')
        if 'port' in query and (not query['port'][0].isdigit()
                               or not 1 <= int(query['port'][0]) <= 65535):
            raise ValueError('PostgreSQL socket port must be in 1..65535')
    elif not parsed.hostname or 'port' in query:
        raise ValueError('PostgreSQL URL needs one explicit host and authority port')
    if host is None and not _loopback(parsed.hostname):
        if query.get('sslmode', [''])[0] not in {'require', 'verify-ca', 'verify-full'}:
            raise ValueError('non-loopback PostgreSQL requires a secure sslmode')
    return url


def validate_redis_connection_url(url: str) -> str:
    parsed, query = _parts(url, {'redis', 'rediss', 'unix'})
    if 'decode_responses' in query:
        raise ValueError('redis_url must not configure decode_responses')
    if set(query) - {'db', 'protocol'}:
        raise ValueError('unsupported Redis connection query override')
    if parsed.scheme == 'unix':
        if parsed.hostname or parsed.port or not _socket_path(unquote(parsed.path)):
            raise ValueError('Redis Unix socket must be an absolute, unambiguous path')
    else:
        if not parsed.hostname:
            raise ValueError('Redis URL needs an explicit host')
        if parsed.scheme == 'redis' and not _loopback(parsed.hostname):
            raise ValueError('non-loopback Redis connections must use rediss')
        database = parsed.path.removeprefix('/')
        if database and (not database.isascii() or not database.isdigit()):
            raise ValueError('Redis database must be a non-negative integer')
        if database and 'db' in query and database != query['db'][0]:
            raise ValueError('Redis database must not have conflicting path/query values')
    if 'db' in query and (not query['db'][0].isascii() or not query['db'][0].isdigit()):
        raise ValueError('Redis db query must be a non-negative integer')
    if 'protocol' in query and query['protocol'][0] not in ('2', '3'):
        raise ValueError('Redis protocol must be 2 or 3')
    return url


def resolve_connection_url(kind: str, *, inline_url: str | None,
                           credential_file: Path | None, production: bool) -> str:
    if (inline_url is None) == (credential_file is None):
        raise ValueError('configure exactly one connection URL source')
    if credential_file is not None:
        # Lazy import avoids initialization cycles with the authentication/store modules.
        from .auth_config import _read_protected

        url = _read_protected(credential_file).removesuffix('\n').removesuffix('\r')
        if len(url.encode('utf-8')) > 4096:
            raise ValueError('connection credential file is too large')
    else:
        url = inline_url
    if not isinstance(url, str):
        raise ValueError('connection URL is missing')
    if production and inline_url is not None:
        try:
            if urlsplit(url).password is not None:
                raise ValueError('production credentials must use a private URL file')
        except ValueError:
            raise ValueError('production credentials must use a private URL file') from None
    if kind == 'postgres':
        return validate_postgres_connection_url(url)
    if kind == 'redis':
        return validate_redis_connection_url(url)
    raise ValueError('unknown connection kind')
