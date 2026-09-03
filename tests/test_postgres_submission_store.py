import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

import linguistic_oj.postgres_migrations as postgres_migrations
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.postgres_migrations import migrate_postgres
from linguistic_oj.postgres_submission_store import PostgresSubmissionStore
from linguistic_oj.submission_store import (
    SubmissionQuotaError,
    SubmissionStatus,
    UserRole,
)

POSTGRES_TEST_DATABASE_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")
ROOT = Path(__file__).parents[1]

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="set POSTGRES_TEST_DATABASE_URL to run PostgreSQL integration tests",
)


def _owner_result(contract: EvaluationContract) -> dict[str, object]:
    identity = contract.evaluation_identity
    return {
        "aggregation_version": identity["aggregation_version"],
        "challenge_id": contract.challenge_id,
        "dataset_sha256": identity["dataset_sha256"],
        "errors": {},
        "generation_settings": identity["generation_settings"],
        "metrics": {"micro_accuracy": 0.75},
        "model_identity": identity["model_identity"],
        "primary_metric": "micro_accuracy",
        "prompt_envelope_version": identity["prompt_envelope_version"],
        "samples_invalid": 1,
        "samples_total": 4,
        "samples_valid": 3,
        "score": 0.75,
        "scorer_version": identity["scorer_version"],
        "selection_sha256": identity["selection_sha256"],
        "student_prompt_sha256": "0" * 64,
        "task": identity["task"],
    }


def _contract_for_challenge(challenge_id: str) -> EvaluationContract:
    config = json.loads(
        (ROOT / "config" / "mvp_evaluation_v2.json").read_text(encoding="utf-8")
    )
    config["catalog"]["challenge_id"] = challenge_id
    config["evaluation_identity"]["challenge_id"] = challenge_id
    config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        config["evaluation_identity"]
    )
    return EvaluationContract.from_mapping(config)


def test_health_check_accepts_current_schema() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None

    PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL).health_check()


def test_postgres_store_persists_user_and_admin_roles() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex

    user = store.register_user(
        auth_subject=f"postgres-role-user-{suffix}",
        public_handle=f"role-user-{suffix}",
    )
    admin = store.register_user(
        auth_subject=f"postgres-role-admin-{suffix}",
        public_handle=f"role-admin-{suffix}",
        role=UserRole.ADMIN,
    )

    assert user.role is UserRole.USER
    assert admin.role is UserRole.ADMIN
    assert store.user_by_subject(user.auth_subject) == user
    assert store.user_by_subject(admin.auth_subject) == admin


def test_existing_postgres_v2_store_upgrades_users_to_default_role() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    import psycopg
    from psycopg import sql

    database_name = f"role_upgrade_{uuid.uuid4().hex}"
    parsed_url = urlsplit(POSTGRES_TEST_DATABASE_URL)
    isolated_url = urlunsplit(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            f"/{database_name}",
            parsed_url.query,
            parsed_url.fragment,
        )
    )
    try:
        with psycopg.connect(POSTGRES_TEST_DATABASE_URL, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
                )
        with psycopg.connect(isolated_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(postgres_migrations._SCHEMA_V1)
                cursor.execute(postgres_migrations._SCHEMA_V2)
                cursor.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (1, 'v1'), (2, 'v2')"
                )
                cursor.execute(
                    "INSERT INTO users(id, auth_subject, public_handle, created_at) "
                    "VALUES ('old-user', 'old-subject', 'old-handle', '2026-09-02')"
                )

        migrate_postgres(isolated_url, applied_at="v3-test")

        upgraded = PostgresSubmissionStore(isolated_url).user_by_subject("old-subject")
        assert upgraded is not None and upgraded.role is UserRole.USER
        with psycopg.connect(isolated_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
                assert tuple(row[0] for row in cursor.fetchall()) == (1, 2, 3)
        with pytest.raises(psycopg.errors.CheckViolation):
            with psycopg.connect(isolated_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE users SET role = 'owner' WHERE id = 'old-user'"
                    )
    finally:
        with psycopg.connect(POSTGRES_TEST_DATABASE_URL, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database_name)
                    )
                )


