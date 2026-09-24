# V1 Student Frontend

## Product boundary

The root route serves separate catalog, practice and result pages from the same FastAPI origin
as the V1 API. It supports the complete browser flow without exposing dataset
answers or private manifests:

- search and filter the public challenge catalog;
- inspect task, model, metric, provenance, and availability metadata;
- read Chinese-first task guidance, public handwritten examples, output schemas,
  and editable zero/few-shot templates for all five tasks;
- enter a prompt with an exact UTF-8 byte count and per-task in-memory drafts;
- register by email, verify, log in, log out, and reset a password using the
  same-origin cookie account service;
- submit an idempotent run and follow queued, running, succeeded, failed, or
  rejected outcomes;
- browse owner-scoped run history, read the exact saved prompt and reuse it for editing; and
- page through the public leaderboard for the exact evaluation identity.

The browser displays percentages only for finite numeric metric values in the
closed interval from zero to one. It does not invent counts, ranks, availability,
or scores. The API remains the source of truth for validation and scoring.

Handwritten teaching material lives in `assets/teaching.js`, not in an answer
endpoint. It is illustrative, not a validated best prompt or a claim that the
platform verifies model reasoning. XPOS examples use German HDT/STTS; Chinese
transliteration examples use the GSDSimp Pinyin convention. Segmentation scoring
matches complete token spans, not just individual boundaries.

## Delivery

The client remains plain semantic HTML, CSS, and JavaScript under
`src/linguistic_oj/web`. It has no Node runtime or frontend package dependency.
Setuptools includes these files in wheels, and FastAPI serves them at `/` and
`/assets/*`. Hash navigation keeps deployment independent of reverse-proxy route
fallbacks.

The approved visual system uses light surfaces, original option A (folded L),
the full two-line name and Fudan blue `#0e419c`. `app-shell.css` styles the working
application and shares `brand-palette.css` with the standalone design preview.
The former `app.css` is no longer loaded by the root document.

- `#challenges`: API-backed catalog, search, language/type/availability filters.
- `#challenge/<id>`: task instructions/examples/scoring tabs and a prompt editor.
- `#result/<submission_id>`: queued/running/terminal result, saved prompt and reuse.
- `#runs`: owner history with pagination.
- `#leaderboard/<evaluation_identity>`: the selected evaluation partition.
- `#admin`: existing teaching management, retaining its role and revision guards.

The catalog and leaderboard no longer surround the editor. Development tools are
collapsed in the footer. Mobile pages stack vertically, and the full wordmark
remains visible. Route-focused headings have no outline; inputs use a subtle
one-pixel border change, while interactive keyboard targets retain focus cues.

## Account and Privacy Boundaries

- `GET /v1/auth/config` selects cookie auth. Only an explicit **404 from this
  endpoint** enables the shipped Bearer interface; 401, 5xx, network failures,
  disabled configuration, and malformed responses never enable that fallback.
- Cookie auth reads `{user, expires_at}` from `/v1/auth/session`. The role comes
  from that user, not from a guessed admin email. Legacy `/v1/users/me` needs no
  role. Cookie credentials use `credentials: "same-origin"`; all account and
  submission POSTs use JSON and `X-LOJ-CSRF: 1`. The browser sets its normal Origin.
- Login tokens are never written to local/session storage. Only the server sets
  the HttpOnly session cookie. A legacy Bearer token stays in page memory.
- `#login`, `#register`, `#forgot-password`, `#account`, `#verify-email?token=...`,
  and `#reset-password?token=...` drive the native account dialog. Mail tokens are
  read from the fragment and immediately removed with `history.replaceState`,
  before network discovery. Opening a link never consumes it; only the explicit
  form POST does. Closing the dialog clears the in-memory mail token. Verification
  does not auto-login; a successful password reset clears the displayed session.
- New passwords count Unicode code points (8..128) and UTF-8 bytes (<=512), not
  JavaScript UTF-16 units. Nicknames are 3..32 code points, without @ or control
  characters. Backend validation remains authoritative.
