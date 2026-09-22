# Project Context

Read the owner-local `PROJECT_COMMUNICATION.md` if present; it is intentionally
excluded from Git. In other checkouts, communicate in clear, concrete Chinese
unless the user requests another language, explain technical terms, and never
present planned work or skipped checks as completed results.

## Canonical Project

- On the project owner's computer, the main project directory is
  `D:\MyWebsite\Online Linguistic Judge`.
- The repository is
  `https://github.com/Linguistic-Online-Judge/Linguistic-AI-Infra-Dataset`.
- The configured Git remote uses the same URL with a `.git` suffix.
- Work in this project, not the former temporary worktree under
  `C:\Users\24300\AppData\Local\Temp\opencode\loj-multi-challenge-runtime`.
- The owner and the other repository collaborator can both reach the school
  server through SSH or the school VPN. Do not ask them to reconfirm this unless
  connection requirements or access actually change.
- Preserve existing uncommitted changes. Do not move or restore code from another
  worktree, commit, push, or deploy without the appropriate user request.

## Current Delivery Scope

- Latest owner priority (2026-09-23): the30-student/full50/all-final-within50s target is
  now a reference, NOT a required current adoption gate. Optimize for the most efficient
  reasonable configuration using complete-job latency, throughput, stability and faithful
  scoring. Do not equate a larger concurrency number with a better configuration.
  Owner explicitly authorized a NEW GPU0 maintenance comparison of4/8/16/32 request slots,
  at most1600 real calls, approximately40–60min development-site interruption, restoring
  the original model/app afterward. No automatic rollout of the winning setting. Preserve
  full50, prompts, original deadlines and scoring. See local communication for active evidence.
  This window is now FINISHED:1600 real calls/32 complete grades, c4/8/16/32 identical
  actual request hashes. Fixed4 active jobs, two waves x4 profiles, real owned PG/Redis/
  Cookie plus Qwen; not30 simultaneous users. Batch634.96/354.40/224.46/162.96s, c32 gains
  37.7% throughput vs16, German full-job77–81s vs108–113s; English16/32 nearly equal.
  Recommend32 request slots for next integration,16 fallback; not automatic rollout or
  quality equivalence. Repeat-score max ranges4.33/3.00/1.33/1.35pp; c32 paired-c4 max
  shift5pp, preserve the c4 English-B outlier and c32 German-B drop. No errors/preemptions.
  Original services restored observed API3818826/app3820644, argv/cwd/all11 tables match,
  8 accounts/74 old grades, health200, no pending requests; rediscover identities before use.
  Evidence artifacts/concurrency-selection-20260923-v1, docs/CONCURRENCY_SELECTION.md,
  private verified33-file archive7a8e2719affed458c3ad20d06e8cb65bec2051404f864e2a45d4b57991706949.
  Source adds bounded8-route/10ms admission slices with backoff AFTER full sweep, not per
  route; original guards/deadlines remain. Local809 passed/40 environment skips/246.90s.

- Latest owner clarification: this is a platform for efficiently dispatching model
  requests and returning faithful prompt evaluations, not a model-internals optimization
  project. Kernel profiling is diagnostic evidence only. Supersede earlier proposals
  to change matrix kernels/backends: focus on platform scheduling, bounded concurrency,
  reusable connections, verified input preparation, result aggregation and delivery.
  Do not modify model architecture/weights, numerical precision or internal kernels
  under this scope. Preserve the exact submitted prompt within the established fixed
  envelope; no hidden rewriting, extra examples or answer repair to improve scores.
  Keep full50 samples, original output/scoring contracts and genuine model results;
  cached corpus preparation is distinct from substituting cached answers or grades.
  Evaluate latency together with prompt-score comparability/repeatability, using
  different student prompts in end-to-end checks. Prior c32 results are not sufficient
  for deployment. Scheduling/resource and service changes still need the appropriate
  authorization; no new maintenance window is implied by this scope clarification.

- The teacher has now confirmed that the intended formal service must be reachable
  from both on- and off-campus networks. Public reachability is an explicit goal,
  not an optional campus-only deployment. See `docs/PUBLIC_DEPLOYMENT.md`.
  Current 8090 remains an SSH-only developer composition, not a verified public
  service. Do not expose its shared accounts/mailbox/draft override as production.
  Source/version consolidation and README updates precede GitHub publishing and
  a separately verified public deployment. The pre-integration Git inspection found detached
  HEAD at `cca0cc4`, while origin/main is `b69d3ff` (36 commits ahead), with a stale
  old-worktree main association. Preserve both histories and review an integration
  branch rather than force-pushing or copying the old worktree implementation.
  The owner subsequently authorized reviewed commit/push to an integration branch,
  without merging or deploying. Current branch: `integration/public-release-prep-20260913`.
  This is a complete development-source baseline; main integration and public
  deployment remain separate actions. Keep runtime/private material out of commits.
  Baseline commit `3040157` and fixes `61c7864`, `a972601` were pushed. GitHub run
  `34760180305` on `a972601` passed 589 Python tests with no skips, required real
  database coverage, 8 Node contracts, 32 student/16 admin browser groups and
  installed-wheel acceptance. The main branch stayed at `b69d3ff`. This Git push
  did not redeploy the school app or create a public website endpoint.

- Production preparation now has `config/production.example.json` and
  `scripts/prepare_public_deployment.py`: offline templates only, new output under
  ignored runtime, never automatic service activation. Domain stays null until
  confirmed. All 70 current contracts pass static Qwen checks but none satisfies
  existing public-activation gates. See `docs/PRODUCTION_PREPARATION.md` for inputs,
  private connection files, proxy/service templates and actual verification scope.
  The first main comparison ported connection/Redis/static-contract protections and
  the provider's uncertain-transport latch. Main merge and the remaining structured
  calibration/frontend/data differences are separate; preserve schema v4 and old
  contract identities. Local regression: 620 passes / 32 skips, 8 Node contracts,
  32 student and 16 admin browser groups. This source change has not been deployed.

