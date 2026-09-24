# Workspace Audit

## Scope and Evidence

This documentation-only audit was made on 2026-09-07 in the canonical checkout
`D:\MyWebsite\Online Linguistic Judge`, not the former temporary worktree or the
separate `D:\University\AI\Linguistic Online Judge` workspace. The configured
`origin` fetch/push URL is
`https://github.com/Linguistic-Online-Judge/Linguistic-AI-Infra-Dataset.git`.
The project path, repository origin, and existing user decisions in `AGENTS.md`
are preserved.

Evidence for this pass is directory inspection, `.gitignore`, `git status`,
`git remote -v`, targeted existing diffs, and reads of actual auth, local launcher,
Qwen composition, migration, browser, and probe code. This is a preservation and
documentation audit, not an exhaustive secret scan, new storage measurement,
cleanup, runtime test run, or release review.

The starting worktree was already dirty: datasets, descriptors/contracts,
benchmark observations, backend/frontend, tests, scripts, and documentation had
modified or untracked files. Untracked files are still user work, not permission
to remove them; they also are not automatically published on GitHub.

## Documentation Ownership

Only these documentation targets are owned by this update:

| Target | Change boundary |
| --- | --- |
| `AGENTS.md` | Current implementation/verification facts, retaining origin, canonical path, and user decisions. |
| `docs/LOCAL_DEVELOPMENT.md` | Replace absent-feature claims with actual launcher, five-step local scope, credentials, retention, and checks. |
| `docs/AUTHENTICATION.md` | New account/security, production configuration/mail, and schema v3 reference. |
| `docs/WORKSPACE_AUDIT.md` | New preservation audit and evidence boundaries. |
| `README.md` | Local quickstart section only; preserve unrelated existing edits. |
| `docs/OPERATIONS.md` | Current auth startup/readiness and obsolete callback instructions only; retain dated server history. |
| `docs/ROADMAP.md` | Local scope/current-phase notes, including current schema v3; retain historical evaluation and release evidence. |

No application code, scripts, tests, datasets, benchmarks, dependency declarations,
ignore rules, or OpenCode configuration were edited by this documentation pass.
Existing historical test totals are not rewritten as current results. Main-suite,
real-browser, PostgreSQL/Redis, SMTP, and full new-auth Qwen acceptance results must
come from actual corresponding runs, not inferred from scripts or copied history.

## Retained Files

| Files/directories | Finding and action |
| --- | --- |
| `.venv/` | Existing ignored Python environment. Retained; no rebuild or dependency installation in this pass. |
| `build/` | Ignored generated package output, including `lib/` and `bdist.win-amd64/`. Retained, even if copies lag behind source. |
| `src/linguistic_online_judge.egg-info/` | Ignored package/editable-install metadata. Retained; not a second authoritative source tree. |
| `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`, `*.py[cod]` | Ignored generated caches. Retained; age/rebuildability alone does not require deletion. |
| `runtime/` | Ignored private/runtime area, including prior acceptance/browser runs, logs, locks, and private manifests. All pre-existing files retained. |
| `runtime/local-development/` | Default future/daily launcher state: local marker, fixture, SQLite accounts/runs, and process lock. Created by the launcher when needed, not by this docs update. Never bulk-reset it to recover a password. |
| `runtime/browser-tests/` | Isolated test app states, logs, screenshots, and any leftover profiles. Existing runs retained; the runner's own temporary profile lifecycle is not permission to clean older runs. |
| `opencode_error.png`, `opencode_file` | Existing untracked user artifacts. Retained without editing or classifying them as junk. |
| `opencode.json` | Existing ignored tool configuration. Retained without editing; application work does not require OpenCode reconfiguration. |
| `Standard_Dataset/`, `Target_Conllus/`, `challenges/`, `config/`, `benchmarks/`, `prompts/` | Source data, evaluation identities, references, and evidence, including dirty/untracked work. Preserved without rebuilding, moving, or rescoring. |

The supplied audit estimate for small rebuildable build/metadata/cache candidates
was approximately **2.2 MiB**, not the total repository or `.venv` size. This
docs-only pass did not remeasure that estimate. Unnecessary cleanup was **not
performed**; the small reported saving does not justify disturbing a working
environment. Ignored means excluded from normal Git tracking, not disposable.

