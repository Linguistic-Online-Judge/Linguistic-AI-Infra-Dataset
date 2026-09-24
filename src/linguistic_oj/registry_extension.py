"""Offline additive development-registry update, with compare-and-swap and journal."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path


def validate_extension(old: dict[str, str], new: dict[str, str]) -> tuple[str, ...]:
    if not old or not new or any(new.get(key) != value for key, value in old.items()):
        raise ValueError('registry extension cannot remove or replace an existing contract')
    added = tuple(sorted(set(new) - set(old)))
    if not added:
        raise ValueError('registry extension must add at least one contract')
    return added


def extend_marker(state_dir: Path, contract_hashes: dict[str, str], expected_sha256: str):
    """Caller holds the instance lock and has verified ownership and an idle database."""
    path = state_dir / 'instance.json'
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_sha256:
        raise ValueError('instance marker changed since upgrade preflight')
    marker = json.loads(original)
    added = validate_extension(marker['contracts'], contract_hashes)
    updated = {**marker, 'contracts': contract_hashes}
    payload = (json.dumps(updated, indent=2, sort_keys=True) + '\n').encode()
    journal_dir = state_dir / 'registry-upgrades'
    journal_dir.mkdir(mode=0o700, exist_ok=True)
    journal = journal_dir / (uuid.uuid4().hex + '.json')
    record = {'kind': 'additive-development-registry-upgrade-v1',
              'created_at': datetime.now(UTC).isoformat(), 'added': list(added),
              'before_sha256': expected_sha256,
              'after_sha256': hashlib.sha256(payload).hexdigest(),
              'before_text': original.decode(), 'after_text': payload.decode()}
    with journal.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    journal.chmod(0o600)
    with tempfile.NamedTemporaryFile(dir=state_dir, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(0o600)
        if path.read_bytes() != original:
            raise ValueError('instance marker changed during upgrade')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'added': list(added), 'marker_sha256': record['after_sha256'],
            'journal': str(journal)}
