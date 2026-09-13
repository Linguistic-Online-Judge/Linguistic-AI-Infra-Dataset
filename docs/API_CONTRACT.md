# V1 API Contract

## Errors

Every non-success response uses one machine-readable envelope:

```json
{
  "error": {
    "code": "RESULT_NOT_READY",
    "message": "Result is not ready",
    "details": {}
  }
}
```

Clients must branch on `error.code`, not on the human-readable message. Request
validation errors expose only safe issue locations/types or a fixed message;
submitted prompts and teaching drafts are never echoed in errors.

## Current Authentication

The local launcher and current production `qwen_api` use same-origin, server-issued
HttpOnly **cookie sessions**, not user-entered Bearer tokens. The local cookie is
`loj_local_session`; production uses Secure `__Host-loj_session`. Authentication
routes and account lifecycle are documented in [Authentication](AUTHENTICATION.md).

- `GET /v1/auth/config` declares enabled account mode and local/SMTP mail delivery.
- `GET /v1/auth/session` returns `{user, expires_at}`; `user` contains `user_id`,
  `public_handle`, and the current database `role`, not email/password hashes.
- `GET /v1/users/me` retains its narrower `{user_id, public_handle}` response. Use
  the session response, not an invented field on `/users/me`, for role display.
- Private routes require a valid cookie. Missing, revoked, malformed, duplicate,
  or expired session cookies return `401 AUTH_INVALID_TOKEN` in this composition.
  An Authorization header neither authenticates nor overrides a cookie account.
- Unsafe auth/private/admin methods require the exact configured `Origin`,
  `Content-Type: application/json`, and `X-LOJ-CSRF: 1`; violations return
  `403 AUTH_CSRF_REJECTED`. The marker is not an authentication credential.
- `POST /v1/submissions` and all unsafe `/v1/admin/*` methods also require
  `X-LOJ-Expected-User` equal to the current cookie user's ID. Missing required,
  duplicate, or mismatching values return `409 AUTH_ACCOUNT_CHANGED`. Private
  reads validate it when supplied. Never retry one user's content as another user.
- Auth/private/admin responses, including errors, use `Cache-Control: no-store`,
  `Pragma: no-cache`, and `Referrer-Policy: no-referrer`.

**Legacy distinction:** the lower-level callback-based `create_app` still declares
`BearerAuth` in OpenAPI and can emit `AUTHENTICATION_REQUIRED` with
`WWW-Authenticate: Bearer` when its injected verifier rejects a principal. That
residual schema declaration is not the current cookie middleware's authentication
contract. Callback-only test/older deployments are distinct from current production,
which forbids callback auth. The browser's legacy Bearer UI activates only when an
older server returns 404 for `/v1/auth/config`, never on a current auth error,
malformed/disabled configuration, or network failure. It offers no admin access.

## Challenge Availability

`GET /v1/challenges` returns a list of loaded catalog entries, and
`GET /v1/challenges/{challenge_id}` returns one entry. Availability now distinguishes
four fields:

- `submission_enabled`: the immutable contract and activation policy permit use.
- `runtime_available`: the deployment has explicitly configured a worker runtime.
- `admissions_closed`: new submissions are paused administratively, or an existing
  admin state no longer matches the loaded source descriptor; false for catalog-only
  entries without an evaluation contract.
- `accepting_submissions`: `submission_enabled && runtime_available && !admissions_closed`.

`submission_enabled` can include an explicit development/test draft override;
production forbids that override. `runtime_available` is configuration, not a
per-request GPU probe. Clearing a pause cannot change either underlying condition.
The descriptor's title, status, source/license fields, dataset/selection hashes,
model identity, and scoring versions remain immutable through administration.

Executable challenge responses also publish `student_prompt_utf8_bytes` so clients
can count UTF-8 bytes and prevent an oversized prompt before submission. Catalog
entries without an evaluation contract expose this field as `null`.

An unavailable configured runtime returns
`503 CHALLENGE_RUNTIME_UNAVAILABLE` before a submission is persisted. Global
readiness covers API dependencies but does not imply that every challenge runtime
is available.

## Published Teaching Read

`GET /v1/challenges/{challenge_id}/teaching` is anonymous and returns exactly
`{challenge_id, published_revision, content}`. `content` is either `null` or the
five-field teaching object below. No publication, or a source-mismatched old
publication, returns `published_revision: 0` and `content: null`. Unknown IDs return
`404 CHALLENGE_NOT_FOUND`; storage failure returns a safe `503 SERVICE_NOT_READY`,
not a false empty lesson. Responses/errors are `no-store`; successful JSON also
uses `X-Content-Type-Options: nosniff`.