## Local and Remote Boundaries

The daily launcher command is `& .\scripts\start_local_dev.ps1` from the project
root. Its canonical browser URL is **http://127.0.0.1:8080**; `localhost` sends a
different Host and is rejected. It binds loopback, locks one state directory for
one process, and fails on fixed-port conflicts without killing another process.
Old state files and ignored acceptance runs are never automatically bulk-cleaned.
See [Local Development](LOCAL_DEVELOPMENT.md) for the Python fallback and recovery
limitations; interrupted running work waits for lease expiry, not instant success.

`scripts/check_local_browser.py` owns a fresh temporary local app process/state and
launches real installed Edge through Node 22+. Its temporary test port is not a
new canonical daily address. It tests the real local Mock backend, while
`node --test tests/browser/contracts.test.mjs` and the `--fixture` browser mode
exercise synthetic frontend contracts. The wrapper was inspected, not run here.

Step 5's supplied observation records successful SSH alias `75` access to
`antlnp75`, `/v1/models` discovery of `Qwen/Qwen3.5-9B`, and one handwritten
`["Cats", "sleep", "."]` request returning `["NOUN", "VERB", "PUNCT"]`.
`scripts/check_qwen_connection.py` can repeat that alias check and one synthetic
generation; it makes no remote file/configuration/service changes and writes no
platform scores. It was not rerun in this docs-only pass. This evidence is not the
application API/queue/worker/new-auth/full real-model chain, not runtime artifact
attestation, and not a rerun of historical benchmarks.

The full school-server GPU Qwen stack was not relocated or reconfigured for this
slice. Current schema v3 code does not establish that a live database migrated;
production legacy users remain unbound until trusted enrollment, without nickname
auto-linking. No server deployment or application-pointer switch is claimed.

## Actions Not Taken

- No force deletion, broad process kill, `git clean`, `git reset`, restore, or
  rollback of another contributor's work.
- No deletion of old files, ignored environments/builds/caches, runtime state, or
  user artifacts; no changes to OpenCode configuration or ignore rules.
- No dependency reinstall, dataset rebuild, benchmark rerun, new model score
  write, remote reconfiguration, or live schema migration.
- No staging, commit, push, or deployment. Publishing source and exposing a
  running service remain separate, explicitly authorized actions.

## 2026-09-07 Follow-Up: Administration and Actual Remote Acceptance

### Review Scope and Preservation

This follow-up appends to, rather than replaces, the earlier audit above. Its
earlier v3 and single-generation-only findings describe the earlier inspection;
the current code and actual remote report now support the v4/admin/backend-chain
facts below. The target remains `D:\MyWebsite\Online Linguistic Judge`, with the
same verified `origin` URL. The separate University workspace was not edited.

Evidence inspected: current dirty/untracked application and test files, both
submission stores, v4 migrations, auth/admin routes and transactions, packaged
student/admin/teaching JavaScript, local launcher/browser wrappers, acceptance
bundle and remote runner, `.gitignore`, directory listings, `git status --short`,
`git remote -v`, current documentation, and the complete actual
`runtime/qwen-admin-acceptance-20260907.json`. Source was read from `src/`, not stale
`build/lib` copies. This review did not itself rerun the remote acceptance, install
dependencies, measure storage, or perform an exhaustive security/release audit.

The pre-existing worktree included modified tracked datasets/descriptors/backend/
tests/docs and many untracked source, script, configuration, benchmark, and frontend
files. All were preserved. Current phase: **local development of student and
loaded-task teaching administration**, not formal public launch or a new-model
project. Existing school-server access remains established; no access reconfirmation
or public DNS task is needed to continue this phase.

