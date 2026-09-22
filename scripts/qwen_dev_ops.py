"""Instance-scoped health, consistent backup and isolated restore for private Qwen18."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, build_opener

KIND = 'linguistic-oj-private-qwen18-v1'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, payload):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    Path(path).chmod(0o600)


def validate_marker(marker, owner):
    instance = marker.get('instance', '')
    if (marker.get('kind') != KIND or marker.get('owner') != owner
            or not re.fullmatch('[a-f0-9]{32}', instance)
            or marker.get('database') != 'loj_dev18_' + instance[:16]
            or not isinstance(marker.get('contracts'), dict)):
        raise ValueError('unexpected instance marker')


class Instance:
    def __init__(self, root, instance):
        import pwd

        self.root = root.resolve(strict=True)
        self.home = instance.resolve(strict=True)
        if not self.home.is_relative_to(self.root):
            raise ValueError('instance must be within the project')
        self.state = self.home / 'state'
        if self.state.stat().st_mode & 0o077:
            raise ValueError('state must be owner-only')
        self.owner = pwd.getpwuid(os.geteuid()).pw_name
        self.marker = json.loads((self.state / 'instance.json').read_text(encoding='utf-8'))
        validate_marker(self.marker, self.owner)
        self.pg_bin = self.root / 'toolchain/postgresql/bin'
        self.pg_socket = self.root / 'services/postgresql/run'
        self.redis_socket = self.root / 'services/redis/run/redis.sock'
        self.port = 5433
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
        self.env.update(PGHOST=str(self.pg_socket), PGPORT=str(self.port), PGUSER=self.owner,
                        PGPASSFILE=str(self.state / 'empty.pgpass'))
        with self.connect('postgres') as connection:
            row = connection.execute(
                "SELECT pg_get_userbyid(datdba), shobj_description(oid,'pg_database') "
                'FROM pg_database WHERE datname=%s', (self.marker['database'],)
            ).fetchone()
            if row != (self.owner, KIND + ':' + self.marker['instance']):
                raise ValueError('database owner/comment differs from state marker')

    def connect(self, database=None, **kwargs):
        import psycopg

        return psycopg.connect(host=str(self.pg_socket), port=self.port, user=self.owner,
                               dbname=database or self.marker['database'], password='',
                               passfile=str(self.state / 'empty.pgpass'), connect_timeout=5,
                               **kwargs)

    def database_url(self, database=None):
        query = urlencode({'host': str(self.pg_socket), 'port': self.port,
                           'passfile': str(self.state / 'empty.pgpass')})
        return (f'postgresql://{quote(self.owner, safe="")}@/'
                f'{database or self.marker["database"]}?{query}')

    def application(self):
        found = []
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                argv = (path / 'cmdline').read_bytes().decode().strip('\0').split('\0')
                if argv[1:3] == ['-m', 'linguistic_oj.qwen_development']:
                    if Path(argv[argv.index('--state-dir') + 1]).resolve() == self.state:
                        found.append((int(path.name), argv))
            except (OSError, ValueError, UnicodeError):
                continue
        if len(found) != 1:
            raise RuntimeError('expected one running application for this instance')
        return found[0]

    def command(self, command, *, log, **kwargs):
        with Path(log).open('xb') as stream:
            subprocess.run([str(self.pg_bin / command[0]), *command[1:]], env=self.env,
                           stdout=stream, stderr=subprocess.STDOUT, check=True,
                           timeout=300, **kwargs)


def table_fingerprints(connection):
    from psycopg import sql

    tables = [row[0] for row in connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )]
    result = {}
    for table in tables:
        checksum = hashlib.sha256()
        count = 0
        # Aggregate fingerprints, never export table rows to reports or logs.
        with connection.cursor(name='fingerprint_' + uuid.uuid4().hex) as cursor:
            cursor.execute(sql.SQL('SELECT row_to_json(t)::text FROM {} t '
                                   'ORDER BY row_to_json(t)::text').format(sql.Identifier(table)))
            for row in cursor:
                checksum.update(row[0].encode('utf-8') + b'\n')
                count += 1
        result[table] = {'count': count, 'sha256': checksum.hexdigest()}
    return result


def health(instance):
    from linguistic_oj.postgres_submission_store import PostgresSubmissionStore

    report = {'instance': instance.marker['instance'], 'database': instance.marker['database']}
    with instance.connect() as connection:
        PostgresSubmissionStore(instance.database_url()).health_check()
        report['schema_version'] = connection.execute(
            'SELECT max(version) FROM schema_migrations').fetchone()[0]
        report['submissions'] = dict(connection.execute(
            'SELECT status,count(*) FROM submissions GROUP BY status').fetchall())
    report['database_ready'] = True
    try:
        pid, argv = instance.application()
        report['pid'] = pid
        report['source_root'] = argv[argv.index('--root') + 1]
        port = int(argv[argv.index('--port') + 1]) if '--port' in argv else 8090
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/health/ready', timeout=10) as response:
            report['application_ready'] = json.load(response) == {'status': 'ready'}
    except Exception as error:
        report['application_ready'] = False
        report['application_error'] = type(error).__name__
    report['uncertain_inference'] = (instance.state / 'uncertain-inference.json').exists()
    report['ready'] = report['application_ready'] and not report['uncertain_inference']
    return report


def archive_files(path, files):
    fingerprints = {}
    with tarfile.open(path, 'x:gz') as archive:
        for name, source in sorted(files.items()):
            if source.is_symlink() or not source.is_file():
                raise ValueError('archive input must be a regular file')
            fingerprints[name] = digest(source)
            archive.add(source, arcname=name, recursive=False)
    Path(path).chmod(0o600)
    return fingerprints


def backup(instance):
    pid, argv = instance.application()
    if '--execution-profile' in argv or (instance.state / 'request-executor').exists():
        raise ValueError('request execution needs a profile-and-ledger-aware backup format')
    source = Path(argv[argv.index('--root') + 1]).resolve(strict=True)
    data = Path(argv[argv.index('--data-root') + 1]).resolve(strict=True)
    if not source.is_relative_to(instance.root) or not data.is_relative_to(instance.root):
        raise ValueError('source/data must be project owned')
    parent = instance.root / 'backups/qwen-development'
    parent.mkdir(mode=0o700, exist_ok=True)
    dest = parent / (datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    dest.mkdir(mode=0o700)
    source_manifest = json.loads((source / 'acceptance-source.json').read_text(encoding='utf-8'))
    source_files = {'acceptance-source.json': source / 'acceptance-source.json'}
    for name, expected in source_manifest['files'].items():
        path = source / name
        if not path.resolve().is_relative_to(source) or digest(path) != expected:
            raise ValueError('source manifest mismatch')
        source_files[name] = path
    resources = {}
    for challenge in instance.marker['contracts']:
        if not re.fullmatch('[a-z0-9-]+', challenge):
            raise ValueError('invalid challenge identifier')
        resources['manifests/' + challenge + '.json'] = (
            data / 'runtime/private/challenges' / (challenge + '.json'))
    for path in (data / 'Standard_Dataset/by_language').glob('*.jsonl'):
        resources['datasets/' + path.name] = path
    resources['runtime-evidence/qwen-launch.json'] = Path(
        argv[argv.index('--launch-evidence') + 1])
    tokenizer = Path(argv[argv.index('--tokenizer-snapshot') + 1]).resolve(strict=True)
    for name in ('tokenizer.json', 'tokenizer_config.json', 'config.json'):
        resources['tokenizer/' + name] = tokenizer / name
    for path in resources.values():
        if not path.resolve().is_relative_to(instance.root):
            raise ValueError('resource outside project')
    source_hashes = archive_files(dest / 'source.tar.gz', source_files)
    resource_hashes = archive_files(dest / 'evaluation-assets.tar.gz', resources)
    write_json(dest / 'instance.json', instance.marker)
    with instance.connect() as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        snapshot = connection.execute('SELECT pg_export_snapshot()').fetchone()[0]
        tables = table_fingerprints(connection)
        instance.command(['pg_dump', '-Fc', '--snapshot=' + snapshot, '--dbname',
                          instance.marker['database'], '--file', str(dest / 'database.dump')],
                         log=dest / 'dump.log')
    report = {
        'kind': 'qwen18-backup-v1', 'created_at': datetime.now(UTC).isoformat(),
        'database': instance.marker['database'], 'instance': instance.marker['instance'],
        'source_root': str(source), 'data_root': str(data), 'source_pid': pid,
        'tables': tables, 'source_files': source_hashes, 'evaluation_files': resource_hashes,
        'queue_recovery': 'PostgreSQL outbox into a fresh instance namespace; '
                          'confirm termination of running jobs before any real replay',
        'model_weights': 'Referenced by pinned model revision; not copied into this backup',
        'files': {name: digest(dest / name) for name in (
            'database.dump', 'instance.json', 'source.tar.gz', 'evaluation-assets.tar.gz')},
    }
    write_json(dest / 'manifest.json', report)
    (dest / 'database.dump').chmod(0o600)
    return {'backup': str(dest), 'tables': tables,
            'manifest_sha256': digest(dest / 'manifest.json')}


def verify_archive(path, expected):
    with tarfile.open(path) as archive:
        names = archive.getnames()
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError('archive inventory mismatch')
        for item in archive.getmembers():
            if not item.isfile() or item.name.startswith('/') or '..' in Path(item.name).parts:
                raise ValueError('unsafe archive member')
            checksum = hashlib.sha256()
            with archive.extractfile(item) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    checksum.update(block)
            if checksum.hexdigest() != expected[item.name]:
                raise ValueError('archive content mismatch')


def verify_copy(directory, expected_manifest=None):
    directory = directory.resolve(strict=True)
    manifest_sha256 = digest(directory / 'manifest.json')
    if expected_manifest is not None and manifest_sha256 != expected_manifest:
        raise ValueError('backup manifest fingerprint mismatch')
    report = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if report['kind'] != 'qwen18-backup-v1':
        raise ValueError('unexpected backup kind')
    if set(report['files']) != {
        'database.dump', 'instance.json', 'source.tar.gz', 'evaluation-assets.tar.gz'
    }:
        raise ValueError('unexpected backup inventory')
    for name, expected in report['files'].items():
        if digest(directory / name) != expected:
            raise ValueError('backup fingerprint mismatch')
    verify_archive(directory / 'source.tar.gz', report['source_files'])
    verify_archive(directory / 'evaluation-assets.tar.gz', report['evaluation_files'])
    return {'files_verified': True, 'manifest_sha256': manifest_sha256}


def verify_restore(instance, directory):
    from psycopg import sql

    from linguistic_oj.challenge import PublicChallenge
    from linguistic_oj.mvp_contract import EvaluationContract
    from linguistic_oj.postgres_submission_store import PostgresSubmissionStore
    from linguistic_oj.submission_jobs import InMemoryJobQueue, OutboxDispatcher

    directory = directory.resolve(strict=True)
    if not directory.is_relative_to(instance.root / 'backups/qwen-development'):
        raise ValueError('restore verification requires an instance backup directory')
    report = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if report['kind'] != 'qwen18-backup-v1' or report['instance'] != instance.marker['instance']:
        raise ValueError('backup belongs to a different instance')
    verify_copy(directory)
    nonce = uuid.uuid4().hex
    database = 'loj_restore_' + nonce
    owner_comment = 'loj-isolated-restore:' + nonce
    result = {'kind': 'qwen18-restore-verification-v1', 'backup': str(directory),
              'restore_database': database, 'passed': False, 'cleanup_confirmed': False}
    created = False
    with instance.connect() as live:
        live.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        before = table_fingerprints(live)
    try:
        with instance.connect('postgres', autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(
                sql.Identifier(database), sql.Identifier(instance.owner)))
            created = True
            admin.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                sql.Identifier(database), sql.Literal(owner_comment)))
        instance.command(['pg_restore', '--exit-on-error', '--no-owner', '--no-privileges',
                          '--dbname', database, str(directory / 'database.dump')],
                         log=directory / ('restore-' + nonce + '.log'))
        with instance.connect(database) as restored:
            restored.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            actual = table_fingerprints(restored)
            store = PostgresSubmissionStore(instance.database_url(database))
            store.health_check()
            if actual != report['tables']:
                raise ValueError('restored rows differ from consistent backup snapshot')
            result['tables_match'] = True
            count = 0
            for submission_id, owner in restored.execute('SELECT id,user_id FROM submissions'):
                prompt = store.owner_prompt(submission_id, owner)
                if prompt is None or hashlib.sha256(prompt.student_prompt.encode()).hexdigest() != (
                    prompt.student_prompt_sha256
                ) or store.owner_prompt(submission_id, 'non-owner') is not None:
                    raise ValueError('restored prompt ownership or text mismatch')
                if store.submission_for_owner(submission_id, owner) is None:
                    raise ValueError('restored history unreadable')
                if store.owner_result(submission_id, owner) is None:
                    raise ValueError('restored result unreadable')
                count += 1
            result['owner_records_checked'] = count
            result['restored_credentials'] = actual['auth_credentials']['count']
            result['restored_teaching_revisions'] = actual['challenge_admin_revisions']['count']
        # A synthetic delivery only in the restored DB and in-memory queue. No model/Redis writes.
        with tarfile.open(directory / 'source.tar.gz') as archive:
            entry = json.load(archive.extractfile('config/challenge_contract_registry_v1.json'))
            chosen = next(item for item in entry['entries'] if item['evaluation_contract_path'])
            contract = EvaluationContract.from_mapping(json.load(archive.extractfile(
                chosen['evaluation_contract_path'])))
            public = PublicChallenge.model_validate_json(archive.extractfile(
                chosen['public_descriptor_path']).read())
        from linguistic_oj.admin_store import source_fingerprint

        user = store.register_user(auth_subject='restore-' + nonce, public_handle='RestoreProbe')
        created_submission = store.create_submission(
            user=user, idempotency_key='restore-probe', student_prompt='isolated recovery probe',
            contract=contract, source_fingerprint=source_fingerprint(public))
        queue = InMemoryJobQueue(contract.contract_snapshot_sha256)
        OutboxDispatcher(store, queue, contract).dispatch_pending()
        replacement = InMemoryJobQueue(contract.contract_snapshot_sha256)
        OutboxDispatcher(store, replacement, contract).recover()
        recovered = []
        while (delivery := replacement.receive()) is not None:
            recovered.append(delivery.message.submission_id)
            replacement.ack(delivery)
        if created_submission.submission.submission_id not in recovered:
            raise ValueError('outbox failed to recover a published queued probe')
        result['isolated_queue_rebuild'] = True
        result['passed'] = True
    finally:
        if created:
            with instance.connect('postgres', autocommit=True) as admin:
                row = admin.execute(
                    "SELECT pg_get_userbyid(datdba), shobj_description(oid,'pg_database') "
                    'FROM pg_database WHERE datname=%s', (database,)).fetchone()
                if row == (instance.owner, owner_comment):
                    admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(database)))
                    result['cleanup_confirmed'] = True
        with instance.connect() as live:
            live.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            result['live_tables_unchanged'] = table_fingerprints(live) == before
        write_json(directory / ('verification-' + nonce + '.json'), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'backup', 'verify-restore', 'verify-copy'])
    parser.add_argument('--project-root', type=Path)
    parser.add_argument('--instance-dir', type=Path)
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    if args.action == 'verify-copy':
        if args.backup_dir is None:
            parser.error('--backup-dir is required')
        print(json.dumps(verify_copy(args.backup_dir, args.manifest_sha256), indent=2))
        return
    if args.project_root is None or args.instance_dir is None:
        parser.error('--project-root and --instance-dir are required')
    if os.name != 'posix':
        parser.error('run on the Linux school host')
    os.umask(0o077)
    instance = Instance(args.project_root, args.instance_dir)
    if args.action == 'status':
        result = health(instance)
    elif args.action == 'backup':
        result = backup(instance)
    else:
        if args.backup_dir is None:
            parser.error('--backup-dir is required')
        result = verify_restore(instance, args.backup_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if (result.get('ready') is False or result.get('passed') is False
            or result.get('cleanup_confirmed') is False):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
