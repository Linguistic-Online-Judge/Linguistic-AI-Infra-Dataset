# Private Synthetic UPOS Calibration Protocol

## Status and purpose

This document freezes the evaluation protocol before any listed prompt is run
against the private synthetic data. The experiment tests whether the fixed judge
distinguishes newly authored prompts on unpublished inputs without adapting a
prompt or gold label to the resulting scores.

This is a calibration experiment, not a production threshold-setting exercise.
The inputs and gold labels remain outside Git under `runtime/private/`. In this
document and the challenge IDs, `private` means locally stored and unpublished;
it is a provenance label, not an implemented platform security level.

The protocol was committed as `8bddc06` and merged as `4b6b456` before the
first real-model request. Execution then completed without changing any frozen
prompt, dataset, model, or generation setting.

## Fixed model and contracts

- Model: `Qwen/Qwen3.5-9B`
- Revision: `c202236235762e1c871ad0ccb60c8ee5ba337b9a`
- Runtime: vLLM `0.27.1+cu129`
- Temperature: `0`
- Top-p: `1`
- Generation seed: `2026`
- Maximum generated tokens: `1024`
- Thinking: disabled
- Maximum concurrent sequences: `1`
- Prompt Envelope: `1.0`
- UPOS response schema: `upos-v1`
- Scorer and aggregation: `1.0`

JSON-constrained decoding, output repair, a second judging model, and per-sample
retries are prohibited. A response that reaches the token limit is scored as
returned. A provider or service failure aborts the run and may be retried only
after the infrastructure fault is fixed.

## Frozen prompts

The four existing reference prompts remain unchanged:

| Role | SHA-256 |
| --- | --- |
| Contract-breaking control | `242dcefa1f79064d42acd5919682b24462cf5aad92c8b2b1562736b2948dff45` |
| Weak reference | `472043ac3ad2683547803bf46f5768f72fecccdfbab39a87d2fe8d44923901f1` |
| Strong candidate reference | `54eedf985d91df70b5c2255d1516440b58c40d8895299bc1df1ecba867ccb7dd` |
| Baseline reference | `e19f8507e18d3274ba4a9247e273312ce5a2b3f59be6c49ecb64097aafa48e92` |

Four new prompts were produced in isolated authoring sessions. Each author saw
only the task, tag inventory, and response contract. Authors did not access the
repository, reference prompts, private data, or prior scores. The labels are
neutral identities, not predicted quality ranks.

| Identity | Public source | SHA-256 |
| --- | --- | --- |
| Alpha | `prompts/reference/upos-independent-alpha-v1.txt` | `bfd9ae0e238564ca0552b08e757a13fb66e3d7ae2de7437766829d0808e07381` |
| Beta | `prompts/reference/upos-independent-beta-v1.txt` | `2ab23d2ae5361ff985929de4c75de6df1d8932868498fcc04ff034a8dfb616db` |
| Gamma | `prompts/reference/upos-independent-gamma-v1.txt` | `7a273546197734f471bd7699388d62ff3d2537e13d9e0a55f197166985bed191` |
| Delta | `prompts/reference/upos-independent-delta-v1.txt` | `656656fd5ce5d9741f5c481165e56fde74998e761e8ec769a1c879fe7de71213` |

The new prompts are AI-assisted independent drafts, not four independent human
authors. They are preserved byte-for-byte with LF line endings and are not
edited after authoring.

## Frozen private data

Two authors working without access to any evaluated prompt or score created 50
original synthetic sentences per language. A different reviewer audited every
token and tag in each language. English required no gold changes. Chinese used
a separate adjudication pass and changed five tokens before freezing. Together
the sets cover all 17 UPOS tags; Chinese has no deliberately unclassifiable `X`
item. Both have globally unique IDs, aligned token/tag arrays, and no exact
duplicate sentences.

