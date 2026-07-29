# Challenge Sets

## Purpose

A challenge is an immutable, versioned subset of the standardized dataset bound
to one language, treebank, task, response schema, and scoring metric. Every
student in the same challenge must use the same sample selection.

## Public and private artifacts

The builder writes two files with different access rules:

```text
challenges/public/<challenge-id>.json
runtime/private/challenges/<challenge-id>.json
```

The public description contains only safe metadata such as language, task,
sample count, and metric. It may be committed to Git and returned by an API.

The private manifest contains sample IDs, the selection seed, and integrity
hashes. `runtime/` is ignored by Git and must only be readable by the backend and
evaluation worker. The manifest does not duplicate gold answers; the backend
uses its sample IDs to load answers from the private standard dataset.

## Deterministic selection

Matching samples are read as a stream and selected with seeded reservoir
sampling. The full JSONL file is never loaded into memory. Given the same source
file, filters, count, and seed, the selected sample IDs are identical.

The builder rejects duplicate source IDs and requests larger than the matching
pool. Selected IDs are sorted before hashing and storage so output is stable.

## First challenge

The initial development challenge is:

```text
ID: zh-gsdsimp-segmentation-v1
Language: Chinese
Treebank: GSDSimp
Task: segmentation
Samples: 50
Seed: 2026
Primary metric: micro_f1
```

Build it from the smaller per-language file:

```powershell
.\.venv\Scripts\python.exe -m linguistic_oj.challenge `
  --dataset "Standard_Dataset\by_language\Chinese_中文.jsonl" `
  --language Chinese `
  --treebank GSDSimp `
  --task segmentation `
  --count 50 `
  --seed 2026 `
  --version v1
```

Changing any sample selection or scoring rule requires a new challenge version.
Do not silently overwrite a challenge that already has recorded submissions.

## Security limitation

Keeping the manifest private prevents the platform from directly revealing its
selection. It does not make public UD source data secret. Strict anti-cheating
challenges require unpublished, independently annotated data stored outside the
public repository.
