# Local Development

## Location and Scope

The project owner's main checkout is `D:\MyWebsite\Online Linguistic Judge`.
The remote repository is
`https://github.com/Linguistic-Online-Judge/Linguistic-AI-Infra-Dataset`.
Other collaborators can use their own checkout paths.

The current priority is to make business flows, frontend quality, and backend
behavior complete locally. Public release is a later stage. Only the existing
school-server Qwen3.5-9B is in scope; do not add multiple models without a new
explicit request. Both developers already have school-server SSH/VPN access.

## Current Local Phase

The required V1 scope is all 18 languages. For the real Qwen9B developer station,
use `scripts/start_qwen_dev.cmd` and `http://127.0.0.1:8090` while the approved SSH
connection remains open. See [18-language Qwen development](QWEN_DEVELOPMENT.md).
The five tiny Mock tasks described below remain the fast, separate 8080 test mode.

The five-step student flow now extends to teaching administration of already
loaded tasks. Save/check/publish teaching and pause/resume new admissions are
implemented locally; neither operation deploys code or activates a draft evaluation
contract. See [Teaching Administration](ADMINISTRATION.md) for the complete boundary.

| Step | Current implementation and boundary |
| --- | --- |
| 1. Local startup | `local_dev.py` and `scripts/start_local_dev.ps1` run the real FastAPI app, packaged frontend, SQLite, in-memory queues, and Mock worker on loopback. |
| 2. Accounts | Email registration/verification, login, logout, password reset, persistent credentials/sessions, local mail capture, and separate user/admin seed identities are present. |
| 3. Browser business flows | Five task lessons, task-specific handwritten examples, explicit editable template loading, asynchronous results, owner history/rankings, cross-tab account protection, and role-gated `#admin` are present. |
| 4. Verification tools | Python tests, Node contracts, synthetic browser fixtures, and isolated real-local-app student/admin Edge runners exist. Current code has changed since earlier counts; the final regression must be recorded from its actual output. |
| 5. School Qwen chain | The earlier two-sample TestClient check is now supplemented by 2026-09-10 real Edge acceptance: all 18 language UPOS tasks, 50 samples each, real Qwen9B results/history/rankings. This is developer acceptance, not independent model-quality, SMTP or production HTTPS certification. |

The Mock browser suite and real school-model browser acceptance are separately
recorded; neither constitutes a production deployment. See
[Authentication](AUTHENTICATION.md) and [Workspace Audit](WORKSPACE_AUDIT.md) for
security and preservation details.

## Start the Local Website