| Documentation target owned in this follow-up | Change boundary |
| --- | --- |
| `docs/ADMINISTRATION.md` | New end-to-end admin guide, exact permission limits, transaction/revision semantics, and verification boundaries. |
| `docs/WORKSPACE_AUDIT.md` | This appended review only; the entire preceding audit remains. |
| `docs/ROADMAP.md` | Current local admin phase, v4, actual isolated chain, and next verification work rather than immediate public rollout. |
| `docs/LOCAL_DEVELOPMENT.md` | Admin walkthrough, persistent v4 state, corrected treebank-specific lessons, test commands, and remote evidence scope. |
| `docs/API_CONTRACT.md` | Current cookie/expected-user/CSRF contract, explicit legacy Bearer distinction, admin routes, and new public read fields. |
| `docs/AUTHENTICATION.md` | Current database role/session authorization, post-task-lock expiry recheck, v4, and production binding limits. |
| `README.md` | Minimal current summary, local admin entry, and next-stage/evidence corrections; retain unrelated data and benchmark history. |
| `AGENTS.md` | Persistent v4/admin/current-phase facts while retaining canonical paths, origin, model scope, and preservation decisions. |
| `docs/OPERATIONS.md` | Dated isolated-acceptance addendum only; no rewriting of older paths, service controls, or historical cleanup records. |

`docs/MODEL_RUNTIME.md` and `pyproject.toml` are outside this ownership; the main
agent owns the Transformers dependency/runtime follow-up. Application code,
scripts, tests, data, benchmarks, configuration, and other agents' edits were not
modified by this documentation pass. No file was removed, restored, staged,
committed, pushed, or deployed.

### Workspace Analysis

| Area | Current role and preservation decision |
| --- | --- |
| `src/linguistic_oj/` | Authoritative Python application: safe dataset inputs, deterministic scoring, fixed contracts, API/jobs, cookie accounts, local composition, and new administration. Preserve new untracked modules as source, not disposable experiments. |
| `src/linguistic_oj/web/` | Actual packaged HTML/CSS/JavaScript student and admin application. No separate Next.js/npm application or generated frontend build is needed. |
| `tests/`, `tests/browser/` | Python contracts/store/security/migration tests plus Node/Edge checks. Presence does not prove a fresh pass; backend, synthetic fixture, browser, and real-service evidence remain distinct. |
| `scripts/` | Real build/validation/launcher tools, including `build_acceptance_bundle.py`, `check_qwen_pipeline.py`, and `check_admin_browser.py`. These are deliberate new source tools, not cleanup candidates. |
| `config/` | Real immutable evaluation contracts, registry, treebank names, and source provenance. `config/.v1-catalog.lock` is a separate ignored process-lock artifact; it does not make the directory junk. No contract or provenance regeneration occurred. |
| `Standard_Dataset/`, `Target_Conllus/` | Dataset and tracked UD source assets, including existing dirty changes. These are not cache/garbage or duplicates to remove. Public source data is not a secret assessment set. |
| `challenges/`, `prompts/`, `benchmarks/` | Versioned descriptors, reference prompts, calibration and historical GPU observations. Preserve identities and prior scores; no benchmark rerun or score replacement was requested. |
| `runtime/` | Ignored private state/evidence, including local accounts, manifests, logs, browser/independent-review runs, the acceptance bundle and actual report. Keep every prior directory and record. The report remains a local artifact, not an automatically committed GitHub resource. |
| `.venv/`, build/egg metadata, caches | Existing working environment and generated output. `build/` can lag behind `src/` and is not authoritative source. No reinstall, rebuild, cleanup, or new disk-saving estimate was performed. |
| `opencode.json`, `opencode_error.png`, `opencode_file` | Existing tool configuration/user artifacts. Retained untouched; no OpenCode configuration change is needed for application documentation. |
| `.git/`, `.github/`, `.gitattributes`, `.gitignore` | Repository/history/CI/tracking infrastructure. No reset, clean, branch/remote/configuration change, or ignore-rule expansion occurred. |
| `docs/`, `AGENTS.md`, `PROJECT_COMMUNICATION.md` | Living implementation and collaboration context, with dated evidence retained. Current facts do not retroactively turn old release candidates into the current application. |

The earlier approximately 2.2 MiB estimate remains an earlier limited estimate,
not a new total or a reason to disturb the environment. "Ignored", "untracked",
"rebuildable", and "stale build copy" are not deletion authorizations. The safe
acceptance bundle is an export allowlist, not a classification of excluded files
as expendable.

### Current Implementation Findings

1. The local launcher serves the real same-origin student/admin app at
   `http://127.0.0.1:8080`, with SQLite, in-memory queues, and five two-sample Mock
   challenges. It is neither the real 18-language dataset nor the remote Qwen app.
   Existing local accounts/passwords/state are retained across restart.
