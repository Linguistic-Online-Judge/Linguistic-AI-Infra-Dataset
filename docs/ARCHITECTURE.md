# Platform Architecture

## Core principle

The platform evaluates prompt quality, not model choice. Every submission for a
given competition uses the same pinned model, inference runtime, parameters,
task instructions, and versioned server-side sample set. A deterministic code
evaluator compares parsed model output with UD gold annotations. An LLM is never
used as the judge.

## Security boundary

The current GitHub repository is public and contains `answers` for the full UD
test set. These answers are therefore not hidden. Before students receive access
to the platform, the repository must be made private or the gold data must be
moved to private server-side storage. Deleting files in a later commit is not
sufficient because the answers remain in Git history.

Making the repository private fixes direct platform leakage but does not make UD
test data truly secret. UD treebanks are public, students can search source
sentences, and the fixed model may have seen UD data during pretraining. Use the
current dataset for development, teaching, and benchmark-style evaluation. A
strict anti-cheating competition requires a separate unpublished set annotated
and reviewed by qualified linguists. The platform should identify which security
level each challenge provides.

For the MVP, the smallest safe approach is:

1. Make this repository private while the two-person team develops the system.
2. Keep public examples in a separate `samples/` directory without hidden test
   answers.
3. Mount or import the full gold dataset only in the backend and worker.
4. Never include `answers`, model credentials, or server-side sample IDs in browser
   responses, logs returned to students, or client bundles.
5. Use newly annotated, unpublished sentences for challenges that require hard
   answer secrecy; do not describe public UD test labels as cryptographically
   hidden.

UD treebanks use different licenses. Record the source and license for every
included treebank before external deployment.

## Recommended MVP components

```text
Next.js web client
        |
        v
FastAPI application ---- PostgreSQL
        |
        v
Redis job queue ---- Python evaluation worker
                         |          |
                         v          v
                  fixed model   private gold data
```

- **Web client**: task selection, prompt editor, submission status, score report,
  history, and leaderboard.
- **API**: anonymous allowlisted challenge summaries/details, authentication,
  submission creation, status, results, and leaderboard queries. The public
  catalog is copied field by field from the validated registry; submission
  creation remains authenticated.
- **Worker**: builds prompts, calls the pinned model, parses JSON responses, runs
  deterministic scorers, and persists aggregate results.
- **Model provider**: one adapter interface. Start with a mock adapter in tests,
  then add one fixed self-hosted model adapter.
- **Safe model input**: immutable task-specific DTOs constructed field by field.
  Providers never receive `DatasetSample`, sample IDs, or `answers`.
- **Gold data service**: loads samples by server-side challenge manifest.
  It must never expose the `answers` field to the client.
- **Database**: users, competitions, problems, model configurations, submissions,
  aggregate outcomes, and leaderboard rows. ADR 0001 does not persist raw
  per-sample responses for the teaching MVP.

SQLite can be used for a single-machine prototype. PostgreSQL remains the
deployment database target. A Redis Streams queue adapter is implemented for
cross-process delivery; production still needs persistent Redis deployment,
credentials, monitoring, and recovery operations.

The current development slice implements an injected FastAPI app, SQLite schema
migration, transactional submission/outbox creation, an immutable challenge
registry, identity-routed in-memory and Redis Streams queues, and explicitly
invoked workers. The API loads every registry entry before serving, selects the
contract by client-supplied challenge ID, and owns one dispatcher and queue route
per executable contract. Each Worker remains bound to one trusted challenge and
contract snapshot. The API returns `202 queued`; model evaluation never runs in
the request handler. Owner queries
bind both authenticated user and submission ID, while leaderboards read only
version-partitioned aggregate rows. Redis uses a Consumer Group, pending-entry
visibility recovery, per-claim receipt tokens, and an active-submission map for
idempotent publication. This prevents stale handlers from acknowledging reclaimed
work and prevents API restarts from duplicating durable Stream entries. Reclaimed
and fresh work are interleaved so neither class can starve the other. Invalid UTF-8
or malformed entries are removed rather than becoming permanent poison messages.
Every queue instance appends a random incarnation ID to its configured consumer
name, while receipt rotation is atomic with stale-entry claiming.
Mock deliveries use a 30-second fenced database lease inside the persisted
300-second job deadline; the 45-second queue visibility also budgets five seconds
for SQLite lock acquisition, five seconds for claim processing, and five seconds
of safety after the lease. Expired leases fail closed because `WORKER_CRASH` is
non-retryable in this contract. A delivery blocked by an existing run or per-user
concurrency limit stays pending until visibility recovery instead of entering an
immediate requeue loop. Redis must run without independent Stream trimming or
key eviction; idempotent publication does self-heal when its tracked Stream entry
is missing, but operational deletion of queue metadata remains unsupported.

