# Production Operations Baseline

## Current boundary

The formal service target is now public access from on- and off-campus networks.
`docs/PUBLIC_DEPLOYMENT.md` records the required domain/ingress, HTTPS, production
accounts/mail and release process. Current 8090 is still the SSH developer instance;
its shared test accounts and captured inbox must not be exposed as the public service.

**2026-09-12 engineering audit:** the server's existing `backup-services` and
`verify-backup-restore` still target `linguistic_oj` / `linguistic_oj_test`, and the
health/restore scripts assume schema v1. They do not establish backup, restore or
health coverage for the active `loj_dev18_dd2ec9df38e540b5` database/schema v4.
Current-instance protection now uses `operations/bin/qwen-development` with
`status`, `backup`, and `verify-restore`. Its first actual backup/isolated restore
and verified off-host copy passed; see [QWEN_DEVELOPMENT_OPERATIONS.md](QWEN_DEVELOPMENT_OPERATIONS.md).
The remaining operational work is in [SYSTEM_ENGINEERING_PLAN.md](SYSTEM_ENGINEERING_PLAN.md).
The service recipes and dated histories below must be read with their original targets.

The 2026-09-10 private developer composition `linguistic_oj.qwen_development` is
separate from the production entry point below. It uses a new ownership-marked
`loj_dev18_` database, a unique Redis namespace, existing read-only candidate data,
and one serial worker loop for the 22 configured contracts spanning 18 languages.
It listens only on `127.0.0.1:8090` and uses application-port SSH forwarding,
development cookies and captured mail. This does not activate public challenges
or change `app/current`. See [Qwen development](QWEN_DEVELOPMENT.md) for access,
real browser acceptance, restart behavior, and outstanding inference markers.

The Qwen v2 API and Worker have passed a real 50-sample loopback smoke using the
user-owned PostgreSQL 18.6 service and persistent Redis 7.4 Unix socket. This proves
the production-shaped submission path, but is not a production deployment. SQLite
remains available for single-machine development only. Do not enable external
submissions until production authentication, protected gold-data storage,
supervised restart recovery, monitoring, and off-host backup retention are in place.

## Health endpoints

### Schema compatibility after main integration

Both SQLite and PostgreSQL remain at schema v4. The two historical v3 variants are
recognized before applying v4: the earlier main branch added only `users.role`,
while the workbench branch also added the four authentication tables. The known
role-only v3 is completed transactionally without changing users, roles, prompts,
submissions, outbox rows or results. Existing accounts receive no inferred passwords
or credential bindings; production startup still requires trusted enrollment for
unbound accounts. Partial authentication tables, a malformed role column, or a v4
database missing authentication tables are rejected instead of silently repaired.
Migration failure rolls back both auth completion and subsequent v4 changes.

### Endpoint behavior

The API exposes unauthenticated, body-free operational endpoints:

- `GET /health/live` returns `200 {"status":"live"}` whenever the API process
  can serve requests.
- `GET /health/ready` returns `200 {"status":"ready"}` only when the API can
  query its submission store and check Redis version/EVAL capability on every queue. PostgreSQL
  readiness also verifies the expected migration version and required tables.
  With the current auth configuration it additionally checks bound accounts and
  certificate-verified SMTP connectivity/authentication. It returns a generic
  `503 SERVICE_NOT_READY` error envelope without connection details when any
  required dependency fails.

The API intentionally does not probe vLLM in readiness. Submission durability
depends on the database and Redis; Worker startup attestation owns vLLM validation.

The same process serves the packaged student client at `/` and immutable client
assets below `/assets/`. The document is protected by a same-origin Content
Security Policy. The current authenticated composition uses server-issued HttpOnly
session cookies, not browser-entered Bearer tokens, and requires an HTTPS reverse
proxy. The client's legacy Bearer UI applies only to an older deployment returning
404 for `/v1/auth/config`, not to a failure of the current account service.

Every API request emits exactly one JSON completion record with only event,
request ID, method, path, status, and duration. Prompt contents, authentication
subjects, authorization headers, idempotency keys, query strings, model content,
and dependency error messages are excluded.

## Required production services

