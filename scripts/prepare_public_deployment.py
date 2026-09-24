"""Prepare offline production templates and report unresolved deployment inputs."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from linguistic_oj.auth import normalize_email, validate_public_origin
from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.qwen_runtime import validate_qwen_evaluation_contract

FIELDS = {'public_origin', 'release_root', 'python', 'shared_dir', 'service_user', 'api_port',
          'registry', 'trusted_proxies', 'tls_certificate', 'tls_certificate_key', 'smtp',
          'runtime_available_challenges'}


def server_path(value):
    if (not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value)
            or '..' in PurePosixPath(value).parts):
        raise ValueError('server paths must be absolute POSIX paths without spaces or traversal')
    return value


def inspect_settings(root, settings):
    if not isinstance(settings, dict) or set(settings) != FIELDS:
        raise ValueError('unexpected production settings fields')
    pending = []
    for name in ('release_root', 'python', 'shared_dir'):
        server_path(settings[name])
        if 'RELEASE_ID' in settings[name]:
            pending.append(name)
    origin = settings['public_origin']
    if origin is None:
        pending.append('public_origin')
    else:
        validate_public_origin(origin)
        parsed = urlsplit(origin)
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ValueError('this proxy template requires a DNS hostname')
        if parsed.hostname == 'localhost' or parsed.hostname.endswith(
            ('.test', '.invalid', '.example')
        ):
            raise ValueError('public_origin cannot be a loopback or placeholder host')
    for name in ('tls_certificate', 'tls_certificate_key'):
        if settings[name] is None:
            pending.append(name)
        else:
            server_path(settings[name])
    user = settings['service_user']
    if user is None:
        pending.append('service_user')
    elif not isinstance(user, str) or not re.fullmatch('[a-z_][a-z0-9_-]*', user) or user == 'root':
        raise ValueError('service_user must name a dedicated non-root service account')
    port = settings['api_port']
    if type(port) is not int or not 1024 <= port <= 65535 or port in {8000, 8080, 8090}:
        raise ValueError('choose a distinct unprivileged API port, not model/dev ports')
    if origin is not None and (urlsplit(origin).port or 443) in {80, port}:
        raise ValueError('HTTPS listener must not conflict with HTTP or internal API listeners')
    proxies = settings['trusted_proxies']
    if not isinstance(proxies, list) or not proxies or len(proxies) > 128:
        raise ValueError('declare exact trusted proxy addresses')
    for address in proxies:
        if not isinstance(address, str):
            raise ValueError('trusted proxy addresses must be strings')
        ipaddress.ip_address(address)
    if '127.0.0.1' not in proxies:
        raise ValueError('the generated local proxy requires explicit 127.0.0.1 trust')
    smtp = settings['smtp']
    smtp_fields = {'host', 'port', 'sender', 'username', 'security'}
    if not isinstance(smtp, dict) or set(smtp) != smtp_fields:
        raise ValueError('unexpected SMTP fields; passwords belong in a separate private file')
    if type(smtp['port']) is not int or not 1 <= smtp['port'] <= 65535:
        raise ValueError('invalid SMTP port')
    if smtp['security'] not in {'ssl', 'starttls'}:
        raise ValueError('SMTP must use ssl or starttls')
    for name in ('host', 'sender', 'username'):
        value = smtp[name]
        if value is None:
            pending.append('smtp.' + name)
        elif not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
            raise ValueError('invalid SMTP setting')
    if smtp['host'] and not re.fullmatch('[A-Za-z0-9.:-]{1,253}', smtp['host']):
        raise ValueError('invalid SMTP host')
    if smtp['username'] and len(smtp['username']) > 320:
        raise ValueError('invalid SMTP username')
    if smtp['sender']:
        try:
            normalize_email(smtp['sender'])
        except Exception:
            raise ValueError('invalid SMTP sender') from None
    if not isinstance(settings['registry'], str) or not settings['registry']:
        raise ValueError('registry must be a release-relative path')
    registry_path = PurePosixPath(settings['registry'])
    if (registry_path.is_absolute() or '..' in registry_path.parts
            or not re.fullmatch(r'[A-Za-z0-9_./-]+', settings['registry'])
            or not (root / Path(registry_path)).resolve().is_relative_to(root.resolve())):
        raise ValueError('registry must stay below the release root')
    registry = load_challenge_contract_registry(root, Path(registry_path))
    for contract in registry.contracts.values():
        validate_qwen_evaluation_contract(contract)
    enabled = settings['runtime_available_challenges']
    if (not isinstance(enabled, list) or any(not isinstance(key, str) for key in enabled)
            or len(enabled) != len(set(enabled)) or set(enabled) - set(registry.contracts)):
        raise ValueError('runtime list contains unknown or duplicate challenges')
    eligible = sorted(key for key, contract in registry.contracts.items()
                      if contract.external_activation_ready)
    blocked = sorted(set(enabled) - set(eligible))
    if blocked:
        pending.append('enabled_challenges_not_publicly_activatable')
    if not eligible:
        pending.append('no_publicly_activatable_contracts')
    if not enabled:
        pending.append('runtime_available_challenges')
    body_limits = {contract.api_request_body_bytes for contract in registry.contracts.values()}
    if len(body_limits) != 1:
        raise ValueError('registry contracts must share the API request body limit')
    return {'status': 'pending_configuration' if pending else 'static_configuration_valid',
            'pending': pending, 'catalog_count': len(registry.public_challenges),
            'contract_count': len(registry.contracts), 'publicly_activatable': eligible,
            'api_request_body_bytes': body_limits.pop(),
            'enabled_but_blocked': blocked, 'live_dependencies_checked': False,
            'production_started': False}


def templates(settings, *, api_request_body_bytes):
    origin = settings['public_origin']
    parsed = urlsplit(origin) if origin else None
    hostname = parsed.hostname if parsed else '__PUBLIC_HOST__'
    authority = parsed.netloc if parsed else '__PUBLIC_HOST__'
    tls_port = (parsed.port or 443) if parsed else 443
    shared = settings['shared_dir']
    auth = {'public_origin': origin, 'trusted_proxies': settings['trusted_proxies'],
            'smtp': {**settings['smtp'], 'password_file': shared + '/smtp-password'}}
    command = [settings['python'], '-m', 'linguistic_oj.qwen_api',
               '--root', settings['release_root'],
               '--postgres-database-url-file', shared + '/postgres.url', '--redis-url-file',
               shared + '/redis.url', '--auth-config-file', shared + '/auth.json', '--registry',
               settings['registry'], '--environment', 'production', '--namespace', 'loj-public',
               '--host', '127.0.0.1', '--port', str(settings['api_port'])]
    for key in settings['runtime_available_challenges']:
        command.extend(['--runtime-available-challenge', key])
    # Registry references and identifiers are operator input; reject systemd shell/specifier syntax.
    if any(not re.fullmatch(r'[A-Za-z0-9_./:-]+', part) for part in command):
        raise ValueError('command fields contain unsupported service-template characters')
    unit = f'''[Unit]
Description=Linguistic Online Judge public API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={settings['service_user'] or '__SERVICE_USER__'}
WorkingDirectory={settings['release_root']}
ExecStart={' '.join(command)}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''
    nginx = f'''# Fill pending settings, provide real certificates, then run nginx -t.
server {{
    listen 80 default_server;
    server_name _;
    return 444;
}}
server {{
    listen 80;
    server_name {hostname};
    return 308 https://{authority}$request_uri;
}}
server {{
    listen {tls_port} ssl default_server;
    server_name _;
    ssl_reject_handshake on;
    return 444;
}}
server {{
    listen {tls_port} ssl;
    server_name {hostname};
    ssl_certificate {settings['tls_certificate'] or '__TLS_CERTIFICATE__'};
    ssl_certificate_key {settings['tls_certificate_key'] or '__TLS_CERTIFICATE_KEY__'};
    ssl_protocols TLSv1.2 TLSv1.3;
    # Application logs contain sanitized route/request identifiers; omit raw request URLs.
    access_log off;
    client_max_body_size {api_request_body_bytes};
    location / {{
        proxy_pass http://127.0.0.1:{settings['api_port']};
        proxy_set_header Host {authority};
        proxy_set_header X-LOJ-Client-IP $remote_addr;
        proxy_set_header X-Forwarded-For "";
        proxy_set_header Forwarded "";
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 30s;
    }}
}}
'''
    executor = {
        'version': 'qwen-serial-executor-v1', 'root': settings['release_root'],
        'registry': settings['registry'], 'state_dir': shared + '/executor-state',
        'postgres_database_url_file': shared + '/postgres.url',
        'redis_url_file': shared + '/redis.url', 'namespace': 'loj-public',
        'vllm_base_url': 'http://127.0.0.1:8000/v1',
        'tokenizer_snapshot': None, 'launch_evidence': None,
        'artifacts': {key: {
            'public_challenge': settings['release_root'] + '/challenges/public/' + key + '.json',
            'private_challenge': None, 'dataset': None,
        } for key in settings['runtime_available_challenges']},
    }
    executor_command = (f"{settings['python']} -m linguistic_oj.qwen_executor run "
                        f"--config {shared}/executor.json")
    executor_unit = f'''[Unit]
Description=Linguistic Online Judge serial Qwen executor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={settings['service_user'] or '__SERVICE_USER__'}
WorkingDirectory={settings['release_root']}
ExecStart={executor_command}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartPreventExitStatus=75 78
RestartSec=5
TimeoutStopSec=infinity
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''
    return {'auth.json.template': json.dumps(auth, ensure_ascii=False, indent=2) + '\n',
            'loj-api.service.template': unit, 'nginx.conf.template': nginx,
            'api-command.json': json.dumps(command, indent=2) + '\n',
            'executor.json.template': json.dumps(executor, ensure_ascii=False, indent=2) + '\n',
            'loj-executor.service.template': executor_unit}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args(arguments)
    root = Path(__file__).resolve().parents[1]
    try:
        settings = json.loads(args.settings.read_text(encoding='utf-8'))
        report = inspect_settings(root, settings)
        files = templates(settings, api_request_body_bytes=report['api_request_body_bytes'])
    except (OSError, ValueError) as error:
        parser.error(str(error))
    output = args.output.resolve()
    if not output.is_relative_to(root / 'runtime') or output.exists():
        parser.error('output must be a new directory below project runtime')
    if not output.parent.is_dir():
        parser.error('output parent must exist')
    output.mkdir(mode=0o700)
    files['preflight.json'] = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    for name, contents in files.items():
        path = output / name
        with path.open('x', encoding='utf-8') as stream:
            stream.write(contents)
        path.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_complete and report['pending']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
