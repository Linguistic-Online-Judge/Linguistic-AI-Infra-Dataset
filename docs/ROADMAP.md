# MVP Roadmap

## 当前下一阶段：版本整合与公网部署准备（2026-09-13）

老师已确认正式网站需要校内外均可访问。当前学校实例已有74项目录、70个执行配置，
XPOS扩充验收已完成。优先整理完整Git版本、README与自动检查，再与学校落实公网域名、
HTTPS入口、真实邮件和正式实例运行方式，见 `PUBLIC_DEPLOYMENT.md`。
当前8090仍为开发通道，不能直接将其共享测试账户和收件箱开放到公网。

三页界面、本人原提示词与复用已同步学校8090，并完成18语言目录及英语/中文两条
真实评测验收。下面按历史阶段保存的待办不代表当前仍未实现。

下一阶段以 [系统工程审查与落地计划](SYSTEM_ENGINEERING_PLAN.md) 为当前入口：
当前8090数据库已完成首轮备份、隔离恢复和异机副本校验；接着形成完整Git版本和可复现交付，推进
自动检查、实例管理/部署回退、多人容量与故障恢复，最后完善草稿和实验比较。
每批以文档中的验收条件结束，不以新增页面数量作为完成依据。
用户另要求每种语言尽可能具备完善的任务覆盖。当前22项执行配置与75项数据候选的
逐语言表、缺失原因及受控扩充路线见 `TASK_COVERAGE_PLAN.md`。
基础覆盖现已扩充至56个可执行配置，新增34项均通过实际50样本执行验收，
见 `FOUNDATION_TASK_EXPANSION.md`；专用词性、转写和教学模板质量是后续独立工作。

## Definition of the MVP

One student can choose one language and one task, submit a prompt, have the same
pinned model run against a versioned server-side sample set, receive a
deterministic score, inspect safe aggregate feedback, and appear on a leaderboard.
Public UD development challenges are reproducible rather than secret; strict
assessments use unpublished data. No gold answer is sent to the browser.

## Current Local Scope

The owner requires all 18 languages for V1; do not reduce delivery to a few
languages. The real private Qwen9B developer workbench now runs on school-server
loopback port 8090 and is reached through an application-only SSH forward.
On 2026-09-10 a real Edge browser registered/logged in and completed one 50-sample
UPOS submission for every language (900 samples), with result, history, and
leaderboard checks. All 18 jobs completed, but model output validity varies and
this is not a model-quality or independent-benchmark pass. See
[Qwen development](QWEN_DEVELOPMENT.md). Keep the five-task 8080 Mock environment
as a separate fast test tool, not the V1 coverage claim.

The formal target now includes on- and off-campus public access. Current priorities
are consolidating the full source version, validating CI, and preparing the public
deployment conditions in `PUBLIC_DEPLOYMENT.md`. The only real evaluation model
remains the existing school-server Qwen3.5-9B. Historical phases below retain their
original scope; they do not override the newly confirmed public-access target.

The five-step local work now has a loopback launcher, email/password account
lifecycle, five-task browser workbench, loaded-task teaching administration,
isolated browser/test tooling, and real-backend Qwen acceptance. Run
`& .\scripts\start_local_dev.ps1` from the root and open `http://127.0.0.1:8080`;
`localhost` is a different, rejected Host. See
[Local Development](LOCAL_DEVELOPMENT.md) for prerequisites, the Python fallback,
seed accounts, `#admin`, retention, and verification commands,
[Teaching Administration](ADMINISTRATION.md) for the management boundary, and
[Workspace Audit](WORKSPACE_AUDIT.md) for the no-cleanup preservation boundary.

Local Mock fixtures contain two handwritten samples per challenge, not the real
18-language benchmark. Their 1000-per-user-per-challenge-per-24-hour technical
budget does not change the real contract's existing 5-per-24-hour limit. English
local XPOS uses Penn Treebank, not German HDT STTS; Chinese local transliteration
uses toneless pinyin and unchanged punctuation, not GSDSimp's tone/punctuation
conventions. Lessons select these defaults by language and treebank.