- A new production-only `linguistic_oj.qwen_executor` offers check/init/run/status/recover,
  one serial consumer loop and per-request durable write-ahead barriers in a dedicated
  local state directory. `executor_state.py` holds a process-lifetime file lock;
  ambiguous requests block every queue and future startup. Recovery requires exact
  operation/binding plus private operator evidence, archives before clearing and never
  replays submissions. See `docs/PRODUCTION_EXECUTOR.md`. Never remove these state/lock
  or audit files to restart. It does not coordinate unmanaged model clients or replace
  the school's running development loop. Actual school rollout, live readiness/admission
  linkage and capacity checks remain pending. Local regression: 636 passed / 33 skipped;
  the extra skip is the Linux SIGTERM drain test. New subprocess/HTTP fault tests use
  controlled local fixtures, not the school model. Production templates now include
  executor config and a service with restart prevention for exit 75/78.

- Classroom target is approximately 30 students. New `qwen_performance` provides
  private per-sample timing/token/score statistics, read-only GPU sampling and matched
  1/2/4 client comparisons. It uses a separate calibration attestation path, never edits
  contracts or online concurrency. Performance requests require --run-real-qwen and
  sufficient actual launch evidence; reuse one dedicated persistent experiment state.
  Its barrier covers the entire batch: recovery must confirm ALL experiment requests
  terminated. Reports contain no prompt/gold/raw-response text and are not classroom
  capacity evidence. See `docs/QWEN_PERFORMANCE.md`. School SSH 75 and jump both closed
  the connection on 2026-09-14; no new school benchmark requests were sent. Connection
  evidence is ignored `runtime/qwen-performance-connection-20260914.json`. Local tests:
  648 passed / 33 skipped, including real local HTTP client concurrency with a fixture
  service. Keep real school performance results pending until connection and testing
  windows permit them; do not describe GitHub Linux checks as school-server tests.

- The owner later explicitly authorized a GPU0 maintenance window after authorized
  GPU1 became occupied by another user's job. Actual c1/c2/c4 school comparisons
  completed in `artifacts/performance-gpu0-window-20260916-v4`: six groups, 114 client
  requests, matching inputs/model/settings. Four clients improved batch throughput
  2.55x (English UPOS) and 3.39x (German dependency), but German score statistics
  varied across settings and one sample varied across three c4 repetitions. Do not
  call this a quality or 30-student capacity pass. See `docs/QWEN_CONCURRENCY_RESULTS.md`.
  Services were restored with matching argv/cwd and all 11 table fingerprints, 7
  accounts / 68 results, no pending experiment request. Restored observed PIDs:
  model API 1919069, engine 1919681, app 1920554; rediscover current identities before
  operations. Model is STILL max_num_seqs=1 on 8000, app on 8090; test 8001 is closed.
  Private restore environments stay on the school server and out of Git. Verified
  local report bundle/analysis lives under ignored `runtime/gpu0-comparison-20260916`.
  Benchmark CLI now supports --model-port for an independent loopback endpoint;
  this does not enable parallel production workers or change existing contracts.

- Owner's confirmed classroom target is 30 simultaneous full 50-sample evaluations,
  ALL final scores within 50 seconds, across ALL tasks including dependency. Only
  increased batch concurrency was authorized for this next optimization. Target is
  config/classroom_capacity_target_v1.json; do not reduce sample counts or substitute
  acceptance acknowledgments/cached counters for actual completed work.
  Model-only c8/c16/c32 budget screens completed on GPU0 in
  artifacts/performance-target50-20260916-v2. English completed 361/504/593 of 1500
  samples within 50s, German 34/53/74; ALL six necessary conditions failed. These
  same-prompt workload copies are not authenticated classroom users, and this is not
  a hardware upper bound. See docs/CLASSROOM_CAPACITY_RESULTS.md. Services restored
  with matching argv/cwd/all 11 table fingerprints, 69 successful results, no pending
  inference. Observed restored API/app PIDs: 3614331 / 3615687; rediscover before use.
  Runtime evidence stays private under runtime/target50-results-20260916.
  CLI supports up to 32 client slots, 30 workload copies, and an optional measurement
  budget: stop assigning at the budget, drain sent requests, exclude late results,
  and return budget_exceeded/exit 3 for an unmet model-only budget. Production limits
  and contracts remain unchanged; target achievement is still pending.

- The owner subsequently authorized compact dependency protocol investigation with
  a requirement to contain impact. New compact_dependency.py implements strict
  parallel-array/triple adapters, without registering them in production parsers.
  ProtocolProvider is explicitly rejected by frozen-contract runtime verification.
  Gold conversion on all 50 HDT samples preserved scores; 128 synthetic prediction
  combinations were checked for each adapter. Two small school studies (24 requests
  total, 2026-09-20, archive names retain 20260917) found BOTH naive candidates unfit
  for adoption: arrays produced two invalid long JSON outputs, triples two token-ID
  mismatches; both reduced selected-sample accuracy. See COMPACT_DEPENDENCY_STUDY.md.
  Do not promote these protocols based on token savings. Live app/model were not
  restarted or reconfigured, app PID stayed 3615687, successful submission count 69.
  Private evidence remains in runtime. Local regression: 687 passed / 33 skipped.

- Follow-up guided dependency study completed 36 school requests on 2026-09-20:
  2 protocols x example/no-example x constrained/free on calibration positions1/2/4,
  then predeclared example+guided on positions3/5/6 twice. The handwritten Ich/lese/.
  example was checked against the full selection. Grammar constraints use only safe
  input IDs/counts/head ranges, not gold labels/heads; compiled with xgrammar0.2.3.
  All 24 constrained outputs satisfied structure/ID checks. Held-out original vs
  triple LAS was23.08% vs5.77%, despite7.921s vs3.490s mean latency. Candidate remains
  REJECTED, not a quality/50-second pass. See docs/GUIDED_DEPENDENCY_STUDY.md.
  No app/model restart or live protocol change; app PID3615687 and69 results stayed
  stable. Local regression:691 passed/33 skipped. Private evidence is under ignored
  runtime/guided-dependency-results-20260920; do not tune against retained answers.

