# Production Operations Baseline

## Current boundary

The Qwen v2 API and Worker have passed a loopback GPU smoke test. That proves the
submission path, but is not a production deployment. The current SQLite store is
for a single-machine prototype only. Do not enable external submissions until
PostgreSQL, persistent Redis 7+, production authentication, protected gold-data
storage, and restart testing are in place.

## Health endpoints

The API exposes unauthenticated, body-free operational endpoints:

- `GET /health/live` returns `200 {"status":"live"}` whenever the API process
  can serve requests.
- `GET /health/ready` returns `200 {"status":"ready"}` only when the API can
  query its submission store and ping its configured Redis queue. It returns a
  generic `503 {"detail":"Service not ready"}` without connection details when
  either dependency fails.

The API intentionally does not probe vLLM in readiness. Submission durability
depends on the database and Redis; Worker startup attestation owns vLLM validation.

Every API request emits exactly one JSON completion record with only event,
request ID, method, path, status, and duration. Prompt contents, authentication
subjects, authorization headers, idempotency keys, query strings, model content,
and dependency error messages are excluded.

## Required production services

- Run Redis 7.4 or later with ACL authentication, AOF persistence, a bounded
  `maxmemory` policy that never evicts queue keys, backups, and monitoring.
- Use PostgreSQL for API and Worker persistence. SQLite remains unsupported for
  a multi-process or restart-tolerant deployment.
- Keep dataset manifests, gold data, tokenizer snapshots, and vLLM launch evidence
  on server-owned volumes that are unreadable by web users and tenants.
- Provide an authentication callback backed by a server-verified identity provider.
  The smoke-test static callback must never be deployed.
- Run API, Worker, and vLLM under a supervisor that restarts failed processes and
  directs stdout/stderr to access-controlled retention.

## Server findings

The current GPU host has Docker Compose installed, but the deployment account
cannot access the Docker daemon. Its system Redis is 6.0 and cannot support the
Worker's `XAUTOCLAIM` recovery protocol; PostgreSQL is not installed. An operator
must either grant a constrained Docker deployment role or provision managed Redis
7+ and PostgreSQL before the next deployment phase.