| Language | Samples | Gold tokens | UPOS tags | Dataset SHA-256 |
| --- | ---: | ---: | ---: | --- |
| Chinese | 50 | 509 | 16 | `70ce7e00bc033ac731c58134119906d8f0d8bda7429031f004bbbfb91920c46b` |
| English | 50 | 437 | 17 | `45196f5d1899ecffb7045efb7935d82d35937a1708ecf9233cd64fd58cfc1f28` |

The challenge Treebank identity is `PrivateSynthetic`, selection count is all 50
records, selection seed is `2028`, and version is `private-v1`. Public-style
challenge descriptions, private manifests, samples, raw answers, and gold labels
must all remain under `runtime/private/`; the current builder cannot truthfully
assign a production private-security level.

The sentences were held out from the evaluated self-hosted model before this
execution and were sent only to that approved inference service during scoring.
They were created and reviewed through isolated AI-assisted sessions, so those
services saw the sentence text, tokenization, and gold UPOS labels. The data
therefore cannot be claimed confidential from the authoring or review service
providers.
Production anti-cheating data still requires offline independent human creation,
rights review, access control, and backend private-challenge validation.

## Execution plan

The matrix contains eight prompts and two private challenges. Every combination
is run twice, for 1,600 sequential provider requests. The full aggregate JSON
from each repeated pair must be byte-identical; otherwise both outcomes are
reported and the experiment pauses without selecting the better run.

Before model execution:

1. Recompute the four new-prompt hashes and two private-dataset hashes.
2. Validate all 100 samples and model inputs with repository code.
3. Build challenge artifacts into private runtime directories.
4. Run every prompt with the Mock Provider and confirm prompt-independent output.
5. Confirm the pinned model, runtime, VPN, and GPU allocation.

The self-hosted campus inference server is the only model provider allowed to
receive the private token sequences. Raw model responses are not committed.

## Analysis rules

Report micro accuracy, valid sample count, and aggregate error profile for every
prompt/challenge combination. The existing four references retain their known
role labels, but no rank is predicted for Alpha, Beta, Gamma, or Delta.

The analysis asks:

1. Does the contract-breaking control remain at zero?
2. Does the known reference ordering replicate on each private language?
3. Do independently authored prompts produce a nonzero score spread?
4. Are accuracy differences accompanied by output-validity differences?
5. Are all repeated aggregates deterministic under the fixed runtime?

No production pass/fail threshold will be chosen from these 100 samples. After
the first model request, a discovered annotation concern is documented but does
not alter `private-v1`; a corrected dataset requires a new version.

## Results

The private challenge selection hashes are
`463e9e3b846d48796e795198483142dcd5d4f484cf1f22efe408289ff3168bb7`
for Chinese and
`f4f5f69e0a5d21640063b65853f4334f419f09e29170483c48c87c9a62cb2504`
for English. Challenge descriptions and manifests remain private. The Mock
Provider produced prompt-independent output: all samples were valid, Chinese
scored `0`, and English scored `0.011441647597254004` for all eight prompts.

Every real prompt/challenge combination was run twice. All 16 repeated
aggregates were byte-identical.

| Prompt | Chinese valid | Chinese accuracy | English valid | English accuracy |
| --- | ---: | ---: | ---: | ---: |
| Contract-breaking | 0 / 50 | 0 | 0 / 50 | 0 |
| Weak | 27 / 50 | 0.3791748526522593 | 33 / 50 | 0.5194508009153318 |
| Strong candidate | 37 / 50 | 0.49901768172888017 | 41 / 50 | 0.562929061784897 |
| Baseline | 41 / 50 | 0.5265225933202358 | 46 / 50 | 0.7070938215102975 |
| Alpha | 40 / 50 | 0.5520628683693517 | 44 / 50 | 0.6498855835240275 |
| Beta | 41 / 50 | 0.5540275049115914 | 42 / 50 | 0.6224256292906178 |
| Gamma | 41 / 50 | 0.581532416502947 | 44 / 50 | 0.6292906178489702 |
| Delta | 40 / 50 | 0.518664047151277 | 45 / 50 | 0.6819221967963387 |