- CPU-only input study completed on 2026-09-20, full50 English/German samples,
  five interleaved repetitions, zero model requests. Warm selected-corpus caching
  reduced preparation medians0.336064->0.048530s /0.662106->0.100141s; batch-local
  prompt counting saved only7.3/9.8ms while preserving all50 rendered checks.
  Exact prepared samples/token sequences matched. Cache rehashes files on every use,
  verifies parsed bytes on misses and returns deep copies;16 entries/8MiB encoded
  payload is not an RSS bound. Runner injection is opt-in, NOT wired into workers or
  deployed. See docs/INPUT_PROCESSING_RESULTS.md. V1 prefix statistics were wrong
  because a tokenizer mapping was iterated as keys; corrected and rerun in v2 with
  mapping regression coverage. Current shared prefixes88/90 tokens; offline reordered
  candidates204/343. No reordered request was sent, no output equivalence established.
  Installed vLLM source requires Qwen3.5 align mode for prefix caching; actual cache
  hits depend on block/state configuration. No cache-enabled model test or restart
  occurred. App remained3615687/ready,69 results, no uncertain inference. Private
  evidence: runtime/input-processing-results-20260920-v2. Classroom target still unmet.

- Owner-authorized prefix-cache off/on GPU0 window completed76 requests on2026-09-20,
  original protocol/layout, c1,6 samples x3 plus1 warmup per task/config. English
  batch7.196->7.338s, German177.607->179.136s: no observed throughput benefit.
  On-mode align achieved0/4228 English and3696/10222 German token hits including
  warmups. Actual attention blocks528 tokens. German outputs changed6/18 and score
  statistics3/18; invalid counts3->0, not a general quality/equivalence pass. Within
  each condition, three repetitions were identical per sample. See PREFIX_CACHE_STUDY.md.
  Window artifacts/prefix-cache-20260920-v1 is finished, original services restored:
  observed API1581888/app1583475, rediscover identities before use. Argv/cwd and all11
  table fingerprints matched after ready,8 accounts/69 results, no unfinished work or
  pending inference, test8001 closed. Production remains original c1/cache defaults.
  Verified35-file private copy: runtime/prefix-cache-results-20260920; no restoration
  environment included. Next investigation is generation-phase timing, not a claim
  that cache hits equal wall-time savings or classroom target achieved.

- Generation-stage follow-up completed26 original nonstreaming requests without restart
  on2026-09-20:6 samples x2 plus1 warmup for English/German. New generation_stages.py
  reads server histogram sum/count deltas, requires exactly one completed request and
  matching usage/phase intervals, yields on online activity, pins model process identity,
  and uses GuardedQwenProvider persistent barriers. Metrics failure never triggers resend.
  Server TTFT means0.094151/0.176112s, prefill0.088568/0.166857s, decode0.290098/9.664343s;
  weighted decode48.26/48.62 tokens/s,76.61%/98.30% of engine inference. These are server
  event intervals (can include preemption), not browser TTFT or pure kernel timings.
  All26 attribution checks passed; English4/12 and German2/12 measured format failures
  retained in timing. API1581888/app1583475 unchanged, ready/69 results, clean experiment
  state. No production source deployment or classroom pass. Private evidence under
  runtime/generation-stages-results-20260920; see docs/GENERATION_STAGE_RESULTS.md.
  Local regression727 passed/33 skipped. Next useful work is batched decode/kernel
  profiling; do not call c1 throughput a hardware limit or infer required GPU counts.

- New authorized c1/c32 batched-kernel window finished107 model requests on2026-09-20:
  German first6 x6 plus1 warmup at each capacity (74), separate trace batches1/32 (33).
  Unprofiled36-request wall355.123->26.262s,13.52x throughput; output digests changed25/36,
  score statistics12/36, invalid6->0; sample3 had two c32 score variants. Do not call
  this quality degradation, repeatability pass or30-user capacity. Each trace contains
  20 pure-generation annotations (context0, generation1/32). Matrix kernels take95.71%/
  79.81% of summed kernel durations, gated delta0.92%/13.61%. New kernel_trace.py unions
  overlapping intervals, bounds decoded trace size and rejects missing/multidevice data.
  Captured-kernel coverage94.63%/98.01% is NOT compute/bandwidth saturation evidence.
  See docs/BATCH_KERNEL_RESULTS.md. Window artifacts/batch-kernel-20260920-v1 is finished;
  original argv/cwd/all11 tables matched,8 accounts/69 results, ready, no pending work,
  test8001 closed. Restored observed API1951479/app1953170; rediscover before operations.
  Private42-file evidence runtime/batch-kernel-results-20260920 excludes restore secrets.
  Local regression735 passed/33 skipped. Current online source and c1 configuration
  unchanged. The subsequent owner scope clarification above supersedes the proposed
  internal-kernel/backend optimization direction. Do not stack this speedup onto prior c32 data.

- Platform execution audit now wires VerifiedSelectionCache into Qwen worker factories:
  one shared16-entry/8MiB-encoded-payload cache per development/executor composition,
  one per standalone worker. Core/Mock/Qwen workers also support explicit injection.
  Every use still verifies artifacts and the full file hash; no prompt/response/grade
  caching. Development serial loop now waits0.25s only after idle rounds, matching
  production's existing behavior; ordering, draining and uncertain-request blocking
  are preserved. See docs/PLATFORM_EXECUTION_AUDIT.md. These source changes are NOT
  deployed to the school service. Local regression739 passed/33 skipped. New isolated
  API/SQLite/queue/Qwen-worker tests use explicit tokenizer/model/auth fixtures: two
  users with different exact prompts,50 samples each,100 provider calls, scores1/0,
  owner isolation, cache1 parse vs baseline2, and warm-cache data mutation rejection.
  Do not call fixtures real Qwen quality/capacity evidence, or multiply the removed
  per-round0.25s wait by50 samples. Prior CPU cache timings are not new end-to-end data.