def test_idempotent_replay_returns_the_original_typed_submission() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    contract = EvaluationContract.from_path(ROOT / "config" / "mvp_evaluation_v2.json")
    suffix = uuid.uuid4().hex
    user = store.register_user(
        auth_subject=f"postgres-replay-{suffix}",
        public_handle=f"postgres-replay-{suffix}",
    )
    request = {
        "user": user,
        "idempotency_key": f"replay-{suffix}",
        "student_prompt": "integration test prompt",
        "contract": contract,
    }

    created = store.create_submission(**request)
    replayed = store.create_submission(**request)

    assert replayed.replayed is True
    assert replayed.submission == created.submission
    assert replayed.submission.status is SubmissionStatus.QUEUED
    assert replayed.submission.evaluation_identity_sha256 == contract.evaluation_identity_sha256


def test_submission_outbox_claim_and_rejection_round_trip() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    contract = EvaluationContract.from_path(ROOT / "config" / "mvp_evaluation_v2.json")
    suffix = uuid.uuid4().hex
    user = store.register_user(
        auth_subject=f"postgres-integration-{suffix}",
        public_handle=f"postgres-{suffix}",
    )
    created = store.create_submission(
        user=user,
        idempotency_key=f"integration-{suffix}",
        student_prompt="integration test prompt",
        contract=contract,
    )

    unpublished = store.unpublished_submission_ids(
        contract.evaluation_identity_sha256,
        contract.contract_snapshot_sha256,
    )
    assert created.submission.submission_id in unpublished
    store.mark_outbox_published(created.submission.submission_id)
    claim = store.claim_submission(
        created.submission.submission_id,
        evaluation_identity_sha256=contract.evaluation_identity_sha256,
        contract_snapshot_sha256=contract.contract_snapshot_sha256,
        lease_seconds=30,
        max_attempts=contract.max_attempts,
        max_running_per_user=contract.max_running_submissions_per_user,
    ).claim

    assert claim is not None
    assert store.complete_rejected(claim)
    result = store.owner_result(created.submission.submission_id, user.user_id)
    assert result is not None
    assert result.status.value == "rejected"
    assert result.failure == {
        "code": "TOKEN_LIMIT_EXCEEDED",
        "failure_contract_version": contract.failure_contract_version,
        "retryable": False,
    }


def test_successful_submission_is_visible_in_owner_result_and_leaderboard() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    contract = EvaluationContract.from_path(ROOT / "config" / "mvp_evaluation_v2.json")
    suffix = uuid.uuid4().hex
    user = store.register_user(
        auth_subject=f"postgres-success-{suffix}",
        public_handle=f"postgres-success-{suffix}",
    )
    created = store.create_submission(
        user=user,
        idempotency_key=f"success-{suffix}",
        student_prompt="integration test prompt",
        contract=contract,
    )
    claim = store.claim_submission(
        created.submission.submission_id,
        evaluation_identity_sha256=contract.evaluation_identity_sha256,
        contract_snapshot_sha256=contract.contract_snapshot_sha256,
        lease_seconds=30,
        max_attempts=contract.max_attempts,
        max_running_per_user=contract.max_running_submissions_per_user,
    ).claim

    assert claim is not None
    owner_result = _owner_result(contract)
    assert store.complete_success(claim, owner_result=owner_result)
    result = store.owner_result(created.submission.submission_id, user.user_id)
    assert result is not None
    assert result.status.value == "succeeded"
    assert result.result == owner_result
    leaderboard = store.leaderboard(contract.evaluation_identity_sha256)
    assert any(
        entry.public_handle == user.public_handle and entry.score == 0.75
        for entry in leaderboard
    )


