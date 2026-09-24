import hashlib
import json
import os

import pytest

import linguistic_oj.sample_cache as cache_module
from linguistic_oj.challenge import build_challenge
from linguistic_oj.dataset import (
    DatasetFormatError,
    SelectedSampleSetError,
    load_dataset_samples_by_id,
)
from linguistic_oj.providers import DeterministicMockProvider
from linguistic_oj.responses import TaskType
from linguistic_oj.runner import _prepare_samples, run_challenge
from linguistic_oj.sample_cache import VerifiedSelectionCache


def _record(identifier='one'):
    return {'id': identifier, 'language': 'English', 'treebank': 'CacheFixture', 'text': 'AB',
            'tasks_available': [task.value for task in TaskType], 'answers': {
                'segmentation': ['A', 'B'], 'upos': ['X', 'X'], 'xpos': ['MOCK', 'MOCK'],
                'dependency': [[1, 'A', 0, 'ROOT', 'root'], [2, 'B', 1, 'A', 'dep']],
                'transliteration': ['A', 'B'],
            }}


@pytest.fixture
def artifacts(tmp_path):
    path = tmp_path / 'data.jsonl'
    path.write_text(json.dumps(_record()) + '\n', encoding='utf-8')
    return build_challenge(path, language='English', treebank='CacheFixture', task='upos',
                            count=1, seed=2026, version='test-v1')


@pytest.mark.parametrize('task', list(TaskType))
def test_cached_and_uncached_inputs_and_scores_are_identical(artifacts, task):
    current = build_challenge(artifacts.dataset_path, language='English', treebank='CacheFixture',
                              task=task, count=1, seed=2026, version='test-v1')
    cache = VerifiedSelectionCache()
    assert _prepare_samples(current) == _prepare_samples(current, selection_cache=cache)
    assert _prepare_samples(current) == _prepare_samples(current, selection_cache=cache)
    original = run_challenge(current, DeterministicMockProvider(), student_prompt='Return JSON.')
    cached = run_challenge(current, DeterministicMockProvider(), student_prompt='Return JSON.',
                            selection_cache=cache)
    assert original == cached


def test_callers_cannot_mutate_cache_owned_gold(artifacts):
    cache = VerifiedSelectionCache()
    first = cache.load(artifacts)
    first[0].answers['upos'][0] = 'TAINTED'
    first[0].tasks_available.clear()
    second = cache.load(artifacts)
    assert second[0].answers['upos'] == ['X', 'X']
    second[0].answers['upos'].clear()
    assert cache.load(artifacts)[0].answers['upos'] == ['X', 'X']


def test_same_size_same_mtime_mutation_is_rejected_on_warm_hit(artifacts):
    cache = VerifiedSelectionCache()
    cache.load(artifacts)
    path = artifacts.dataset_path
    metadata = path.stat()
    changed = path.read_bytes().replace(b'"AB"', b'"AC"')
    assert len(changed) == metadata.st_size
    path.write_bytes(changed)
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    with pytest.raises(ValueError, match='dataset_sha256'):
        cache.load(artifacts)


def test_changed_bytes_after_initial_validation_cannot_poison_cache(artifacts, monkeypatch):
    cache = VerifiedSelectionCache()
    original = artifacts.dataset_path.read_bytes()
    validate = cache_module.validate_challenge_artifacts

    def modify_after_validation(current):
        validate(current)
        current.dataset_path.write_bytes(original.replace(b'"AB"', b'"AC"'))

    monkeypatch.setattr(cache_module, 'validate_challenge_artifacts', modify_after_validation)
    with pytest.raises(SelectedSampleSetError, match='bytes changed'):
        cache.load(artifacts)
    artifacts.dataset_path.write_bytes(original)
    monkeypatch.setattr(cache_module, 'validate_challenge_artifacts', validate)
    assert cache.load(artifacts)[0].text == 'AB'


def test_oversized_entries_are_not_retained(artifacts, monkeypatch):
    cache = VerifiedSelectionCache(max_encoded_bytes=1)
    original = cache_module.load_dataset_samples_by_id
    calls = []

    def load(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(cache_module, 'load_dataset_samples_by_id', load)
    cache.load(artifacts)
    cache.load(artifacts)
    assert len(calls) == 2


def test_entry_bound_evicts_oldest_selection(artifacts, tmp_path, monkeypatch):
    other_path = tmp_path / 'other.jsonl'
    other_path.write_text(json.dumps(_record('other')) + '\n', encoding='utf-8')
    other = build_challenge(other_path, language='English', treebank='CacheFixture', task='upos',
                             count=1, seed=2026, version='test-v1')
    cache = VerifiedSelectionCache(max_entries=1)
    original = cache_module.load_dataset_samples_by_id
    reads = []

    def load(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(cache_module, 'load_dataset_samples_by_id', load)
    cache.load(artifacts)
    cache.load(artifacts)
    cache.load(other)
    cache.load(artifacts)
    assert reads == [artifacts.dataset_path, other_path, artifacts.dataset_path]


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_hashed_stream_preserves_bom_and_universal_newline_semantics(tmp_path, newline):
    path = tmp_path / 'data.jsonl'
    lines = (json.dumps(_record('a')), json.dumps(_record('b')), '')
    payload = ('\ufeff' + newline.join(lines)).encode()
    path.write_bytes(payload)
    expected = load_dataset_samples_by_id(path, ('b', 'a'))
    verified = load_dataset_samples_by_id(path, ('b', 'a'),
                                           expected_sha256=hashlib.sha256(payload).hexdigest())
    assert expected == verified


def test_verified_loader_still_validates_unselected_rows(tmp_path):
    path = tmp_path / 'data.jsonl'
    payload = (json.dumps(_record()) + '\n{"id":"unselected-only"}\n').encode()
    path.write_bytes(payload)
    with pytest.raises(DatasetFormatError):
        load_dataset_samples_by_id(path, ('one',),
                                   expected_sha256=hashlib.sha256(payload).hexdigest())
