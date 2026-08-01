# Challenge Aggregation

## Purpose

Per-sample scorers return deterministic counts. The aggregation layer combines
those counts across one versioned challenge and produces a stable report. It does
not call a model, load gold data, or persist a submission.

The current aggregation contract version is `1.0`.

## Sample outcomes

Each `SampleEvaluationOutcome` contains exactly one of:

- a structurally valid task score; or
- a deterministic `ParseErrorCode` plus the number of gold items.

All outcomes in one aggregate must use the same task and unique sample IDs.
`aggregate_challenge()` receives a validated `ChallengeArtifacts` pair rather
than caller-supplied challenge labels. Missing, extra, or duplicate IDs abort the
run rather than producing a partial score.
Structurally invalid tag, dependency, or transliteration scores must be represented
as malformed outcomes instead of valid scores.

The private manifest records each selected sample's trusted gold-item count and
includes those counts in `selection_sha256`. Every valid or malformed outcome
must use that exact denominator. This prevents a malformed response from claiming
a smaller denominator and inflating the aggregate score.

The challenge's scorer, aggregation, response-schema, metrics, dataset hash, and
selection hash must match across the public description, private manifest, and
runtime contracts. `ChallengeArtifacts` also carries the configured dataset path
and verifies its actual SHA-256 before aggregation. Score counters must be
non-negative integers with valid task-specific bounds, and their finite metrics
must agree with those counters.

## Micro metrics

Segmentation accumulates token-span counts across all samples:

```text
micro_precision = sum(correct_tokens) / sum(predicted_tokens)
micro_recall = sum(correct_tokens) / sum(gold_tokens)
micro_f1 = harmonic_mean(micro_precision, micro_recall)
```

This is intentionally not the arithmetic mean of sentence-level F1 scores.

UPOS and XPOS accumulate correct tags and gold tokens. Dependency accumulates
correct heads, correct labeled arcs, and gold arcs. Transliteration accumulates
correct token transliterations and exact sentences.

## Malformed model output

A parse failure contributes no correct items and remains in the gold denominator.
For segmentation it contributes zero predicted tokens, so recall and F1 are
penalized. Error codes are counted in the final report.

Malformed model output is different from infrastructure failure. Timeouts,
network errors, GPU failures, and provider outages must be retried or fail the
whole run. They must never be converted into a student's zero-scored sample.
`UNKNOWN_TASK` is likewise a platform configuration failure, not a student error.

## Result shape

```json
{
  "challenge_id": "zh-gsdsimp-segmentation-v2",
  "task": "segmentation",
  "scorer_version": "1.0",
  "aggregation_version": "1.0",
  "dataset_sha256": "6ed8c27ef391ccc0638fff87731f1898673f0ffe81b470173e2fa9ca2012f5ff",
  "selection_sha256": "3f0583073ee5efadf0062e8f9b5a707bd29b7e6f9a85393742841d615f29f81d",
  "samples_total": 50,
  "samples_valid": 47,
  "samples_invalid": 3,
  "primary_metric": "micro_f1",
  "score": 0.87,
  "metrics": {
    "micro_precision": 0.88,
    "micro_recall": 0.86,
    "micro_f1": 0.87
  },
  "errors": {
    "INVALID_JSON": 2,
    "LENGTH_MISMATCH": 1
  }
}
```

The upcoming offline runner will create these outcomes by connecting safe model
inputs, a provider, response parsing, and the existing per-sample scorers.