The actual `runtime/qwen-admin-acceptance-20260907.json` records `passed: true` for
cookie registration/login, owner and expected-user/CSRF/Origin guards, real
PostgreSQL outbox/Redis/Qwen scoring, admin publish/pause/replay/resume/demotion, and
logout with rejection of the old cookie. It is **HTTP-inprocess TestClient**, with
`browser_verified: false` and memory-captured email: two generation calls, one
evaluated submission, two handwritten samples/six gold items, score `1.0`.
This is synthetic integration evidence, not a benchmark or public deployment.
Its isolated schema/keys were cleaned; the full PostgreSQL concurrency matrix,
browser-to-Qwen, and real SMTP/HTTPS remain separate gates. The earlier one-request
connection probe and historical benchmarks remain distinct records.

## Historical Milestone: Local Administration

Implemented: the cookie-role-gated `#admin` view lists already loaded tasks, keeps
source/evaluation metadata read-only, and offers plain-text draft preview/save,
saved-revision checks, publication, and new-submission pause/resume. Teaching
publication does not activate an evaluation contract or silently change a student's
model input; templates load only on the student's explicit click.

Both stores target schema v4 and commit revision-checked changes with operator
audits atomically. Current database user/role/session authorization is refreshed
after the task lock, denying sessions that expired while waiting. Admission fencing
is in persistence after idempotency replay/conflict handling; existing jobs,
history, and results survive a pause. Resume uses only existing source, policy,
contract, and runtime conditions. There are no arbitrary file/dataset/model/scorer
edits, rights approval, task creation, or role-assignment UI.

Next exit criterion: complete the current local student/admin regression across
desktop/mobile and keyboard flows, publication read failures, uncertain writes,
revision conflicts, account switching/demotion/expiry, and restart retention.
Record actual final run results, not inferred totals from intermediate runs. Run
the opt-in PostgreSQL migration/concurrency parameters separately; the successful
remote basic chain is not a substitute. Source-rights approval and formal public
launch are later, explicitly separate work, not prerequisites for improving this
local administration phase.
## Account and access boundary (with historical ADR context)

- The public service does not depend on the school eHall identity system.
- V1 uses only two account roles: general user and administrator. It does not
  create separate student and teacher roles.
- Authentication is server-verified email/password/cookie login. The earlier undecided
  credential method in ADR0002 has been superseded by the implemented account lifecycle.
- The formal service must be reachable on and off campus; the current school-hosted
  private service is not proof of public ingress. Campus-network assumptions are not access control.
- Frontend visual design is owned by the project team. The East China Normal
  University online judge is an optional reference rather than a requirement.

ADR0002 retains the historical decisions. Current capabilities and scope are documented
in AUTHENTICATION.md, ADMINISTRATION.md and PUBLIC_DEPLOYMENT.md.

## Gate 0: security and specification

- Make the repository private before treating the checked-in test data as hidden.
- Decide whether the first challenge is a public-data teaching benchmark or a
  strict anti-cheating assessment. The latter needs unpublished human annotation.
- Decide which UD treebanks and licenses are allowed in the first challenge.
- Freeze response JSON schemas for segmentation, UPOS, dependency, and
  transliteration.
- Choose one initial language/task pair and a bounded server-side challenge set.
- Obtain the school GPU server specifications before choosing the self-hosted
  model size. The teacher has confirmed that a development server can be provided.

Exit criterion: the team can state exactly what a student sees, what stays
server-side, what source-data leakage remains possible, and how every malformed
or valid output is scored.

The first MVP decision is now frozen in ADR 0001. It is an English EWT UPOS
`public_reproducible` teaching benchmark, not a strict anti-cheating assessment.
The official Treebank page states CC BY-SA 4.0; external activation remains
blocked until the local release or commit, file hashes, attribution/share-alike
requirements, and underlying-text rights review are recorded.

## Phase 1: deterministic evaluation core

- Implement token-span segmentation precision, recall, and F1.
- Implement strict UPOS/XPOS positional accuracy.
- Implement dependency UAS and LAS keyed by token ID.
- Implement Unicode-normalized token transliteration accuracy and sentence exact
  match.
- Add response schema parsing and malformed-output categories.
- Add unit tests for perfect, partial, malformed, missing, and extra output.

