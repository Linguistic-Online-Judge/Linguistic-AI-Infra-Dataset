# Engineering Work Log

## 2026-09-01: Runtime and Persistence Foundation

- Added and GPU-smoke-tested the Qwen v2 API, Redis Worker, vLLM, and deterministic
  evaluation path.
- Added API liveness/readiness endpoints and allowlisted request completion logs.
- Provisioned user-owned PostgreSQL 18.6 and Redis 7.4 on the GPU host without
  changing system packages. PostgreSQL uses a private Unix socket; Redis uses AOF,
  `noeviction`, and a private Unix socket.
- Implemented `PostgresSubmissionStore` with schema migration, idempotency, quotas,
  outbox recovery, fenced leases, retry/failure/rejection handling, results, and
  leaderboard queries. A real PostgreSQL smoke passed on the GPU host.
- Added PostgreSQL integration tests and GitHub Actions PostgreSQL 18 service setup.
  API and Worker can now explicitly select the same PostgreSQL URL; production
  activation remains pending.
- Verified a real 50-sample Qwen path on the GPU host using the persistent Redis
  Unix socket and PostgreSQL: API submission, outbox dispatch, Worker claim,
  runtime attestation, vLLM inference, deterministic scoring, result persistence,
  and owner result retrieval all passed. Temporary API and vLLM processes were
  stopped after the smoke and GPU 0 was released.
- Hardened the PostgreSQL path after review: idempotent replay now preserves typed
  submission status, API startup and a periodic dispatcher recover unpublished
  outbox work, PostgreSQL operations have bounded connect/lock/statement timeouts,
  lease fencing uses database-authoritative time after row locks, and readiness
  verifies the migration version and required tables. Idle Workers now sweep leases
  at most every five seconds instead of opening four PostgreSQL connections per
  second. Local tests passed with `196 passed, 7 skipped`; the dedicated remote
  PostgreSQL integration database passed all 11 migration/store tests.
- Added `/mnt/local/babylm26_g2/projects/linguistic-oj` as a mode-700, additive
  server workspace index. Its symbolic links expose the existing repo, services,
  models, environments, toolchain, and caches under one root without moving data
  or changing any verified runtime path. PostgreSQL, Redis, process, and GPU state
  were rechecked after creation.

## 2026-09-02: Canonical Server Workspace

- Backed up both PostgreSQL databases, globals, stopped service state, and the
  project-owned home caches before moving data. Database dumps passed SHA-256 and
  archive checks.
- Moved all confirmed project-owned server resources under
  `/mnt/local/babylm26_g2/projects/linguistic-oj`, then left compatibility links at
  legacy paths. The shared home Hugging Face cache containing GPT-BERT resources
  was explicitly excluded.
- Restarted PostgreSQL and Redis from canonical paths. Database row baselines,
  schema version, Redis persistence, both Python environments, uv, model files,
  compatibility paths, and API dependency health checks passed. All 11 remote
  PostgreSQL migration/store tests passed after migration.
- Replaced the temporary flat deployment copy with an immutable-style smoke
  release at `app/releases/smoke-20260902-uncommitted` and an `app/current`
  pointer. Smoke evidence moved to `artifacts`, private challenge data moved to
  `private`, and legacy paths remain compatibility links.
- Added repeatable PostgreSQL and Redis service controls, a read-only workspace
  health check, online PostgreSQL/Redis backups with checksums, managed Redis
  configuration, and logrotate configuration. A verified online backup was
  created before the final layout and service restart work.
- Consolidated vLLM and FlashInfer caches under the workspace while leaving home
  compatibility links. `uv cache prune` found no unused entries, so the cache was
  retained. Both Qwen model snapshots remain available.
- Removed only rebuildable toolchain trees after checking the installed service
  binary paths and retained source archives. Deployment, private data,
  operations, backups, and service state were normalized to owner-only
  permissions beneath the mode-`700` workspace root.
- Restarted Redis through `operations/config/redis.conf`; its two-key baseline,
  key-set digest, AOF `everysec`, `noeviction`, data directory, and private socket
  settings were preserved. The canonical health check and compatibility-link
  checks passed afterward.
- Added an isolated backup restore verifier. The final backup passed SHA-256
  validation, both PostgreSQL dumps restored with schema version 1 and submission
  counts `3/25`, and the Redis RDB restored two keys through a separate socket;
  all temporary restore state was removed automatically.
- Final verification passed with 11 remote PostgreSQL integration tests, one real
  remote Redis queue test, `196 passed, 7 skipped` locally, Ruff, and
  `git diff --check`.

## 2026-09-02: V1 Multi-Challenge Backend Candidate