- A discovery outage leaves login and recovery usable but blocks protected work
  until a session is confirmed. Failed logout is not displayed as successful.
- Every protected 401 clears the displayed session, even if its error body is not
  JSON. A late 401 from an older account generation cannot clear a newer login.
- Reauthenticating the same account releases the old submission busy state and
  restarts result polling. Drafts and frozen request bodies/keys survive, so retrying
  an unconfirmed submission remains idempotent rather than creating another run.
- Every authenticated submission/history/status/result request freezes
  `X-LOJ-Expected-User` from `state.user.user_id` before sending. Caller-supplied
  values are overwritten; DOM nicknames are never used. This header is a consistency
  assertion, **not a credential**. The backend must compare it with the cookie
  account during authorization; browser notifications cannot close that race.
- A `409 AUTH_ACCOUNT_CHANGED` invalidates the local account, clears private DOM,
  account-dialog identity, drafts, history cursors and frozen attempts, and asks
  for explicit login. No retry or automatic adoption of the other cookie owner
  transfers an old prompt to a new account. Old-generation private responses are
  rejected even when the server returned success.
- Dynamic API values are assigned with `textContent`; the client does not inject
  API strings as HTML.
- The document sends a same-origin Content Security Policy, denies framing and
  MIME sniffing, disables camera/location/microphone access, and sends no referrer.
- The prompt stays owner-scoped. Public leaderboards expose only the contract's
  allowlisted handle, score, sample counts, rank, identity, and completion time.

### Shared-Cookie Tabs

`BroadcastChannel("loj-account-v1")` sends only `{type: "auth-changed"}` after
cookie-affecting login, logout and password-reset attempts, including ambiguous
failures. It sends no user ID, email, token, cookie, password or prompt. Messages
are untrusted revalidation hints, never a source of authentication or user data.

Receiving a notification, focusing a page, returning it to visible state or
restoring it from the back/forward cache triggers `GET /v1/auth/session`. An
account change or sign-out clears the old private state and requires explicit
login; a revalidation outage also fails closed. A same-account response updates
session presentation without changing `accountSequence`, interrupting a valid
submission or discarding a draft. Unsupported/unavailable broadcast delivery is
covered by visibility/focus checks plus the mandatory server-side expected-user
comparison, not a storage-token fallback.

Session reads use `authSequence` to reject responses older than an own login,
logout or invalidation. Focus/notification events during account operations are
rechecked afterward, and a notification during bootstrap restarts discovery
before a stale account is accepted. Notifications arriving during an existing
check queue a follow-up rather than being lost. Revalidation never auto-submits.

## Requests and Recovery

Catalog, history, leaderboard, and result reads have loading, empty, and retry
states. History and leaderboard pagination retain opaque cursors and suppress
concurrent duplicate page loads. Request generations prevent late success/error
responses from replacing a newer task, page, or account. Leaving a result view
stops its polling; returning or opening a history record restarts it. A history
record selects its own immutable evaluation identity, not just today's catalog
identity. History summaries still exclude prompt text. A separate owner-only
`GET /v1/submissions/<id>/prompt` returns the exact stored prompt and its SHA-256.
This read is independent of the result's scoring contract and does not expose
sample answers or model outputs. It works before or after completion, and after
reload. Prompt reads are fenced by account, page context, sequence and run ID;
logout clears the cached prompt and its DOM. An unavailable prompt read has its
own retry and never substitutes the current draft for the submitted text.

Continuing from a result explicitly copies the stored prompt into the task's
in-memory draft. Replacing a different existing draft requires confirmation.
It does not submit. A run whose challenge has left the catalog still supports
result/prompt viewing, but cannot be copied into an unavailable task editor.

A submission freezes its exact prompt/body and idempotency key. Ambiguous network
retries reuse both, even after switching away and back to the same draft. Repeating
an accepted unchanged prompt opens its original record. A separate, confirmed
"evaluate again" action deliberately creates a new request and counts as another
submission. These protections last for the page lifetime; after reload, inspect
history before resubmitting. Logout/session invalidation clears private results,
drafts, pending request references, and prevents late private responses rendering.