Exit criterion: all metrics run offline without a model or web server and are
fully covered by repeatable tests.

Implemented so far: per-sample scorers, strict response JSON contracts, parser
error categories, token-level transliteration scoring, and versioned
challenge-level aggregation for all supported tasks. A deterministic offline
runner now connects safe inputs, mock generation, parsing, scoring, and aggregation.

## Phase 2: dataset and challenge layer

- Stream JSONL instead of loading the approximately 176 MiB (185 MB) file into
  request handlers.
- Generate versioned server-side challenge manifests containing immutable sample
  IDs and trusted gold denominators.
- Return only safe problem input DTOs without `answers`.
- Add deterministic sample selection and integrity hashes.
- Add dataset validation and source/license metadata.

Exit criterion: a command can build one challenge and prove that its public
payload contains no gold fields.

Implemented so far: streaming JSONL filtering, deterministic reservoir sampling,
versioned public challenge metadata, private manifests, integrity hashes, and the
first 50-sample Chinese GSDSimp segmentation challenge (`v2`). The current challenge is
marked `draft` and `public_reproducible`; conflicting content cannot overwrite an
existing challenge version, even when a clean clone has no private manifest.
Public SHA-256 fingerprints bind its source, selection, and per-sample
denominators. A tracked name map makes Treebank casing reproducible.
Task-specific immutable model input DTOs now enforce the gold-data boundary.

## Phase 3: fixed model runner

- Define a provider-independent model adapter.
- Implement a mock provider for tests.
- Evaluate candidate self-hosted models locally through Ollama, then on the school
  GPU development server.
- Pin one model artifact, runtime, generation parameters, and prompt envelope.
- Define raw-response retention and enforce time/output limits. ADR 0001 chooses
  aggregate-only persistence for the teaching MVP.

Exit criterion: the same local evaluation command can switch from mock to the
pinned model without changing scoring code.

Implemented so far: the provider-neutral request/result contract, deterministic
mock provider, complete dataset/artifact preflight, synchronous offline runner,
fixed Prompt Envelope `1.0`, and OpenAI-compatible adapter for the pinned
self-hosted Qwen3.5-9B/vLLM runtime. Prompt calibration covers public, held-out
Treebank, and unpublished synthetic data. ADR 0001 freezes the historical v1
model/challenge contract and inference limits. The v2 runtime contract adds exact
tokenizer and chat-template hashes, all-sample prompt/context preflight, remaining
deadline propagation, bounded provider responses, conservative termination-aware
retry, and startup runtime attestation. The development Mock path retains its own
deterministic code-point preflight and cannot write to the Qwen partition. A real
Qwen3.5-9B/vLLM smoke has completed all five task families over their frozen
50-sample manifests; its evidence also calibrated the dependency job deadline to
900 seconds without changing the evaluation identity.

## Phase 4: backend service and jobs

- Create the FastAPI application and database migrations.
- Add users, challenges, model configurations, submissions, and result tables.
- Store an explicit general-user or administrator role for every account.
- Add submission, status, result, and leaderboard endpoints.
- Run evaluations in a worker; never block an HTTP request on model inference.
- Add rate limits, retry policy, idempotency, and safe logs.

Exit criterion: an API integration test completes a mock submission end to end.

Current local phase note: SQLite/PostgreSQL schema code is now v4 (v3 auth/roles
plus v4 teaching/admission state and revision audit), with persistent credentials,
cookie sessions, and transactional admin controls. Production startup
requires protected auth configuration and refuses unbound legacy users until
trusted enrollment; nickname auto-linking is forbidden. This is not a live schema
migration or production integration claim. The isolated remote run created and
removed a new v4 test schema, not production account bindings. See
[Authentication](AUTHENTICATION.md).

Implemented so far: FastAPI app factory, SQLite schema migration, authenticated
owner-scoped submission/status/result routes, transactional idempotency and
outbox, identity-routed in-memory and Redis Streams queues, explicit fenced Mock
Worker execution, bounded complete-job retries, aggregate-only results, safe
failure DTOs, version-isolated leaderboards, explicit `user`/`admin` account roles,
an owner-safe current-user endpoint, registry-driven multi-challenge routing, and
anonymous allowlisted challenge list/detail endpoints. One API process now owns
one queue/outbox dispatcher per executable contract while each Worker remains
bound to one selected challenge. The integration suite covers public catalog
ordering and availability, `202 queued`, replay/conflict behavior, cross-owner
`404`, preflight rejection, duplicate delivery, visibility recovery, retry
success/exhaustion, safe platform failure, and Mock/Qwen identity separation. CI
validates Redis behavior against a real Redis 7.4 service; local runs skip that
one test when `REDIS_TEST_URL` is not configured.

