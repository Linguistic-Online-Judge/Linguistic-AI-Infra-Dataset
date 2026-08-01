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
aggregation layer now implements it. Model orchestration and submission
persistence are not implemented yet, so the current file remains a reproducible
selection artifact rather than an active competition.

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