Drafts, operator identities, and revision history are never returned here or in
the public catalog. Published content is plain text, not HTML. The student browser
loads a selected template only on an explicit click, with confirmation before
replacing an existing prompt. It does not silently append teaching instructions to
model input or change the fixed response schema/scorer. A failed teaching read
blocks template loading until a successful refresh.

## Owner Prompt Read

`GET /v1/submissions/{submission_id}/prompt` requires the submission owner's
authenticated account. The browser always supplies `X-LOJ-Expected-User`.
Success returns exactly:

```json
{
  "submission_id": "submission-id",
  "challenge_id": "challenge-id",
  "student_prompt": "The original, unmodified prompt text.",
  "student_prompt_sha256": "<64 lowercase hexadecimal characters>"
}
```

The text is decoded from the existing stored UTF-8 bytes, preserving whitespace
and Unicode. The route reads no dataset answers or raw model responses, creates
no submission, and changes no schema or scoring contract. It is available for
queued and terminal submissions alike. Both stores select by submission ID **and**
owner ID. A missing or other-owned ID returns the same `404 SUBMISSION_NOT_FOUND`;
the admin role grants no additional access. Successful reads use `Cache-Control:
no-store` and `X-Content-Type-Options: nosniff`; current auth middleware also applies
the standard private-response headers. Public leaderboards and history summaries
continue to exclude prompt text.

## Administration

All `/v1/admin/*` routes require cookie authentication and current database role
`admin`. A valid non-admin gets `403 ADMIN_REQUIRED`. Existing owner-only access
does not change: administrators cannot read another user's private results.
Only server-loaded challenge IDs can be managed; there are no challenge creation,
file/dataset/model/scorer editing, rights approval, role assignment, or runtime
activation endpoints. See [Teaching Administration](ADMINISTRATION.md).

| Method and path | Strict request body | Success |
| --- | --- | --- |
| `GET /v1/admin/challenges` | None | `200 {items: [...]}`; loaded tasks, no pagination. |
| `GET /v1/admin/challenges/{challenge_id}` | None | `200` admin detail. |
| `PUT /v1/admin/challenges/{challenge_id}/teaching-draft` | `expected_revision`, `content` | `200` detail with saved draft and incremented revision. |
| `POST /v1/admin/challenges/{challenge_id}/check` | `expected_revision` | `200 {can_publish, issues, revision}`; no mutation. |
| `POST /v1/admin/challenges/{challenge_id}/publish` | `expected_revision` | `200` detail with published saved draft and incremented revision. |
| `POST /v1/admin/challenges/{challenge_id}/admissions` | `expected_revision`, `closed` | `200` detail with incremented revision; `closed` must be a boolean. |

List items contain exactly `challenge_id`, `title`, `language`, `treebank`, `task`,
`has_contract`, `admissions_closed`, `revision`, `published_revision`, and
`can_reopen`. Detail contains `challenge` (the current public challenge response),
`revision`, `draft`, `published`, `published_revision`, `admissions_closed`, and
`can_reopen`. Draft and published values are independently nullable teaching
objects. A catalog-only entry may publish teaching but cannot change admissions.

The teaching object requires all five string fields, with no extra fields:

| Field | Character range |
| --- | --- |
| `title` | 1-120 |
| `summary` | 0-500 |
| `instructions` | 1-4000 |
| `zero_shot_prompt` | 1-3000 |
| `few_shot_prompt` | 1-3000 |

Required nonempty fields cannot be whitespace-only. Newline is allowed; other
Unicode control/format/surrogate characters are not. Admin request bodies are
bounded to 16,384 UTF-8 bytes, including streamed requests. Duplicate JSON keys,
extra fields, or coercible-but-wrong types return `422 REQUEST_VALIDATION_ERROR`;
oversize bodies return `413 REQUEST_BODY_TOO_LARGE`. Character limits do not
replace this byte limit or a submission's separate prompt budget.

`expected_revision` is a strict integer in `0..9223372036854775806`, starting at
`0` for untouched tasks. Every state-changing action increments the current
revision and appends an audit row in one transaction. Publication also records
that revision as `published_revision`. Check validates the saved draft/source
binding but neither increments nor audits; it is not a rights approval, benchmark,
or runtime health check. Publish revalidates rather than trusting an earlier check.