2. `admin@example.test` / `LocalAdmin`, initial password
   `Local-only-passphrase-2026!`, is local-only. Role `admin` exposes teaching
   management at `#admin`; ordinary users cannot acquire it via a fragment, header,
   Bearer token, nickname, registration field, or website role editor.
3. Administration accepts only server-loaded challenge IDs, including teaching for
   catalog-only entries. Source/rights, dataset/sample/model/scorer/evaluation fields
   are read-only. There is no arbitrary filesystem selector, task creator, model
   launcher, role-management UI, or cross-owner private-result access.
4. Teaching has independent draft and published snapshots. Preview is local and
   plain text; save does not publish; check validates the saved revision; publish
   revalidates and exposes only the source-matching published content. The immutable
   evaluation contract may still be draft/closed. No model input is silently augmented:
   only a student's explicit template click replaces the editor, with confirmation
   for a nonempty prompt. Failed teaching reads keep templates blocked.
5. Admin mutations use revision compare-and-swap with atomic append-only-in-application
   audit insertion. The current database operator ID, role, and session are authorized
   inside the auth lock. After acquiring the task lock, fresh database time and
   authorization are checked again, denying a session that expired while waiting.
   Audit failure rolls back the state change; the audit is not a cryptographic log
   or a new public audit/rollback endpoint.
6. Pause is checked in both stores after idempotency replay/conflict and before
   quotas/new submission/outbox persistence. PostgreSQL shares the task advisory
   lock even for a first policy row; SQLite serializes writes. Previously accepted
   work/history/results and replay survive pause. Resume requires the existing
   source binding, contract/activation policy, and configured runtime; publication
   is never physical runtime activation.
7. Schema code is v4: v3 auth/roles plus two admin tables, retaining old records.
   SQLite applies missing supported migrations on construction; PostgreSQL migration
   stays explicit. Unbound legacy production users remain a startup/readiness blocker,
   without nickname auto-linking. Fresh-schema acceptance is not legacy enrollment
   or a migration of production tables.
8. Local English XPOS teaching now uses Penn Treebank rather than German HDT STTS;
   Chinese `LocalPractice` uses toneless pinyin and unchanged punctuation rather than
   GSDSimp tone marks/punctuation conversion. These are corrected task-specific
   handwritten lessons/templates, not new dataset/scorer versions or model scores.

### Actual Remote Evidence

The report was read directly, not inferred from the existence of a script. Its
JSON supplies the checks, timings, isolation identifiers, results, and cleanup
below. The server snapshot location, bundle hash, socket/port, dependency-install
location, and unchanged-service facts are supplied operator run context; they are
not fields independently attested by this JSON.

| Evidence | Recorded fact and limit |
| --- | --- |
| Local artifact | `runtime/qwen-admin-acceptance-20260907.json`, schema `qwen-isolated-acceptance-v1`. |
| Completion | `passed: true`, `stage: complete`; `2026-09-07T12:52:25.465438+00:00` to `2026-09-07T12:52:36.626038+00:00`. |
| Execution | `environment: isolated-test`, `transport: HTTP-inprocess-TestClient`, `browser_verified: false`; real backend dependencies, not a listening deployed browser API. |
| Account/security checks | Registration, explicit verification and token replay rejection, cookie login, owner-only access/history/result/leaderboard, per-account idempotency, expected-user/CSRF/Origin and rights gates, logout and replay of the old revoked cookie. |
| Admin checks | Real PostgreSQL draft/publication visibility, pause rejecting new work while replay and existing result remain, resume, and demotion rejecting the same cookie's subsequent admin access. |
| Evaluation | PostgreSQL outbox publication, real Redis delivery/worker acknowledgement, existing attested Qwen worker, deterministic aggregate persistence, and result retrieval. |
| Model work | Exactly 2 generation calls out of limit 2; 2 submissions created, 1 evaluated, 1 unevaluated guard-only submission. No provider request remained active at completion. |
| Synthetic result | 2 handwritten samples, 6 gold items, 2 valid/0 invalid samples, score 1.0; `benchmark: false`, `production_scores_written: false`. Not a model-quality claim or historical benchmark rerun. |
| PostgreSQL isolation | New schema `qwen_accept_u2035_e2ed08213e5e4b3e800405b2bc7ea7c8` on the existing colocated Unix socket/port 5433; actual repository migration SQL created v1-v4 state only there. |
| Redis isolation | Database 15, namespace `qwen-acceptance:eb94379385684bf99bea9403b88bc7ac`; four exact stream/active/receipts/owner key names are in the report. No requirement that the whole database be empty. |
| Cleanup | PostgreSQL: 11 tables and 1 schema dropped, confirmed. Redis: 3 remaining keys deleted, confirmed absent across all 4 names. Temporary fixture: confirmed removed. No Redis FLUSH, SCAN/prefix cleanup, or modification of old acceptance state. |
| Mail/activation | `development-memory-capture`, `draft_override: true`, `external_activation_ready: false`. Not SMTP delivery, HTTPS, a public-rights approval, or an activated challenge. |
| Attestation scope | `operator-evidence+local-tokenizer-hashes+live-model-metadata`; not cryptographic proof of model weight bytes in the running process. |

