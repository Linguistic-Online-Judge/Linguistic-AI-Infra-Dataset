import os
from pathlib import Path

import pytest

from scripts.check_request_backup import exercise

ROOT = Path(__file__).parents[1]
URL = os.environ.get('POSTGRES_TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not URL, reason='requires explicit PostgreSQL test URL')


@pytest.mark.parametrize('uncertain', [False, True])
def test_owned_database_dump_restore_and_request_gate(tmp_path, uncertain):
    report = exercise(ROOT, tmp_path, URL, uncertain=uncertain,
        pg_bin=os.environ.get('POSTGRES_TEST_BIN_DIR'),
        container=os.environ.get('POSTGRES_TEST_CONTAINER_ID'))
    assert report['passed'] and report['fixture_database_cleanup_confirmed']
    assert report['verification']['cleanup_confirmed']
    assert report['verification']['request_execution']['requires_reconciliation'] is uncertain
    assert report['source_state_unchanged'] and report['real_qwen_requests'] == 0
