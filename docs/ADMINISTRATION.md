# Teaching Administration

## Current Scope

The current phase is local development of the teaching and evaluation business
flows, not a formal public launch. The same FastAPI application serves the student
workbench and the administrator view at `/#admin`. The only real evaluation model
in scope remains the existing school-server Qwen3.5-9B.

Administration manages **existing, server-loaded `challenge_id` values only**.
It is a teaching-content and new-submission control surface, not a file manager,
dataset editor, model launcher, or production activation tool.

| Allowed | Not granted by the admin role |
| --- | --- |
| Read the loaded catalog and its fixed source/evaluation metadata. | Create arbitrary challenges, select filesystem paths, upload datasets, or hot-load a registry. |
| Save and preview a teaching draft, check it, then publish it. | Change gold answers, sample selection, response schemas, scorers, metrics, model identity, or inference settings. |
| Pause new submissions for a loaded executable challenge. | Cancel or delete accepted jobs, erase history/results, rescore records, or change leaderboard partitions. |
| Resume that admission switch if the existing contract, policy, source binding, and configured runtime permit it. | Approve source rights, change a draft evaluation contract to active, bypass an activation hold, start a worker/vLLM process, or deploy code. |
| Use the admin's own student history and results. | Read another user's private submissions/results, assign account roles through the website, or enroll legacy production accounts. |

A catalog-only challenge without an evaluation contract can have teaching content
saved and published, but cannot have admissions paused or resumed. Unknown IDs
return `404 CHALLENGE_NOT_FOUND` after authorization. The catalog and source fields
remain read-only; teaching titles do not rename the immutable descriptor.

## Local Walkthrough

From the project root, start `& .\scripts\start_local_dev.ps1` and open
**http://127.0.0.1:8080**. See [Local Development](LOCAL_DEVELOPMENT.md) for the
Python fallback, prerequisites, and state preservation. Do not substitute
`localhost` or expose this seed-account/mail-capture service to the network.

1. Log in as `admin@example.test` (`LocalAdmin`, role `admin`). The initial local
   password is `Local-only-passphrase-2026!`; use your changed password after a
   reset. Restart does not restore credentials or a deliberately changed role.
2. Open the teaching-administration navigation item, or
   **http://127.0.0.1:8080/#admin**. Ordinary users do not receive this navigation
   item; typing the fragment does not grant access.
3. Choose an existing task. Check its language/treebank, fixed model and metric,
   source rights, sample count, and evaluation identity before editing teaching
   text. These fields describe the task and are not editable here.
4. Fill in the teaching title, optional summary, instructions, zero-shot template,
   and few-shot template. Preview renders the current form as plain text; it does
   not save, publish, or call the model.
5. Save the draft, run the check on that saved revision, then explicitly confirm
   publication. The browser enables publication only after a successful check for
   the current saved revision. The backend independently rechecks at publication;
   a prior check is not a transferable authorization token.
6. Return to the student task and refresh its published teaching. Confirm that
   the published revision, not an unpublished replacement draft, is visible.
   Loading either template requires the student's click; replacing an existing
   prompt asks for confirmation. The student can edit it before submitting.
7. Pause new submissions and check the student availability state. Already accepted
   jobs continue normally and existing results remain readable. Resume only when
   `can_reopen` allows it. Neither action changes the evaluation contract.

Saved drafts, published snapshots, admission state, and revision audit rows persist
in the local SQLite database across restarts. Unsaved form changes are browser
memory only. Switching/reloading a task with unsaved edits asks for confirmation.
Account changes, expiry, or loss of admin rights clear or block private admin state;
do not rely on the page as a backup of an unsaved lesson.

## Drafts, Publication, and Templates

There are two separate meanings of "draft": an immutable evaluation descriptor can
remain `status: draft`, while its editable teaching content has its own draft and
published snapshots. **Publishing teaching does not activate evaluation.** It does
not alter source/license decisions, a model process, a dataset, or a scorer.

`TeachingContent` has exactly these string fields:

