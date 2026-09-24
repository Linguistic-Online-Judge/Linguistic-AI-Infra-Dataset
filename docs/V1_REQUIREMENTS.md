# Linguistic Online Judge V1 Requirements

## Product definition

V1 is the first usable release of a multilingual prompt-engineering online judge.
Students choose a language and task, submit a prompt, run that prompt against the
same pinned model and versioned challenge as every other student, receive a score
computed by deterministic code, inspect safe aggregate feedback, and participate
in a version-isolated leaderboard.

The development repository and current UD-derived answers are public. Challenges
built from those files are therefore `public_reproducible` teaching benchmarks,
not secret or anti-cheating assessments. Strict assessments require separately
annotated unpublished data stored outside the public repository and its Git
history.

## Language scope

V1 targets exactly these 18 languages, matching `Standard_Dataset/metadata.json`:

1. Arabic
2. Chinese
3. Danish
4. Dutch
5. English
6. French
7. German
8. Hebrew
9. Hindi
10. Hungarian
11. Italian
12. Japanese
13. Korean
14. Portuguese
15. Russian
16. Spanish
17. Swedish
18. Thai

The earlier reference to 19 languages was a counting error and is superseded by
this list.

The initial representative challenge for each language uses the largest treebank
with at least 50 task-eligible samples, with a case-insensitive treebank-name
tie-break. Every sample in the selected treebank must pass strict gold validation.
Each initial challenge selects 50 samples with seed `2026` and remains `draft`
until every release gate is complete. Five-task deployed GPU evidence is recorded;
source identity and license evidence are recorded, but rights approval and
production operations remain blocking.

## Task scope

The platform supports five internal task types. The four Guidance task families
map to them as follows:

| Guidance family | Platform task | Primary metric | Availability rule |
| --- | --- | --- | --- |
| Segmentation | `segmentation` | Micro-F1 | Languages/treebanks with segmentation gold |
| Part-of-speech tagging | `upos` | Micro Accuracy | All current samples |
| Optional treebank-specific POS | `xpos` | Micro Accuracy | Only samples with complete XPOS gold |
| Dependency parsing | `dependency` | LAS, with UAS secondary | All current samples |
| Transliteration/pinyin | `transliteration` | Token Accuracy | Only samples with complete transliteration gold |

V1 must expose a truthful language-by-task availability matrix. It must not create
empty or fabricated challenges for combinations unsupported by the source data.

The frozen catalog provides UPOS coverage for all 18 languages and task-family
coverage through `zh-gsdsimp-segmentation-v2`, `de-hdt-xpos-v1`,
`de-hdt-dependency-v1`, and `zh-gsdsimp-transliteration-v1`. Every executable
challenge contains 50 strictly eligible samples and remains `draft`.

## Required student flow

1. Authenticate with a server-verified identity and non-email public handle.
2. Browse and filter challenges by language and task.
3. Read the task, metric, sample count, model identity, version, and security label.
4. Start from a free-form prompt or an editable zero-shot, few-shot, or CoT teaching
   template. Strategy labels do not change the evaluation pipeline or score.
5. Submit asynchronously and observe `queued`, `running`, `rejected`, `succeeded`,
   or `failed` status.
6. Inspect safe aggregate metrics and error categories without gold labels, sample
   IDs, raw model responses, or another user's prompt.
7. Browse personal submission history and the matching versioned leaderboard.

## Required platform behavior

- One immutable evaluation contract per challenge version.
- One pinned model revision, tokenizer, runtime, and generation configuration for
  every comparable leaderboard partition.
- Deterministic parsers and scorers; no LLM judge or score repair.
- Private manifests and gold data are available only to evaluation workers.
- PostgreSQL is the deployed source of truth and Redis Streams provides durable
  asynchronous delivery with recovery and fenced claims.
- Quotas, idempotency, body/token limits, deadlines, safe errors, and body-free logs
  remain enforced for every challenge.
- API, Worker, and leaderboard results remain partitioned by the complete
  evaluation identity so incompatible versions never mix.
- API, Worker, vLLM, PostgreSQL, and Redis have controlled startup, health checks,
  logs, backups, restore verification, and rollback procedures.

## V1 API boundary

The web application may rely on these product endpoints:

- `GET /v1/challenges`
- `GET /v1/challenges/{challenge_id}`
- `GET /v1/users/me`
- `GET /v1/submissions`
- `POST /v1/submissions`
- `GET /v1/submissions/{submission_id}`
- `GET /v1/submissions/{submission_id}/result`
- `GET /v1/leaderboards/{evaluation_identity_sha256}`

Challenge catalog data is public metadata. Current-user, submission, history, and
owner-result data require authentication. Swagger is a developer interface, not
the student product UI. The frozen response, error, availability, idempotency, and
pagination semantics are specified in `docs/API_CONTRACT.md`.

## Release gates

- Every catalog entry has matching, versioned public metadata.
- Every submission-enabled entry also has a matching evaluation contract, private
  manifest, dataset hash, queue, Worker configuration, and GPU smoke evidence.
- At least one valid challenge exists for every language before claiming complete
  18-language V1 coverage.
- Every advertised task has at least one real end-to-end challenge.
- Multi-challenge routing, ownership, history, queue recovery, restart behavior,
  and leaderboard isolation pass automated and deployed integration tests.
- The deployed release comes from a clean reviewed Git commit and exact release
  identity, not an uncommitted smoke snapshot.
- Secrets, runtime databases, backups, private manifests, and server logs are not
  committed to the public repository.
- Public benchmark limitations and treebank attribution/license information are
  visible to users.

## Decisions still requiring product-owner approval

- Whether Guidance's dynamic difficulty/participation weighting is a V1 scoring
  requirement or a later competition feature. Raw linguistic metrics must remain
  visible and immutable even if a separate point weight is added.
- Whether teachers need privileged per-sample raw model-output retention. The
  current privacy and reproducibility contract intentionally persists aggregate
  results only.
