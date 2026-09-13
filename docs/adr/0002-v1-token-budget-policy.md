# ADR 0002: V1 Token Budget Policy

## Status

Accepted for draft evaluation contracts.

## Decision

Every executable V1 contract is audited with the tokenizer files, revision, chat
template, and thinking mode pinned in its evaluation identity. The audit renders
the exact 50 private-manifest samples with the Alpha reference prompt and measures
compact canonical gold responses.

UPOS also has a finite domain-specific bound: the longest allowed UD tag is
repeated for the largest selected item count and serialized as compact JSON. One
additional token is required for termination. Other task schemas permit unbounded
strings, so their evidence is explicitly limited to canonical selected gold; the
runtime still rejects responses that exceed the configured completion budget.

The frozen completion budgets are:

| Challenges | `max_tokens` |
| --- | ---: |
| 14 bounded UPOS challenges | 256 |
| Hebrew, Japanese, Spanish, and Swedish UPOS | 512 |
| Chinese GSDSimp segmentation | 256 |
| German HDT XPOS | 256 |
| German HDT dependency | 1024 |
| Chinese GSDSimp transliteration | 512 |

For every contract, `max_rendered_input_tokens + max_tokens <= 4096`. The API and
worker additionally render and check every submitted prompt before evaluation, so
the Alpha prompt measurement is evidence for the baseline rather than a claim that
all possible student prompts have identical size.

## Evidence

`benchmarks/observations/qwen3.5-9b-v1-token-budget-audit.json` covers all 22
executable contracts and 1,100 selected samples. Its internal report SHA-256 is
`82ef537f26a4b25a7f585070438d4cf17e29c5b541dc2ebce39205151da8cd0f`.
