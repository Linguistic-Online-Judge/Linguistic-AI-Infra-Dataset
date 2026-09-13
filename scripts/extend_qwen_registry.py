"""Explicit offline extension: validate artifacts, lock instance, preserve old contracts."""
import argparse
import hashlib
import json
from pathlib import Path

from linguistic_oj.local_dev import _state_lock
from linguistic_oj.qwen_development import load_development_catalog, prepare_store
from linguistic_oj.registry_extension import extend_marker, validate_extension


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'data-root', 'state-dir', 'postgres-socket', 'registry'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--postgres-port', type=int, default=5433)
    parser.add_argument('--expected-marker-sha256', required=True)
    args = parser.parse_args()
    with _state_lock(args.state_dir):
        if (args.state_dir / 'uncertain-inference.json').exists():
            raise ValueError('previous inference termination is unconfirmed')
        raw = (args.state_dir / 'instance.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.expected_marker_sha256:
            raise ValueError('marker no longer matches the approved preflight')
        old = json.loads(raw)
        registry, _ = load_development_catalog(args.root, args.data_root, args.registry)
        hashes = {key: value.contract_snapshot_sha256 for key, value in registry.contracts.items()}
        validate_extension(old['contracts'], hashes)
        store, _ = prepare_store(args.state_dir, pg_socket=args.postgres_socket,
                                 pg_port=args.postgres_port, initialize=False,
                                 contract_hashes=old['contracts'])
        with store._connect() as connection:
            if connection.execute("SELECT count(*) FROM submissions WHERE status IN "
                                  "('queued','running')").fetchone()[0]:
                raise ValueError('drain outstanding submissions before changing the registry')
            stored = connection.execute('SELECT DISTINCT challenge_id,contract_snapshot_sha256 '
                                        'FROM submissions').fetchall()
            if any(hashes.get(key) != value for key, value in stored):
                raise ValueError('registry does not preserve historical submission contracts')
        print(json.dumps(extend_marker(args.state_dir, hashes, args.expected_marker_sha256),
                         indent=2))


if __name__ == '__main__':
    main()