The Qwen worker is a separate fail-closed class rather than a relaxed Mock worker.
It requires `mvp-evaluation-v2`, exact tokenizer artifact and chat-template hashes,
deployment-owned model/runtime evidence, a 4,096-token runtime context,
single-model concurrency, and contract-matching provider timeout and response
limits before it can consume a job. This is configuration attestation within a
trusted host boundary, not cryptographic verification of a vLLM process or model
weights. Its database lease covers the complete 300-second deadline, so its queue
visibility must be at least 315 seconds. This conservative MVP avoids a heartbeat
race; a later deployment may replace it with fenced lease renewal.

Only `PROVIDER_TIMEOUT` and `PROVIDER_TRANSPORT` can restart the complete job.
The first failure atomically returns the same submission to `queued` and nacks the
existing delivery. The next claim increments the attempt number without extending
the original deadline. A second failure is terminal and owner feedback reports
`retryable=false`. Mock calls are synchronous and have returned before retry; a
real provider must additionally prove remote request termination or cancellation.
An HTTP timeout or ambiguous disconnect is therefore terminal even though its
failure code is generally retryable; an HTTP response such as `503` confirms that
request ended and may use the second attempt. Every request timeout is clamped to
the remaining absolute job deadline, and response bodies are bounded before JSON
decoding.

## Submission flow

1. A student chooses a language and task and submits one prompt.
2. The API finds that challenge in its validated registry and selects the matching
   contract-specific outbox dispatcher. Unknown and public-only entries cannot
   create submissions.
3. The API stores an immutable submission with the complete evaluation-contract
   snapshot and its canonical identity SHA-256.
4. The selected single-contract Worker validates the paired public/private
   artifacts, then loads the fixed
   server-side sample IDs and trusted gold denominators for that challenge.
5. For each sample, the worker combines the student's prompt with the platform's
   fixed task envelope and requests strict JSON output from the model.
6. The response parser validates the schema. Malformed output receives a
   deterministic zero for that sample; no LLM repair judge is used.
7. The scorer computes segmentation F1, tag accuracy, UAS/LAS, or token-level
   transliteration accuracy with sentence exact match.
8. The worker stores aggregate metrics and safe error categories, then marks the
   submission `rejected`, `succeeded`, or `failed`.
9. The leaderboard reads persisted scores and their stored contract snapshots; it
   does not reinterpret old results through the current registry or rerun evaluations.

The offline runner implements artifact/sample loading, the safe provider
boundary, mock and pinned self-hosted generation, fixed Prompt Envelope `1.0`,
response parsing, scoring, and in-memory aggregation. The Mock service slice now
adds API submission storage, explicit background execution, aggregate result
persistence, idempotency, ownership checks, and leaderboard queries. The Qwen
runtime layer adds pinned-tokenizer preflight for every fully rendered sample,
deadline propagation, provider response bounds, and startup attestation checks.

Mock scores use a separate `runtime=mock` identity and deterministic
Unicode-code-point preflight. Qwen uses the new `mvp-evaluation-v2` partition,
whose tokenizer revision, files, chat template, and counting method are part of
the canonical evaluation identity. `QwenSubmissionWorker` itself loads the
verified local tokenizer snapshot and verifies the co-located vLLM deployment
before it consumes work. Production still requires protected launch evidence,
authentication, PostgreSQL, persistent Redis operations, and deployment-safe
logging.

## Fairness and reproducibility

- Partition results by the complete canonical evaluation identity: contract,
  challenge and artifact hashes, model revision, runtime, generation parameters,
  Prompt Envelope, response schema, scorer, and aggregation versions.
- Pin the model artifact by exact version or checksum and attest it at worker
  startup.
- Use fixed parameters, initially `temperature=0`, fixed maximum output tokens,
  and a fixed seed when the runtime supports it.
- Use the same challenge manifest for all students in one competition. Only
  unpublished-data challenges should be described as hidden.
- Store the complete immutable contract snapshot and identity digest on every
  submission rather than copying a partial set of version columns.
- Do not use a rotating cloud "free model" alias for official ranking because
  providers may silently update it. A self-hosted open model is more reproducible,
  although compute is not free.

GPU inference can still have small nondeterministic effects even at temperature
zero. Development can use local Ollama and a mock provider. The teacher has
confirmed server deployment and can provide a development server; exact GPU
specifications and the final production worker topology are still pending.

## Dataset usage

`standard_dataset.jsonl` is about 176 MiB (185 MB), so request handlers must not
scan it for every submission. The dataset layer should create indexed challenge manifests or
import required fields into PostgreSQL. Each competition should use a bounded,
versioned sample set rather than all 135,180 sentences on every submission.

A practical MVP challenge can start with 50-100 server-side samples for one
language and one task. Scale only after model latency and cost are measured.

## Repository strategy

Keep a monorepo during the MVP because there are only two developers and shared
types are still changing. Suggested future layout:

```text
apps/api/             FastAPI HTTP application
apps/web/             Next.js client
src/linguistic_oj/    evaluator, parsers, dataset and model adapters
tests/                unit and integration tests
docs/                 specifications and decisions
```

Split the gold dataset into separate private storage later if independent access
control or deployment size makes it necessary.
