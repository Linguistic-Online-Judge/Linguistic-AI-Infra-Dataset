"""Bounded selected-corpus cache; every use retains complete artifact/file verification."""

from collections import OrderedDict
from copy import deepcopy
from threading import RLock

from .challenge import ChallengeArtifacts, validate_challenge_artifacts
from .dataset import load_dataset_samples_by_id


class VerifiedSelectionCache:
    """No student prompts, model replies or grades; never return mutable cache-owned objects."""

    def __init__(self, *, max_entries=16, max_encoded_bytes=8 * 1024 * 1024):
        if (type(max_entries) is not int or max_entries <= 0
                or type(max_encoded_bytes) is not int or max_encoded_bytes <= 0):
            raise ValueError('cache bounds must be positive integers')
        self._max_entries = max_entries
        self._max_encoded_bytes = max_encoded_bytes
        self._entries = OrderedDict()
        self._encoded_bytes = 0
        self._lock = RLock()

    def load(self, artifacts: ChallengeArtifacts):
        # This includes rehashing the configured file, even on a warm hit. Metadata alone
        # (mtime/size/inode) is not an integrity check and cannot authorize a cache hit.
        validate_challenge_artifacts(artifacts)
        key = (artifacts.public.dataset_sha256, artifacts.private.sample_ids)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return deepcopy(cached[0])
            samples = load_dataset_samples_by_id(artifacts.dataset_path, key[1],
                                                 expected_sha256=key[0])
            # Bound encoded payload as well as entry count; Python object overhead is extra.
            import json

            size = sum(len(json.dumps(sample.model_dump(), ensure_ascii=True).encode('ascii'))
                       for sample in samples)
            if size <= self._max_encoded_bytes:
                while self._entries and (len(self._entries) >= self._max_entries
                       or self._encoded_bytes + size > self._max_encoded_bytes):
                    _, (_, removed_size) = self._entries.popitem(last=False)
                    self._encoded_bytes -= removed_size
                self._entries[key] = (deepcopy(samples), size)
                self._encoded_bytes += size
            return samples