- Run Redis 7.4 or later with ACL authentication, AOF persistence, a bounded
  `maxmemory` policy that never evicts queue keys, backups, and monitoring.
- Pass authenticated Redis URLs through `--redis-url-file` (for example, a
  systemd credential under `/run/credentials`) rather than process arguments.
  Non-loopback Redis connections must use `rediss://`.
- Use PostgreSQL for API and Worker persistence. SQLite remains unsupported for
  a multi-process or restart-tolerant deployment. Pass authenticated URLs through
  `--postgres-database-url-file`; non-loopback connections must set `sslmode` to
  `require`, `verify-ca`, or `verify-full`.
- Keep dataset manifests, gold data, tokenizer snapshots, and vLLM launch evidence
  on server-owned volumes that are unreadable by web users and tenants.
- Provide a protected authentication configuration through `--auth-config-file`.
  Production `qwen_api` forbids `--authenticate` callbacks, including static smoke
  callbacks. See the startup requirements below.
- Keep the challenge registry deployment-owned. Never replace a contract snapshot
  under an existing challenge ID. Assign any changed contract a new versioned
  challenge ID, keep the previous entry and Worker running, and remove them only
  after their outbox and queue have drained.
- The API cannot run old and new contracts with different platform-wide admission
  limits at the same time. A change to one of those shared limits requires a
  maintenance window: stop new submissions, drain every old outbox and queue,
  verify the drain, then start the replacement registry and Workers.
- Run API, Worker, and vLLM under a supervisor that restarts failed processes and
  directs stdout/stderr to access-controlled retention.

## Authentication Startup

Current checkout requirements supersede the older callback-based startup recipe;
they do not assert that any server release or live database has been updated.
`linguistic_oj.qwen_api` defaults to production and requires PostgreSQL plus
`--auth-config-file`. Configure exactly one auth source; callbacks are limited to
explicit development/test compositions. Local Mock startup instead uses
`scripts/start_local_dev.ps1`, not this production entry point.

See [Authentication](AUTHENTICATION.md) for the protected JSON file shape, separate
SMTP password file, and platform-specific private-file permission checks. Set a
canonical root HTTPS `public_origin`, use certificate-verified SMTP SSL/STARTTLS,
and keep secrets out of the repository, command arguments, and logs. Startup and
readiness validate schema/account bindings and SMTP availability; readiness also
checks Redis but does not attest Qwen or guarantee future email delivery.

List only the exact actual connecting proxy IP addresses in `trusted_proxies`.
The trusted proxy must **overwrite** `X-LOJ-Client-IP` with one validated client IP,
never append or pass through a user-supplied value. Missing/ambiguous values from
a trusted peer fail closed. Arbitrary `Forwarded`/`X-Forwarded-*` values remain
ignored, and Uvicorn proxy-header processing stays disabled. Preserve the exact
browser Origin and same-origin JSON requests with `X-LOJ-CSRF: 1`; submission POSTs
also require the cookie account's `X-LOJ-Expected-User` snapshot.

Current SQLite/PostgreSQL migration code targets **schema v3**, appending auth
tables and roles to v2 without rebinding legacy users. PostgreSQL migration is an
explicit operation, not a side effect of auth readiness. Production startup
rejects any legacy user without credentials until trusted enrollment or an
isolated new database is arranged. Never auto-link by nickname, discard existing
records, or treat a schema migration as identity verification. No live migration,
application switch, or full GPU stack relocation/reconfiguration occurred in this
local/documentation slice; the dated server history below is preserved as history.

Production email is one background thread with at most 64 pending requests in a
nondurable process-local queue. Valid, non-throttled requests admitted to the queue
receive the same `202 accepted` regardless of account eligibility or later SMTP
failure; queue unavailability/fullness returns a generic 503. Pending work can be
lost on shutdown/crash, with no automatic durable retry. Monitor
`auth_mail_delivery_failed` and `auth_mail_pending_discarded_on_shutdown`, and allow
users to request a new link. Local inbox capture is development-only, never a
production fallback. The narrow SSH/model probe does not verify this authenticated
API-to-queue-to-worker-to-Qwen chain.

