# Authentication

## Scope and Implementation

The canonical checkout is `D:\MyWebsite\Online Linguistic Judge`. Its current
FastAPI app and packaged same-origin browser client support email-first accounts,
explicit verification, password login/reset, cookie sessions, logout, and
role-gated administration of teaching for loaded challenges.
This describes code, not a production deployment or a fresh test-suite result.

| File | Responsibility |
| --- | --- |
| `src/linguistic_oj/auth.py` | Validation, password hashing, sessions, action links, throttling, and trust boundaries. |
| `src/linguistic_oj/auth_routes.py` | Strict JSON routes, cookie settings, Host/Origin/CSRF and expected-user middleware. |
| `src/linguistic_oj/auth_store.py` | Shared SQLite/PostgreSQL account persistence, schema v3 auth additions retained within current v4, and trusted role updates. |
| `src/linguistic_oj/admin.py`, `admin_store.py` | Cookie/admin routes, schema v4 teaching/admission state and audit, in-transaction authorization. |
| `src/linguistic_oj/auth_config.py` | Protected production configuration and certificate-verified SMTP. |
| `src/linguistic_oj/auth_mail.py` | Bounded, nondurable production mail queue. |
| `src/linguistic_oj/local_dev.py` | Explicit loopback-only Mock composition, isolated seeds and captured mail. |
| `src/linguistic_oj/qwen_api.py` | Production composition requiring the auth configuration file. |
| `src/linguistic_oj/web/assets/account.js`, `admin.js` | Account/admin UI, fragment handling, role invalidation, and cross-tab session revalidation. |

## Account Lifecycle

All paths below are relative to the app's same origin. POST bodies are strict
JSON objects with exactly the listed string fields; logout uses `{}`. Duplicate
keys, extra fields, and non-string values are rejected. Auth bodies are bounded
to 8192 bytes, including streamed requests.

| Method and path | Body fields | Success |
| --- | --- | --- |
| `GET /v1/auth/config` | None | Enabled account mode and mail-delivery mode. |
| `GET /v1/auth/session` | None | Public user and session expiry; 401 if invalid/expired. |
| `POST /v1/auth/register` | `email` | `202 {"status":"accepted"}`; request verification mail. |
| `POST /v1/auth/verify-email` | `token`, `password`, `public_handle` | `200 {"status":"verified"}`; create account, not a login session. |
| `POST /v1/auth/login` | `email`, `password` | Public user, expiry, and HttpOnly session cookie. |
| `POST /v1/auth/logout` | None (`{}`) | `200 {"status":"signed_out"}`; revoke current session and clear cookie. |
| `POST /v1/auth/password-reset/request` | `email` | `202 {"status":"accepted"}`; request reset mail. |
| `POST /v1/auth/password-reset/confirm` | `token`, `password` | `200 {"status":"password_updated"}`; invalidate all account sessions and clear this browser cookie. |

Registration initially takes only an email address. The account is created only
after a valid verification token, password, and available public handle are
submitted. Link navigation/GET does not consume the token or log the user in.
Verification and reset both require an explicit subsequent login.

- Email is trimmed, casefolded, and validated, with a 254-byte bound. No
  provider-specific dot removal or plus-tag alias rewriting is performed.
