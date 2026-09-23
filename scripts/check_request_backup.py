"""Owned-database backup/restore fixture. No school app/model changes or inference HTTP."""

import json
import os
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from linguistic_oj.auth import AuthService
from linguistic_oj.bounded_executor_state import BoundedExecutorState
from linguistic_oj.executor_state import executor_lock
from linguistic_oj.mvp_contract import canonical_sha256
from linguistic_oj.postgres_migrations import migrate_postgres
from linguistic_oj.postgres_submission_store import PostgresSubmissionStore
from linguistic_oj.providers import DeterministicMockProvider
from linguistic_oj.request_backup import SETTINGS_VERSION
from linguistic_oj.request_development import STATE_CHILD, initialize_state
from linguistic_oj.runner import run_challenge
from linguistic_oj.submission_jobs import InMemoryJobQueue, OutboxDispatcher
from scripts.check_request_development import prepare_fixture
from scripts.qwen_dev_ops import backup_request, digest, verify_copy, verify_restore, write_json


def fixture_files(root, directory, *, owner='explicit-fixture', uncertain=False):
    args, marker, profile, _ = prepare_fixture(root, directory, max_jobs=4)
    instance_id = uuid.uuid4().hex
    marker.update(instance=instance_id, database='loj_dev18_' + instance_id[:16], owner=owner)
    (args.state_dir / 'instance.json').write_text(json.dumps(marker), encoding='utf-8')
    binding = initialize_state(args.state_dir, profile)
    child = args.state_dir / STATE_CHILD
    key = next(iter(profile.contracts))
    with executor_lock(child):
        state = BoundedExecutorState(child, binding)
        state.begin(key)
        # Simulated interruption before HTTP. Explicit fixture evidence, never real inference.
        state = BoundedExecutorState(child, binding)
        pending = state.snapshot()['pending']
        proof = directory / 'fixture-proof.json'
        write_json(proof, {'binding_sha256': binding, 'pending_request_ids': sorted(pending),
            'all_prior_requests_terminated': True, 'confirmed_by': 'fixture-only',
            'evidence_reference': 'intent-only fixture; no HTTP was sent'})
        state.recover_all(canonical_sha256(pending), proof)
        if uncertain:
            operation = state.begin(key, request_context={
                'submission_sha256': 'a' * 64, 'sample_id_sha256': 'b' * 64,
                'request_sha256': 'c' * 64, 'sample_position': 1})
            state.block(operation)
    (args.tokenizer_snapshot / 'config.json').write_text('{"fixture":true}', encoding='utf-8')
    files = {p.relative_to(args.root).as_posix(): digest(p)
             for p in args.root.rglob('*') if p.is_file()}
    write_json(args.root / 'acceptance-source.json',
               {'schema_version': 'isolated-acceptance-source-v1', 'files': files})
    settings = directory / 'request-backup-settings.json'
    write_json(settings, {'version': SETTINGS_VERSION, 'source_root': str(args.root),
        'data_root': str(args.data_root), 'state_dir': str(args.state_dir),
        'execution_profile': str(args.execution_profile), 'registry': args.registry.as_posix(),
        'tokenizer_snapshot': str(args.tokenizer_snapshot),
        'launch_evidence': str(args.launch_evidence)})
    return args, marker, profile, settings


class OwnedFixtureInstance:
    """Same interface as the operator instance; only a fresh UUID-marked fixture database."""

    def __init__(self, directory, url, *, pg_bin=None, container=None):
        import psycopg

        from linguistic_oj.postgres_migrations import validate_postgres_url

        self.root = directory.resolve()
        self._url = validate_postgres_url(url)
        self.pg_bin, self.container = pg_bin, container
        self.oid = None
        self.marker = None
        with psycopg.connect(self._url) as connection:
            self.owner = connection.execute('SELECT current_user').fetchone()[0]

    def database_url(self, database=None):
        parsed = urlsplit(self._url)
        return urlunsplit(parsed._replace(path='/' + (database or self.marker['database'])))

    def connect(self, database=None, **kwargs):
        import psycopg

        return psycopg.connect(self.database_url(database), **kwargs)

    def create(self, args, marker):
        from psycopg import sql

        self.state, self.marker = args.state_dir, marker
        self.comment = 'loj-request-backup-fixture:' + marker['instance']
        with self.connect('postgres', autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(
                sql.Identifier(marker['database']), sql.Identifier(self.owner)))
            self.oid = admin.execute('SELECT oid FROM pg_database WHERE datname=%s',
                                    (marker['database'],)).fetchone()[0]
            admin.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                sql.Identifier(marker['database']), sql.Literal(self.comment)))
        migrate_postgres(self.database_url(), applied_at='isolated-backup-fixture')
        return PostgresSubmissionStore(self.database_url())

    def command(self, command, *, log, **kwargs):
        from psycopg.conninfo import conninfo_to_dict

        command = list(command)
        with open(log, 'xb') as output:
            if self.container:
                # Use the actual service container's matching PostgreSQL client version.
                prefix = ['docker', 'exec', '-i', self.container, command.pop(0), '-U', self.owner]
                if '--file' in command:
                    index = command.index('--file')
                    path = command[index + 1]
                    del command[index:index + 2]
                    with open(path, 'xb') as dump:
                        subprocess.run(prefix + command, stdout=dump, stderr=output,
                                       check=True, timeout=120)
                else:
                    path = command.pop()
                    with open(path, 'rb') as dump:
                        subprocess.run(prefix + command, stdin=dump, stdout=output,
                                       stderr=subprocess.STDOUT, check=True, timeout=120)
            else:
                if self.pg_bin is None:
                    raise ValueError('explicit matching PostgreSQL client directory required')
                env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
                parameters = conninfo_to_dict(self._url)
                for key, value in parameters.items():
                    name = {'user': 'PGUSER', 'dbname': 'PGDATABASE',
                            'passfile': 'PGPASSFILE'}.get(key, 'PG' + key.upper())
                    env[name] = value
                subprocess.run([str(Path(self.pg_bin) / command[0]), *command[1:]], env=env,
                    stdout=output, stderr=subprocess.STDOUT, check=True, timeout=120)

    def close(self):
        from psycopg import sql

        if self.oid is None:
            return True
        with self.connect('postgres', autocommit=True) as admin:
            row = admin.execute(
                "SELECT oid, pg_get_userbyid(datdba), shobj_description(oid,'pg_database') "
                'FROM pg_database WHERE datname=%s', (self.marker['database'],)).fetchone()
            if row != (self.oid, self.owner, self.comment):
                raise ValueError('fixture database ownership changed; preserved')
            admin.execute(sql.SQL('DROP DATABASE {}').format(
                sql.Identifier(self.marker['database'])))
        return True


