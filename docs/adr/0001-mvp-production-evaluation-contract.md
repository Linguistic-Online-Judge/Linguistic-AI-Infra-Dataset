# ADR 0001: MVP Production Evaluation Contract

- Status: Accepted
- Date: 2026-08-29
- Contract: `mvp-evaluation-v1`

## Context

The deterministic evaluator, challenge layer, prompt envelope, self-hosted model
provider, and pinned Qwen3.5-9B runtime are implemented. The next product gate is
an asynchronous submission service. Before API, worker, database, and leaderboard
code are written, they need one immutable definition of what constitutes a
comparable submission.

The current repository and UD answers are public. The AI-assisted synthetic data
used for calibration are unpublished but are not production-secret because the
authoring and review services saw their text, tokens, and gold labels. The first
MVP therefore cannot honestly claim strict anti-cheating security.

One 50-sample evaluation takes tens of seconds and can take several minutes for a
prompt that induces verbose output. The verified server runs one model sequence
at a time. An HTTP request must not remain open while inference runs, and multiple
students must not compete directly for GPU memory.

## Decision

The first MVP is a public-data teaching benchmark, not a strict assessment. It
uses the following fixed configuration:

- Challenge: `en-ewt-upos-v1`, 50 English EWT UPOS samples.
- Security label: `public_reproducible`.
- Catalog status: `draft` until local UD release provenance is recorded.
- Annotation/database license: CC BY-SA 4.0, as stated by the official UD English
  EWT page. Underlying-text rights still require review.
- Model: `Qwen/Qwen3.5-9B` at revision
  `c202236235762e1c871ad0ccb60c8ee5ba337b9a`.
- Runtime: vLLM `0.27.1+cu129`.
- Generation: temperature `0`, top-p `1`, seed `2026`, thinking disabled, and
  `max_tokens=256`.
- Prompt Envelope, response schema, scorer, and aggregation versions: `1.0`,
  `upos-v1`, `1.0`, and `1.0`.

The machine-readable source of this decision is
`config/mvp_evaluation.json`. API, worker, result, and leaderboard code must read
or snapshot this contract rather than duplicating these values in route handlers.
The model revision and runtime are currently operator-verified identities; the
future worker must verify the local artifact and runtime at startup before they
can be treated as runtime-attested.

## Output-budget evidence

The selected EWT manifest contains at most 43 gold items in one sample. With the
pinned Qwen tokenizer, compact `{"tags":[...]}` responses containing the longest
allowed tag require:

| Tag count | Output tokens |
| ---: | ---: |
| 1 | 7 |
| 43 | 175 |
| 64 | 259 |
| 128 | 515 |

The 256-token budget leaves 81 tokens of headroom for the longest current sample.
It is challenge-specific: a challenge with 64 or more items must not inherit this
setting without a new budget check.

The baseline prompt was run twice with `max_tokens=256`. Both canonical aggregate
hashes were identical and matched the previous 1,024-token benchmark:
35/50 valid samples, micro accuracy `0.34024179620034545`, one `INVALID_TAG`, and
14 `LENGTH_MISMATCH` errors. All 100 provider requests ended with normal `stop`
completions and no output-length completion. The aggregate is stored in
`benchmarks/qwen3.5-9b-en-ewt-upos-mvp-v1.json`; the separately captured vLLM
metrics are stored in
`benchmarks/observations/qwen3.5-9b-en-ewt-upos-mvp-v1.json`. The current provider
discards per-request `finish_reason`; the observation therefore binds the captured
counts to the evaluation-identity, generation, prompt, and aggregate hashes. The
production worker must retain aggregate finish-reason counts without exposing
per-sample output.

## Submission limits

The API and worker will enforce the values in `config/mvp_evaluation.json`:

- Student prompt: nonempty, at most 8,192 UTF-8 bytes and 2,048 pinned-tokenizer
  tokens.
- Fully rendered model input: at most 3,840 tokens, leaving exactly 256 tokens in
  the 4,096-token model context.
- Provider request timeout: 120 seconds.
- Whole evaluation job timeout: 300 seconds.
- Accepted submissions: at most five per user, challenge, and rolling 24 hours.
- Outstanding queued plus running submissions: at most three per user and 100
  globally.
- Running submissions: at most one per user.
- Model concurrency: one until a later load test proves a higher safe value.
- API request body: at most 16,384 bytes; idempotency key: 1-128 ASCII characters
  with a full-string match of `[A-Za-z0-9._~-]+`; provider response body: at most
  32,768 bytes before JSON parsing.

The edge proxy and ASGI receive path enforce the body limit before buffering or
JSON decoding. `Content-Length` above the cap returns HTTP `413`; missing-length
or chunked bodies use a streaming byte counter and stop reading at the cap. Token
limits are checked with the pinned tokenizer during preflight, before the first
provider request. The tokenizer revision and chat-template counting method must
match the pinned model, include special tokens, and preflight every rendered
sample. A token-limit violation ends in `rejected`, produces no score, and
consumes one rate-limit slot; replaying the same idempotency key does not consume
another.

Per-user admission pressure returns HTTP `429`; a full global queue returns HTTP
`503`. The 300-second deadline covers the complete submission across both
attempts, not each attempt. Every provider timeout is clamped to the remaining
deadline.

## API and worker semantics

Submission creation is asynchronous. The API stores an immutable submission,
places its ID on a queue, and returns HTTP `202` with status `queued`. A separate
worker claims the ID, changes it to `running`, executes the fixed contract, and
finishes as `rejected`, `succeeded`, or `failed`.

