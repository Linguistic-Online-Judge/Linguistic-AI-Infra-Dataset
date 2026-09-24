"""Temporary loopback-only acceptance budget relay; never part of normal serving.

Preserves request/response bytes, persists budget before forwarding, and never retries.
Unknown upstream termination closes the client transport and latches the entire relay.
"""

import argparse
import hashlib
import json
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from linguistic_oj.auth_config import _read_protected
from linguistic_oj.bounded_executor_state import read_bounded_state
from linguistic_oj.executor_state import _check_directory, _write_atomic, executor_lock
from linguistic_oj.mvp_contract import canonical_sha256


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Budget:
    def __init__(self, directory, ledger, maximum, backend):
        if type(maximum) is not int or not 1 <= maximum <= 250:
            raise ValueError('acceptance budget must be1..250')
        _check_directory(directory)
        self.path = directory / 'budget.json'
        self.ledger = ledger
        self.maximum = maximum
        self.lock = RLock()
        self.faulted = False
        ledger_state = read_bounded_state(ledger)
        self.ledger_binding = ledger_state['binding_sha256']
        self.binding = canonical_sha256({'ledger': self.ledger_binding, 'backend': backend,
                                        'maximum': maximum})
        if self.path.exists():
            self.value = json.loads(_read_protected(self.path, max_bytes=262144))
            if (not isinstance(self.value, dict) or set(self.value) != {
                'binding', 'maximum', 'started', 'completed', 'inflight', 'blocked'
            } or self.value['binding'] != self.binding or self.value['maximum'] != maximum
                    or type(self.value['started']) is not int
                    or not 0 <= self.value['started'] <= maximum
                    or self.value['blocked'] is not False or self.value['inflight']
                    or not isinstance(self.value['completed'], list)
                    or len(self.value['completed']) != self.value['started']
                    or sorted(row['ordinal'] for row in self.value['completed'])
                    != list(range(1, self.value['started'] + 1))):
                raise ValueError('acceptance budget cannot restart with changed or uncertain state')
        else:
            self.value = {'binding': self.binding, 'maximum': maximum, 'started': 0,
                          'completed': [], 'inflight': {}, 'blocked': False}
            _write_atomic(self.path, self.value)

    def begin(self, body):
        with self.lock:
            if self.faulted or self.value['started'] >= self.maximum:
                return None
            ledger = read_bounded_state(self.ledger)
            digest = hashlib.sha256(body).hexdigest()
            if (ledger['binding_sha256'] != self.ledger_binding or ledger['blocked']
                    or not any(record.get('request_context', {}).get('request_sha256') == digest
                               for record in ledger['pending'].values())):
                return None
            ordinal = self.value['started'] + 1
            self.value['started'] = ordinal
            self.value['inflight'][str(ordinal)] = digest
            try:
                _write_atomic(self.path, self.value)
            except BaseException:
                self.faulted = True
                raise
            return ordinal

    def finish(self, ordinal, status, body):
        with self.lock:
            digest = self.value['inflight'].pop(str(ordinal))
            self.value['completed'].append({'ordinal': ordinal, 'request_sha256': digest,
                'status': status, 'response_sha256': hashlib.sha256(body).hexdigest()})
            try:
                _write_atomic(self.path, self.value)
            except BaseException:
                self.faulted = True
                raise

    def block(self):
        with self.lock:
            self.faulted = True
            self.value['blocked'] = True
            _write_atomic(self.path, self.value)


def create_server(*, listen_port, backend_port, budget):
    if listen_port == backend_port:
        raise ValueError('relay ports must differ')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    base = f'http://127.0.0.1:{backend_port}'

    class Handler(BaseHTTPRequestHandler):
        timeout = 180

        def log_message(self, *args):
            pass

        def reply(self, status, body, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in ('/health', '/v1/models', '/metrics'):
                return self.reply(404, b'{}')
            if budget.faulted:
                return self.reply(503, b'{}')
            if self.path == '/health' and budget.value['started'] >= budget.maximum:
                return self.reply(503, b'{}')
            try:
                with opener.open(base + self.path, timeout=5) as response:
                    self.reply(response.status, response.read(4 * 1024 * 1024),
                               response.headers.get('Content-Type', 'application/json'))
            except Exception:
                self.reply(503, b'{}')

        def do_POST(self):
            ordinal = None
            terminated = False
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if self.path != '/v1/chat/completions' or not 0 < length <= 1048576:
                    return self.reply(400, b'{}')
                body = self.rfile.read(length)
                payload = json.loads(body)
                if (len(body) != length or payload.get('stream') is not False
                        or payload.get('model') != 'Qwen/Qwen3.5-9B'):
                    return self.reply(400, b'{}')
                ordinal = budget.begin(body)
                if ordinal is None:
                    return self.reply(503, b'{"error":{"message":"acceptance gate closed"}}')
                request = Request(base + self.path, data=body,
                                  headers={'Content-Type': 'application/json'}, method='POST')
                try:
                    response = opener.open(request, timeout=150)
                except HTTPError as error:
                    response = error
                with response:
                    answer = response.read(1048577)
                    if len(answer) > 1048576:
                        raise ValueError('upstream response exceeds acceptance envelope')
                    status = response.status
                terminated = True
                budget.finish(ordinal, status, answer)
                self.reply(status, answer)
            except BaseException:
                # Do not turn an unknown upstream outcome into a retryable HTTP status.
                if ordinal is not None and not terminated:
                    try:
                        budget.block()
                    except BaseException:
                        pass
                self.close_connection = True

    class Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = False

    return Server(('127.0.0.1', listen_port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--ledger-dir', type=Path, required=True)
    parser.add_argument('--listen-port', type=int, default=8000)
    parser.add_argument('--backend-port', type=int, default=8001)
    parser.add_argument('--max-requests', type=int, default=250)
    args = parser.parse_args()
    if args.listen_port != 8000 or args.backend_port != 8001:
        parser.error('operational relay is restricted to the declared8000->8001 window')
    with executor_lock(args.state_dir, create=True):
        budget = Budget(args.state_dir, args.ledger_dir, args.max_requests,
                        f'http://127.0.0.1:{args.backend_port}')
        server = create_server(listen_port=args.listen_port, backend_port=args.backend_port,
                               budget=budget)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *args: Thread(target=server.shutdown).start())
        try:
            server.serve_forever()
        finally:
            server.server_close()
    return 75 if budget.faulted or budget.value['inflight'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