- Bounded whole-job executor prototype now exists in bounded_executor_state.py and
  bounded_dispatch.py, NOT wired to production/school. Each lane owns independent
  workers/providers, at most one sample request per job, shared durable request map
  written before sends. Optional core claim_guard serializes queue/DB admission against
  blocking without holding across generation. Normal stop drains claimed jobs; unknown
  requests/I/O failures block dispatch and retain conservative pending evidence. Restart
  needs whole-set offline reconciliation, audit before clear, no recovery replay. State
  version differs from serial v1; never auto-migrate or delete old state. Observation/
  recovery only: python -m linguistic_oj.bounded_executor_state status|recover.
  Production runtime rejects experimental_execution providers. Declared prototype capacity
  is NOT attestation. See docs/BOUNDED_EXECUTION_PLAN.md for recommended1->2->4 qualification,
  required PostgreSQL/Redis/readiness/admission integration and30-user final-score gate.
  Local regression757 passed/33 skipped. Explicit local HTTP/API/SQLite/queue fixtures:
  3 users/4 prompts/full50 each/200 calls, per-user1 running, exact ownership/scores; early
  stop completes2 claimed jobs/100 calls and leaves2 queued. Two-request real HTTP process
  kill test proves client exit does not imply server termination; exact recovery sends
  nothing. School GPU/service and frozen contracts unchanged; no real capacity/quality pass.

- Current qualification work (2026-09-21): school real PostgreSQL/Redis/cookie-auth
  bounded fixtures c2/c4 passed, each4 users/5 jobs/full50/250 fake HTTP model calls,
  owned schema11 tables and Redis keys cleaned. New mandatory CI test_bounded_services.
  See docs/BOUNDED_SERVICES_QUALIFICATION.md. Maintenance preflight discovered API1951479
  already absent (old log SIGTERM2026-09-20 19:14:46, sender unknown), while app1953170
  misleadingly reported ready. V1 real qualification stopped after100 responses because
  upfront queued English work hit original300s deadline; German900s unchanged. V1 restored
  API1818178/app1820025 with matching11 tables/69 results; no pending inference.
  V2 FINISHED under owner authorization at artifacts/bounded-qualification-20260921-v2:
  8 distinct users,4 fixed prompts x2, full50 each, c1/c2/c4; controlled arrival at most
  capacity outstanding. This is quality calibration, NOT simultaneous pressure acceptance.
  Preserve v1 failure and do not fabricate its unread German API result. Upper planned
  total including v1 was1300, communicated to owner. V2 completed1200 responses/24 full
  results, all transient resources cleaned. Controlled-arrival batch times2343.29/1228.31/
  689.67s (1/1.91/3.40x). Repeat score ranges max0/1.00/1.67 percentage points; cross-c1
  maximum shifts4.00pp(c2),1.35pp(c4). Selected within-task B>A ordering remained, not a
  general quality pass. Per-job German model-call sums518–609s; target remains unmet.
  Original services restored API2088789/app2091021; argv/cwd/11-table fingerprints match,
  8 accounts/69 results, no pending work, model /health200 and alias verified. Rediscover
  current identities before operations; this window is finished, not reusable authorization.
  New source adds optional ProbedAvailability/LocalModelProbe, dynamic admission/catalog/
  admin flags and model-aware development readiness/queue pause. Read-only loopback health
  and alias probes,1s shared cache; existing results remain readable and explicit disables
  dominate. This source fix is NOT deployed to the school running application.
  Source485770d CI35584817201 passed806 Python/no skips, required real bounded PG/Redis,
  browser and wheel checks. Verified v1/v2 archives/analysis under ignored
  runtime/bounded-qualification-results-20260921; details in the qualification doc.

- Request-level prototype added in sample_scheduler.py: bounded independent providers,
  fewest-inflight/round-robin ties, spare-slot borrowing, late arrivals and exact future
  submission/sample binding. prepare_job retains full manifest/preflight/original deadline;
  only full aggregates, no fabricated zeroes or answer caching. Bounded state accepts
  optional hashed request_context; old records remain valid but older readers reject new
  fields. No production wiring, no per-sample durable resume. Local787 passed/35 skipped.
  Owner explicitly authorized a NEW GPU0 window for400 real calls, fixed c4 service,
  original English/German full50 prompts, per-job-limit1 vs shared4, ABBA two repeats.
  FINISHED artifacts/request-scheduling-20260922-v1:400 responses/8 complete full50 aggregates.
  Fixed c4 service, per-job limit1 vs4, same actual request hashes. English serial42.96/
  42.15s -> shared25.27/23.32s; German516.84/516.60 ->155.00/153.90s. Matched score deltas
  English-0.6667pp/German+0.1042pp; both policies German repeat range0.3125pp. Not quality
  equivalence or50s classroom pass. Existing app/model restored API642931/app644512,
  original argv/cwd and11-table fingerprints matched,8 accounts/69 grades, no pending
  work, test8001 cleaned, model health200. Rediscover current process identities before
  operations; this completed window does not authorize new service changes.
  Source SHAa9df8b1231c806bd5b8528f746dd81afcb33f7378389d90e4c04dbec15a7314a.
  Model-request-layer only, not API/database/browser/classroom acceptance. Verified21-file
  private archive d5e42d3f537fb28739e93b414a99c08737d4fd7977afa9f6c250b9dbfca81717 under
  runtime/request-scheduling-results-20260922. Code0da8248 CI35676958688 passed822/no skips.
  No production deployment. See docs/REQUEST_LEVEL_SCHEDULING.md for interpretation/next gates.

