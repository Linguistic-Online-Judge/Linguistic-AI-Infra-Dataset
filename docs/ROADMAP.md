# MVP Roadmap

## Definition of the MVP

One student can choose one language and one task, submit a prompt, have the same
pinned model run against a versioned server-side sample set, receive a
deterministic score, inspect safe aggregate feedback, and appear on a leaderboard.
Public UD development challenges are reproducible rather than secret; strict
assessments use unpublished data. No gold answer is sent to the browser.

## Confirmed account and access boundary

- The public service does not depend on the school eHall identity system.
- V1 uses only two account roles: general user and administrator. It does not
  create separate student and teacher roles.
- Authentication must still be verified by the server before protected data is
  returned. The final registration and credential method remains undecided.
- A later public release is expected to run on an overseas server, so an internal
  campus-network assumption must not be used as a security control.
- Frontend visual design is owned by the project team. The East China Normal
  University online judge is an optional reference rather than a requirement.

These decisions are recorded in ADR 0002. Registration fields and administrator
capabilities must not be invented until they are approved.

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
deterministic code-point preflight and cannot write to the Qwen partition.

## Phase 4: backend service and jobs

- Create the FastAPI application and database migrations.
- Add users, challenges, model configurations, submissions, and result tables.
- Store an explicit general-user or administrator role for every account.
- Add submission, status, result, and leaderboard endpoints.
- Run evaluations in a worker; never block an HTTP request on model inference.
- Add rate limits, retry policy, idempotency, and safe logs.

Exit criterion: an API integration test completes a mock submission end to end.

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

Implemented first slice: a Chinese-first Next.js application now provides the
anonymous challenge index and challenge detail routes. It validates API data
before rendering, exposes no private evaluation fields, and includes loading,
empty, service-failure, and not-found states. Component accessibility tests and
visual checks cover desktop and mobile layouts. Authentication, prompt editing,
submission progress, history, results, and leaderboards remain future slices.

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
3. Configure persistent Redis deployment, credentials, health monitoring, and
   worker process entry points.
4. Move deployment persistence to PostgreSQL and add production authentication,
   migrations, rate-limit operations, and structured safe logging.
5. Build the web flow only after submission persistence and background jobs are
   stable under concurrency and restart tests.