## Phase 5: web application

- Build login, challenge list, task details, and prompt editor.
- Offer zero-shot, few-shot, and CoT templates as editable teaching aids.
- Show queued/running/rejected/succeeded/failed status and metric explanations.
- Show submission history and leaderboard.
- Verify desktop and mobile layouts.

Exit criterion: a new user can complete the MVP flow without direct API use.

Current local phase note: the packaged HTML/CSS/JavaScript app includes account
verification/reset, five task guides and editable templates, submission tracking,
owner history/results, rankings, shared-cookie tab synchronization, and role-gated
teaching administration. Backend same-origin/CSRF and expected-user checks protect
stale-account writes; store authorization protects stale roles/expired waiting
sessions. The real local-app Edge wrapper and synthetic fixture suite are separate verification
paths; continue checking responsive/keyboard behavior and failure handling before
expanding scope. The isolated cookie/admin/Qwen backend chain has passed; production
SMTP/HTTPS and browser-to-Qwen verification remain separate gates, not consequences
of a passing Mock browser flow or TestClient run.
The retained independent first slice in `web/` is a Chinese-first Next.js application with the
anonymous challenge index and challenge detail routes. It validates API data
before rendering, exposes no private evaluation fields, and includes loading,
empty, service-failure, and not-found states. Component accessibility tests and
visual checks cover desktop and mobile layouts. That catalog-only component does not
replace the native workbench described above.

## Phase 6: deployment and fairness validation

- Containerize API, worker, model runtime, database, Redis, and web client.
- Configure backups, secret management, monitoring, and health checks.
- Load-test concurrent submissions and cap challenge size.
- Run repeatability tests and record all version metadata.
- Perform a gold-data leakage review before inviting students.

Exit criterion: a staged class trial can run without manual score calculation.

## Two-person split

- Developer A: evaluator, response schemas, dataset/challenge layer, quality tests.
- Developer B: model adapter, API/jobs, database, deployment baseline.
- Shared: architecture decisions, frontend, PR review, security and demo testing.

Every change should start from an issue, use a short feature branch, include tests
or a clear manual verification, and merge through a pull request reviewed by the
other developer.

## Immediate sequence

1. Completed: load the verified local tokenizer snapshot and trusted vLLM
   attestation in a real worker process entry point.
2. Completed: run the v2 contract in a single-concurrency GPU worker without
   permitting Mock scores in the Qwen partition.
3. Candidate complete: persistent Redis, Qwen worker entry point, recovery logic,
   and health checks. Production monitoring, credentials, and supervision remain.
4. Candidate baseline: PostgreSQL schema v2, rate limits, migration code, and safe
   logs. Current checkout appends schema v3 authentication and v4 administration;
   production migration, trusted enrollment, and integration verification remain.
5. Completed: packaged responsive web flow over the frozen API.
6. Completed for identification, blocked for approval: pin all catalog sources to
   UD 2.18 and record per-treebank license and underlying-text risks.
7. Current local phase: complete student/admin acceptance for the loaded-task
   teaching workflow, account/role freshness, revision/audit fencing, and admission
   replay/retention. Keep the existing model and source/evaluation contracts fixed.
8. Next verification: run the full opt-in real PostgreSQL admin migration/concurrency
   matrix and record fresh final local/browser results. Preserve the successful
   2026-09-07 basic remote cookie/admin/Qwen report without promoting its synthetic
   score to benchmark evidence.
9. Later public-release gates: resolve held rights, collect per-contract GPU evidence
   for every entry to activate, approve legacy account enrollment/migration, verify
   real browser/SMTP/HTTPS operation, and build a reviewed immutable release with
   activation/startup gates, supervision, monitoring, and off-host restore evidence.