- RequestSubmissionExecutor now connects finite admission rounds to the existing queue,
  claim/preflight, request scheduler and transactional result publication. Read-only
  claim_is_current exists in both stores; PostgreSQL uses DB time. Before-sample claim
  observations do NOT replace complete_success's authoritative lease/deadline fence.
  Scheduler terminal callbacks run outside its lock and only after job in-flight calls
  drain; existing confirmed-error retry/max-attempt/deadline rules remain, uncertain
  requests block all work. Fast completed jobs publish before slower cohort peers.
  This is isolated finite-round integration, NOT continuous late admission/production.
  School c2/c4 real PostgreSQL/Redis/cookie tests passed with fake HTTP model:4 users,
  5 full50 jobs/250 calls each, per-user one running job, bound samples, owner results,
  empty ledgers/queues and owned cleanup11 tables/2 Redis keys. Real Qwen calls0, no
  restart/deployment. Evidence runtime/request-submission-services-results-20260922;
  docs/REQUEST_SUBMISSION_INTEGRATION.md. Local797 passed/37 skipped, full run372.68s;
  earlier timeout was not a passing run. New PG negative lease fence test awaits CI.
  No schema migration. Earlier request-layer speedup cannot be assumed after DB checks.

- Continuous isolated RequestSubmissionExecutor.run now admits late jobs while peers run,
  publishes via the same DB/receipt fences and retires completed input/outcome/claim contexts.
  Active-job bound defaults4/max32; two diagnostic rings default256, not a hard RSS bound.
  One coordinator interleaves bounded queue observations, preparation and publication;
  independent sample threads share the durable ledger. Same-process ExecutorAvailability
  connects API runtime_probe, claims and dispatch. Model outage pauses work, deadlines keep
  running; graceful stop seals admission/drains claimed jobs; incident health is sticky.
  Ledger read faults latch dispatch and retain in-flight evidence; model-health recovery
  cannot clear ledger/scheduler/publication faults. Continuous lifetimes are single-use.
  See docs/CONTINUOUS_REQUEST_EXECUTION.md. Local808 passed/40 environment skips/404.62s.
  School artifacts/continuous-submission-services-20260922-v1 c2/c4 real PG/Redis/Cookie
  checks passed: each4 users/5 full50 jobs/250 fake HTTP calls, late arrival while peer
  in flight, health admission/catalog gating, retained result reads, correct owner/sample
  binding, owned cleanup11 tables/2 keys. Zero Qwen calls, no model/app/source deployment.
  Shared health is in-process only; cross-process production readiness, multitask load,
  real-Qwen integrated latency/quality and30-user50s acceptance remain pending. The last
  model maintenance window is finished; this source work does not authorize another.

- Current XPOS release: `artifacts/xpos-20260913-v1/source`, sibling `data`,
  registry `config/challenge_contract_registry_xpos_v1.json`. School 8090 has
  74 catalog entries / 70 executable contracts: 18-language segmentation/UPOS/
  dependency, XPOS in 15 languages, Chinese transliteration. The original 56
  contracts and pre-upgrade 54 runs were preserved. Fourteen new XPOS tasks passed
  full browser workflow checks, exactly 14 submissions / 700 samples. Reused the
  foundation acceptance account; final counts are 7 accounts / 68 successful runs.
  See `docs/XPOS_TASK_EXPANSION.md` and ignored
  `runtime/browser-tests/xpos-20260913-v1/report.json` (`passed: true`).
  XPOS format validity was only 5–26/50 with the few-shot templates: do not call
  execution success a model-quality pass. Hebrew XPOS duplicates UPOS in every
  current pool; no redundant task was added. Danish/Hungarian lack complete XPOS.
  Backup `20260913T084818Z-8a6421c3` passed isolated restore for all 68 owner records,
  7 credentials, 11 matching tables and queue rebuilding, with cleanup confirmed.
  Its NEW off-host copy is incomplete because the school/jump connection failed;
  do not claim `runtime/backups/20260913T084818Z-8a6421c3` is verified. Older full
  copies remain intact. Resume the pinned transfer via `runtime/fetch_xpos_backup.py`
  after the connection recovers, then verify-copy. School backup itself is complete.
  `qwen-xpos.mjs --run-real-qwen --limit 2` resumes incomplete checks without
  duplicating accepted runs. Use the canonical project path for every tool call.

- The 2026-09-12 system audit and next engineering phase are documented in
  `docs/SYSTEM_ENGINEERING_PLAN.md`. Priority: current-instance backup/isolated
  restore, complete Git/reproducible delivery, CI, supervised operations and
  rollback, then measured capacity/fault recovery and product refinements.
  The first remediation batch is in `docs/QWEN_DEVELOPMENT_OPERATIONS.md`:
  `operations/bin/qwen-development status|backup|verify-restore` now targets the
  active marked database. Backup `20260912T180549Z-9d2ebb81` was restored in an
  isolated DB: 11 matching table fingerprints, 6 credentials, 20 owner records,
  in-memory queue rebuilding, confirmed cleanup and unchanged live data. A checked
  off-host copy is in ignored `runtime/backups/`; keep it private and preserve it.
  Legacy `backup-services`/health/restore scripts still target older databases and
  do not diagnose 8090. `Linger=no` and no Qwen developer user service were confirmed.
  Core source was initially untracked; the authorized integration branch now
  collects the complete source baseline. CI includes browser/wheel checks and
  `LOJ_ADMIN_POSTGRES_TEST_URL`; rely on its actual run result before calling it verified.
  Installed-wheel acceptance passed in a fresh Windows Python 3.14 environment;
  `requirements/application-win-py314.txt` records only that tested dependency set.
