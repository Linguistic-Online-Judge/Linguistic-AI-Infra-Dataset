# Private Synthetic UPOS Calibration Protocol

## Status and purpose

This document freezes the evaluation protocol before any listed prompt is run
against the private synthetic data. The experiment tests whether the fixed judge
distinguishes newly authored prompts on unpublished inputs without adapting a
prompt or gold label to the resulting scores.

This is a calibration experiment, not a production threshold-setting exercise.
The inputs and gold labels remain outside Git under `runtime/private/`.

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
a separate adjudication pass and changed five tokens before freezing. Both sets
contain all 17 UPOS tags, globally unique IDs, aligned token/tag arrays, and no
exact duplicate sentences.

| Language | Samples | Gold tokens | Dataset SHA-256 |
| --- | ---: | ---: | --- |
| Chinese | 50 | 509 | `70ce7e00bc033ac731c58134119906d8f0d8bda7429031f004bbbfb91920c46b` |
| English | 50 | 437 | `45196f5d1899ecffb7045efb7935d82d35937a1708ecf9233cd64fd58cfc1f28` |

The challenge Treebank identity is `PrivateSynthetic`, selection count is all 50
records, selection seed is `2028`, and version is `private-v1`. Public-style
challenge descriptions, private manifests, samples, raw answers, and gold labels
must all remain under `runtime/private/`; the current builder cannot truthfully
assign a production private-security level.

The sentences are unpublished and hidden from the evaluated self-hosted model,
but they were created and reviewed through isolated AI-assisted sessions. They
therefore cannot be claimed confidential from the authoring service provider.
Production anti-cheating data still requires offline independent human creation,
rights review, access control, and backend private-challenge validation.

## Execution plan

The matrix contains eight prompts and two private challenges. Every combination
is run twice, for 1,600 sequential provider requests. The full aggregate JSON
from each repeated pair must be byte-identical; otherwise both outcomes are
reported and the experiment pauses without selecting the better run.

Before model execution:

1. Recompute all six frozen file hashes.
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
