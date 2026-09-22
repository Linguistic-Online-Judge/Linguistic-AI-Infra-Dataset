"""Required in CI: fresh ownership-marked PostgreSQL/Redis plus cookie auth and HTTP fixture."""

import os
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.check_bounded_pipeline import exercise_fixture, fixture_contract, resources_module

ROOT = Path(__file__).parents[1]
POSTGRES = os.environ.get('POSTGRES_TEST_DATABASE_URL')
REDIS = os.environ.get('REDIS_TEST_URL')
pytestmark = pytest.mark.skipif(not POSTGRES or not REDIS,
    reason='requires explicit PostgreSQL and Redis test URLs')


@pytest.mark.parametrize('capacity', [2, 4])
@pytest.mark.parametrize('request_level,continuous', [(False, False), (True, False), (True, True)])
def test_bounded_full_jobs_on_owned_postgres_and_redis(
    tmp_path, capacity, request_level, continuous,
):
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
                                  capacity=capacity, lifecycle=lifecycle,
                                  request_level=request_level, continuous=continuous)
        assert report['passed'] and report['peak_model_fixture_requests'] == capacity
        assert report['request_level'] is request_level
        assert report['continuous'] is continuous
        assert report['counts'] == {'submissions': 5, 'results': 5, 'submission_outbox': 5}
    assert cleanup['postgres']['confirmed'] and cleanup['postgres']['tables_dropped'] == 11
    # Successful ACKs removed the now-empty active/receipt hashes; stream + owner remain.
    assert cleanup['redis']['confirmed'] and cleanup['redis']['keys_deleted'] == 2


def test_postgres_claim_observation_and_publication_use_actual_lease_and_database_time(tmp_path):
    from psycopg.conninfo import conninfo_to_dict

    from linguistic_oj.providers import DeterministicMockProvider
    from linguistic_oj.runner import run_challenge
    from linguistic_oj.submission_store import _timestamp

    owned = resources_module(ROOT)
    cleanup = {}
    postgres = owned.OwnedPostgres(conninfo_to_dict(POSTGRES), cleanup)
    try:
        store = postgres.create()
        artifacts, contract = fixture_contract(ROOT, tmp_path, 2)
        user = store.register_user(auth_subject='isolated-lease-owner', public_handle='LeaseOwner')
        created = store.create_submission(user=user, idempotency_key='lease-check',
                                          student_prompt='Return JSON.', contract=contract)
        claim = store.claim_submission(created.submission.submission_id,
            evaluation_identity_sha256=contract.evaluation_identity_sha256,
            contract_snapshot_sha256=contract.contract_snapshot_sha256,
            lease_seconds=contract.job_deadline_seconds, max_attempts=contract.max_attempts,
            max_running_per_user=contract.max_running_submissions_per_user).claim
        assert claim is not None and store.claim_is_current(claim)
        for changed in (replace(claim, user_id='another-owner'),
                        replace(claim, attempt_number=claim.attempt_number + 1),
                        replace(claim, lease_token='stale-token'),
                        replace(claim, contract_snapshot_sha256='a' * 64)):
            assert not store.claim_is_current(changed)
        aggregate = run_challenge(artifacts, DeterministicMockProvider(),
                                   student_prompt='Return JSON.')
        result = contract.owner_result(aggregate, student_prompt_sha256=claim.student_prompt_sha256)
        with store._connect() as connection:
            with connection.cursor() as cursor:
                expired = _timestamp(store._database_now(cursor) - timedelta(seconds=1))
                cursor.execute('UPDATE submissions SET lease_expires_at = %s WHERE id = %s',
                               (expired, claim.submission_id))
        assert not store.claim_is_current(claim)
        assert not store.complete_success(claim, owner_result=result)
        store.expire_leases(evaluation_identity_sha256=contract.evaluation_identity_sha256)
        terminal = store.owner_result(claim.submission_id, user.user_id)
        assert terminal.status.value == 'failed' and terminal.failure['code'] == 'WORKER_CRASH'
        assert terminal.result is None
    finally:
        postgres.close()
    assert cleanup['postgres']['confirmed'] and cleanup['postgres']['tables_dropped'] == 11