def test_retry_returns_a_live_claim_to_queued_before_final_failure() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    contract = EvaluationContract.from_path(ROOT / "config" / "mvp_evaluation_v2.json")
    suffix = uuid.uuid4().hex
    user = store.register_user(
        auth_subject=f"postgres-retry-{suffix}",
        public_handle=f"postgres-retry-{suffix}",
    )
    created = store.create_submission(
        user=user,
        idempotency_key=f"retry-{suffix}",
        student_prompt="integration test prompt",
        contract=contract,
    )
    first_claim = store.claim_submission(
        created.submission.submission_id,
        evaluation_identity_sha256=contract.evaluation_identity_sha256,
        contract_snapshot_sha256=contract.contract_snapshot_sha256,
        lease_seconds=30,
        max_attempts=contract.max_attempts,
        max_running_per_user=contract.max_running_submissions_per_user,
    ).claim

    assert first_claim is not None
    assert store.retry_submission(first_claim, max_attempts=contract.max_attempts)
    second_claim = store.claim_submission(
        created.submission.submission_id,
        evaluation_identity_sha256=contract.evaluation_identity_sha256,
        contract_snapshot_sha256=contract.contract_snapshot_sha256,
        lease_seconds=30,
        max_attempts=contract.max_attempts,
        max_running_per_user=contract.max_running_submissions_per_user,
    ).claim
    assert second_claim is not None
    assert store.complete_failure(
        second_claim,
        failure_contract_version=contract.failure_contract_version,
        code="RUNTIME_MISCONFIGURATION",
        retryable=False,
    )
    result = store.owner_result(created.submission.submission_id, user.user_id)
    assert result is not None
    assert result.status.value == "failed"
    assert result.failure is not None
    assert result.failure["code"] == "RUNTIME_MISCONFIGURATION"


def test_retry_uses_database_time_after_waiting_for_the_submission_lock() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    contract = EvaluationContract.from_path(ROOT / "config" / "mvp_evaluation_v2.json")
    suffix = uuid.uuid4().hex
    user = store.register_user(
        auth_subject=f"postgres-lock-{suffix}",
        public_handle=f"postgres-lock-{suffix}",
    )
    created = store.create_submission(
        user=user,
        idempotency_key=f"lock-{suffix}",
        student_prompt="integration test prompt",
        contract=contract,
    )
    claim = store.claim_submission(
        created.submission.submission_id,
        evaluation_identity_sha256=contract.evaluation_identity_sha256,
        contract_snapshot_sha256=contract.contract_snapshot_sha256,
        lease_seconds=1,
        max_attempts=contract.max_attempts,
        max_running_per_user=contract.max_running_submissions_per_user,
    ).claim
    assert claim is not None

    blocker = store._connect()
    blocker.execute(
        "SELECT id FROM submissions WHERE id = %s FOR UPDATE",
        (created.submission.submission_id,),
    )
    started = threading.Event()
    retried: list[bool] = []

    def retry_after_lock() -> None:
        started.set()
        retried.append(store.retry_submission(claim, max_attempts=contract.max_attempts))

    thread = threading.Thread(target=retry_after_lock)
    thread.start()
    assert started.wait(timeout=1)
    time.sleep(1.2)
    blocker.rollback()
    blocker.close()
    thread.join(timeout=3)

    assert thread.is_alive() is False
    assert retried == [False]


