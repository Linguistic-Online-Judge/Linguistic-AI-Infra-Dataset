# Structured Task Calibration

## Scope

The first five-task Qwen3.5-9B GPU smoke exposed high structural invalidity for
German HDT XPOS and Chinese GSDSimp transliteration. The prompt-only stage
preserved the frozen challenges, model revision, runtime, generation settings,
prompt envelope, response schema, parser, scorer, and aggregation while varying
only the student prompt. A separate structured-output stage deliberately changed
the generation protocol and is evidence for a future versioned contract, not an
update to the existing identity. The existing v1 prompts, contracts, and
observation report remain unchanged.

## Baseline observations

| Task | Valid samples | Invalid JSON | Length mismatch | Primary score |
| --- | ---: | ---: | ---: | ---: |
| HDT XPOS | 25 / 50 | 10 | 15 | 0.12708333333333333 |
| GSDSimp transliteration | 28 / 50 | 2 | 20 | 0.33114754098360655 |

The source observation is
`benchmarks/observations/qwen3.5-9b-v1-five-task-gpu-smoke.json`. A succeeded
challenge result means the full pipeline completed; malformed sample outputs
still receive deterministic zero credit and remain visible in the error counts.

## Candidate prompts

`prompts/reference/xpos-hdt-smoke-v2.txt` adds the complete 49-tag inventory
observed in the HDT source, explicitly preserves HDT's `PROAV` spelling, states
the required `tags` object, and requires the output array length to equal the
input token count. SHA-256:
`9c7423ba9b8a5a376b7f2f97b707d594b2d6bd25e124581776c6c87cc3df385a`.

`prompts/reference/transliteration-gsdsimp-smoke-v2.txt` describes the task as
matching token-level GSDSimp `MISC.Translit` annotations rather than silently
correcting them into idealized contextual Pinyin. It records the treebank's
ASCII punctuation mappings, apostrophe convention, occasional untranslated rare
characters, required `transliterations` object, and exact token-count invariant.
SHA-256:
`5a922d6530cdc4c6e791e019c33a0635ecad61423552d1b5d896f2876231dcfd`.

These prompts are rejected calibration candidates, not validated improvements.
Their hashes and the HDT inventory are checked by
`tests/test_reference_prompts.py`.

The first repeated A/B run rejected both v2 prompts. The XPOS candidate produced
20/50 valid samples and score `0.11354166666666667`, versus 26/50 and
`0.13958333333333334` for the rerun v1 baseline. The transliteration candidate
produced 26/50 valid samples and score `0.1778688524590164`, versus 28/50 and
`0.33114754098360655` for v1. Both v2 runs generated substantially fewer tokens
and completed faster, but the higher length-error rate and score regressions make
them unsuitable replacements. Repeated runs within the same vLLM process were
identical.

The shorter v3 candidates removed the two-item JSON examples and verbose
self-check instructions that may have primed short arrays. XPOS v3 produced
25/50 valid samples and score `0.13229166666666667`; transliteration v3 produced
24/50 valid samples and score `0.3368852459016393`. The slight transliteration
score increase did not offset its validity regression. Both repeated runs were
identical, so v3 was also rejected. XPOS v3 SHA-256:
`fd9f71cc12b1a0ddbcece10031ac40ae94aafbf9465391388cfc5319e3c7e70e`.
Transliteration v3 SHA-256:
`bce43e380538732a0a9e95bba195558041bcab568f2b16ce220783ba1e36cfae`.

## A/B acceptance

Run prompt candidates against the same challenge and runtime. The reports must match on
dataset and selection hashes, challenge ID, model identity, runtime version,
generation settings, prompt envelope, response schema, scorer, and aggregation.
Only `student_prompt_sha256`, aggregate scores, errors, and runtime observations
may differ.

Record separate reports rather than replacing the original evidence. Compare at
least valid sample count, each parser error category, primary score, finish
reasons, generated-token totals, response byte counts, and request latency. Do
not activate a prompt solely because it reduces malformed output; it must also
avoid a material score regression.

## Structured-output result

`dynamic-response-constraint-v5` derives an exact output count from the safe
model input. Transliteration uses a dynamic JSON schema. HDT XPOS uses a compact
finite regex because JSON schema with arbitrary strings allowed repeated
length-limited output even after increasing `max_tokens`; the regex permits only
the 49 frozen HDT tags and exactly the required number of array items.

Both final candidates were run twice with identical scores, validity, completion
reasons, generated-token counts, and response byte counts:

| Task | Baseline valid | Structured valid | Baseline score | Structured score | Baseline time | Structured time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| HDT XPOS | 26 / 50 | 50 / 50 | 0.13958333333333334 | 0.33125 | about 104.2 s | about 64.2 s |
| GSDSimp transliteration | 28 / 50 | 50 / 50 | 0.33114754098360655 | 0.37131147540983606 | about 218.0 s | about 194.5 s |

The structured runs had no parser errors and every completion ended with `stop`.
XPOS generation fell from 4,724 to 2,784 tokens; transliteration fell from 10,257
to 9,090. The result supports a new structured-output evaluation identity. It
does not authorize enabling `--structured-json` on an existing Worker: runtime
attestation explicitly rejects that mode until the contract declares it.

The machine-readable report is
`benchmarks/observations/qwen3.5-9b-structured-output-calibration-v1.json`, with
internal report SHA-256
`3b7fdaaed8528c008da6d977c42d32e0e867639ac5c29c5e0012230a5d5bf7ce`.
