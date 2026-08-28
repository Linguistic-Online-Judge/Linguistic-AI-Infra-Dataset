# Qwen3.5 UPOS Benchmark

## Scope

This benchmark extends the first Chinese segmentation run to a fixed-token
Universal Dependencies UPOS task in two languages:

| Challenge | Language | Treebank | Samples | Seed |
| --- | --- | --- | ---: | ---: |
| `zh-gsdsimp-upos-v1` | Chinese | GSDSimp | 50 | 2026 |
| `en-ewt-upos-v1` | English | EWT | 50 | 2026 |

The Chinese challenge uses the same selected sentences as the segmentation
baseline, which permits task-level comparison without changing the sample set.
The English challenge adds a separate EWT sample. Both use response schema
`upos-v1`, scorer `1.0`, aggregation `1.0`, and prompt envelope `1.0`.

## Fixed prompt

Both languages and both models used this exact baseline student prompt:

```text
Assign exactly one Universal Dependencies UPOS tag to each input token. Preserve the input token order and return the same number of tags as tokens. Use only these tags: ADJ, ADP, ADV, AUX, CCONJ, DET, INTJ, NOUN, NUM, PART, PRON, PROPN, PUNCT, SCONJ, SYM, VERB, X. Do not merge, split, reorder, omit, or add tokens. Return only the JSON object required by the platform schema.
```

Generation remained non-thinking and deterministic: temperature `0`, top-p
`1`, seed `2026`, and maximum output length `1,024`. JSON-constrained decoding
was not enabled, so malformed output remained observable. Each model/challenge
combination was run twice; the two aggregate JSON results were identical.

## Accuracy and validity

| Model | Challenge | Valid | Invalid | Micro accuracy |
| --- | --- | ---: | ---: | ---: |
| Qwen3.5-4B | Chinese GSDSimp | 3 / 50 | 47 / 50 | 0.014754098360655738 |
| Qwen3.5-9B | Chinese GSDSimp | 16 / 50 | 34 / 50 | 0.12295081967213115 |
| Qwen3.5-4B | English EWT | 22 / 50 | 28 / 50 | 0.14162348877374784 |
| Qwen3.5-9B | English EWT | 35 / 50 | 15 / 50 | 0.34024179620034545 |

The deterministic mock remained valid for all samples. Its micro accuracy was
0.004918032786885246 for Chinese and `0` for English because it predicts `X`
for every token.

## Error distribution

| Model | Challenge | Invalid JSON | Invalid tag | Length mismatch | Empty value |
| --- | --- | ---: | ---: | ---: | ---: |
| Qwen3.5-4B | Chinese GSDSimp | 16 | 2 | 29 | 0 |
| Qwen3.5-9B | Chinese GSDSimp | 4 | 2 | 28 | 0 |
| Qwen3.5-4B | English EWT | 6 | 0 | 21 | 1 |
| Qwen3.5-9B | English EWT | 0 | 1 | 14 | 0 |

Malformed samples retain the trusted gold denominator and contribute zero
correct tags. Therefore, micro accuracy measures task accuracy and output
contract compliance together rather than silently discarding invalid samples.

## Runtime observations

The runtime measurements below cover 200 sequential requests per model: two
50-sample Chinese runs and two 50-sample English runs.

| Measurement | 4B | 9B |
| --- | ---: | ---: |
| Successful `stop` completions | 156 | 192 |
| `length` completions | 44 | 8 |
| End-to-end latency | 3.222 s/request | 2.768 s/request |
| Time to first token | 0.107 s/request | 0.133 s/request |
| Decode rate | 82.7 tokens/s | 48.9 tokens/s |
| Prompt tokens | 315.0/request | 315.0/request |
| Generation tokens | 257.7/request | 128.9/request |
| Peak observed GPU memory | about 19.0 GiB | about 22.5 GiB |

The 4B model decodes faster per token but reaches the 1,024-token limit much
more often. The 9B model produces shorter outputs, so its average end-to-end
latency is lower despite slower token decoding.

## Interpretation

Qwen3.5-9B is stronger than 4B in both languages, but neither model is ready to
serve as a reliable UPOS backend with prompt `v1`. In particular, the 9B valid
rate is only 32% for Chinese and 70% for English. A versioned prompt experiment
should target exact output length and concise tag-only generation before final
model selection. The current result must remain as the unchanged `v1` baseline
rather than being replaced after prompt tuning.

The four aggregate artifacts under `benchmarks/` contain no sample IDs, model
inputs, raw responses, gold answers, or private manifests.

The follow-up [`PROMPT_CALIBRATION.md`](PROMPT_CALIBRATION.md) comparison keeps
Qwen3.5-9B fixed and varies only the student prompt.