Use Python 3.11+ and the project `.venv`. Run commands from
`D:\MyWebsite\Online Linguistic Judge`. Create the environment only if it does not
already exist; do not replace a working environment:

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e '.[api,dev]'
```

Start the site from the project root:

```powershell
& .\scripts\start_local_dev.ps1
```

On Windows, you can instead double-click `scripts/start_local_dev.cmd`. It runs
the same Python entry point in a visible console and keeps startup errors on
screen. Keep that window open while using the website. This is not a Windows
service, a startup task, or a promise of continuous availability after reboot.
If the browser says `ERR_CONNECTION_REFUSED`, first check that this process is
running and that the complete URL includes `:8080`; it is not a password error.

GitHub hosts source code; pushing to `main` does not start this server. A separate
test branch is optional, not a prerequisite for running or sharing the project.
GitHub Pages can host a static preview but cannot run this FastAPI/database/worker
application. A continuously available complete site requires a running server.

Open **http://127.0.0.1:8080**. Keep the terminal/process running. Stop your own
launcher with `Ctrl+C` when finished. This is the canonical daily-development
address: `localhost` sends a different Host header and is **not accepted** by this
launcher, even though it may resolve to loopback.

If PowerShell execution policy blocks the script, run the module directly at the
same fixed default port, without changing policy:

```powershell
./.venv/Scripts/python.exe -m linguistic_oj.local_dev --root .
```

Do not use an execution-policy bypass or change global policy. If port 8080 is
occupied, startup fails without choosing another port or stopping another process.
Identify the owner of the listener and coordinate a normal stop if appropriate;
do not use broad process-kill commands. The CLI also holds an exclusive OS lock
on its state directory, allowing only one launcher process per state directory.

No PostgreSQL server, Redis server, GPU, SMTP service, public domain, or Node build
is required. Node 22+ is needed only for JavaScript checks/browser automation, and
installed Edge only for browser automation, not to run the app server. FastAPI
serves `src/linguistic_oj/web` at `/` and `/assets/*`; there is no separate npm or
Next.js app to start. Do not open the HTML file directly or serve the repository
as static content, which would omit the backend and risk exposing private data.

## Local Accounts and Mail

These accounts are seeded only when absent in the isolated local database:

| Email | Public handle | Role |
| --- | --- | --- |
| `alice@example.test` | `LocalAlice` | `user` |
| `bob@example.test` | `LocalBob` | `user` |
| `admin@example.test` | `LocalAdmin` | `admin` |

Their common **initial** password is `Local-only-passphrase-2026!`. These are
public local-testing credentials, never production credentials. The launcher
does not overwrite an existing account or a password changed through reset.
The development panel still displays/fills the initial password, so after a
reset use the password you actually chose, including after restarting the app.
An admin role does not grant access to another user's private submission results.
Role assignment is a trusted server operation, not a public registration field or
an admin-page feature.

To exercise account and submission flows:

1. Sign in as Alice, select a task, edit a template, and submit. Follow the queued
   or running state to an aggregate result, then inspect history and rankings.
2. Sign out and sign in as Bob to check separate private history. For simultaneous
   independent identities use separate browser profiles; tabs in one profile share
   the same cookie, and account changes trigger cross-tab invalidation.
3. Register a new `.test` email. Close the account dialog, expand the local testing
   panel, and refresh its inbox. Open the verification link, choose a password and
   public handle, then explicitly log in; opening a link alone creates no account.
4. Request a password reset, refresh the local inbox, open the reset link, and
   choose a new password. Old sessions/passwords stop working; log in explicitly
   with the new password. No external mail is sent.

The local inbox is a process-memory deque of at most **100 messages**, newest
first. All local testers can see it; this is not a private or production mailbox.
Restart clears captured inbox entries/links, **not passwords or accounts**.
Unexpired action-token hashes remain in the database, so an already-held link may
still work until used, superseded, or expired. Request a new link if the inbox was
cleared. Local mail routes require `X-LOJ-Development: 1` and reject cross-site
discovery, but these controls do not make this development service safe to expose.

## Use Teaching Administration

1. Log in as `admin@example.test`, using the initial password above only if it has
   not been reset. Open the teaching-management navigation item or
   **http://127.0.0.1:8080/#admin**. Alice and Bob are ordinary users; an admin URL
   or a forged role header does not elevate them.
2. Select an already loaded task and read its language/treebank, source/rights,
   fixed model, and scoring metadata. This page cannot create tasks or choose files,
   datasets, model paths, scorers, or rights decisions.
3. Edit the five plain-text teaching fields, preview, save the draft, check the
   saved revision, then explicitly publish. Draft and published content are separate;
   publication does not overwrite the student's prompt or open evaluation.
4. Refresh the student's published teaching. Click a zero/few-shot template button
   deliberately; it copies only that template, not the surrounding instructions.
   Replacing a nonempty prompt asks for confirmation. A failed teaching read keeps
   template buttons unavailable until retried successfully.
5. Pause new submissions. Existing queue work, idempotent replay, owner history,
   and results remain intact. Resume only if the existing contract/policy/runtime
   allows it. A revision conflict or uncertain save requires explicit reload and
   reconciliation, never an automatic overwrite.

Admin mutations use the cookie's current database role, exact Origin/JSON,
`X-LOJ-CSRF: 1`, and `X-LOJ-Expected-User`. The store rechecks authorization and
fresh database time after waiting for the task lock, rejecting expired sessions
even if the request originally passed middleware. Saved mutations and revision
audits commit together. See [Teaching Administration](ADMINISTRATION.md) for limits,
source-fingerprint changes, transaction ordering, and error recovery.

## Fixtures, Limits, and Retention

Default state is in ignored `runtime/local-development/`, including
`local-environment.json`, `handwritten-fixtures-v1.jsonl`,
`accounts-and-runs.sqlite3`, and `.server.lock`. The database retains accounts,
password hashes, sessions, rate-limit state, submissions, results, teaching
drafts/publications, admission switches, and admin revision audits across restarts;
sessions still expire or can be revoked. Unsaved student/admin browser drafts are
page-memory state, not durable server records.

The current SQLite/PostgreSQL schema is **v4**. Version 3 introduced authentication
and roles; version 4 appends `challenge_admin_state` and
`challenge_admin_revisions`. The SQLite store applies supported missing migrations
when constructed, preserving old account/session/submission/result rows. It does
not reset accounts or link legacy users by nickname. PostgreSQL migration remains
explicit and is not part of local startup; never point the local launcher at a
production database.

There are **five handwritten local challenges with two samples each**: English
UPOS, XPOS, dependency, and Chinese segmentation/transliteration. These reuse a
tiny handwritten fixture, not ten distinct benchmark sentences. They do not load
the real 18-language dataset or call Qwen. The Mock identity and leaderboard
partitions are separate; mock scores test plumbing, not linguistic model quality.

The lessons now select conventions by language and treebank, not just task name:
English `LocalPractice` XPOS uses Penn Treebank tags (`NNS`, `VBP`, `.`), whereas
German HDT uses STTS (`NN`, `VVFIN`, `$.`). Chinese `LocalPractice` transliteration
uses lowercase toneless pinyin, no spaces within a word, and unchanged punctuation;
GSDSimp uses tone marks and its defined punctuation conversion. These are distinct
teaching defaults, not changes to a dataset or scorer. UPOS and XPOS are both
part-of-speech tasks, but their label sets are not interchangeable.

`local_dev.py` sets a technical testing budget of **1000 submissions per user per
challenge per 24 hours**, while retaining other queue, concurrency, prompt, and
deadline limits. This is a time-window budget, not a cumulative lifetime cap or
an unlimited allowance. The existing real evaluation contract's **5 per user per
challenge per 24 hours** is unchanged. Authentication throttles are separate and
remain persistent across restarts.

On restart, the in-memory queues are new. The database outbox restores matching
queued deliveries; work beyond its deadline fails. A job interrupted while
`running` is not instantly requeued or declared successful: after its stored
lease expires, worker sweeps mark it failed with `WORKER_CRASH` or `JOB_DEADLINE`.
This is a single-process development recovery path, not production durability.

The launcher refuses a nonempty unmarked state directory, a state path outside a
dedicated child of `runtime/`, or conflicting fixture/version bytes. It never
cleans old files or acceptance directories. If a genuinely separate fixture state
is needed, select a new isolated directory with `-StateDirectory` (script) or
`--state-dir` (module), leaving the old one intact. Do not point the launcher at
Qwen databases or delete state merely to reset passwords or bypass limits.

## Verification Commands

Earlier student-flow acceptance on 2026-09-07: 412 Python tests passed and 18 environment-dependent
tests skipped; Ruff passed; five Node contract tests passed; 30 real-browser
groups passed against a fresh local FastAPI instance. Desktop and 320/390/768px
views were checked. The skipped cases do not provide live PostgreSQL, Redis, or
Windows symlink-permission evidence. These are historical results, not the final
admin-phase totals. Later supplied intermediate results also precede the latest
lock-expiry/browser changes; record the final main regression from its actual
output, without adding expected new cases to an old total.

```powershell
./.venv/Scripts/python.exe -m pytest
./.venv/Scripts/ruff.exe check src tests scripts
node --test tests/browser/contracts.test.mjs
node tests/browser/run.mjs --fixture
./.venv/Scripts/python.exe scripts/check_local_browser.py
./.venv/Scripts/python.exe scripts/check_admin_browser.py
```

The Node contract checks exercise handwritten lesson contracts and frontend
invariants without a real backend. `--fixture` opens real Edge against a synthetic
test server; it is not backend verification. `check_local_browser.py` instead
starts a fresh isolated **real local app** at a temporary loopback test port and
launches installed Edge through Node 22+; it needs no daily server already running
and does not use `runtime/local-development`. Temporary test ports do not replace
the canonical daily address above. If Edge is installed elsewhere, `LOJ_BROWSER`
can specify its executable. No browser download or npm install is performed.

The browser suite creates accounts, resets a test password, writes mock submissions,
and tests shared-cookie tabs, owner isolation, idempotent retries, errors, keyboard
focus, and desktop/mobile widths. Fault cases use explicit browser interception;
they are not evidence of real infrastructure outages. Do not aim the low-level
`run.mjs --url` runner at daily/shared records. The wrapper stops only the temporary
app it spawned; the driver closes its test browser/profile. Logs, screenshots,
and isolated state remain under ignored `runtime/browser-tests/`; old runs are
not bulk-cleaned.

`check_admin_browser.py` starts its own fresh SQLite/Mock app, then runs the
student and admin Edge drivers against that same isolated test instance. It avoids
daily port 8080/state and retains its own test artifacts. The admin cases cover
role access, published-versus-draft content, plain-text rendering, explicit template
loading, conflicts, admission controls, and stale-account/role responses. Do not
turn an earlier student count or intermediate admin count into a current pass.

PostgreSQL/Redis tests are opt-in via `POSTGRES_TEST_DATABASE_URL` and
`REDIS_TEST_URL`; the admin store's PostgreSQL parameter cases separately require
`LOJ_ADMIN_POSTGRES_TEST_URL`, an explicit loopback test database URL. Use dedicated
test resources only, never production. The admin pytest fixture is separate from
the remote runner's ownership-proved RESTRICT cleanup; do not substitute its test
schema cleanup for the acceptance runner. A skipped parameter is not real-database
evidence, and the remote basic acceptance below is not the full concurrency suite.

## Step 5: Existing Qwen and Isolated Acceptance

The connection check confirmed SSH alias `75` to `antlnp75`,
discovery of `Qwen/Qwen3.5-9B` in `/v1/models`, and one handwritten token sequence
`["Cats", "sleep", "."]` returning `["NOUN", "VERB", "PUNCT"]`.
To repeat only that narrow probe deliberately:

```powershell
./.venv/Scripts/python.exe scripts/check_qwen_connection.py --ssh-host 75
```

The script uses existing SSH access and the remote loopback model service at port
8000. It reads the model list and makes **one synthetic generation request**;
it is not just a passive health check. It changes no remote files, configuration,
services, or platform scores. Alias discovery is not artifact/tokenizer attestation.
This narrow probe does not exercise the application API, queue, worker, or account
chain. It remains distinct from the later real-backend acceptance below.

The actual `runtime/qwen-admin-acceptance-20260907.json` records a successful
isolated school-server run on 2026-09-07, ending at
`12:52:36.626038+00:00`: `passed: true`, `stage: complete`. It exercised registration,
verification-token replay rejection, login cookies, expected-user/CSRF/Origin and
rights gates, owner-only results/history/ranking, per-account idempotency, logout
and old-cookie rejection, and real PostgreSQL admin publish/pause/replay/resume/
demotion. PostgreSQL outbox and Redis delivery fed the existing Qwen worker.

There were **two generation calls**, **one evaluated submission**, **two handwritten
samples**, and **six gold items**, with score **1.0**. A second submission was
created only for cross-account guards and not evaluated. These are synthetic
integration checks, not a benchmark or new production scores. The transport is
`HTTP-inprocess-TestClient`, `browser_verified: false`, and mail is
`development-memory-capture`; this is not a deployed network API or browser-to-Qwen,
real-email, or HTTPS result.

The operator run used the existing PostgreSQL Unix socket/port 5433 with one new
random schema, and Redis database 15 with four exact unique namespaced keys. The
report confirms cleanup of the schema's 11 tables, the schema, three remaining
Redis keys, and the temporary fixture. Four reserved key names and three actual
deletions are compatible: an empty transient key need not remain after processing.
No Redis flush was used. The full opt-in PostgreSQL admin concurrency/migration
parameter suite remains a separate check.

`scripts/build_acceptance_bundle.py` packages allowlisted source only, not runtime,
`.env`, `.venv`, or datasets. `scripts/check_qwen_pipeline.py` is an opt-in Linux
server runner, not a daily local-start command. It requires a new protected report
outside Git and outside the source snapshot, a private existing parent, read-only
tokenizer/launch inputs, and explicitly selected colocated storage. It performs
bounded model work and ownership-proved cleanup rather than service management.
Snapshot identity, installed sibling dependencies, and exact evidence limits are
recorded in the 2026-09-07 addendum to [Operations](OPERATIONS.md).

Historical benchmarks were not rerun; the existing full GPU Qwen stack, service
configuration, and `app/current` pointer were not relocated or reconfigured.

## Showing the Site to the Collaborator

- Sending `localhost` or `127.0.0.1` does not share a site: those addresses refer
  to the recipient's own computer.
- The preferred local-development collaboration path is to share reviewed source
  through Git and let each developer run the same documented launcher. A GitHub
  repository URL is a source-code location, not a running website.
- Current untracked frontend files are not automatically available in the remote
  repository. Do not promise identical checkouts until intended changes have
  actually been published through the team's review process.
- Screen sharing is sufficient for an immediate visual review without network
  exposure.
- The current launcher is loopback-only. A future LAN preview needs a separate,
  explicit security design, not just a bind-address change. `0.0.0.0` is not a
  browser URL; never expose the seed accounts or captured mail to LAN/public users.
- When the developers need a single shared running instance, deploy a separate
  school-server test environment and use their approved SSH/VPN access. That is
  not required to improve the local application first.

## Next Business and Design Work

Complete the current local student/admin regression, focusing on responsive and
keyboard use, revision/uncertain-response recovery, account demotion/expiry, and
task-specific lesson accuracy. Run the full opt-in database concurrency and
migration cases separately; the real-backend basic acceptance does not replace them.
This phase does not add arbitrary-file administration, a model, or a public launch.

Production still needs reviewed legacy enrollment/migration, real browser-to-Qwen
and protected HTTPS/SMTP acceptance, supervised services, monitoring, backups,
source-rights approval, and a reviewed release. See [Operations](OPERATIONS.md).
Preserve the HTML/CSS/JavaScript stack and existing school-server model setup unless
a concrete request changes scope.
