"""Start an installed wheel outside the checkout and verify a real local Mock flow."""
import argparse
import http.cookiejar
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, required=True)
    args = parser.parse_args()
    # On Linux, resolving a venv's Python symlink selects the base interpreter
    # and bypasses that environment's installed wheel.
    python = args.python.absolute()
    if not python.is_file():
        parser.error('--python must point to an existing interpreter')
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='loj-wheel-') as temporary:
        home = Path(temporary)
        (home / 'config').mkdir()
        (home / 'config/mvp_evaluation.json').write_bytes(
            (root / 'config/mvp_evaluation.json').read_bytes())
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        env['PYTHONNOUSERSITE'] = '1'
        installed = subprocess.check_output(
            [str(python), '-c', 'import linguistic_oj; print(linguistic_oj.__file__)'],
            cwd=home, env=env, text=True).strip()
        if Path(installed).resolve().is_relative_to(root / 'src'):
            raise RuntimeError('smoke test resolved the source checkout, not the installed wheel')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def request(path, body=None, expected=None):
            headers = {'Accept': 'application/json'}
            if body is not None:
                headers.update({'Content-Type': 'application/json', 'Origin': base,
                                'X-LOJ-CSRF': '1'})
            if expected:
                headers['X-LOJ-Expected-User'] = expected
            data = None if body is None else json.dumps(body).encode()
            query = Request(base + path, data=data, headers=headers)
            with browser.open(query, timeout=10) as result:
                return json.load(result)

        with (home / 'server.log').open('w') as log:
            server = subprocess.Popen(
                [str(python), '-m', 'linguistic_oj.local_dev', '--root', str(home),
                 '--port', str(port)], cwd=home, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 45
                while True:
                    if server.poll() is not None:
                        raise RuntimeError('installed application exited before readiness')
                    try:
                        if request('/health/ready') == {'status': 'ready'}:
                            break
                    except (URLError, TimeoutError):
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError('installed application did not become ready')
                    time.sleep(.1)
                for path in ('/', '/assets/app.js', '/assets/app-shell.css',
                             '/assets/brand-palette.css', '/assets/brand-option-a.svg'):
                    with browser.open(base + path) as response:
                        assert response.status == 200 and response.read()
                session = request('/v1/auth/login', {'email': 'alice@example.test',
                                                   'password': 'Local-only-passphrase-2026!'})
                owner = session['user']['user_id']
                task = next(item for item in request('/v1/challenges') if item['task'] == 'upos')
                body = {'challenge_id': task['challenge_id'],
                        'student_prompt': 'Installed package.'}
                headers = {'Content-Type': 'application/json', 'Origin': base, 'X-LOJ-CSRF': '1',
                           'X-LOJ-Expected-User': owner, 'Idempotency-Key': 'wheel-smoke'}
                with browser.open(Request(base + '/v1/submissions', data=json.dumps(body).encode(),
                                          headers=headers), timeout=10) as response:
                    submission = json.load(response)
                path = '/v1/submissions/' + submission['submission_id']
                deadline = time.monotonic() + 30
                while request(path, expected=owner)['status'] in ('queued', 'running'):
                    if time.monotonic() > deadline:
                        raise RuntimeError('installed Mock worker did not complete')
                    time.sleep(.1)
                result = request(path + '/result', expected=owner)
                assert result['outcome'] == 'succeeded'
                assert result['model_identity']['runtime'] == 'mock'
                assert request(path + '/prompt', expected=owner)['student_prompt'] == (
                    body['student_prompt'])
                print('PASS installed wheel: assets, login, Mock submission, result, owner prompt.')
            finally:
                if server.poll() is None and os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(server.pid), '/T', '/F'],
                                   capture_output=True, check=False, timeout=15)
                elif server.poll() is None:
                    server.terminate()
                server.wait(timeout=30)


if __name__ == '__main__':
    main()