## Server findings

The current GPU host has Docker Compose installed, but the deployment account
cannot access the Docker daemon. User-owned PostgreSQL 18.6 and Redis 7.4 services
therefore run from `/mnt/local/babylm26_g2/projects/linguistic-oj` without changing
system packages. The system Redis remains 6.0 and must not be used by the Worker.
The user service manager has `Linger=no`, so host-reboot recovery still requires
administrator support or a different supervisor strategy.

## Server workspace

The canonical server workspace is
`/mnt/local/babylm26_g2/projects/linguistic-oj`. Project-owned deployment files,
service state, Qwen models, virtual environments, toolchain, caches, logs, and
backups live below this mode-`700` root. Legacy paths below
`/mnt/local/babylm26_g2` remain as compatibility links so existing environment
shebangs and verified commands continue to work. New operational commands must use
the canonical paths.

The shared `/home/babylm26_g2/.cache/huggingface` directory contains another
project's GPT-BERT resources. It is explicitly outside the Linguistic OJ workspace
and must not be moved, modified, or deleted by this project's operations.

The shared `babylm26_g2` account cannot provide security isolation between two
people using the same UID. Other experiments must use separate directories,
virtual environments, cache paths, ports, PID files, logs, and coordinated GPUs.
They must not use PostgreSQL port `5433`, the Linguistic OJ Redis socket, databases
`linguistic_oj` or `linguistic_oj_test`, or broad process-kill commands. Separate
Unix accounts remain the required solution for real access and audit isolation.

The current smoke snapshot is stored under
`app/releases/smoke-20260902-uncommitted`, with `app/current` as the deployment
pointer. `repo` and the old `/mnt/local/babylm26_g2/linguistic-oj-smoke` path are
compatibility links to that release. Runtime evidence is under
`artifacts/smoke-20260902`, while private challenge data is stored separately
under `private`. This uncommitted smoke snapshot must be replaced by a release
built from a reviewed Git commit before production use.

The non-production `app/releases/candidate-20260902-schema-v2` snapshot validates
the multi-challenge backend against schema v2 in the isolated
`linguistic_oj_test` database. Its server suite passed all 219 tests with real
PostgreSQL and Redis. It is not selected by `app/current`; the production database
and operational health check remain on schema v1 until a reviewed Git release is
ready for an atomic migration and application switch.

The later non-production `app/releases/candidate-20260902-v1-catalog` snapshot
retains that candidate as a baseline and adds the 18-language representative UPOS
catalog, its source datasets, private manifests, and executable draft contracts.
The catalog builder reran successfully against the deployed files, proving source
hash and immutable-artifact agreement. The full server suite passed all 223 tests
with `linguistic_oj_test` on schema versions 1 and 2 and Redis database 15, and
Ruff passed for source, tests, and scripts. Redis database 15 was empty afterward.
This candidate is also not selected by `app/current`; production remains on schema
version 1.

The hardened successor
`app/releases/candidate-20260903-v1-catalog-hardened` preserves both earlier
candidates and adds exact immutable-contract comparison, pre-write registry
collision checks, crash-released cross-platform catalog locking, atomic registry
replacement, and symbolic-link/NTFS alternate-stream path defenses. Its deployed
18-language source and private artifacts passed an idempotent catalog rebuild. All
239 server tests passed with real PostgreSQL and Redis, including Linux symbolic
link tests; scoped Ruff passed and Redis database 15 was empty afterward. It is not
selected by `app/current`, and the production database remains on schema version 1.