The actual source snapshot was
`/mnt/local/babylm26_g2/projects/linguistic-oj/artifacts/acceptance-20260907-admin-cookie-v1/snapshot`.
The supplied bundle SHA-256 is
`ecb2c25d13b4805169c8bbc829a36aa9ebbbd4b757c117c59eca08411a2d4fea`.
It is an allowlisted **working-tree snapshot, not a reviewed Git release**. Local
source can advance afterward; the report does not certify every later change.
Acceptance dependencies were installed only in isolated sibling `deps/`, not in
the vLLM environment. Existing services, launch configuration, models, and
`app/current` were not changed. vLLM checks used the operator's existing older
launch-evidence file, matching local tokenizer/chat-template hashes and live
alias/resolved snapshot metadata. No stronger weight/process proof is claimed.

`build_acceptance_bundle.py` includes selected source/assets, the runner, README,
dependency declaration, and two fixed configuration files with a per-file hash
manifest marked `production_release: false`. It excludes runtime, `.env`, `.venv`,
datasets, and unrelated configuration; its output is a new archive under ignored
project runtime with an existing parent. This is not a reviewed full release bundle.

The co-located Linux `check_qwen_pipeline.py` requires a new report outside Git and
the snapshot, an existing owner-only parent, report mode 0600, and a private
temporary fixture directory (0700). PostgreSQL cleanup rechecks schema OID, owner,
and a random creation marker, drops the owned tables together with RESTRICT, then
the empty schema with RESTRICT, never CASCADE. Redis cleanup verifies the owner
marker and deletes only the four exact reserved names, never flushes the database.
Three actual deletions do not imply a leaked fourth key: transient empty keys can
already be absent. Normal failure/SIGINT/SIGTERM attempts cleanup; SIGKILL, host
failure, ownership changes, and connection loss cannot guarantee it. Do not infer
permission to run cleanup against a guessed schema or another namespace.

### Verification Gaps and Next Stage

| Boundary | Next work or remaining limitation |
| --- | --- |
| Final local regression | Supplied intermediate Python and student/admin browser results precede the latest changes. The main agent will run the final suite; no final total is calculated or asserted here. Existing dated totals above remain historical. |
| PostgreSQL concurrency/migration matrix | Admin parameters use `LOJ_ADMIN_POSTGRES_TEST_URL` and were still skipped locally. Basic real-PG remote operations do not prove all concurrency, lock-expiry, rollback, or old-v3 migration parameters passed remotely. |
| Browser evidence | Real local Edge against Mock is distinct from synthetic fixtures and from this remote TestClient report. Finish the current student/admin browser run, including responsive/keyboard, account/role revalidation, and uncertain-response handling. |
| Remote packaging/runtime dependencies | The main agent owns the dependency/runtime follow-up. Isolated sibling installation proves the acceptance environment, not a clean production install or a changed vLLM environment. |
| Production accounts and mail | Legacy credential binding remains blocked until trusted enrollment or a deliberately isolated new database. True SMTP delivery and HTTPS/browser-to-Qwen acceptance are not done. |
| Formal public release | Reviewed Git release, rights approvals, per-contract activation evidence, protected data/secrets, supervision, monitoring, and off-host recovery remain later gates. No deployment pointer changed. |

The next product step is to finish this local teaching-management workflow and its
regression, not expand to arbitrary-source administration, add a model, remove
workspace files, or begin formal public launch. No new product submission quota,
data-retention policy, or release approval was introduced.