- Passwords require 8-128 Unicode characters and at most 512 UTF-8 bytes. They
  are not silently trimmed/normalized. Argon2id stores password hashes, with
  process-wide hashing concurrency bounded to four operations.
  The eight-character minimum is the owner's usability choice for this version;
  letters and digits are accepted without requiring a mixture, and symbols and
  Unicode remain supported for existing accounts. The UI describes ordinary
  characters rather than exposing encoding limits. This is not a claim of NIST
  password-policy compliance: current [OWASP guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#implement-proper-password-strength-controls)
  recommends at least 15 characters without multi-factor authentication.
- Public handles require 3-32 characters, no `@`, no leading/trailing whitespace,
  and no Unicode control/category-C characters. They are unique display names,
  not proof of identity. Public user payloads contain `user_id`, `public_handle`,
  and `role`, not email or password hashes.
- New registrations have role `user`. `set_account_role(user_id, role)` is a trusted
  server operation using the auth transaction lock, not a public role-elevation
  endpoint or an admin-page action. `admin` does not bypass owner-only
  submission/history/result access; another owner's record returns 404.
- Verification links expire after 30 minutes; reset links after 15 minutes.
  Tokens are single-use. Issuing a new link supersedes the previous link for the
  same email/purpose; completing an action removes that email's action tokens.
- Reset preserves user ID, handle, role, and owned submissions, increments the
  credential version, and revokes all sessions. An in-flight login that checked
  the old password cannot mint a new session after the reset transaction.

## Cookies and Same-Origin Protection

Sessions last at most eight hours, with expiry checked against database time.
Login rotates the presented session. Only SHA-256 digests of opaque random session
and action tokens are stored, alongside the password hashes. Persistent database
sessions can survive a server restart but remain subject to expiry/revocation.

| Mode | Cookie | Flags |
| --- | --- | --- |
| Local development | `loj_local_session` | HttpOnly, `SameSite=Strict`, `Path=/`, HTTP loopback only. |
| Production | `__Host-loj_session` | HttpOnly, Secure, `SameSite=Lax`, `Path=/`, no Domain. |

The backend authenticates these cookies only. An Authorization/Bearer header never
overrides a cookie account or supplies a fallback; malformed or duplicate session
cookies fail closed. The frontend does not persist credentials in localStorage or
sessionStorage. Its existing legacy Bearer UI is available only if an older
deployment explicitly returns 404 from `/v1/auth/config`; errors, disabled/malformed
config, and network failures do not activate it in the current composition.

Mutating requests to auth/private/admin routes require exactly the configured `Origin`,
`Content-Type: application/json`, and `X-LOJ-CSRF: 1`. The marker is a same-origin
cross-site request forgery (CSRF) defense, not an authentication credential.
Auth and private responses use `Cache-Control: no-store`, `Pragma: no-cache`, and
`Referrer-Policy: no-referrer`. Email action tokens use URL fragments, not query
strings sent in HTTP requests; the frontend removes the token fragment from the
address bar before fetching and requires an explicit confirmation POST.

The browser snapshots the displayed account's `user_id` into
`X-LOJ-Expected-User` for private reads/writes. The backend compares a supplied
header exactly to the authenticated cookie account; **`POST /v1/submissions` and
all unsafe `/v1/admin/*` methods require it**. Missing required or
mismatching/duplicate values return `409 AUTH_ACCOUNT_CHANGED`, before the mutation.
Submission idempotency and admin revision requirements still apply. Never retry
Alice's old prompt or admin draft under Bob's new cookie.

Tabs in one browser profile share cookies. `BroadcastChannel("loj-account-v1")`
sends only `{type: "auth-changed"}` notifications, not identities or credentials,
after login/logout/reset attempts, including ambiguous failures. Receiving tabs
revalidate with the backend; focus, visibility, and restored-page events also
trigger revalidation. Account changes clear private results, history, and drafts,
and late old-account responses are ignored. This improves tab synchronization;
the backend expected-user guard remains necessary when notifications are missed.
Admin controls also suspend during session revalidation, discard late responses
after an account/role change, and block after permission loss. These UI controls
do not replace current database authorization.

## Current Administrator Access

The `#admin` view is for an authenticated session whose current role is `admin`.
It manages teaching drafts, publication, and new-submission switches for existing
loaded challenge IDs. There are no arbitrary path/file/dataset/model/scorer edits,
source-rights changes, account-role UI, or physical runtime/deployment controls.
Published content is public plain text; drafts and revision audit data are not
student read fields. See [Teaching Administration](ADMINISTRATION.md) and
[API Contract](API_CONTRACT.md) for exact routes, fields, and revision handling.

Admin reads and writes resolve the cookie token hash to **current database user ID,
role, and unexpired session inside the auth-locked transaction**. Neither a cached
`Principal`, the role shown on an old page, nor `X-User-Role`/Bearer headers grants
permission. A current ordinary user gets `403 ADMIN_REQUIRED`; invalid/expired
sessions get `401 AUTH_INVALID_TOKEN`; a stale expected user gets
`409 AUTH_ACCOUNT_CHANGED`.

For save/check/publish/admissions, authorization occurs before the task lock and
again **after acquiring it and refreshing database time**. Thus expiry during a
lock wait is rejected before even a check result or state/audit write. PostgreSQL
uses the shared database-wide auth advisory lock plus the per-task transaction
lock; SQLite serializes through `BEGIN IMMEDIATE`. Role demotion, logout, and reset
use the same auth lock, so a request that passed HTTP middleware can still be
denied when it reaches the store. A mutation already committed before revocation
is not retroactively reversed. The audit operator ID is database-derived.

Admin changes and audit insertion are atomic and revision-checked. Closing new
admissions uses the same task fence as submission persistence, **after existing
idempotency replay/conflict detection**, preserving accepted work/history/results.
Publishing teaching neither changes evaluation permissions nor silently augments
the student's model input. Resume cannot bypass existing rights/policy/runtime
gates. An admin role still never bypasses another owner's private result boundary.

## Local Development Boundary

From the root, use `& .\scripts\start_local_dev.ps1` and open
**http://127.0.0.1:8080**. If PowerShell blocks the script, use
`./.venv/Scripts/python.exe -m linguistic_oj.local_dev --root .` without changing
execution policy. Install `.[api,dev]` in the project `.venv` first. No PostgreSQL
server, Redis server, GPU, or SMTP service is needed for this Mock composition.
`localhost` sends a different Host and is rejected by the launcher's exact
all-route HTTP/Host guard; the canonical address is not interchangeable with it.

The local launcher seeds only its isolated database (by default, under ignored
`runtime/local-development`): `alice@example.test` / `LocalAlice`,
`bob@example.test` / `LocalBob`, and `admin@example.test` / `LocalAdmin`.
The common **initial** password is
`Local-only-passphrase-2026!`. Existing accounts and changed passwords are not
overwritten on restart. The testing panel still displays the initial password
after reset, so use the password actually chosen. These credentials must never
be provisioned as production accounts.

`LocalAdmin` starts with role `admin`; after login, open
**http://127.0.0.1:8080/#admin**. Existing role changes are not undone by local seed
provisioning. There is no seed-credential restore or web-based self-promotion route;
the normal single-use-email-token password reset flow remains available.

Local mail is captured synchronously, not sent to SMTP. The development inbox is
bounded to 100 messages in memory and clears on restart; credentials and action
token hashes do not. The panel exposes captured verification/reset links to local
testers, so it must remain isolated and loopback-only. Do not expose or proxy this
launcher to the LAN or Internet. The five two-sample handwritten Mock challenges
and their local 1000-per-user-per-challenge-per-24-hour testing budget are separate
from real Qwen evaluations and the unchanged real 5-per-24-hour contract limit.

See [Local Development](LOCAL_DEVELOPMENT.md) for flow checks, state locking,
queued/running restart behavior, and separate fixture/real-Edge test commands.

## Production Configuration

`qwen_api` requires `--auth-config-file` in production and rejects callback auth.
Configure exactly one of `auth_config_file` or `authenticate`; the latter is only
for explicit development/test composition. Production also requires PostgreSQL,
Redis, and separately configured/attested Qwen workers, not local seed accounts.

The protected JSON shape is illustrated below, **not a deployed configuration**:

```json
{
  "public_origin": "https://judge.example.test",
  "trusted_proxies": [],
  "smtp": {
    "host": "smtp.example.test",
    "port": 587,
    "sender": "judge@example.test",
    "username": "judge@example.test",
    "password_file": "smtp-password.txt",
    "security": "starttls"
  }
}
```

Use the actual canonical root HTTPS origin, with no path, trailing slash, userinfo,
query, or fragment. `smtp` requires exactly those keys; `security` is `ssl` or
`starttls`. Both modes verify certificates, and authentication occurs only after
TLS is established; no plaintext fallback exists. `password_file` is absolute or
relative to the config file's directory. Keep both files in protected operator
storage, out of Git, browser assets, command-line secrets, and public logs.

Both files must be regular, non-symlink files of at most 16 KiB. On POSIX they must
be owned by the process user or root, with no group/other permissions (for example,
0600). Windows ACL checks allow only the current user, SYSTEM, and Administrators,
including ownership and inherited allow rules; failed inspection rejects startup.
Do not weaken these checks to make a deployment start.

If a reverse proxy is used, replace `trusted_proxies: []` only with the **exact
actual IP addresses of the connecting proxy peers**. Do not use hostnames, CIDRs,
wildcards, guessed loopback trust, or client-controlled forwarded addresses.
The proxy must **overwrite `X-LOJ-Client-IP`** with one validated actual client IP,
not append or preserve incoming values. A trusted peer with a missing, duplicate,
or invalid value is rejected. Untrusted peers cannot set their effective address
using this header. `Forwarded` and `X-Forwarded-*` remain ignored even for trusted
peers, and the entry point disables Uvicorn proxy-header processing. Restrict
backend ingress and preserve same-origin browser requests through the HTTPS proxy.

Auth throttles use database-persisted 15-minute operation/identity and coarse
client-network buckets (IPv4 /24 or IPv6 /64). Mail requests allow five attempts
per identity and 30 per network bucket; login/confirmation allow 10 and 100.
`429 AUTH_RATE_LIMITED` includes `Retry-After: 900`. Restarting does not clear these
buckets. These controls are separate from the submission contract's daily budget.

## Production Email Delivery

Production uses **one background thread and a queue of at most 64 pending mail
requests**, plus any in-flight job. Eligibility checks, token issuance, and SMTP
delivery occur in that worker, outside the HTTP response. All valid, non-throttled
requests admitted to the queue return the same `202 {"status":"accepted"}`,
whether an email is eligible for that operation or a later SMTP attempt fails.
Acceptance is not a promise that mail was delivered or that an account exists.
Invalid requests, rate limits, and a full/stopped/unavailable queue still return
their appropriate errors; queue unavailability is a generic 503 for every email.

This queue is **process-local and nondurable**, with no automatic durable retry.
Shutdown discards pending work and waits up to 30 seconds for an in-flight job;
crashes can also lose accepted work. Monitor sanitized
`auth_mail_delivery_failed` and `auth_mail_pending_discarded_on_shutdown` events,
investigate delivery/queue readiness, and let the user request a new link after
recovery. Do not log recipients, links, tokens, passwords, or raw SMTP exceptions.
Local capture must never become a production fallback. SMTP health checks cover
connectivity, TLS, login, and NOOP, not guaranteed delivery to a recipient's inbox.

## Schema v4 and Existing Users

SQLite and PostgreSQL schema code now targets **v4**. Version 3 appended roles,
`auth_credentials`, `auth_sessions`, `auth_action_tokens`, and `auth_rate_limits`
to the existing v2 schema. Existing user IDs, subjects, handles, submissions, and
results are preserved. Old users receive the default `user` role, but **no email
credential binding is fabricated**. Version 4 appends `challenge_admin_state` and
`challenge_admin_revisions` for teaching snapshots, admission state, and operator
audits, without rewriting v3 credentials/sessions or old submission/outbox/result
rows. The migration version sequence is `1, 2, 3, 4`.

SQLite's `SubmissionStore` applies missing supported migrations on construction.
PostgreSQL migration is explicit through `migrate_postgres`; store construction
and health checks do not silently migrate it. Back up and rehearse against a
dedicated database before a separately approved production migration.

Production auth startup/readiness rejects any legacy user with no credential
binding. A trusted account-enrollment procedure, or a deliberately isolated new
database, is required before serving. Such enrollment must establish ownership;
there is no nickname-based auto-linking or public migration/enrollment shortcut.
Do not delete old users or attach historical scores to a newly registered matching
handle. A schema upgrade alone is not trusted enrollment.

The 2026-09-07 isolated server acceptance applied the actual v1-v4 migration SQL
only to one freshly created random PostgreSQL schema, then exercised cookie auth,
admin operations, outbox/Redis/Qwen scoring, and logout with old-cookie replay
rejection. The report `runtime/qwen-admin-acceptance-20260907.json` has
`passed: true` and confirms removal of all 11 temporary tables and that schema.
This does not migrate the existing production application tables, enroll legacy
users, or select a new `app/current` release.

The successful run was `HTTP-inprocess-TestClient`, not browser verification;
email used development memory capture, not SMTP, and there was no HTTPS ingress
acceptance. Two synthetic samples/six gold items and score `1.0` establish basic
integration only. The full opt-in PostgreSQL concurrency/migration test matrix,
true email delivery, and production legacy binding remain separate gates. The
existing full GPU stack was not moved or reconfigured; historical benchmark scores
and old server test totals are not new auth/admin evidence.

## Verification References

Inspect `tests/test_auth.py`, `tests/test_auth_config.py`, `tests/test_auth_mail.py`,
`tests/test_auth_postgres.py`, `tests/test_admin.py`, `tests/test_admin_store.py`,
`tests/test_local_dev.py`, and the store/Qwen API
migration tests for executable assertions. PostgreSQL auth tests are opt-in via
`POSTGRES_TEST_DATABASE_URL` and create an isolated test schema; never use a live
database. Admin PostgreSQL parameters separately use `LOJ_ADMIN_POSTGRES_TEST_URL`;
skipped parameters cannot be counted as successful real concurrency tests. Browser
contract fixtures are not backend evidence; `scripts/check_local_browser.py` and
`scripts/check_admin_browser.py` launch isolated real local Mock apps and installed
Edge. Final main regression totals must come from the current run. See
[Operations](OPERATIONS.md) for the remaining deployment gates.
