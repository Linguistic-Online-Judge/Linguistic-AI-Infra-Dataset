"""Required in CI: fresh ownership-marked PostgreSQL/Redis plus cookie auth and HTTP fixture."""

import os
from contextlib import ExitStack
from pathlib import Path

import pytest

from scripts.check_bounded_pipeline import exercise_fixture, resources_module

ROOT = Path(__file__).parents[1]
POSTGRES = os.environ.get('POSTGRES_TEST_DATABASE_URL')
REDIS = os.environ.get('REDIS_TEST_URL')
pytestmark = pytest.mark.skipif(not POSTGRES or not REDIS,
    reason='requires explicit PostgreSQL and Redis test URLs')


@pytest.mark.parametrize('capacity', [2, 4])
def test_bounded_full_jobs_on_owned_postgres_and_redis(tmp_path, capacity):
    from psycopg.conninfo import conninfo_to_dict

    owned = resources_module(ROOT)
    cleanup, lifecycle = {}, {'worker_stopped': True}

    def close(name, resource):
        if not lifecycle['worker_stopped'] or not lifecycle.get('requests_reconciled', True):
            pytest.fail(f'{name} namespace retained because a fixture worker is active')
        resource.close()

    with ExitStack() as resources:
        postgres = owned.OwnedPostgres(conninfo_to_dict(POSTGRES), cleanup)
        resources.callback(close, 'postgres', postgres)
        store = postgres.create()

        def queue_factory(contract):
            redis = owned.OwnedRedis(None, 15, contract, cleanup, redis_url=REDIS)
            resources.callback(close, 'redis', redis)
            return redis.create(contract)

        report = exercise_fixture(ROOT, tmp_path, store, queue_factory,
                                  capacity=capacity, lifecycle=lifecycle)
        assert report['passed'] and report['peak_model_fixture_requests'] == capacity
        assert report['counts'] == {'submissions': 5, 'results': 5, 'submission_outbox': 5}
    assert cleanup['postgres']['confirmed'] and cleanup['postgres']['tables_dropped'] == 11
    # Successful ACKs removed the now-empty active/receipt hashes; stream + owner remain.
    assert cleanup['redis']['confirmed'] and cleanup['redis']['keys_deleted'] == 2