- The owner requests fuller task coverage in each of the 18 languages. The first
  expansion is deployed: 60 catalog entries / 56 executable contracts, with
  segmentation, UPOS and dependency in all 18 languages, plus German XPOS and
  Chinese transliteration. The original v1 registry remains the 22-task baseline;
  use `config/challenge_contract_registry_foundation_v1.json` for the current
  school instance via `--registry`. New source is
  `artifacts/foundation-20260913-v1/source`, data is sibling `data`; existing state,
  database and old contract hashes are preserved. All 34 new 50-sample jobs passed
  execution through the real browser/Qwen path (1700 samples), not a model-quality
  pass. Final observed state: 7 accounts, 54 successful runs, no outstanding jobs.
  Report: `runtime/browser-tests/foundation-20260913-v1/report.json`.
  `tests/browser/qwen-foundation.mjs --run-real-qwen --limit 2` is resumable and
  skips completed records. Never put it in ordinary CI. Details, token budgets,
  source choice and backup evidence: `docs/FOUNDATION_TASK_EXPANSION.md`.
  New backup `20260912T214730Z-78cc303d` restored successfully: 54 owner records,
  seven credentials, matching table fingerprints, queue rebuild and cleanup.
  Earlier read-only scanning
  found 75 language/task candidate combinations with a >=50-sample treebank:
  segmentation/UPOS/dependency in 18 languages, XPOS in 16, transliteration in 5.
  Danish/Hungarian lack complete XPOS in current data. Only Arabic/Chinese/Hindi/
  Korean/Thai currently have complete per-token Translit. Missing data is not proof
  of linguistic impossibility. See `docs/TASK_COVERAGE_PLAN.md`; candidates still
  need task-definition, source-rights, token-budget, teaching and runtime validation.
  For future additions use the explicit offline, additive CAS/journal flow in
  `scripts/extend_qwen_registry.py`; do not manually replace markers or old scores.

- Prioritize local development, complete business flows, professional frontend
  design, and backend correctness before public deployment.
- The owner finds the current frontend too dense: excessive simultaneous panels,
  repeated explanations and developer text, weak focus, and insufficient whitespace.
  Prioritize information architecture and concise user copy, not just larger padding.
  Offer real, accessible product references and agree a direction before a visual
  rewrite. Preserve all 18 languages; do not remove coverage to simplify the screen.
  `docs/PRODUCT_UX_REVIEW.md` records the next-phase audit and reference choices.
- A separate light, clickable three-view layout prototype now lives at
  `web/assets/layout-preview.html` (relative to `src/linguistic_oj/`). It can be
  opened directly as a file or at `http://127.0.0.1:8080/assets/layout-preview.html`.
  It uses explicitly labelled fixture data, makes no API/model calls, and does
  not replace the real workbench. The owner approved the three-view structure
  and chose reference 1 (Apple-inspired restraint). They explicitly dislike dark
  tones: use light surfaces, whitespace, clear type and restrained accents.
  The same prototype now has a light high-fidelity treatment; do not offer dark
  variants again unless the owner changes this preference.
- The owner rejects thick outer focus rings around inputs. Use a subtle existing
  border change for inputs, selects and textareas; retain visible keyboard focus.
  Show the full name "Linguistic Online Judge" beside an original minimal mark,
  with a two-line wordmark allowed. Their Mozilla example specifies arrangement,
  not the icon's appearance. The owner rejected the first bracket mark in
  `linguistic-mark.svg`. Keep it as a historical asset, not the default.
  FINAL OWNER CHOICE: original option A (folded L), in Fudan blue. Use
  `brand-option-a.svg` for the header and favicon, beside the full two-line name.
  Do not substitute A1-A6 or reopen the choice unless the owner asks.
  `brand-options.html` retains refinements and earlier options as design archives.
  The layout renders the approved mark directly in HTML; explicit mark parameters
  remain available solely for archived design comparisons.
- The owner wants all blues to lean toward Fudan blue. The official color-system
  graphic specifies R14 G65 B156: use `#0e419c` as the preview's shared primary blue
  (`brand-palette.css`), with coordinated hover/tints for buttons, links, selection,
  and marks. Source: `https://www.fudan.edu.cn/2405/list.htm`. Default the design
  comparison to blue; retain monochrome only as a mark comparison, not a dark theme.
  The original A and Fudan-blue combination is approved for subsequent UI integration.
- The approved design is now connected to the actual root frontend: catalog at
  `#challenges`, practice at `#challenge/<id>`, result at `#result/<submission_id>`,
  separate owner history and identity-partitioned leaderboard, plus existing admin.
  `app-shell.css` replaces the root's old stylesheet; the independent layout preview
  remains a fixture. Search is labelled "搜索". Development tools are in the footer.
  `GET /v1/submissions/{id}/prompt` now reads the exact saved prompt for its owner
  only, with no-store. It uses existing SQLite/PostgreSQL columns (no migration).
  Result reload and explicit continue-editing use this endpoint; copying a prompt
  does not submit and must confirm before replacing a different in-memory draft.
  Clear and fence prompt state across account/task/page changes as for other private data.
  Verification: 547 Python passes / 32 skips, 8 Node contracts, 32 real-local student
  browser groups and 16 real-local admin groups. Eighteen-language UI coverage used
  explicit directory fixtures; normal 8080 still has five handwritten tasks.
  The school 8090 instance was subsequently updated and verified on 2026-09-12;
  see `docs/QWEN_DEVELOPMENT.md` for the deployment and real-browser evidence.
- Previous school developer source: `artifacts/qwen-development-18-v1/snapshot-pages-20260911-v1`.
  The upgrade preserved all 11 table fingerprints (5 accounts, 18 old runs).
  Subsequent real browser acceptance added one test user and exactly two UPOS runs
  (English/Chinese, 50 samples each). Final inspection: 6 accounts, 20 successful
  runs, no outstanding work or uncertain-inference marker. Report:
  `runtime/browser-tests/qwen-pages-1789191380780/report.json` (`passed: true`).
  `tests/browser/qwen-pages.mjs --run-real-qwen` repeats those two real jobs and
  must not be included in ordinary local regression. The docs link is now inside
  the collapsed developer tools, and source bundles include web SVG assets.
- The owner also rejects frames around page headings and redundant explanatory
  subtitles (e.g. "选一种语言，开始练习。" under "选择任务"). Route-focused headings
  must not receive a visible outline. Do not add automatic subtitle/tagline filler
  beneath headings; include supporting text only when it adds necessary information.