| Error | Meaning |
| --- | --- |
| `409 ADMIN_REVISION_CONFLICT` | Reload and reconcile; expected revision is stale. |
| `409 ADMIN_SOURCE_CHANGED` | Loaded descriptor differs from stored source binding; review and save a current draft before publishing/reopening. |
| `409 ADMIN_DRAFT_REQUIRED` / `ADMIN_DRAFT_INVALID` | Publication has no valid saved draft; check instead returns these issues with `can_publish: false`. |
| `409 CHALLENGE_NOT_OPEN` | No evaluation contract, or existing policy/runtime disallows resume. |
| `401 AUTH_INVALID_TOKEN` / `403 ADMIN_REQUIRED` | Current session or database role no longer authorizes the operation. |
| `409 AUTH_ACCOUNT_CHANGED` | Expected browser account does not match the cookie account. |

Admin identity, role, and session expiry are checked in the store's auth-locked
transaction. After waiting for the shared task lock, database time is refreshed
and authorization checked again, so an expired waiting session cannot write or
check. Stale page role labels and forged headers are not authorization. No admin
audit read/rollback route is implemented. Admin mutations use revision CAS, not
submission idempotency; uncertain responses require explicit reload, not blind retry.

## Submission Acceptance

`POST /v1/submissions` requires the cookie/Origin/CSRF/expected-user guards above
and an `Idempotency-Key`. Missing and malformed keys
both return `422 INVALID_IDEMPOTENCY_KEY`. A `202` response means the submission
and transactional outbox record are durably persisted. It does not promise that
the immediate Redis publish succeeded; background recovery republishes an
unpublished live job. Retrying the same request and key returns the same
submission ID. Keys are scoped to the account; a different account using the same
key does not own or replay the first account's submission.

The admin pause is enforced atomically in both stores **after** idempotency
replay/conflict detection, and before quotas or new submission/outbox insertion.
A paused new request returns `409 CHALLENGE_PAUSED` with no persisted submission.
An identical previously accepted request still replays its ID; changed content
under that key still returns `409 IDEMPOTENCY_CONFLICT`. Existing auth, immutable
contract, and runtime checks still precede the store, so this is specifically an
admin-pause replay guarantee, not permission to bypass every availability gate.

Pause and admission share a transaction lock even before the first admin state
row exists. Already admitted work can finish before a concurrent pause commits;
later new work is blocked. Queued/running jobs continue under normal deadline and
worker rules. History, results, outbox recovery, and leaderboard identities are
retained, not deleted or canceled. Resume only clears the switch under the existing
source/contract/policy/runtime conditions; teaching publication cannot activate it.

POST and detail responses include the immutable `evaluation_identity_sha256`, so
the client can link directly to the correct leaderboard without consulting the
current challenge version.

Quota errors include `limit` and `current` in `error.details`. A rolling 24-hour
`SUBMISSION_RATE_LIMIT` also includes `retry_after_seconds` and the HTTP
`Retry-After` header. `OUTSTANDING_SUBMISSION_LIMIT` depends on work completion and
therefore has no invented reset time. `GLOBAL_QUEUE_FULL` is a `503` capacity
response.

Queued submissions are deadline-swept independently of Redis delivery. Expired
queued work becomes a terminal `JOB_DEADLINE` failure and is no longer eligible
for outbox publication or outstanding-capacity accounting.

## Results

`GET /v1/submissions/{submission_id}/result` is discriminated by `outcome`:

- `succeeded`: additionally discriminated by `task`; metrics have exact schemas
  for segmentation, UPOS, XPOS, dependency, and transliteration.
- `failed`: contains a finite platform failure code, contract version, and
  retryability flag.
- `rejected`: currently reports `TOKEN_LIMIT_EXCEEDED` as a safe terminal outcome.

Queued and running work returns `409 RESULT_NOT_READY`. Owner scoping returns the
same `404 SUBMISSION_NOT_FOUND` for absent and other-user submission IDs.

## Pagination

Submission history and leaderboards return `{ "items": [...], "next_cursor":
... }`. Limits are bounded to `1..100`; cursors are opaque, versioned, and bound to
their query. Leaderboard cursors also carry an `as_of` boundary so entries created
after the first page do not shift subsequent pages. A syntactically valid but
unknown evaluation identity returns `404 LEADERBOARD_NOT_FOUND`.
