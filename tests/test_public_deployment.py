import json
from pathlib import Path

import pytest

from linguistic_oj import auth_config, qwen_api
from scripts import prepare_public_deployment as preparation

ROOT = Path(__file__).parents[1]


@pytest.fixture
def settings():
    return json.loads((ROOT / 'config/production.example.json').read_text(encoding='utf-8'))


def test_example_preflight_keeps_domain_pending_and_existing_contracts_closed(settings):
    report = preparation.inspect_settings(ROOT, settings)
    assert report['status'] == 'pending_configuration'
    assert report['catalog_count'] == 74
    assert report['contract_count'] == 70
    assert report['publicly_activatable'] == []
    assert {'public_origin', 'no_publicly_activatable_contracts',
            'runtime_available_challenges'} <= set(report['pending'])
    assert report['production_started'] is False
    assert report['live_dependencies_checked'] is False
    settings['runtime_available_challenges'] = ['en-childes-upos-v1']
    report = preparation.inspect_settings(ROOT, settings)
    assert report['enabled_but_blocked'] == ['en-childes-upos-v1']
    assert 'enabled_challenges_not_publicly_activatable' in report['pending']


def test_generated_templates_match_real_auth_and_cli_interfaces(settings, monkeypatch):
    settings.update(public_origin='https://judge.school.edu', service_user='loj',
                    release_root='/srv/linguistic-oj/releases/20260913',
                    tls_certificate='/etc/loj/fullchain.pem',
                    tls_certificate_key='/etc/loj/privkey.pem')
    settings['smtp'].update(host='smtp.school.edu', sender='judge@school.edu',
                            username='judge@school.edu')
    report = preparation.inspect_settings(ROOT, settings)
    files = preparation.templates(settings, api_request_body_bytes=report['api_request_body_bytes'])
    shared = settings['shared_dir']
    private_files = {
        shared + '/auth.json': files['auth.json.template'],
        shared + '/smtp-password': 'fixture-only-password',
        shared + '/postgres.url': 'postgresql://operator@127.0.0.1/production_fixture',
        shared + '/redis.url': 'redis://127.0.0.1/15',
    }
    monkeypatch.setattr(auth_config, '_read_protected', lambda path: private_files[path.as_posix()])
    command = json.loads(files['api-command.json'])
    args = qwen_api.parse_args(command[3:])
    assert args.environment == 'production'
    assert args.host == '127.0.0.1'
    assert args.port == 8100
    auth = auth_config.build_auth_service(None, Path(shared + '/auth.json'))
    try:
        assert auth.public_origin == settings['public_origin']
        assert auth.cookie_name == '__Host-loj_session'
        assert auth.development is False
    finally:
        auth.close()
    proxy = files['nginx.conf.template']
    assert 'ssl_reject_handshake on;' in proxy
    assert 'proxy_set_header X-LOJ-Client-IP $remote_addr;' in proxy
    assert f"client_max_body_size {report['api_request_body_bytes']};" in proxy
    assert 'https://judge.school.edu$request_uri' in proxy
    assert '$host' not in proxy
    assert 'fixture-only-password' not in ''.join(files.values())
    executor = json.loads(files['executor.json.template'])
    assert executor['namespace'] == args.namespace
    assert executor['root'] == str(args.root).replace('\\', '/')
    assert executor['artifacts'] == {}
    service = files['loj-executor.service.template']
    assert 'RestartPreventExitStatus=75 78' in service
    assert 'TimeoutStopSec=infinity' in service
    assert '-m linguistic_oj.qwen_executor run --config ' in service


@pytest.mark.parametrize(('field', 'value'), [
    ('public_origin', 'http://judge.school.edu'),
    ('public_origin', 'https://JUDGE.school.edu'),
    ('public_origin', 'https://judge.school.edu:443'),
    ('public_origin', 'https://judge.school.edu/path'),
    ('public_origin', 'https://127.0.0.2'),
    ('public_origin', 'https://placeholder.invalid'),
    ('registry', '../outside.json'), ('registry', 'C:/outside.json'),
    ('service_user', 'root'), ('service_user', 'loj\nExecStart=/bad'),
    ('shared_dir', '/srv/%n'), ('shared_dir', '/srv/../other'),
    ('trusted_proxies', ['127.0.0.1', 1]),
    ('runtime_available_challenges', ['unknown-task']),
])
def test_invalid_settings_fail_before_templates_are_written(settings, field, value):
    settings[field] = value
    with pytest.raises(ValueError):
        preparation.inspect_settings(ROOT, settings)


def test_cli_preserves_existing_output(settings, tmp_path, monkeypatch):
    registry = preparation.load_challenge_contract_registry(ROOT, Path(settings['registry']))
    monkeypatch.setattr(preparation, '__file__', str(tmp_path / 'scripts/prepare.py'))
    monkeypatch.setattr(preparation, 'load_challenge_contract_registry', lambda *args: registry)
    (tmp_path / 'runtime').mkdir()
    config = tmp_path / 'settings.json'
    config.write_text(json.dumps(settings), encoding='utf-8')
    output = tmp_path / 'runtime/preparation'
    args = ['--settings', str(config), '--output', str(output), '--require-complete']
    with pytest.raises(SystemExit) as error:
        preparation.main(args)
    assert error.value.code == 2
    assert json.loads((output / 'preflight.json').read_text())['status'] == 'pending_configuration'
    snapshots = {file.name: file.read_bytes() for file in output.iterdir()}
    with pytest.raises(SystemExit):
        preparation.main(args)
    assert {file.name: file.read_bytes() for file in output.iterdir()} == snapshots