- Interface copy must be accurate, concise, and natural Chinese. State outcomes
  directly; avoid personification, defensive claims, filler contrasts, and design
  commentary in product text. The owner specifically rejected "服务故障不会伪装成零分".
  Verify scoring/error wording against actual behavior and review hidden panels,
  empty states and confirmations too. See `PROJECT_COMMUNICATION.md`.
- V1 must cover all 18 languages. The owner explicitly rejected reducing delivery
  to one or two languages. Small fixtures remain test tools, not the V1 language scope.
- Real developer evaluation is available via the school-hosted
  `linguistic_oj.qwen_development` app and application-only SSH forwarding to
  `http://127.0.0.1:8090`. Keep the existing local Mock app on 8080 separate.
  See `docs/QWEN_DEVELOPMENT.md` for the 18-language browser evidence and boundaries.
- The current delivery adds private 18-language Qwen browser evaluation. On
  2026-09-10 all 18 UPOS tasks completed 50 samples each through the actual browser,
  database, queue, model and scoring path. Keep full concurrency/migration and
  public-launch acceptance separate; completed jobs do not mean correct model outputs.
- The real developer service uses an ownership-marked independent PostgreSQL
  database and Redis namespace, reads existing candidate data, and serializes
  the configured task workers (currently 56). It never falls back to Mock generation.
- Its development cookie is `loj_qwen_dev_session`, separate from the 8080 Mock
  cookie because browser cookies are not isolated by port. Email remains captured
  in the developer inbox; neither environment sends production email.
- `uncertain-inference.json` stops startup after a model request whose termination
  was not confirmed. Do not delete this marker or restart blindly to resume traffic.
- The product baseline is the teacher's linguistics LLM teaching and online
  evaluation platform: multilingual tasks, learning and practice, automatic
  scoring, result analysis, and rankings.
- The only current real evaluation model is the school-server Qwen3.5-9B.
  Multi-model support is deferred until the owner explicitly requests it.
- Public DNS/ingress, HTTPS, production accounts/mail and supervised operation are
  now part of the formal-deployment plan. Their actual school-provided settings
  still need to be established. Keep internal services and private data private.
- Treat local mock testing, school-server real-model testing, and public release
  as distinct environments with accurately labelled results.

## Verify Actual Implementation

- The frontend is plain HTML, CSS, and JavaScript in
  `src/linguistic_oj/web`, served by FastAPI at `/` and `/assets/*`.
- Do not assume a top-level Next.js `web/` application or npm startup commands
  exist. Inspect the current files before giving runnable instructions.
- `linguistic_oj.local_dev` and `scripts/start_local_dev.ps1` now start the real
  web/API stack with SQLite, in-memory queues, and a Mock worker. From the project
  root, run `& .\scripts\start_local_dev.ps1`; the canonical browser address is
  `http://127.0.0.1:8080`. `localhost` sends a different Host and is not accepted by
  this launcher. Keep the process running; a documented URL is not a live service.
- Use Python 3.11+ and the project `.venv`, installing `.[api,dev]`. If PowerShell
  blocks the script, use `./.venv/Scripts/python.exe -m linguistic_oj.local_dev --root .`
  at the same fixed port. Do not recommend execution-policy bypasses or global
  policy changes. Node 22+ and installed Edge are test tools, not app dependencies.
- The same-origin client supports email registration, explicit email verification,
  password login/reset, logout, task lessons/templates, asynchronous submissions,
  owner history/results, partitioned leaderboards, and teaching administration at
  `#admin`. Backend auth is cookie-only;
  the old frontend Bearer flow is only for an older deployment whose auth config
  endpoint returns 404, not an authentication-error fallback.
- The owner requested an 8-character password minimum. Current policy is 8-128
  characters (512 UTF-8 bytes maximum), with no required character-class mixture.
  Keep letters/digits, symbols, Unicode and existing password hashes compatible.
  User-facing hints use plain language, not Unicode/byte jargon. Do not call the
  eight-character choice compliant with a 15-character no-MFA recommendation.
- Mutating auth/private/admin requests require the exact Origin, JSON, and
  `X-LOJ-CSRF: 1`. `POST /v1/submissions` and all unsafe `/v1/admin/*` methods also
  require `X-LOJ-Expected-User` to match the cookie account. BroadcastChannel
  notifications and session revalidation synchronize tabs; the backend guard still
  rejects stale-account requests.
- Default state is isolated in ignored `runtime/local-development`. The five
  handwritten local challenges have two samples each, not the real 18-language
  dataset or Qwen benchmark. Local scores never represent Qwen performance.
- Local seed accounts are `alice@example.test` / `LocalAlice`, `bob@example.test` /
  `LocalBob`, and `admin@example.test` / `LocalAdmin`. Their common initial password
  is `Local-only-passphrase-2026!`, exclusively for the isolated loopback Mock app.
  Restart preserves changed credentials. The development panel still displays
  the initial password after a reset; use the password actually chosen.
- `LocalAdmin` has the local admin role and can open
  `http://127.0.0.1:8080/#admin`. Seed provisioning does not restore a deliberately
  changed role. Public registration creates users, and there is no role-assignment UI.
- Administration is only for server-loaded `challenge_id` values: plain-text
  draft/preview/check/publish and new-admission pause/resume. It cannot choose
  arbitrary paths, create tasks, edit datasets/models/scorers/source rights, launch
  services, or read another owner's private results. Published teaching is separate
  from immutable evaluation `status: draft`; publication is not physical activation.
- Teaching templates load only on a student's explicit click, with confirmation
  before replacing an existing prompt. Published instructions are not silently
  appended to model input; failed publication reads block template loading.
- English `LocalPractice` XPOS uses Penn Treebank tags, not German HDT STTS.
  Chinese `LocalPractice` transliteration uses toneless lowercase pinyin and
  unchanged punctuation, not GSDSimp's tone-marked/punctuation-conversion rules.