- Froze the V1 product boundary at exactly 18 languages and documented the public
  benchmark limitations, required API, task availability rules, and release gates.
- Added a validated public challenge/contract registry, multi-contract API routing,
  per-contract Redis queues and dispatchers, owner-scoped history pagination, and
  public challenge provenance fields.
- Added additive SQLite and PostgreSQL schema v2 migrations and indexes for owner
  history, quotas, leases, outbox dispatch, and deterministic leaderboard queries.
  Production entry points now reject SQLite outside development or test use.
- Fixed temporarily unclaimable jobs to requeue behind fresh work instead of waiting
  for the full visibility timeout. The Worker loop applies its configured idle delay
  after an unsuccessful claim, preventing a tight retry loop.
- Local verification passed with `211 passed, 8 skipped`, Ruff, and
  `git diff --check`.
- Created `app/releases/candidate-20260902-schema-v2` without changing
  `app/current`. Backup `backups/online/20260902T150447Z` passed full restore
  verification before the isolated test database migrated from schema v1 to v2.
  The candidate then passed all 219 server tests with real PostgreSQL and Redis;
  production PostgreSQL remains on schema v1 and healthy.
- Froze the representative-catalog policy at the largest eligible treebank per
  language, deterministic name tie-break, 50 samples, and seed `2026`. Added a
  repeatable fail-before-write builder and generated 18 draft UPOS challenges,
  private manifests, and Qwen contracts. The registry now exposes 23 current
  descriptors and 19 executable draft contracts while keeping every entry closed
  to production submissions pending provenance and GPU evidence.
- Local catalog verification passed with an idempotent real-data rebuild,
  `215 passed, 8 skipped`, Ruff, and `git diff --check`. Preserved the earlier
  schema-v2 candidate and created `app/releases/candidate-20260902-v1-catalog` for
  the new catalog. Its deployed source data and private artifacts passed the same
  idempotent rebuild; the complete server suite passed all 223 tests with real
  PostgreSQL and Redis, scoped Ruff passed, and Redis test database 15 was empty
  afterward. Production stayed on schema v1 and `app/current` was not changed.
- Hardened immutable catalog writes after independent review: exact canonical JSON
  comparison now distinguishes numeric representations; completed registries reject
  challenge, identity, descriptor-path, and contract-path rebinding before writes;
  the official entry point enforces the ordered 18-language UPOS/count-50/seed-2026
  V1 policy; and relative roots, public-only descriptors, symbolic links, unsafe
  ancestors, normalized-empty paths, and NTFS alternate data streams fail closed.
  Builds use an OS advisory lock that is released on process death and registry
  replacement uses a securely created temporary file.
- Final local verification passed with an idempotent real-data rebuild,
  `227 passed, 12 skipped`, Ruff, and `git diff --check`; local skips require either
  service integrations or Windows symbolic-link privileges. Created the preserved
  successor `app/releases/candidate-20260903-v1-catalog-hardened`; its Linux rebuild
  and all 239 tests passed with real PostgreSQL/Redis, scoped Ruff passed, and Redis
  test database 15 was empty. Production health remained schema v1 and `app/current`
  remained on `smoke-20260902-uncommitted`.
- Rebuilt all 135,180 standard samples with field-derived strict task eligibility.
  The executable UPOS catalog now selects visible, valid pools for exactly 18
  languages; Arabic moved to PUD, French to FQB, Japanese to PUD, and English to
  CHILDES. Added deterministic model-input rejection for unavailable token forms.
- Added a fixed-tokenizer budget audit over the exact private manifests and Qwen
  chat template. Four UPOS contracts use 512 completion tokens; the others use
  256. Added executable segmentation, XPOS, dependency, and transliteration
  exemplars with measured budgets of 256, 256, 1024, and 512 respectively. The
  resulting registry has 26 public descriptors and 22 executable draft contracts.
- Local verification passed with an idempotent real-data rebuild,
  `256 passed, 12 skipped`, Ruff, and `git diff --check`. The isolated server
  candidate `candidate-20260903-v1-strict-upos-budget` passed its deterministic
  tokenizer audit and all 268 tests with real PostgreSQL/Redis; scoped Ruff passed
  and Redis database 15 was empty. Production remained schema v1 and `app/current`
  remained on `smoke-20260902-uncommitted`.
- Froze the browser-facing V1 API contract: one typed error envelope, OpenAPI
  Bearer security declarations, explicit policy/runtime/acceptance challenge
  flags, durable-outbox `202` semantics, immutable evaluation identities in
  submission responses, discriminated terminal results, structured quota
  feedback, strict versioned cursors, bounded snapshot-stable leaderboards, and
  independent queued-deadline expiration.