def test_postgres_history_and_outbox_are_isolated_across_challenges() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    first_contract = _contract_for_challenge("en-ewt-upos-v1")
    second_contract = _contract_for_challenge("en-ewt-upos-v2")
    suffix = uuid.uuid4().hex
    user = store.register_user(
        auth_subject=f"postgres-history-{suffix}",
        public_handle=f"postgres-history-{suffix}",
    )
    first = store.create_submission(
        user=user,
        idempotency_key=f"history-first-{suffix}",
        student_prompt="first integration prompt",
        contract=first_contract,
    )
    second = store.create_submission(
        user=user,
        idempotency_key=f"history-second-{suffix}",
        student_prompt="second integration prompt",
        contract=second_contract,
    )

    first_partition = store.unpublished_submission_ids(
        first_contract.evaluation_identity_sha256,
        first_contract.contract_snapshot_sha256,
    )
    second_partition = store.unpublished_submission_ids(
        second_contract.evaluation_identity_sha256,
        second_contract.contract_snapshot_sha256,
    )
    newest = store.submissions_for_owner(user.user_id, limit=1)
    older = store.submissions_for_owner(
        user.user_id,
        limit=2,
        before_created_at=newest[0].created_at,
        before_submission_id=newest[0].submission_id,
    )

    assert first.submission.submission_id in first_partition
    assert first.submission.submission_id not in second_partition
    assert second.submission.submission_id in second_partition
    assert second.submission.submission_id not in first_partition
    assert newest[0].submission_id == second.submission.submission_id
    assert [record.submission_id for record in older] == [first.submission.submission_id]


def test_postgres_leaderboard_pagination_keeps_absolute_ranks() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex
    contract = _contract_for_challenge(f"postgres-leaderboard-{suffix}")
    for index in range(3):
        user = store.register_user(
            auth_subject=f"postgres-board-{index}-{suffix}",
            public_handle=f"postgres-board-{index}-{suffix}",
        )
        created = store.create_submission(
            user=user,
            idempotency_key=f"board-{index}-{suffix}",
            student_prompt="integration test prompt",
            contract=contract,
        )
        claim = store.claim_submission(
            created.submission.submission_id,
            evaluation_identity_sha256=contract.evaluation_identity_sha256,
            contract_snapshot_sha256=contract.contract_snapshot_sha256,
            lease_seconds=30,
            max_attempts=contract.max_attempts,
            max_running_per_user=contract.max_running_submissions_per_user,
        ).claim
        assert claim is not None
        assert store.complete_success(claim, owner_result=_owner_result(contract))

    first = store.leaderboard(contract.evaluation_identity_sha256, limit=2)
    second = store.leaderboard(
        contract.evaluation_identity_sha256,
        limit=2,
        after_rank=first[-1].rank,
    )

    assert [entry.rank for entry in first] == [1, 2]
    assert [entry.rank for entry in second] == [3]


def test_postgres_structured_quota_and_queued_deadline_sweep() -> None:
    assert POSTGRES_TEST_DATABASE_URL is not None
    store = PostgresSubmissionStore(POSTGRES_TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex
    mapping = json.loads(_contract_for_challenge(f"postgres-quota-{suffix}").snapshot_json)
    mapping["limits"]["submissions_per_user_per_challenge_per_24h"] = 1
    contract = EvaluationContract.from_mapping(mapping)
    user = store.register_user(
        auth_subject=f"postgres-quota-{suffix}",
        public_handle=f"postgres-quota-{suffix}",
    )
    created = store.create_submission(
        user=user,
        idempotency_key=f"quota-first-{suffix}",
        student_prompt="integration test prompt",
        contract=contract,
    )

    with pytest.raises(SubmissionQuotaError) as captured:
        store.create_submission(
            user=user,
            idempotency_key=f"quota-second-{suffix}",
            student_prompt="integration test prompt",
            contract=contract,
        )
    assert captured.value.code == "SUBMISSION_RATE_LIMIT"
    assert captured.value.limit == captured.value.current == 1
    assert captured.value.retry_after_seconds is not None

    with store._connect() as connection:
        connection.execute(
            "UPDATE submissions SET deadline_at = '2000-01-01T00:00:00.000000+00:00' "
            "WHERE id = %s",
            (created.submission.submission_id,),
        )
    assert store.expire_queued_deadlines() == 1
    result = store.owner_result(created.submission.submission_id, user.user_id)
    assert result is not None and result.failure is not None
    assert result.failure["code"] == "JOB_DEADLINE"
    assert store.unpublished_submission_ids(
        contract.evaluation_identity_sha256,
        contract.contract_snapshot_sha256,
    ) == ()