| Field | Character limit |
| --- | --- |
| `title` | 1-120; not whitespace-only. |
| `summary` | 0-500; may be empty, but the field is required. |
| `instructions` | 1-4000; not whitespace-only. |
| `zero_shot_prompt` | 1-3000; not whitespace-only. |
| `few_shot_prompt` | 1-3000; not whitespace-only. |

The full JSON request is bounded to **16,384 UTF-8 bytes**, not 16,384 characters.
Duplicate/unknown fields, type coercion, and Unicode control/format/surrogate
characters are rejected; newline is allowed. HTML-looking text remains literal
text, not executable markup. There is no Markdown/HTML renderer or file upload.
Published content is anonymously readable, so never paste credentials, personal
data, private sample inputs, or gold answers into these fields. The structural
check does not detect every secret, validate linguistic correctness, or certify
that a prompt performs well.

The student read endpoint is `GET /v1/challenges/{challenge_id}/teaching`:

```json
{
  "challenge_id": "a-loaded-challenge-id",
  "published_revision": 0,
  "content": null
}
```

It returns only source-matching published content, never the draft, operator ID,
or audit history. With no published content it returns revision `0` and `null`;
the browser can then use its task-specific handwritten lesson/templates. A failed
or malformed teaching read is not treated as an empty publication: template
buttons remain unavailable until the read succeeds, leaving the student's prompt
unchanged. Responses use `Cache-Control: no-store`.

When published content exists, template buttons copy **only the chosen published
template** into the editor after an explicit click. Instructions, summary, and
title are not silently appended to model input. Publishing or refreshing teaching
does not rewrite an existing student prompt. The fixed response schema, scorer,
prompt byte/token checks, and runtime preflight remain independent; a teaching
template passing content limits is not guaranteed to fit an evaluation budget.

The handwritten defaults distinguish English `LocalPractice` XPOS (Penn Treebank
labels such as `NNS`, `VBP`, `.`) from German HDT XPOS (STTS labels such as `NN`,
`VVFIN`, `$.`). Chinese `LocalPractice` transliteration uses lowercase toneless
pinyin and preserves punctuation, unlike GSDSimp's tone-marked pinyin and defined
punctuation conversion. Do not publish one treebank's convention over another.

## Revision and Audit Semantics

Every save/check/publish/admissions request supplies the last-read
`expected_revision`. This is
compare-and-swap (CAS): the server changes state only if that revision still
matches, preventing one administrator from silently overwriting another.

| Action | Effect |
| --- | --- |
| `save` | Increment revision; replace the draft; retain the existing published snapshot. |
| `check` | Validate the saved draft and current source binding; return `can_publish`, `issues`, and `revision`; no revision increment or audit write. |
| `publish` | Revalidate, increment revision, copy the saved draft into the published snapshot, and set `published_revision` to the new revision. |
| `admissions` | Increment revision and set the new-admission switch without changing teaching snapshots. |

Initial state is revision `0`. The backend accepts a strict integer
`expected_revision` in `0..9223372036854775806`; strings, floats, and booleans are
not revisions. A stale revision returns `409 ADMIN_REVISION_CONFLICT` without a
write. After a conflict or an uncertain write response, explicitly reload and
reconcile the current version. Do not automatically replay an admin mutation or
substitute the new revision into an old unsaved request. Admin operations use CAS,
not the submission `Idempotency-Key` protocol.

State and audit insertion commit in the **same transaction**. An audit failure
rolls back the state change. `challenge_admin_revisions` stores the challenge,
revision, `save`/`publish`/`admissions` action, database-resolved operator user ID,
timestamp, and previous/next snapshots. The application appends records; there is
no audit browsing, deletion, or rollback endpoint in this phase. This is not a
cryptographically tamper-proof log against the database operator. Protect and
back up these rows together with other application state; old drafts remain in
the audit history even after a new draft replaces them.

The server computes a fingerprint of the complete loaded public descriptor, never
accepting one from a browser. If that descriptor changes outside administration
and no longer matches an existing admin state, the previous publication is hidden
for the new source and executable admissions fail closed. Publish/resume rejects
the stale binding with `ADMIN_SOURCE_CHANGED`.
Review and save a draft against the current descriptor, check/publish it as needed,
then explicitly resume if otherwise allowed. Rebinding does not silently reopen
admissions or delete the old snapshots.