Malformed model content is student-visible behavior: the job still succeeds and
the deterministic parser assigns that sample zero. Dataset-integrity failures,
provider transport failures, runtime misconfiguration, and timeouts are platform
failures: they produce no score and never enter the leaderboard.

Platform failures use `platform-failure-v1`. `PROVIDER_TIMEOUT` and
`PROVIDER_TRANSPORT` are eligible for retry only after termination of the prior
request is confirmed. `DATASET_INTEGRITY`, `MODEL_IDENTITY_MISMATCH`,
`RUNTIME_MISCONFIGURATION`, `WORKER_CRASH`, and `JOB_DEADLINE` are not retryable
in the conservative MVP.
The owner failure DTO contains only `failure_contract_version`, `code`, and
`retryable`; no exception message is exposed.

No per-sample model-output retry is allowed. A transient infrastructure failure
may restart the complete idempotent job once under the same submission ID and
remaining deadline, but only after the prior model request is known to have
terminated. The worker must never create a second leaderboard result for the
retry.

Submission creation requires an idempotency key. Reusing the same key with the
same user and body returns the original submission; reusing it with different
content is a conflict.

The database enforces unique `(user_id, idempotency_key)` and unique
`result.submission_id` constraints. The request hash covers the challenge,
contract version, and exact UTF-8 prompt bytes. Quota check, submission insert,
and an outbox record are one transaction. Queue delivery is at-least-once, so a
worker must claim a fenced lease with an attempt number; a stale worker cannot
write after a newer attempt owns the lease.

## Authentication and access

User identity comes from a server-verified authentication subject, never a
client-supplied `user_id`. Submission and owner-result endpoints verify ownership
on every request. Public leaderboards use a non-email public handle and never
expose an authentication subject or email address.

A `draft` challenge rejects external submissions. Activation also fails unless a
source release or commit, source-file SHA-256 values, attribution requirements,
share-alike requirements, and underlying-text rights review are all recorded.
Tests and local development may use an explicit development override; production
startup fails closed if that override is set.

## Persistence and feedback

The initial integration slice uses SQLite and a queue interface with an in-memory
test implementation. PostgreSQL and Redis are deployment targets after the Mock
end-to-end integration test passes.

The database stores the student prompt for its owner, its SHA-256 identity, the
immutable evaluation-contract snapshot, status timestamps, safe failure code,
and aggregate result. Prompts and raw model responses must not appear in logs or
leaderboards. Raw per-sample responses are not persisted in the teaching MVP.

Owner results and public leaderboard rows use separate allowlists in
`config/mvp_evaluation.json`. The prompt SHA-256 is owner-only. Neither view
contains sample IDs, model inputs, raw responses, per-sample outcomes, trusted
denominators, or gold labels. Exception messages are never returned; owners see
only a versioned safe platform-failure code.

API, proxy, telemetry, and vLLM logging use structured field allowlists with
request/response body capture disabled. The prompt, idempotency key, raw model
content, and authentication subject are never log fields.

## Leaderboard identity

A leaderboard partition is the SHA-256 of canonical JSON for the complete
`evaluation_identity` object in `config/mvp_evaluation.json`. It includes the
contract version, challenge and integrity hashes, model and runtime identity,
generation settings, Prompt Envelope, response schema, scorer, and aggregation
versions. Canonicalization is versioned as `python-json-v1`: the authoritative
backend serializes the parsed object with Python `json.dumps`, `sort_keys=True`,
`ensure_ascii=False`, `allow_nan=False`, and `separators=(",", ":")`, then encodes
the result as UTF-8. This definition intentionally preserves parsed Python float
forms such as `0.0`; non-Python implementations must reproduce these bytes, while
API clients consume rather than calculate the server digest. The accepted
`mvp-evaluation-v1` identity is
`ab2692baf0ef6caacdcb852c00cef098b5a9c860121b5d1159b59d0f599d3825`;
CI recomputes it from the configuration.

The best score per user is shown. Equal scores are ordered by the earliest
successful submission time; output-validity counts are displayed but are not an
undeclared tie-break metric.

## Consequences

- Backend development can proceed without pretending that current data are
  secret.
- Long-running inference is isolated from HTTP request handling.
- Scores cannot silently mix after a model, challenge, or evaluator update.
- Strict competitions remain blocked on offline human-authored, independently
  double-annotated private data and backend access controls.
- External activation remains blocked until the exact local UD release or source
  commit, file hashes, attribution/share-alike requirements, and underlying-text
  rights scope are recorded alongside the CC BY-SA 4.0 annotation license. File
  hashes use records containing a repository-relative POSIX path and a lowercase
  64-character SHA-256 value.

## Runtime-Attested Successor

`mvp-evaluation-v1` remains frozen so its baseline evidence and leaderboard
partition stay interpretable. `config/mvp_evaluation_v2.json` is its successor for
the online Qwen worker. It adds the pinned tokenizer repository/revision,
`tokenizer_config.json` hash, `tokenizer.json` hash, chat-template hash, and exact
counting method to the evaluation identity. Its identity SHA-256 is
`97af30df18b531c1eecdbf6a22f3a7983c8c93eb48e338917d8fd10a9e55483d`.

The v2 worker verifies the local tokenizer snapshot and injected runtime evidence
before consuming a job; it preflights every rendered sample, bounds provider
response bodies, and clamps every request to the remaining shared deadline. An
ambiguous HTTP timeout or disconnect is terminal, even though its failure code is
normally retryable, because this MVP has no server-side request cancellation
acknowledgment. A real deployment must still provide a trusted worker process
entry point and co-located vLLM attestation before v2 scores are enabled.