The isolated successor
`app/releases/candidate-20260903-v1-strict-upos-budget` rebuilds the standard
dataset with strict task eligibility, replaces invalid representative treebanks,
and contains 26 public descriptors with 22 executable draft contracts spanning
all five internal task types. Its fixed Qwen3.5-9B tokenizer audit passed for all
1,100 selected samples; the evidence report SHA-256 is
`82ef537f26a4b25a7f585070438d4cf17e29c5b541dc2ebce39205151da8cd0f`.
After the V1 API and student frontend freeze, the complete Linux suite passed all
277 tests with real PostgreSQL and Redis, scoped Ruff passed, and Redis database 15
was empty afterward. A subsequent real Qwen3.5-9B/vLLM run completed all five task
families through API submission, PostgreSQL outbox/state, Redis Streams, the
attested Worker, deterministic scoring, persistence, and owner result retrieval.
The machine-checkable observation is
`benchmarks/observations/qwen3.5-9b-v1-five-task-gpu-smoke.json`, with internal
report SHA-256
`6555f3d656c25e7888de0ca9c96909128faae78aaa2ef740b81c7d03977d27c8`.
Dependency calibration failed closed at 300 seconds, completed with only about 53
seconds of headroom at 600 seconds, and passed against the final 900-second
contract. The temporary API and vLLM were stopped, GPU 0 was released, smoke
credentials/helpers were removed, and dedicated Redis database 15 was flushed.
This execution gate does not activate the draft contracts or establish a model
quality threshold. The post-smoke candidate suite passed all 278 tests with real
PostgreSQL and Redis, full scoped Ruff and JavaScript syntax checks passed, and
Redis database 15 remained empty. The candidate is not selected by `app/current`;
production remains on schema version 1.

The separate source-provenance gate now pins all 22 treebanks referenced by the 26
public catalog descriptors to official Universal Dependencies 2.18 repositories
and release commits. Canonical LF hashes for all 22 source files match the upstream
`r2.18` bytes. LICENSE and README URLs and content hashes are recorded in
`config/v1_source_provenance.json`, whose internal report SHA-256 is
`7fd8e9b56661c6ab2fc15e2667291d4097f4f5d2fed932817b55afaa7ac5dd76`.
This establishes source identity, not permission to activate. Every rights decision
remains blocked, and Chinese Beginner, German HDT, English GUM, Italian
KIParlaForest, and Portuguese CINTIL are explicit holds pending deployment-policy
review, written clearance, or replacement. See `docs/SOURCE_PROVENANCE.md`.
With the provenance consistency test included, the complete candidate suite passes
all 279 tests with real PostgreSQL and Redis.

Project-owned vLLM and FlashInfer caches are canonical under `cache`; their home
paths are compatibility links. The unrelated shared home Hugging Face cache
remains untouched. `uv cache prune` currently reports no unused entries, so uv
cache internals must not be deleted manually. Both Qwen3.5-4B and Qwen3.5-9B are
retained. Rebuildable toolchain trees have been removed only where verified
source archives and installed binaries remain.

## Server service controls

Run these commands from the canonical workspace as `babylm26_g2`:

```sh
operations/bin/postgresql-service status
operations/bin/postgresql-service start
operations/bin/postgresql-service stop
operations/bin/postgresql-service restart

operations/bin/redis-service status
operations/bin/redis-service start
operations/bin/redis-service stop
operations/bin/redis-service restart

operations/bin/health-check
operations/bin/backup-services
operations/bin/verify-backup-restore backups/online/<UTC timestamp>
```

Redis reads `operations/config/redis.conf`; PostgreSQL uses its canonical data,
socket, and log paths on port `5433`. Online backups are written to
`backups/online/<UTC timestamp>` with a manifest and SHA-256 checksums. Set
`LINGUISTIC_OJ_BACKUP_COPY_TO` to copy a completed backup to an rsync-compatible
external destination. A local backup on the same disk is not disaster recovery.
The restore verifier uses temporary PostgreSQL databases and a separate private
Redis socket, checks manifest counts when present, and cleans up its temporary
state without altering the live services.

Log rotation is defined by `operations/config/logrotate.conf`. It still needs an
administrator-approved scheduler. Service startup after a host reboot likewise
remains unsolved while this account has `Linger=no`.

## 2026-09-07 Isolated Cookie/Admin/Qwen Acceptance

This dated addendum leaves all preceding server paths, candidate histories,
service commands, test totals, and past cleanup records intact. For the **current
checkout**, schema is now v4 and the isolated cookie/admin/Qwen backend chain has
passed. This supersedes the earlier current-code v3/single-probe-only notes, not
the historical state of any earlier deployment. The immediate phase remains
local student/admin development, not formal public launch or a new-model rollout.

