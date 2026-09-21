import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from linguistic_oj.runtime_availability import LocalModelProbe, ProbedAvailability


def test_probe_is_read_only_cached_and_recovers_from_model_failure():
    control = {'health': 200, 'model': 'fixed-model'}
    paths, now = [], [0.]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            paths.append(self.path)
            status = control['health'] if self.path == '/health' else 200
            body = (b'' if self.path == '/health' else
                    json.dumps({'data': [{'id': control['model']}]}).encode())
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        probe = LocalModelProbe(f'http://127.0.0.1:{server.server_port}', 'fixed-model',
                                clock=lambda: now[0])
        assert probe.healthy()
        assert probe.healthy() and paths == ['/health', '/v1/models']
        control['health'] = 503
        now[0] = 2
        assert not probe.healthy()
        assert not probe.healthy() and len(paths) == 3
        control.update(health=200, model='other-model')
        now[0] = 4
        assert not probe.healthy()
        control['model'] = 'fixed-model'
        now[0] = 6
        assert probe.healthy()
        assert set(paths) == {'/health', '/v1/models'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_availability_preserves_disables_even_if_they_arrive_during_probe():
    flags = None

    def probe(key):
        flags[key] = False
        return True

    flags = ProbedAvailability({'task': True}, probe)
    assert flags['task'] is False
    flags['task'] = True
    assert flags['task'] is False
    assert flags.get('unknown', False) is False


def test_probe_error_and_non_boolean_response_fail_closed():
    def fail(key):
        raise RuntimeError('private diagnostic')

    assert ProbedAvailability({'task': True}, fail)['task'] is False
    assert ProbedAvailability({'task': True}, lambda key: 'yes')['task'] is False
    assert ProbedAvailability({'task': False}, lambda key: pytest.fail('disabled task probed'))[
        'task'] is False


@pytest.mark.parametrize('url', ['https://external.example', 'http://localhost:8000',
    'http://127.0.0.1:8000/v1', 'http://user:secret@127.0.0.1:8000', 'http://127.0.0.1/?x=1'])
def test_probe_rejects_implicit_or_non_loopback_endpoints(url):
    with pytest.raises(ValueError):
        LocalModelProbe(url, 'fixed-model')
