import os
from pathlib import Path

from linguistic_oj import auth_config
from linguistic_oj.submission_jobs import QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS, InMemoryJobQueue
from linguistic_oj.submission_store import SubmissionStore
from scripts.check_bounded_pipeline import exercise_fixture

ROOT = Path(__file__).parents[1]


def test_cookie_authenticated_bounded_harness_with_controlled_http_model(tmp_path, monkeypatch):
    if os.name == 'nt':
        monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)
    store = SubmissionStore(tmp_path / 'fixture.db')

    def queue_factory(contract):
        return InMemoryJobQueue(contract.contract_snapshot_sha256,
            visibility_timeout_seconds=contract.job_deadline_seconds
            + QWEN_QUEUE_VISIBILITY_BUFFER_SECONDS)

    report = exercise_fixture(ROOT, tmp_path, store, queue_factory, capacity=2)
    assert report['passed'] and report['cookie_auth']
    assert report['real_qwen_requests'] == 0
    assert report['model_fixture_calls'] == 250 and report['submissions'] == 5