### Snapshot and Environment

The actual server source snapshot was:

```text
/mnt/local/babylm26_g2/projects/linguistic-oj/artifacts/acceptance-20260907-admin-cookie-v1/snapshot
```

The supplied source-bundle SHA-256 was:

```text
ecb2c25d13b4805169c8bbc829a36aa9ebbbd4b757c117c59eca08411a2d4fea
```

This was an allowlisted **working-tree snapshot, not a reviewed Git release** and
not selected by `app/current`. Dependencies were installed only into its isolated
sibling `deps/`. No dependency was installed into the vLLM environment; existing
services, model files, old launch configuration, and the application pointer were
not changed. These snapshot/socket/install/service facts are supplied operator
run context. The JSON report below independently records the application checks,
aggregate result, isolation identifiers, and cleanup, not a signed deployment or
bundle-to-process attestation. Later code changes require their own verification.

`scripts/build_acceptance_bundle.py` packages only selected Python/web source,
the acceptance runner, README, dependency declaration, and two fixed contract
configuration files. It adds per-file SHA-256 hashes and explicit
`production_release: false` / `working-tree-snapshot-not-a-reviewed-commit` markers.
The new archive stays below local ignored `runtime/` in an existing parent;
runtime contents, `.env`, `.venv`, datasets, private manifests, and unrelated
configuration are not bundled. Exclusion is not permission to delete those files.
This source export and the runner were used for the actual acceptance, not merely
added as unexecuted recipes. Dependency/runtime follow-up remains separately owned;
the acceptance install is not proof of clean production packaging.

### Report and Actual Coverage

The actual local report copy is
`runtime/qwen-admin-acceptance-20260907.json`. It was read for this addendum and
records `passed: true`, `stage: complete`, from
`2026-09-07T12:52:25.465438+00:00` to
`2026-09-07T12:52:36.626038+00:00`.

| Area | Actual result and scope |
| --- | --- |
| HTTP/auth | In-process TestClient requests exercised the real application, registration/verification, used-token rejection, cookie login, expected-user/CSRF/Origin and rights gates, owner isolation/history/result/ranking, and per-account idempotency. |
| Logout | Session revocation was checked by replaying the old cookie, not just observing a cleared client cookie jar. |
| Persistence/queue/model | Real PostgreSQL outbox/state, real Redis delivery and acknowledgement, existing Qwen worker attestation, generation, deterministic scoring, persistence, and owner retrieval passed. |
| Administration | Real PostgreSQL save/publish visibility, pause rejecting new admissions while old replay/results survive, resume under the existing test policy/runtime, and demotion rejecting the same cookie's admin access passed. |
| Model budget/result | Exactly 2 generation calls, 1 evaluated submission, 2 handwritten samples, 6 gold items, 2 valid/0 invalid samples, score 1.0. A second submission was guard-only and not evaluated. No provider request remained active at completion. |
| Explicit limitations | `transport: HTTP-inprocess-TestClient`, `browser_verified: false`, `benchmark: false`, `production_scores_written: false`, memory-captured email, draft override, and `external_activation_ready: false`. |

The 1.0 score is a **synthetic integration assertion, not a benchmark or evidence
of model quality on the real dataset**. The run is stronger than the earlier
single-generation model connection probe, but not a browser, network ingress,
SMTP, HTTPS, or supervised production deployment test. Historical five-task GPU
benchmarks were not rerun or replaced.

Attestation used the operator's existing older launch-evidence file, verified
local tokenizer/config/chat-template hashes, and live `/v1/models` alias, resolved
snapshot root, and context metadata. The report names the scope as
`operator-evidence+local-tokenizer-hashes+live-model-metadata`. This is **not
cryptographic proof of the weight bytes loaded by the running vLLM process**.
No service restart or new launch-evidence file is claimed.

### Storage Isolation and Cleanup

The run used the existing colocated PostgreSQL Unix socket/port **5433**, creating
only a new random schema
`qwen_accept_u2035_e2ed08213e5e4b3e800405b2bc7ea7c8`. The real migration definitions
created v1-v4 state there: five original core/migration tables, four auth tables,
and two admin tables, **11 tables total**. Existing application schemas, historical
accounts, and production bindings were not migrated or removed. Short existing
database-wide advisory locks still apply; schema separation is not total workload
or same-UID security isolation.