## Atomic Admission and Authorization

Admission is checked **inside submission persistence, after idempotency replay or
conflict detection and before quotas/new submission/outbox insertion**, not only
in a request handler or a disabled button. Thus an identical accepted request can
still replay its original ID while paused; reusing its key for changed content
still conflicts. A new request returns `409 CHALLENGE_PAUSED` and creates neither
a submission nor an outbox record. This replay guarantee concerns the admin pause;
ordinary authentication and the API's existing contract/runtime checks still apply.

SQLite uses `BEGIN IMMEDIATE`. PostgreSQL takes a transaction-scoped task advisory
lock shared by admission and administration, including the first policy write
before a state row exists. If a submission wins the lock, its transaction can
finish before pause; if pause wins, later new admissions are rejected. Queued and
running work, deadlines, worker recovery, existing results, history, and ranking
identities are not canceled or rewritten by the switch.

Reopening requires an existing contract, its existing activation policy (or an
explicit non-production draft override), a matching source binding, and the
configured runtime-availability flag. That flag is not a fresh GPU health probe.
Admin publication never sets it or starts a process. Production forbids the draft
override.

All admin routes require a valid current cookie session and database role `admin`.
The operator ID and role are read from the current database under the auth
transaction lock, not trusted from a client header, old page, or cached principal.
Writes authorize before taking the task lock, then **refresh database time and
reauthorize after the task lock**. A session that expires while waiting cannot
save, check, publish, or change admissions. Role demotion and session revocation
are serialized through the same auth lock; an already committed mutation is not
retroactively undone by a later revocation.

Unsafe admin methods require exact same-origin JSON, `X-LOJ-CSRF: 1`, and
`X-LOJ-Expected-User` matching the cookie account. Reads check a supplied expected
user as well. Forged role/user headers and Bearer tokens grant no admin access.
The frontend hides navigation and drops late stale-account/role responses, but the
database checks are the authorization boundary. See
[Authentication](AUTHENTICATION.md) and [API Contract](API_CONTRACT.md).

## Schema and Verification

SQLite and PostgreSQL now target **schema v4**. Version 3 added auth and roles;
version 4 appends `challenge_admin_state` and `challenge_admin_revisions`, retaining
existing accounts, sessions, submissions, outbox, and results. SQLite applies
supported missing migrations on store construction. PostgreSQL migration remains
explicit; production legacy credential binding is still a separate startup gate.

Relevant code is `admin.py`, `admin_store.py`, the two submission stores,
`auth_store.py`, `auth_routes.py`, and `web/assets/{admin,teaching,account,app}.js`.
Relevant checks are `tests/test_admin.py`, `tests/test_admin_store.py`, migration
tests, Node contracts, and `scripts/check_admin_browser.py`. The latter starts a
fresh local SQLite/Mock app and runs student and admin Edge suites, never the
daily development database. PostgreSQL admin parameter tests require the separate
`LOJ_ADMIN_POSTGRES_TEST_URL` opt-in. Skips are not PostgreSQL concurrency evidence.

The actual 2026-09-07 remote report
`runtime/qwen-admin-acceptance-20260907.json` records `passed: true`, real PostgreSQL
admin publish/pause/replay/resume/demotion, cookie auth and logout revocation,
PostgreSQL outbox, Redis, and one Qwen-evaluated submission. It used two handwritten
samples, six gold items, and exactly two generation calls; score `1.0` is synthetic
integration evidence, **not a benchmark**. Transport was
`HTTP-inprocess-TestClient`, with `browser_verified: false` and captured rather
than real email. It does not establish the entire opt-in PostgreSQL concurrency
suite, browser-to-Qwen operation, SMTP/HTTPS, or public-launch readiness. See the
dated addenda in [Workspace Audit](WORKSPACE_AUDIT.md) and
[Operations](OPERATIONS.md) for snapshot identity, isolation, and cleanup proof.

Next work is to complete the current local student/admin regression, including
responsive/keyboard behavior, stale/expired accounts, revision conflicts, and
uncertain responses, then separately run the opt-in real-database concurrency and
migration checks. No new model, arbitrary-file administration, or public rollout
is implied by this phase.
