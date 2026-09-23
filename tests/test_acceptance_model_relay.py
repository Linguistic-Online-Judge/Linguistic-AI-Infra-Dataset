import hashlib
import json
import os
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from linguistic_oj import auth_config
from linguistic_oj.bounded_executor_state import BoundedExecutorState
from linguistic_oj.executor_state import executor_lock
from scripts.acceptance_model_relay import Budget, create_server


@pytest.mark.parametrize('disconnect', [False, True])
def test_relay_preserves_bytes_caps_calls_and_blocks_unknown_termination(
    tmp_path, monkeypatch, disconnect,
):
    if os.name == 'nt':
        monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)
    seen = []
    answer = b'{"unchanged": "model response bytes"}'

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(self.rfile.read(int(self.headers['Content-Length'])))
            if disconnect:
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model_thread = Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    state_dir, budget_dir = tmp_path / 'ledger', tmp_path / 'budget'
    state_dir.mkdir(mode=0o700)
    budget_dir.mkdir(mode=0o700)
    with executor_lock(state_dir, create=True):
        state = BoundedExecutorState.initialize(state_dir, 'a' * 64, max_inflight=1)
        budget = Budget(budget_dir, state_dir, 2, f'http://127.0.0.1:{model.server_port}')
        server = create_server(listen_port=0, backend_port=model.server_port, budget=budget)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for index in range(3):
                body = json.dumps({'model': 'Qwen/Qwen3.5-9B', 'stream': False,
                                   'messages': ['exact', index]}, ensure_ascii=False).encode()
                operation = state.begin('fixture', request_context={
                    'request_sha256': hashlib.sha256(body).hexdigest(),
                    'submission_sha256': 'b' * 64,
                    'sample_id_sha256': 'c' * 64, 'sample_position': index + 1})
                request = Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                                  data=body, headers={'Content-Type': 'application/json'})
                if disconnect and index == 0:
                    with pytest.raises(RemoteDisconnected):
                        urlopen(request, timeout=5)
                elif disconnect or index == 2:
                    with pytest.raises(HTTPError) as error:
                        urlopen(request, timeout=5)
                    assert error.value.code == 503
                else:
                    with urlopen(request, timeout=5) as response:
                        assert response.read() == answer
                    assert seen[-1] == body
                state.finish(operation)
            assert len(seen) == (1 if disconnect else 2)
            assert budget.value['started'] == len(seen)
            assert budget.faulted is disconnect
            if disconnect:
                with pytest.raises(ValueError):
                    Budget(budget_dir, state_dir, 2, f'http://127.0.0.1:{model.server_port}')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            model.shutdown()
            model.server_close()
            model_thread.join(5)