- Admin writes use `expected_revision` CAS and atomic state/audit commits. Current
  database user ID/role/session is checked under the auth lock, then database time
  is refreshed and authorization rechecked after the task lock; expiry while waiting
  denies save/check/publish/admissions. Stale page roles or forged headers grant nothing.
- Both stores fence new admission after idempotency replay/conflict detection and
  before quota/new submission/outbox writes. Pause preserves accepted queues,
  history/results, and replay; normal auth/contract/runtime gates still apply.
  Resume only clears the switch under existing policy/runtime/source binding.
- The local inbox retains at most 100 messages in process memory. Restart clears
  that inbox, not accounts or passwords. The launcher locks its state directory
  for one process and fails on port conflicts without killing another process.
  Queued work can be restored; interrupted running work waits for lease expiry
  handling, not instant successful recovery.
- Local code sets a technical budget of 1000 submissions per user per challenge
  per 24 hours. This does not change the existing real-contract limit of 5 per
  24 hours and is neither a lifetime quota nor an unlimited-submission promise.
- SQLite/PostgreSQL schema code is now v4: v3 added auth/roles; v4 appends
  `challenge_admin_state` and `challenge_admin_revisions`, preserving prior rows.
  Production rejects legacy users without credential bindings until trusted
  enrollment or an isolated new database is arranged; never auto-link by nickname.
  Current migration code does not prove a live database was migrated.
- Production `qwen_api` requires a protected `--auth-config-file`, HTTPS and SMTP;
  authentication callbacks are forbidden in production. Trust only exact actual
  proxy IPs, with the proxy overwriting `X-LOJ-Client-IP`. Arbitrary forwarded
  headers are not trusted. Production mail uses one thread and a nondurable queue
  of 64 pending requests; accepted requests return 202 even if later SMTP delivery
  fails. Monitor sanitized failure logs and support resend; local capture is dev-only.
- Recheck these implementation facts as the project evolves. Never treat prior
  conversation claims as proof that a feature exists in this worktree.

## Verification and Preservation

- The five-step local scope and commands are recorded in
  `docs/LOCAL_DEVELOPMENT.md`; account/security details are in
  `docs/AUTHENTICATION.md`; admin boundaries and operations are in
  `docs/ADMINISTRATION.md`. No PostgreSQL server, Redis server, GPU, or SMTP service
  is needed for the local Mock app.
- `scripts/check_local_browser.py` starts an isolated temporary local app and a
  real installed Edge browser. `node --test tests/browser/contracts.test.mjs` and
  `node tests/browser/run.mjs --fixture` are separate synthetic checks. Do not
  describe fixtures as backend or real-model verification, or invent test totals.
- `scripts/check_admin_browser.py` starts a fresh SQLite/Mock app and runs student
  and admin Edge suites, never daily port/state. Earlier student/admin counts and
  intermediate Python results are not final current regression totals. The main
  run must record actual results after the latest expiry/browser changes.
- The earlier SSH `75` / `antlnp75` model-list and one `Cats sleep .` generation
  probe remains narrow. `scripts/check_qwen_connection.py` is not the app chain.
  The later actual `runtime/qwen-admin-acceptance-20260907.json` records `passed: true`
  for cookie auth/owner guards, PostgreSQL outbox/Redis/Qwen scoring, admin
  publish/pause/replay/resume/demotion, and logout with old-cookie replay rejection.
- That run used `HTTP-inprocess-TestClient`, `browser_verified: false`, and captured
  email. Exactly two generations evaluated one submission/two handwritten samples/
  six gold items at score 1.0; a second guard-only submission was not evaluated.
  It is synthetic, not a benchmark, real-email/HTTPS/browser proof, or public release.
- The operator run used the existing PostgreSQL socket/port 5433 and a new random
  schema; the report confirms removal of 11 tables and that schema. Redis DB 15
  used four exact unique namespaced keys, three remaining keys deleted with cleanup
  confirmed, no FLUSH. Temporary fixture cleanup is confirmed. This basic real-PG
  run does not establish all concurrency/migration parameters; admin PostgreSQL
  tests separately require `LOJ_ADMIN_POSTGRES_TEST_URL` and remain unverified when skipped.
- The server source snapshot was
  `/mnt/local/babylm26_g2/projects/linguistic-oj/artifacts/acceptance-20260907-admin-cookie-v1/snapshot`;
  bundle SHA-256 was `ecb2c25d13b4805169c8bbc829a36aa9ebbbd4b757c117c59eca08411a2d4fea`.
  It is a working-tree snapshot, not a reviewed Git release. Dependencies were
  installed only in isolated sibling `deps/`; the vLLM environment, services, and
  `app/current` were not changed. Later source changes require new evidence.
- Attestation reads existing operator launch evidence, hashes local tokenizer/chat
  template files, and compares live alias/resolved snapshot metadata. It is not
  cryptographic proof of the running process's weights. Historical benchmarks were
  not rerun; the full GPU stack was not relocated or reconfigured.
- `build_acceptance_bundle.py` packages allowlisted source/config only, excluding
  runtime, `.env`, `.venv`, and datasets. `check_qwen_pipeline.py` requires a new
  0600 report in an existing private parent outside Git/snapshot, private temporary
  fixtures, and ownership-proved RESTRICT PostgreSQL cleanup plus exact Redis keys.
  It does not stop services or guarantee cleanup after SIGKILL/host failure.
- Preserve existing ignored environments, build/egg metadata, caches, runtime
  records, old acceptance directories, and user artifacts `opencode_error.png`
  and `opencode_file`. See `docs/WORKSPACE_AUDIT.md`. Ignored does not mean disposable.
- Do not edit OpenCode configuration for application work. Do not force-delete or
  run `git clean`/`git reset`. Commit, push, live migration, service changes, and
  deployment require a separate appropriate request, not a local feature check.

These files persist project context; they do not prove remote deployment or
guarantee that an assistant in an unrelated workspace has loaded the context.