## Local Testing Panel

The panel is hidden unless `GET /v1/development` with `X-LOJ-Development: 1`
explicitly supplies mock development configuration. A 404 creates no panel and
does not enable any authentication behavior. Its account buttons only fill login
fields; they never auto-login or elevate a production account. Main's isolated
local launcher supplies `alice@example.test`, `bob@example.test`, and
`admin@example.test`, with the **development-only** password
`Local-only-passphrase-2026!`. Never use these credentials in production.

The explicit development panel alone displays these passwords and the in-memory
captured inbox from `/v1/development/mail` (same development header). Inbox links
must be same-origin, root-path, query-free URLs with only a `verify-email` or
`reset-password` fragment and one nonempty token. Other links are not clickable.
The mock banner is driven by development configuration or `model_identity.runtime
=== "mock"`, never by a title or fabricated model availability. Mock scores and
rankings are labelled as local workflow tests, not Qwen capability evidence.

## Accessibility

The interface uses landmarks, visible labels, keyboard focus indicators, live
status regions, textual availability states, an accessible modal, semantic result
tables, and a skip link. Tabs support arrow/Home/End keys. Short control transitions
are disabled by `prefers-reduced-motion`. The layout supports 320 CSS pixels and
does not rely on hover or color alone.

## Verification

### Current three-page integration

- Python: **547 passed, 32 skipped**; skipped database/service tests are not proof
  of PostgreSQL execution. The new PostgreSQL prompt-read implementation has
  optional coverage in the existing integration suite.
- `node --test tests/browser/contracts.test.mjs`: **8 passed**.
- `scripts/check_local_browser.py`: **32 groups passed** against a fresh real
  local SQLite/auth/queue/Mock application. Includes prompt reload/reuse, delayed
  prompt responses, exact result-partition navigation and cross-account guards.
- `scripts/check_admin_browser.py`: **16 admin groups passed** against an isolated
  real local application, in addition to its student checks.
- All 18 languages were covered by an explicitly injected **directory fixture**;
  the normal 8080 catalog remains the five handwritten local tasks. This does not
  claim another 18-language real-model evaluation.
- Screenshots: `runtime/browser-tests/local-app/`, including
  `catalog-18-fixture-1440.png`, `workbench-1440.png`, `result-pages-1440.png`,
  `result-pages-320.png`, and editor/account mobile views.

The school 8090 application serves its own source snapshot. On 2026-09-12 it was
updated to `snapshot-pages-20260911-v1`, then verified with actual 18-language
catalog browsing and two real Qwen jobs (English/Chinese, 50 samples each).
Result reload, saved prompt reuse, owner history, matching leaderboards and admin
owner-denial passed. Evidence: `runtime/browser-tests/qwen-pages-1789191380780/`.
The docs link is inside the collapsed developer panel. Later local edits still
require an explicit source synchronization; they are not deployed automatically.

The frontend itself needs no Node dependency. Syntax and contract checks require
Node 22+; the browser suite also requires an **already installed** Chromium-family
browser (Edge is detected in its usual Windows locations). Set `LOJ_BROWSER` to an
executable elsewhere. Nothing installs globally, downloads a browser, or creates
`node_modules`.

```powershell
node --check src/linguistic_oj/web/assets/app.js
node --check src/linguistic_oj/web/assets/account.js
node --check src/linguistic_oj/web/assets/teaching.js
node --check tests/browser/cdp.mjs
node --check tests/browser/run.mjs
node --check tests/browser/fixture-server.mjs
node --check tests/browser/contracts.test.mjs
node --check tests/browser/shared-cookie.mjs
node --test tests/browser/contracts.test.mjs
node tests/browser/run.mjs --fixture
```

`--fixture` uses a narrow, test-only HTTP server and synthetic contracts. It proves
browser behavior, **not backend correctness**. Run against the real local launcher
in a separate terminal once that launcher is available:

