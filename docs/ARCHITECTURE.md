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
- **API**: authentication, safe problem metadata, submission creation, status,
  results, and leaderboard queries.
- **Worker**: builds prompts, calls the pinned model, parses JSON responses, runs
  deterministic scorers, and persists aggregate results.
- **Model provider**: one adapter interface. Start with a mock adapter in tests,
  then add one fixed self-hosted model adapter.
- **Gold data service**: loads samples by server-side challenge manifest.
  It must never expose the `answers` field to the client.
- **Database**: users, competitions, problems, model configurations, submissions,
  per-sample outcomes, and leaderboard aggregates.

SQLite can be used for a single-machine prototype. PostgreSQL and Redis are the
target once multiple evaluations can run concurrently.

## Submission flow

1. A student chooses a language and task and submits one prompt.
2. The API stores an immutable submission with the active model configuration
   and challenge version.
3. The worker loads the fixed server-side sample IDs for that challenge.
4. For each sample, the worker combines the student's prompt with the platform's
   fixed task envelope and requests strict JSON output from the model.
5. The response parser validates the schema. Malformed output receives a
   deterministic zero for that sample; no LLM repair judge is used.
6. The scorer computes segmentation F1, tag accuracy, UAS/LAS, or token-level
   transliteration accuracy with sentence exact match.
7. The worker stores aggregate metrics and safe error categories, then marks the
   submission complete.
8. The leaderboard reads persisted scores; it does not rerun evaluations.

## Fairness and reproducibility

- Pin the model artifact by exact version or checksum.
- Pin the inference runtime and prompt envelope version.
- Use fixed parameters, initially `temperature=0`, fixed maximum output tokens,
  and a fixed seed when the runtime supports it.
- Use the same challenge manifest for all students in one competition. Only
  unpublished-data challenges should be described as hidden.
- Record model version, runtime version, parameters, challenge version, and
  scorer version on every submission.
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
