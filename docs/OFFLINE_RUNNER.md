# Offline Evaluation Runner

## Purpose

The offline runner connects the existing challenge, dataset, response parser,
per-sample scorers, and challenge aggregation without an API or database. It uses
a deterministic mock provider so the complete evaluation path can be tested
without network access, credentials, or a real model.

## Gold-free model inputs

The runner constructs a new task-specific DTO field by field. It never serializes
a `DatasetSample` and removes fields afterward.

| Task | Fields visible to the provider |
| --- | --- |
| Segmentation | canonical boundary-free `text` |
| UPOS/XPOS | fixed `tokens` |
| Dependency | fixed token `token_id` and `form` |
| Transliteration | `text` and fixed `tokens` |

Provider requests do not contain sample IDs, source metadata, `answers`, gold
tags, dependency heads, dependency relations, or transliterations. Fixed
tokenization is intentional input for every task except segmentation.

For segmentation, the DTO concatenates validated token forms into the exact
surface used by the scorer. This removes all gold boundaries and avoids treating
ordinary whitespace in the original UD sentence text as part of a predicted
token. The provider never receives the token list used to construct that surface.

## Evaluation flow

```text
public challenge + private manifest + dataset
                     |
                     v
        artifact and complete-sample preflight
                     |
                     v
             safe ModelRequest DTO
                     |
                     v
                 provider
                     |
                     v
          strict JSON response parser
                     |
                     v
             per-sample scorer
                     |
                     v
           challenge aggregation
```

All selected samples are loaded and validated before the first provider call.
The runner checks dataset identity, sample coverage, language, treebank, task
availability, gold structure, and trusted manifest denominators.

## Failure boundary

Malformed model JSON, response schema violations, invalid UPOS tags, and fixed
token count/ID mismatches become deterministic malformed outcomes. They receive
zero correct items while retaining the trusted gold denominator.

Dataset, manifest, gold, provider, scorer, or platform contract failures abort
the complete run. A provider exception or invalid provider return type is never
converted into a student's zero, and no partial aggregate is returned.

## Deterministic mock

The current mock uses safe input fields only:

| Task | Mock response |
| --- | --- |
| Segmentation | one token per Unicode code point |
| UPOS | `X` for every token |
| XPOS | `MOCK` for every token |
| Dependency | first token as root, remaining tokens in a chain |
| Transliteration | copies each source token |

The mock is a reproducible integration tool, not a linguistically capable model.

## Run the development challenge

The ignored private manifest and a UTF-8 prompt file must exist locally before
running. Keeping the prompt in a file avoids exposing it through process arguments
and shell history.

```powershell
.\.venv\Scripts\python.exe -m linguistic_oj.runner `
  --public "challenges\public\zh-gsdsimp-segmentation-v2.json" `
  --private "runtime\private\challenges\zh-gsdsimp-segmentation-v2.json" `
  --dataset "Standard_Dataset\by_language\Chinese_中文.jsonl" `
  --provider mock `
  --prompt-file "runtime\private\prompts\segmentation.txt"
```

The current 50-sample regression result is:

```text
samples_total: 50
samples_valid: 50
samples_invalid: 0
micro_f1: 0.3949447077409162
```

Repeated runs produce byte-identical aggregate JSON. Normal output contains no
raw responses, model inputs, sample IDs, or gold data. Failure output reports only
the exception category without a traceback or private values. The CLI writes no
result files.

## Deferred production work

The runner is synchronous and mock-only. Real model adapters, prompt-envelope
versioning, model/runtime/parameter pinning, timeouts, retries, persistence,
background jobs, API endpoints, and concurrency remain separate later phases.