```powershell
$stateDir = "runtime/browser-tests/local-app-state-$([guid]::NewGuid().ToString('N'))"
.\.venv\Scripts\python.exe -m linguistic_oj.local_dev --root . --port 8767 --state-dir $stateDir
node tests/browser/run.mjs --url http://127.0.0.1:8767
```

The real-app suite requires explicit mock development discovery and refuses
non-loopback URLs. It creates local submissions and a uniquely named test account,
and changes that test account's password. Use an isolated development database,
not shared teaching records. The unique state directory also avoids retained
authentication rate limits across repeated runs. Launcher/state-directory options
are owned by the local-development launcher; see `LOCAL_DEVELOPMENT.md`.

Browser automation uses the Chrome DevTools Protocol through Node's WebSocket,
not Playwright or npm packages. Cases include five task/template flows, real-page
cookie login/reload, signup/verify/reset via the captured inbox, owner isolation,
post-acceptance response loss/idempotency, stale submissions/ranks/history,
pagination, failed/rejected results, read recovery, auth outages and 404-only
legacy compatibility. It also covers same-account reauthentication during a held
submission response, non-JSON private 401s, failed logout/retry, signed-in password
reset with old-session/password invalidation, and network/malformed/disabled auth
discovery. Faults and page fixtures are explicitly injected through the browser;
passing those cases does not claim real infrastructure failures were reproduced.
All browser POST headers and ambiguous-retry bodies/keys are checked.

`shared-cookie.mjs` creates two real Edge page targets in the same browser profile,
not separate isolated browser sessions. It exercises real cookie sharing, cross-tab
login/logout cleanup, delayed private results, suppressed broadcast delivery,
visibility/focus recovery, same-account in-flight submissions and session reads
overlapping login/logout/bootstrap. Wrong expected-user values must produce
`409 AUTH_ACCOUNT_CHANGED` on history, status, results and submission writes.
The stale-page submission case checks that Bob's record count does not increase
and that no retry is emitted. The fixture server implements that contract only
as an explicit synthetic fixture; the real-app run exercises AuthService instead.

Screenshots and temporary browser profiles go under the already ignored
`runtime/browser-tests/{fixtures,local-app}/`. The suite checks 1440, 768, 390,
and 320 pixel page widths, captures workbench and mobile editor/login images plus
all five expanded teaching examples at 320 pixels, checks dialog keyboard focus,
and fails on uncaught JavaScript errors. Successful
fixture checks and screenshot review do not substitute for the real-app run or
school-server Qwen/SMTP/production security validation.

### Interrupted Frontend Handoff Verification

In the canonical checkout, Node 24.17.0 and the installed headless Edge passed the
four contract tests and all 22 fixture browser groups after the two regression
fixes above. The original frontend reproduced both regression failures before
their fixes. Screenshot review retained the existing annotation-bench visual
system, including the mobile catalog tray, fixed navigation, readable teaching
examples, and 320-pixel login dialog. No stylesheet or teaching-content changes
were needed.

At that earlier checkpoint, `src/linguistic_oj/local_dev.py` was not yet present,
so real-launcher verification was pending. The later checkpoint below supersedes
that pending status. Neither frontend continuation changed backend or Python tests.

### Shared-Cookie Security Verification

On 2026-09-07, Node 24.17.0 and installed headless Edge passed five Node contract
tests and all 30 fixture browser groups. All 30 groups also passed against the
real `linguistic_oj.local_dev` application, including the shared-cookie account
comparison and rejection cases. That run used the isolated state directory
`runtime/browser-tests/local-security-1788763928056` and a temporary loopback port;
it did not touch shared local records, school-server models or production.

The launcher and Node suite ran within one parent orchestration process, and the
launcher was stopped afterward. Tool invocations must not assume a server from a
previous invocation is still running. The first real run exposed a test-only
interceptor race between an old-document revalidation and new-document bootstrap;
the test retires old listeners before intercepting the new bootstrap read, and a
fresh isolated-state rerun passed. The UI layout, styles and teaching content were
unchanged by this security fix. Mock scores remain workflow evidence only.
