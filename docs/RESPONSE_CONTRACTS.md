# Model Response Contracts

## General rules

Every model response must be one JSON object and nothing else. Markdown code
fences, explanations, reasoning text, comments, missing fields, extra fields, and
automatic type coercion are rejected. The parser returns a deterministic error
category without using another LLM to repair the response. The upcoming
evaluation runner will convert it into a malformed sample outcome; the aggregation
layer then counts that sample as zero while retaining its gold denominator.

The platform can obtain the structural portion of each contract as JSON Schema
through `response_json_schema()` and include it in the fixed task envelope.
Contextual checks remain in `parse_model_response()`: fixed token count, the 17
UPOS tags, dependency token IDs, and valid dependency head IDs.

## Segmentation

Input shown to the model:

```json
{"text":"然而，这样的处理也衍生了一些问题。"}
```

Required response:

```json
{"tokens":["然而","，","这样","的","处理","也","衍生","了","一些","问题","。"]}
```

The parser does not compare token count with gold data because choosing token
boundaries is the task. The scorer requires the concatenated predicted token
surface to equal the gold surface before computing span precision, recall, and
F1.

## UPOS and XPOS

The platform supplies fixed tokenization so tagging measures tags rather than a
mixture of segmentation and tagging ability.

Input shown to the model:

```json
{"tokens":["然而","，","这样"]}
```

Required response:

```json
{"tags":["SCONJ","PUNCT","PRON"]}
```

The number of tags must equal the number of input tokens. UPOS values must be one
of the 17 UD universal tags. XPOS values are treebank-specific non-empty strings.

## Dependency

Input shown to the model:

```json
{"tokens":[{"token_id":1,"form":"我"},{"token_id":2,"form":"没有"}]}
```

Required response:

```json
{
  "arcs": [
    {"token_id":1,"head_id":2,"deprel":"nsubj"},
    {"token_id":2,"head_id":0,"deprel":"root"}
  ]
}
```

Every input token ID must appear exactly once. `head_id` must be `0` for ROOT or
reference an input token ID. The scorer computes UAS from `head_id` and LAS from
`head_id` plus exact `deprel`.

## Transliteration

Token-level output is the official response contract. The platform supplies the
source text and fixed tokenization:

```json
{
  "text":"然而，这样的处理",
  "tokens":["然而","，","这样","的","处理"]
}
```

Required response:

```json
{"transliterations":["rán'ér",",","zhèyàng","de","chùlǐ"]}
```

The output count must equal the input token count. The primary metric is exact
token accuracy. The secondary metric is sentence exact match, which is true only
when every token transliteration is correct. Comparison performs Unicode NFC
normalization but does not remove tones, fold case, trim spaces, or replace
punctuation.

The first transliteration challenge should use one curated treebank, initially
Chinese GSDSimp, because underscore, punctuation, spacing, and syllable-boundary
conventions differ across UD treebanks.

## Error codes

The parser returns one deterministic category for malformed output:

| Code | Meaning |
| --- | --- |
| `UNKNOWN_TASK` | Unsupported task name |
| `INVALID_JSON` | Response is not valid JSON |
| `TOP_LEVEL_NOT_OBJECT` | Top-level JSON value is not an object |
| `MISSING_FIELD` | Required field is absent |
| `EXTRA_FIELD` | Unspecified field is present |
| `WRONG_TYPE` | Field or item has the wrong JSON type |
| `EMPTY_VALUE` | Required list or string is empty |
| `INVALID_VALUE` | Numeric or other field constraint failed |
| `INVALID_TAG` | UPOS value is outside the UD tag inventory |
| `DUPLICATE_TOKEN_ID` | Dependency token ID appears more than once |
| `LENGTH_MISMATCH` | Output item count differs from fixed input tokens |
| `TOKEN_ID_MISMATCH` | Dependency IDs differ from fixed input IDs |
| `INVALID_HEAD_ID` | Dependency head does not reference ROOT or an input token |

`UNKNOWN_TASK` indicates a platform configuration error. The runner must abort
the evaluation instead of converting it into a student's malformed zero. The
remaining codes describe model-response failures that may be aggregated.
