"""Live availability observations, distinct from startup/runtime identity attestation."""

import json
import math
from collections.abc import MutableMapping
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class ProbedAvailability(MutableMapping):
    """Keep explicit runtime disables authoritative; transient probe failures can recover."""

    def __init__(self, declared, probe):
        if not callable(probe):
            raise TypeError('runtime probe must be callable')
        self._declared = dict(declared)
        self._probe = probe
        self._lock = RLock()

    def __getitem__(self, key):
        with self._lock:
            if not self._declared[key]:
                return False
        try:
            healthy = self._probe(key) is True
        except Exception:
            healthy = False
        with self._lock:
            return self._declared[key] and healthy

    def __setitem__(self, key, value):
        with self._lock:
            if key not in self._declared or type(value) is not bool:
                raise ValueError('runtime updates require an existing task and a boolean')
            self._declared[key] = value

    def __delitem__(self, key):
        raise TypeError('runtime task declarations cannot be removed')

    def __iter__(self):
        with self._lock:
            return iter(tuple(self._declared))

    def __len__(self):
        return len(self._declared)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LocalModelProbe:
    """Bounded read-only /health and model-alias checks, with one shared short cache."""

    def __init__(self, base_url, model, *, ttl_seconds=1.0, timeout_seconds=1.0, clock=monotonic):
        parsed = urlsplit(base_url)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or not isinstance(model, str) or not model):
            raise ValueError('model probe requires an explicit loopback HTTP service')
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in (ttl_seconds, timeout_seconds)):
            raise ValueError('probe timing bounds must be positive and finite')
        self._base = base_url.rstrip('/')
        self._model = model
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())
        self._lock = RLock()
        self._until = float('-inf')
        self._healthy = False

    def healthy(self):
        with self._lock:
            if self._clock() < self._until:
                return self._healthy
            healthy = False
            try:
                with self._opener.open(self._base + '/health', timeout=self._timeout) as response:
                    if response.status != 200:
                        raise ValueError('model health failed')
                with self._opener.open(self._base + '/v1/models',
                                       timeout=self._timeout) as response:
                    if response.status != 200:
                        raise ValueError('model metadata failed')
                    raw = response.read(16385)
                if len(raw) > 16384:
                    raise ValueError('model metadata exceeds probe bound')
                payload = json.loads(raw)
                healthy = self._model in {item['id'] for item in payload['data']}
            except Exception:
                healthy = False
            self._healthy = healthy
            self._until = self._clock() + self._ttl
            return healthy