The known references reproduce their prior ordering in both languages:

```text
contract-breaking < weak < strong candidate < baseline
```

The independently authored prompts have a nonzero spread but no common ranking:

```text
Chinese: delta < alpha < beta < gamma
English: beta < gamma < alpha < delta
```

The independent-prompt score range is `0.062868` in Chinese and `0.059497` in
English. Their descriptive Spearman rank correlation is `-0.8`, but four prompts
are far too few for statistical inference. All four beat the strong reference in
both languages. Three beat the baseline in Chinese; none beat it in English.
This is evidence of discrimination and language dependence, not evidence for one
universally best prompt.

## Error profiles

| Prompt | Chinese invalid JSON | Chinese invalid tag | Chinese length | English invalid JSON | English invalid tag | English length |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Contract-breaking | 50 | 0 | 0 | 50 | 0 | 0 |
| Weak | 0 | 18 | 5 | 0 | 9 | 8 |
| Strong candidate | 0 | 1 | 12 | 0 | 0 | 9 |
| Baseline | 0 | 0 | 9 | 0 | 0 | 4 |
| Alpha | 0 | 0 | 10 | 0 | 0 | 6 |
| Beta | 0 | 0 | 9 | 0 | 3 | 5 |
| Gamma | 0 | 0 | 9 | 0 | 1 | 5 |
| Delta | 0 | 1 | 9 | 0 | 1 | 4 |

The contract-breaking control again completed normally but returned prose. For
task-oriented prompts, token-count mismatch remained the dominant protocol
failure. Accuracy and output validity are related but not interchangeable: for
example, Alpha and Gamma have equal English validity but different accuracy.

## Runtime observations

Each row covers 200 sequential requests: two Chinese and two English runs.

| Prompt | Prompt tokens/request | Generated tokens/request | TTFT | Latency |
| --- | ---: | ---: | ---: | ---: |
| Contract-breaking | 223.18 | 203.20 | 0.099 s | 4.290 s |
| Weak | 202.18 | 30.15 | 0.094 s | 0.695 s |
| Strong candidate | 404.18 | 29.91 | 0.135 s | 0.732 s |
| Baseline | 295.18 | 36.86 | 0.120 s | 0.860 s |
| Alpha | 1288.18 | 67.23 | 0.343 s | 1.718 s |
| Beta | 1095.18 | 60.67 | 0.274 s | 1.510 s |
| Gamma | 1403.18 | 53.84 | 0.348 s | 1.444 s |
| Delta | 1053.18 | 29.96 | 0.273 s | 0.871 s |

All 1,600 requests ended with a normal `stop`; there were no token-limit,
abort, repetition, or service-error completions. Overall means were 745.555
prompt tokens, 63.9775 generated tokens, 0.211 seconds to first token, and 1.515
seconds end-to-end per request. Peak GPU memory was 23,392 MiB on one RTX 3090.

## Outcome and limits

The experiment addresses all five preregistered analysis questions. It
demonstrates deterministic scoring, a reliable contract-breaking control,
stable ordering for known references, and meaningful score dispersion for unseen
prompts. It also shows that prompt comparisons cannot safely be generalized from
one language: the independent-prompt rankings reverse substantially across
Chinese and English.

The synthetic sentences are shorter and cleaner than public UD samples, which
likely contributes to their higher scores. The data were AI-assisted, only 100
samples were used, and the authoring and review providers saw the text, tokens,
and gold labels. Published dataset and selection hashes cannot reconstruct the
corpus by themselves, but they can confirm a candidate copy and therefore create
a bounded confidentiality risk. These aggregates must not define a production
threshold. The next security and validity gate is an offline, independently
human-authored and double-annotated private set with documented rights,
adjudication, and backend access controls.
