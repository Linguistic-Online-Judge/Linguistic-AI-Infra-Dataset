# MVP Roadmap

## Definition of the MVP

One student can choose one language and one task, submit a prompt, have the same
pinned model run against a versioned hidden sample set, receive a deterministic
score, inspect safe aggregate feedback, and appear on a leaderboard. No gold
answer is sent to the browser.

## Gate 0: security and specification

- Make the repository private before treating the checked-in test data as hidden.
- Decide whether the first challenge is a public-data teaching benchmark or a
  strict anti-cheating assessment. The latter needs unpublished human annotation.
- Decide which UD treebanks and licenses are allowed in the first challenge.
- Freeze response JSON schemas for segmentation, UPOS, dependency, and
  transliteration.
- Choose one initial language/task pair and a bounded hidden challenge set.
- Select the deployment machine before choosing the self-hosted model size.

Exit criterion: the team can state exactly what a student sees, what stays
server-side, what source-data leakage remains possible, and how every malformed
or valid output is scored.

## Phase 1: deterministic evaluation core

- Implement token-span segmentation precision, recall, and F1.
- Implement strict UPOS/XPOS positional accuracy.
- Implement dependency UAS and LAS keyed by token ID.
- Implement Unicode-normalized exact transliteration match.
- Add response schema parsing and malformed-output categories.
- Add unit tests for perfect, partial, malformed, missing, and extra output.

Exit criterion: all metrics run offline without a model or web server and are
fully covered by repeatable tests.

Implemented so far: per-sample scorers, strict response JSON contracts, parser
error categories, and token-level transliteration scoring. Challenge-level metric
aggregation remains before Phase 1 is complete.

## Phase 2: dataset and challenge layer

- Stream JSONL instead of loading the 176 MB file into request handlers.
- Generate versioned challenge manifests containing hidden sample IDs.
- Return only safe problem input DTOs without `answers`.
- Add deterministic sample selection and integrity hashes.
- Add dataset validation and source/license metadata.

Exit criterion: a command can build one challenge and prove that its public
payload contains no gold fields.

Implemented so far: streaming JSONL filtering, deterministic reservoir sampling,
versioned public challenge metadata, private manifests, integrity hashes, and the
first 50-sample Chinese GSDSimp segmentation challenge.

## Phase 3: fixed model runner

- Define a provider-independent model adapter.
- Implement a mock provider for tests.
- Evaluate candidate self-hosted models on the deployment hardware.
- Pin one model artifact, runtime, generation parameters, and prompt envelope.
- Store raw responses privately and enforce time/output limits.

Exit criterion: the same local evaluation command can switch from mock to the
pinned model without changing scoring code.

## Phase 4: backend service and jobs

- Create the FastAPI application and database migrations.
- Add users, challenges, model configurations, submissions, and result tables.
- Add submission, status, result, and leaderboard endpoints.
- Run evaluations in a worker; never block an HTTP request on model inference.
- Add rate limits, retry policy, idempotency, and safe logs.

Exit criterion: an API integration test completes a mock submission end to end.

## Phase 5: web application

- Build login, challenge list, task details, and prompt editor.
- Offer zero-shot, few-shot, and CoT templates as editable teaching aids.
- Show queued/running/completed status and metric explanations.
- Show submission history and leaderboard.
- Verify desktop and mobile layouts.

Exit criterion: a new user can complete the MVP flow without direct API use.

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

1. Finish Phase 1 scorers and tests.
2. Define strict response JSON schemas.
3. Build a 50-sample Chinese segmentation challenge without exposing answers.
4. Run the challenge with a mock model.
5. Benchmark candidate fixed models on the intended server.