def exercise(root, directory, url, *, uncertain=False, pg_bin=None, container=None):
    from linguistic_oj.qwen_development import load_development_catalog

    instance = OwnedFixtureInstance(directory, url, pg_bin=pg_bin, container=container)
    report = {'passed': False, 'real_qwen_requests': 0}
    try:
        args, marker, profile, settings = fixture_files(root, directory,
                                                       owner=instance.owner, uncertain=uncertain)
        store = instance.create(args, marker)
        _, artifacts = load_development_catalog(args.root, args.data_root, args.registry)
        auth = AuthService(store, public_origin='http://127.0.0.1:8090', development=True,
                           mailer=lambda *args: None)
        key = next(iter(profile.contracts))
        contract = profile.contracts[key]
        users = []
        for index in range(2):
            email = f'backup-fixture-{index}@example.test'
            auth.provision_development_account(email, 'backup-fixture-password', f'Owner{index}')
            user = store.user_by_subject(store.auth_account(email).subject)
            users.append(user)
            created = store.create_submission(user=user, idempotency_key='fixture-' + str(index),
                student_prompt=f'  fixture{index} 中文\r\ne\u0301 ', contract=contract)
            claim = store.claim_submission(created.submission.submission_id,
                evaluation_identity_sha256=contract.evaluation_identity_sha256,
                contract_snapshot_sha256=contract.contract_snapshot_sha256,
                lease_seconds=contract.job_deadline_seconds, max_attempts=contract.max_attempts,
                max_running_per_user=contract.max_running_submissions_per_user).claim
            aggregate = run_challenge(artifacts[key], DeterministicMockProvider(),
                                       student_prompt=claim.student_prompt)
            assert store.complete_success(claim, owner_result=contract.owner_result(aggregate,
                student_prompt_sha256=claim.student_prompt_sha256))
        queued = store.create_submission(user=users[0], idempotency_key='queued',
            student_prompt='preserve queued prompt', contract=contract)
        queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
        OutboxDispatcher(store, queue, contract).dispatch_pending()
        if uncertain:
            created = store.create_submission(user=users[1], idempotency_key='interrupted',
                student_prompt='interrupted fixture', contract=contract)
            assert store.claim_submission(created.submission.submission_id,
                evaluation_identity_sha256=contract.evaluation_identity_sha256,
                contract_snapshot_sha256=contract.contract_snapshot_sha256,
                lease_seconds=contract.job_deadline_seconds, max_attempts=contract.max_attempts,
                max_running_per_user=contract.max_running_submissions_per_user).claim
        evidence_before = {p.relative_to(args.state_dir).as_posix(): digest(p)
                           for p in args.state_dir.rglob('*') if p.is_file()}
        captured = backup_request(instance, settings)
        backup_dir = Path(captured['backup'])
        assert verify_copy(backup_dir, captured['manifest_sha256'])['files_verified']
        verified = verify_restore(instance, backup_dir, captured['manifest_sha256'])
        assert verified['passed'] and verified['cleanup_confirmed']
        assert verified['live_tables_unchanged']
        assert verified['owner_records_checked'] == (4 if uncertain else 3)
        assert verified['restored_credentials'] == 2 and verified['isolated_queue_rebuild']
        state_report = verified['request_execution']
        assert state_report['requires_reconciliation'] is uncertain
        assert state_report['recovery_audits'] == 1 and state_report['safety_gate_preserved']
        assert not state_report['automatic_start_allowed']
        assert state_report['model_requests_sent'] == 0
        queued_result = store.owner_result(queued.submission.submission_id, users[0].user_id)
        assert queued_result.status.value == 'queued'
        assert evidence_before == {p.relative_to(args.state_dir).as_posix(): digest(p)
                                   for p in args.state_dir.rglob('*') if p.is_file()}
        report.update(passed=True, backup=captured, verification=verified,
                      source_state_unchanged=True, source_grades=2, fixture_scoring_samples=100)
    finally:
        report['fixture_database_cleanup_confirmed'] = instance.close()
    return report
