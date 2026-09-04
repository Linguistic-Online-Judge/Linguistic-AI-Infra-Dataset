# Challenge Sets

## Purpose

A challenge is an immutable, versioned subset of the standardized dataset bound
to one language, treebank, task, response schema, and scoring metric. Every
student in the same challenge must use the same sample selection. Rebuilding
identical content is a no-op; different content under an existing ID is rejected
and requires a new version.

## Public and private artifacts

The builder writes two files with different access rules:

```text
challenges/public/<challenge-id>.json
runtime/private/challenges/<challenge-id>.json
```

The public description contains only safe metadata such as language, task,
sample count, metric, scorer and aggregation versions, and SHA-256 fingerprints.
The fingerprints bind the public artifact to one source file and one sample
selection without exposing sample IDs. It may be committed to Git and returned
by an API.

The server-side manifest contains immutable sample ID/gold-item-count pairs, the
selection seed, task/scorer/aggregation versions, and integrity hashes. The gold
counts are denominators, not answers, and are included in `selection_sha256`.
`runtime/` is ignored by Git and must only be readable by the backend and
evaluation worker. The backend uses the IDs to load answers from its configured
standard dataset.

This separation prevents an API from directly returning the selection. It does
not make the current selection secret: the UD data, builder, filters, count, and
seed are public, so the same 50 IDs can be reconstructed. Public metadata marks
this limitation as `security_level: public_reproducible`.

## Deterministic selection

Matching samples are read as a stream and selected with seeded reservoir
sampling. The full JSONL file is never loaded into memory. Given the same source
file, filters, count, and seed, the selected sample IDs are identical.

The builder rejects duplicate IDs in the matching candidate pool and requests
larger than that pool. Selected IDs are sorted before hashing and storage so
output is stable.

## Challenge registry validation

The challenge registry is a small index of public challenge descriptions and
their optional evaluation contracts. A registry document has this shape:

```json
{
  "schema_version": "challenge-contract-registry-v1",
  "entries": [
    {
      "public_descriptor_path": "challenges/public/example-v1.json",
      "evaluation_contract_path": "config/example-evaluation-v1.json"
    }
  ]
}
```

The Qwen API and Worker call `load_challenge_contract_registry()` during trusted
startup, before accepting work. The loader performs all checks immediately:

1. Every referenced path must be a relative POSIX path below the project root.
2. Public descriptor paths and evaluation contract paths must not be repeated.
3. Every public description must have a unique challenge ID and valid task,
   metric, response-schema, version, status, and SHA-256 metadata.
4. When an evaluation contract is present, its challenge ID, status, security
   level, dataset and selection hashes, task, response schema, scorer version,
   and aggregation version must match the public description.
5. The returned maps are read-only, so later application code cannot replace a
   validated entry by accident.

An entry may set `evaluation_contract_path` to `null` when it is public catalog
metadata only. Such an entry appears in `public_challenges` but not in
`contracts`. The API returns `403` if a client tries to submit to that entry and
`404` for an unknown challenge ID. A mismatch or malformed entry raises an
exception; startup fails instead of serving a partly validated registry.

The API creates one contract-routed outbox dispatcher and Redis Stream for each
executable entry. All executable contracts must share the platform-wide request
body, outstanding submission, running submission, and global queue limits. Each
Worker selects one challenge ID at startup and remains bound to that public
description, contract snapshot, private manifest, dataset, and queue. It never
selects a contract from an untrusted job message.

The registry does not read private manifests, selected sample IDs, gold answers,
or datasets. Those remain separate server-side inputs. Registry tests create all
descriptions and contracts in temporary directories and do not register held or
production data.

No production registry is committed yet. Deployment must supply the registry
path explicitly after its entries have passed source-rights and activation
review.

## Public challenge catalog API

The API exposes the validated public registry without authentication:

- `GET /v1/challenges` returns summaries sorted by `challenge_id`.
- `GET /v1/challenges/{challenge_id}` returns the full public description and
  returns `404` for an unknown challenge ID.

List summaries contain only the challenge ID, title, version, language,
treebank, task, sample count, primary metric, security level, publication
status, and `submissions_open`. Details additionally contain public secondary
metrics, response/scorer/aggregation versions, and dataset/selection hashes.
Both responses are copied field by field into dedicated response models; the
API never serializes an evaluation contract directly.

`submissions_open` reports whether this deployment has an executable contract
that passes its activation policy. It is not an authentication decision:
creating a submission still requires a registered user. Public-only entries
remain visible with `submissions_open: false`. A development or test process
that explicitly enables draft submissions reports an executable draft as open,
matching submission preflight behavior.

The catalog never includes private manifests, selected sample IDs, gold
answers, prompts, model configuration, queue routes, or contract snapshots.

## First challenge

The initial development challenge is:

```text
ID: zh-gsdsimp-segmentation-v2
Language: Chinese
Treebank: GSDSimp
Task: segmentation
Samples: 50
Seed: 2026
Primary metric: micro_f1
Scorer version: 1.0
Aggregation version: 1.0
Security level: public_reproducible
Status: draft
```

The current builder only emits `public_reproducible` and `draft`; private-data
security validation and challenge activation are future backend workflows.

`micro_f1` declares the metric this challenge uses, and the deterministic
aggregation and offline runner implement it. Submission persistence and
multi-challenge routing are implemented, but this challenge remains a reproducible
development artifact rather than an active competition.

Build it from the smaller per-language file:

```powershell
.\.venv\Scripts\python.exe -m linguistic_oj.challenge `
  --dataset "Standard_Dataset\by_language\Chinese_中文.jsonl" `
  --language Chinese `
  --treebank GSDSimp `
  --task segmentation `
  --count 50 `
  --seed 2026 `
  --version v2
```

Changing any sample selection or scoring rule requires a new challenge version.
The builder enforces this rule: an identical rerun is allowed, while different
public or private content under the same ID raises `ChallengeExistsError` before
either file is changed.

The original `v1` public file remains unchanged as a historical draft. Adding
explicit scorer/aggregation contracts and trusted manifest denominators changed
the challenge contract, so the current development artifact is `v2` rather than
an in-place edit of `v1`. Catalog code can parse the legacy public metadata, but
it cannot be loaded as evaluation-ready `ChallengeArtifacts`.

Evaluation workers must use `load_challenge_artifacts()` with the configured
dataset path. Loading recomputes the dataset SHA-256 and rejects a wrong or
modified source file before any score can be aggregated.

## Security limitation

Keeping the manifest server-side prevents the platform from directly returning
its selection, but the first challenge is exactly reproducible from public inputs.
It is suitable for development and teaching, not strict anti-cheating assessment.
Strict challenges require unpublished, independently annotated data stored
outside the public repository. A future backend workflow must validate that
provenance before it can assign a private security level or activate a challenge.