- The API-frozen tree passed the complete local suite with `262 passed, 14
  skipped`, full-repository Ruff, and `git diff --check`. The isolated Linux
  candidate passed all 276 tests with real PostgreSQL and Redis, including
  structured quota, deadline sweep, rejected-result, and absolute leaderboard-rank
  coverage; scoped Ruff passed and Redis database 15 was empty afterward.
- Added the packaged, dependency-free V1 student frontend with challenge filtering,
  UTF-8-aware prompt entry, in-memory Bearer authentication, durable run tracking,
  typed result rendering, owner history, and evaluation-identity leaderboard
  pagination. Desktop and compact Edge renders were reviewed; a browser-driven
  preview connected an account, enabled a valid prompt, submitted it, and rendered
  the queued state through the real API boundary.
- The frontend-frozen tree passed `263 passed, 14 skipped` locally and all 277
  tests on Linux with real PostgreSQL and Redis. Full Ruff, JavaScript syntax,
  wheel package-data inspection, and browser render checks passed; Redis database
  15 was empty afterward.
- Completed a real five-task Qwen3.5-9B/vLLM smoke through the frozen API,
  PostgreSQL outbox and state, isolated Redis Streams, runtime-attested Worker,
  deterministic parsers/scorers, result persistence, and authenticated owner
  retrieval. Segmentation, UPOS, XPOS, dependency, and transliteration each ran all
  50 selected samples and reached a typed `succeeded` result. Scores are recorded
  as observations rather than activation thresholds.
- The initial dependency run failed closed at the shared 300-second deadline. A
  600-second calibration completed in about 547 seconds, leaving insufficient
  operational headroom, so the deterministic catalog builder and final dependency
  contract now use 900 seconds. The final snapshot completed in about 533 seconds
  with identical aggregate output; its evaluation identity remained unchanged.
  The fixed-tokenizer audit was regenerated byte-for-byte on the GPU host and now
  has internal report SHA-256
  `82ef537f26a4b25a7f585070438d4cf17e29c5b541dc2ebce39205151da8cd0f`.
- Added the machine-checked five-task observation at
  `benchmarks/observations/qwen3.5-9b-v1-five-task-gpu-smoke.json`; its internal
  report SHA-256 is
  `6555f3d656c25e7888de0ca9c96909128faae78aaa2ef740b81c7d03977d27c8`.
  Contract, evaluation, prompt, raw-result, launch-evidence, and token-audit hashes
  were verified. API logs contained no credentials or prompt bodies; vLLM emitted
  only upstream processor-documentation startup warnings and no request failures.
- Stopped the temporary API and vLLM processes after evidence capture, verified no
  compute process remained on GPU 0, flushed dedicated Redis database 15 to zero
  keys, and removed local/remote smoke credentials and helper scripts. Production
  schema, `app/current`, shared Hugging Face cache, and all draft activation states
  remained unchanged.
- The post-smoke candidate passed all 278 tests with real PostgreSQL and Redis,
  full scoped Ruff, JavaScript syntax, report-hash consistency, and
  `git diff --check`. Redis database 15 remained empty, GPU 0 remained released,
  the production health check still reported schema version 1, and `app/current`
  still targeted `smoke-20260902-uncommitted`.
- Traced every treebank referenced by the 26 public V1 descriptors to official
  Universal Dependencies 2.18 repositories and release commits. After canonical
  CRLF-to-LF normalization, all 22 local source files matched the pinned `r2.18`
  upstream bytes. Recorded source, release-commit, LICENSE, README, attribution,
  share-alike, and underlying-text findings in
  `config/v1_source_provenance.json`; its internal report SHA-256 is
  `7fd8e9b56661c6ab2fc15e2667291d4097f4f5d2fed932817b55afaa7ac5dd76`.
- Kept provenance activation fail closed. No treebank is marked rights-cleared;
  Chinese Beginner, German HDT, English GUM, Italian KIParlaForest, and Portuguese
  CINTIL are explicit holds because of advertising/noncommercial, academic-use,
  source-specific, upstream-agreement, or no-derivatives constraints. Current
  challenge descriptors and contract snapshots remain unchanged and `draft`, so
  the completed GPU evidence remains bound to the exact tested artifacts.
- The provenance-complete candidate passed all 279 tests with real PostgreSQL and
  Redis plus full scoped Ruff and `git diff --check`. The production conclusion is
  still `No-Go`: source identity is proven, but rights approval, per-activated-entry
  GPU evidence, reviewed Git release identity, production authentication/HTTPS,
  schema-v2 migration, supervision, monitoring, and off-host restore evidence
  remain blocking.
