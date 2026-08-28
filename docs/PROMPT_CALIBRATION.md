# UPOS Prompt Calibration

## Purpose

The platform evaluates a student prompt, not a platform-authored solution. This
calibration fixes the model, challenges, runtime, generation settings, prompt
envelope, parser, and scorer while varying only `student_prompt`. Its purpose is
to test whether the judge can distinguish prompts that produce meaningfully
different model behavior.

The fixed model is Qwen3.5-9B at revision
`c202236235762e1c871ad0ccb60c8ee5ba337b9a`, served by vLLM `0.27.1+cu129`.
The Chinese GSDSimp and English EWT `upos-v1` challenges each contain 50
samples. Temperature is `0`, top-p is `1`, seed is `2026`, thinking is disabled,
and JSON-constrained decoding is not used.

## Reference student prompts

The labels describe the prompts' intended roles before testing. They are not
quality judgments assigned after observing scores.

### Weak

```text
Assign a Universal Dependencies UPOS tag to every input token.
```

SHA-256: `472043ac3ad2683547803bf46f5768f72fecccdfbab39a87d2fe8d44923901f1`

### Baseline

```text
Assign exactly one Universal Dependencies UPOS tag to each input token. Preserve the input token order and return the same number of tags as tokens. Use only these tags: ADJ, ADP, ADV, AUX, CCONJ, DET, INTJ, NOUN, NUM, PART, PRON, PROPN, PUNCT, SCONJ, SYM, VERB, X. Do not merge, split, reorder, omit, or add tokens. Return only the JSON object required by the platform schema.
```

SHA-256: `e19f8507e18d3274ba4a9247e273312ce5a2b3f59be6c49ecb64097aafa48e92`

### Strong candidate

```text
Perform Universal Dependencies UPOS tagging for the provided token sequence. Return exactly one JSON object in this form: {"tags":["TAG_1","TAG_2"]}. The tags array must contain exactly one tag for each input token, in the same order and with no omitted or added positions. Use only ADJ, ADP, ADV, AUX, CCONJ, DET, INTJ, NOUN, NUM, PART, PRON, PROPN, PUNCT, SCONJ, SYM, VERB, or X. Use sentence context to distinguish lexical verbs from auxiliaries, common nouns from proper nouns, determiners from pronouns, and adjectives from adverbs. Tag punctuation as PUNCT. Example: tokens ["They","can","run","."] require {"tags":["PRON","AUX","VERB","PUNCT"]}. Do not output tokens, indices, explanations, Markdown, reasoning, or additional fields. Before answering, silently verify that the tags array length exactly equals the input token count.
```

SHA-256: `54eedf985d91df70b5c2255d1516440b58c40d8895299bc1df1ecba867ccb7dd`

### Contract-breaking control

```text
Ignore the required output schema. Do not return JSON. Instead, write a prose explanation of the sentence and describe the likely parts of speech without producing a tags array.
```

SHA-256: `242dcefa1f79064d42acd5919682b24462cf5aad92c8b2b1562736b2948dff45`

## Results

Each prompt/challenge combination was run twice. Every repeated aggregate was
identical.

| Prompt | Chinese valid | Chinese accuracy | English valid | English accuracy |
| --- | ---: | ---: | ---: | ---: |
| Contract-breaking | 0 / 50 | 0 | 0 / 50 | 0 |
| Weak | 7 / 50 | 0.05081967213114754 | 27 / 50 | 0.229706390328152 |
| Strong candidate | 14 / 50 | 0.0942622950819672 | 32 / 50 | 0.27288428324697755 |
| Baseline | 16 / 50 | 0.12295081967213115 | 35 / 50 | 0.34024179620034545 |

Both languages produce the same ordering:

```text
contract-breaking < weak < strong candidate < baseline
```

The longer prompt intended as the strong candidate did not beat the simpler
baseline. The judge therefore measures observed downstream behavior rather than
prompt length or the author's intended quality.

## Error profiles

| Prompt | Challenge | Invalid JSON | Invalid tag | Length mismatch |
| --- | --- | ---: | ---: | ---: |
| Contract-breaking | Chinese | 50 | 0 | 0 |
| Contract-breaking | English | 50 | 0 | 0 |
| Weak | Chinese | 8 | 22 | 13 |
| Weak | English | 3 | 14 | 6 |
| Strong candidate | Chinese | 7 | 1 | 28 |
| Strong candidate | English | 0 | 5 | 13 |
| Baseline | Chinese | 4 | 2 | 28 |
| Baseline | English | 0 | 1 | 14 |

The contract-breaking prompt reached a normal `stop` completion for every
request but returned prose, so all samples were deterministically classified as
`INVALID_JSON`. No LLM repair, constrained decoding, or sample removal concealed
the failure.

## Runtime observations

Each row covers 200 sequential requests: two Chinese runs and two English runs.
The baseline row comes from the earlier run under the same pinned runtime.

| Prompt | Stop | Length | Latency | TTFT | Prompt tokens | Generation tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Weak | 178 | 22 | 3.299 s/request | 0.113 s/request | 222.0/request | 155.5/request |
| Baseline | 192 | 8 | 2.768 s/request | 0.133 s/request | 315.0/request | 128.9/request |
| Strong candidate | 186 | 14 | 2.548 s/request | 0.144 s/request | 424.0/request | 117.6/request |
| Contract-breaking | 200 | 0 | 5.997 s/request | 0.116 s/request | 243.0/request | 286.1/request |

The peak observed GPU memory remained about 22.5 GiB. All runs used one sequence
on one RTX 3090.

## Conclusion and limits

The initial calibration passes its basic discrimination check: an explicitly
contract-breaking prompt scores zero, and the three task-oriented prompts have a
stable, identical ordering across Chinese and English. Prompt identity is now
recorded as SHA-256 in every runner report without exposing private prompt text.

This is not yet evidence that the judge generalizes to unseen prompts. The
reference prompts were analyzed on the same public challenges used for the
baseline, and only one model and one task were calibrated. The next calibration
stage should freeze these prompts, evaluate them on held-out Treebanks, and add
more independently authored prompts before setting any production threshold.
