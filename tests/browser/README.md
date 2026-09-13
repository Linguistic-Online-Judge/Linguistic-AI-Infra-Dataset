# Browser Acceptance

The application stays plain HTML/CSS/JavaScript. These tests use Node 22+ and an
already installed Edge/Chromium browser through CDP. They do not install a browser,
use a frontend framework, or run a real model.

## Commands

From the canonical project root:

```powershell
node --test tests/browser/contracts.test.mjs
node tests/browser/run.mjs --fixture
node tests/browser/admin-run.mjs --fixture
```

`--fixture` uses the synthetic allowlisted server in `fixture-server.mjs`, not the
Python API or database. Admin fixtures implement the shared teaching, revision,
publication and admissions response shapes.

For real local integration, the separately named wrapper starts a fresh SQLite
child app on an ephemeral loopback port, runs both browser suites, then stops only
that child process tree:

```powershell
.\.venv\Scripts\python.exe scripts/check_admin_browser.py
```

The existing `scripts/check_local_browser.py` wrapper is unchanged. An orchestrator
that already owns an isolated Mock app can instead run:

```powershell
node tests/browser/admin-run.mjs --url http://127.0.0.1:ISOLATED_TEST_PORT
```

Replace `ISOLATED_TEST_PORT` with that child's numeric port. The admin runner rejects
port 8080 and non-loopback targets, requires explicit Mock development discovery,
and requires an initially unpublished task. Never point it at retained teaching
records: it saves and publishes teaching text, switches seeded test accounts, and
pauses/resumes a task. It does not change account roles in the database.

## Coverage

- Eight Node checks cover teaching output contracts, language/treebank lesson
  selection, Unicode and UTF-8 limits, safe inbox links, metrics, DOM IDs, expected
  user/CSRF headers, and stale account/role response fencing.
- The original 30 browser groups remain. Teaching screenshot assertions now use
  the selected language/treebank lesson, with independent English LocalPractice
  Penn tags and Chinese no-tone transliteration checks.
- The 16 admin browser groups cover administrator navigation and read-only sources,
  forbidden anonymous/student access, plaintext preview, validation, draft/public
  separation, check/publish, fixed student schemas, explicit same-version template
  insertion, pause/history/result behavior, eligible resume, real two-tab revision
  conflicts, account switching, stale reads/writes, and session revalidation.
- Admin GETs and writes use current-cookie authorization. Tests exercise the real
  expected-user guard, real 403 student denial, real revision conflicts and real
  `CHALLENGE_PAUSED` responses in `--url` mode. Browser requests never fall back to
  Bearer during an account or management outage.
- Role demotion, an ineligible resume policy, failed publish-check issues and
  network/non-JSON failures are explicitly injected browser faults, including in
  real-app mode. They verify frontend handling, not a real database role change or
  production licensing/runtime enforcement. No role-edit endpoint is invented.

## Latest Verification

On 2026-09-07, all eight Node checks, all 30 original fixture groups, all 16 admin
fixture groups, and both real-app suites (30 + 16) passed in the canonical checkout.
The real run used the isolated wrapper above, not the daily development database.

Latest real admin screenshots:

`runtime/browser-tests/admin-local-app-1788782560394/`

- `admin-editor-1440.png`, `admin-editor-768.png`, `admin-editor-390.png`,
  `admin-editor-320.png`: existing annotation-bench theme and responsive editor.
- `admin-preview-320.png`: plaintext preview and separate admissions controls.
- `student/student-published-320.png`: published guide, revision and explicit
  template controls, separated from fixed output schemas.

Original suite screenshots remain in `runtime/browser-tests/local-app/`; latest
admin fixture screenshots are in `runtime/browser-tests/admin-fixtures-1788782493847/`.
The suites fail on page overflow and uncaught JavaScript exceptions. Screenshots
were also visually inspected at desktop and 320-pixel widths.

## Boundaries

This frontend slice does not modify the 26-task real catalog, Python backend/store
implementation, models, dataset files, licensing metadata, or scoring contracts.
There is no SSH, daily-port testing, production deployment, PostgreSQL integration
run, or Qwen evaluation here. The isolated app supplies only its five handwritten
Mock tasks. Full real-catalog runtime/rights checks and actual same-user role
demotion remain backend/integration verification work, not claims of this suite.
