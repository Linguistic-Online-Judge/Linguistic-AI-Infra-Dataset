"""Generate an offline private-workbench profile and user-service template; never activate."""

import argparse
import json
import re
from pathlib import Path, PurePosixPath

from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.request_development import write_profile

FIELDS = {'release_root', 'data_root', 'python', 'dependency_overlay', 'state_dir', 'profile',
          'registry', 'postgres_socket', 'postgres_port', 'redis_socket', 'tokenizer_snapshot',
          'launch_evidence', 'port', 'request_slots', 'max_active_jobs'}


def server_path(value):
    if (not isinstance(value, str) or not re.fullmatch('/[A-Za-z0-9_./-]+', value)
            or '..' in PurePosixPath(value).parts):
        raise ValueError('service paths require explicit safe absolute POSIX paths')


def prepare(root, settings, output):
    if not isinstance(settings, dict) or set(settings) != FIELDS:
        raise ValueError('invalid request development settings')
    for name in FIELDS - {'registry', 'postgres_port', 'port', 'request_slots', 'max_active_jobs'}:
        server_path(settings[name])
    registry = settings['registry']
    if (not isinstance(registry, str) or not re.fullmatch('[A-Za-z0-9_./-]+', registry)
            or Path(registry).is_absolute() or '..' in Path(registry).parts):
        raise ValueError('registry must stay in the release')
    for name in ('port', 'postgres_port'):
        if type(settings[name]) is not int or not 1024 <= settings[name] <= 65535:
            raise ValueError('invalid service port')
    if settings['port'] in (8000, 8001):
        raise ValueError('workbench port must not be a model port')
    loaded = load_challenge_contract_registry(root, Path(registry))
    if not output.parent.is_dir():
        raise ValueError('output parent must exist')
    output.mkdir(mode=0o700)
    digest = write_profile(output / 'execution-profile.json', loaded.contracts,
        capacity=settings['request_slots'], max_jobs=settings['max_active_jobs'])
    command = [settings['python'], '-m', 'linguistic_oj.qwen_development']
    fields = {'root': 'release_root', 'data-root': 'data_root', 'state-dir': 'state_dir',
        'postgres-socket': 'postgres_socket', 'postgres-port': 'postgres_port',
        'redis-socket': 'redis_socket', 'tokenizer-snapshot': 'tokenizer_snapshot',
        'launch-evidence': 'launch_evidence', 'port': 'port', 'registry': 'registry',
        'execution-profile': 'profile'}
    for flag, name in fields.items():
        command.extend(['--' + flag, str(settings[name])])
    unit = '\n'.join([
        '[Unit]', 'Description=Private Linguistic OJ request workbench',
        'StartLimitIntervalSec=60', 'StartLimitBurst=3', '', '[Service]', 'Type=simple',
        'WorkingDirectory=' + settings['release_root'],
        ('Environment=PYTHONPATH=' + settings['release_root']
         + '/src:' + settings['dependency_overlay']),
        'UnsetEnvironment=HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy',
        'ExecStart=' + ' '.join(command), 'UMask=0077', 'Restart=on-failure', 'RestartSec=5',
        'RestartPreventExitStatus=75 78', 'KillSignal=SIGTERM', 'KillMode=control-group',
        'TimeoutStopSec=infinity', 'SendSIGKILL=no', 'NoNewPrivileges=true', '',
        '[Install]', 'WantedBy=default.target', ''])
    (output / 'linguistic-oj-request-development.service').write_text(unit, encoding='utf-8')
    init = [settings['python'], '-m', 'linguistic_oj.request_development', 'init',
        '--root', settings['release_root'], '--registry', registry,
        '--profile', settings['profile'], '--state-dir', settings['state_dir']]
    (output / 'initialize-command.txt').write_text(' '.join(init) + '\n', encoding='utf-8')
    report = {'profile_sha256': digest, 'request_slots': settings['request_slots'],
        'max_active_jobs': settings['max_active_jobs'], 'contracts': len(loaded.contracts),
        'source_contracts_modified': False, 'model_attested': False, 'services_started': False,
        'pending': ['maintenance_window', 'new_release_paths', 'model_capacity_launch_evidence',
                    'profile_aware_backup_and_restore', 'backup_and_drain',
                    'explicit_state_initialization', 'service_installation',
                    'real_mixed_load_acceptance']}
    (output / 'preparation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'settings', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root.resolve(), json.loads(args.settings.read_text()),
                             args.output.absolute()), indent=2))


if __name__ == '__main__':
    main()