Redis used database **15** and the unique namespace
`qwen-acceptance:eb94379385684bf99bea9403b88bc7ac`. Four exact names for stream,
active, receipts, and owner marker were reserved, as recorded in the protected
report. The database did not need to be empty. Cleanup verified ownership and
absence across those names, deleting **three remaining keys**; an already absent
transient empty key explains why four reserved names need not yield four deletions.
There was **no FLUSHDB/FLUSHALL, SCAN/KEYS discovery, or prefix-based deletion** in
this run. Older historical flush records above refer to their own earlier runs,
not the cleanup policy for this acceptance.

The report confirms all of the following:

- PostgreSQL: 11 tables and 1 schema dropped; `confirmed: true`.
- Redis: 3 keys deleted and all 4 exact names absent; `confirmed: true`.
- Temporary handwritten fixture directory removed; `confirmed: true`.

`check_qwen_pipeline.py` rechecks the schema OID, owner, and random creation marker
before dropping its tables together with **RESTRICT**, then the empty schema with
**RESTRICT**, never CASCADE. External dependencies therefore cause failure rather
than cascading into other resources. Redis cleanup compares the exact owner
marker before deleting the four reserved names. It never derives deletion targets
from a general prefix search.

The Linux runner requires absolute non-symlink source/input paths and a **new**
report outside both Git and the snapshot in an existing owner-only directory.
The report is created with mode **0600**; its temporary fixture child is private
(**0700**). It contains aggregates/hashes, not passwords, cookies, verification
links, prompts, or raw model output. Connection secrets, if needed, come from a
protected file, not command arguments. Use the runner's `--help` and actual
operator-selected paths; do not aim it at guessed database/service resources.

Normal errors and SIGINT/SIGTERM attempt independent cleanup and fail acceptance
if any cleanup is unconfirmed. SIGKILL, host/process loss, changed ownership, or
lost connections cannot guarantee cleanup. Keep the exact protected report for
inspection in that case; do not rerun guessed cleanup or broad deletion. A timed-out
model request may still finish remotely; the runner never stops vLLM. Existing
old acceptance directories and unrelated project/home caches were retained.

### Current Admin and Remaining Gates

Current schema v4 appends `challenge_admin_state` and `challenge_admin_revisions`
to v3 auth/roles. Production migration is still explicit and requires separate
approval/rehearsal. **Legacy users without credential bindings still block
production auth startup/readiness**; this fresh-schema run did not resolve trusted
enrollment and must not be used to justify nickname auto-linking or deletion.

Administration only manages already loaded IDs and plain-text teaching/admission
state. Mutations require cookie auth, exact Origin/JSON, `X-LOJ-CSRF: 1`, and
`X-LOJ-Expected-User`. Current database user/role/session are checked under the auth
transaction lock, with fresh database time and reauthorization after the task lock
so expiry while waiting denies the operation. Revision CAS and audit writes are
atomic. New admission fencing follows idempotency replay/conflict detection and
retains accepted queue work/history/results. Resume uses only existing policy,
source, and runtime conditions. Content publication cannot change rights, files,
datasets, models, scorers, or physically activate a service. See
[Teaching Administration](ADMINISTRATION.md).

The full opt-in PostgreSQL admin migration/concurrency parameters are still a
separate gate (`LOJ_ADMIN_POSTGRES_TEST_URL`); local skips and the basic real-PG run
do not prove every real concurrency/expiry/migration case passed. The main agent
will record fresh final local/student/admin browser regression results after the
latest changes; intermediate and older test totals are not reused as final totals.

True SMTP delivery, HTTPS/proxy and browser-to-Qwen acceptance, legacy enrollment,
rights approval/per-contract activation evidence, a reviewed Git release,
supervision, monitoring, and off-host restore remain uncompleted public-release
gates. The next phase is local teaching-management correctness and usability,
without a new model, removal of workspace files, commit/push, or public launch.
