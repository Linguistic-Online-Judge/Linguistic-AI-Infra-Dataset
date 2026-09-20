"""Experimental dependency wire format. Not registered in any production response route."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .providers import ModelRequest, PromptEnvelope
from .responses import (
    ParseErrorCode,
    ResponseParseError,
    ResponseParseResult,
    TaskType,
    parse_model_response,
)

PROTOCOL = "dependency-parallel-arrays-v1"
BASELINE_PROTOCOL = "dependency-v1"
TRIPLES_PROTOCOL = "dependency-triples-v1"
PROTOCOLS = (BASELINE_PROTOCOL, PROTOCOL, TRIPLES_PROTOCOL)
SEMANTIC_INSTRUCTION = (
    "Analyze the syntactic dependencies of the input tokens. "
    "Use the provided token IDs for heads. Use head 0 and lowercase root for the sentence root. "
    "Predict exactly one head and one relation for each input token. "
)


def experiment_prompt(protocol: str) -> str:
    if protocol == BASELINE_PROTOCOL:
        output = "Return an arcs array of objects containing token_id, head_id, and deprel. "
    elif protocol == PROTOCOL:
        output = ("Return heads (integer head IDs) and deprels (relation strings) arrays. "
                  "Both arrays must follow the input token order. ")
    elif protocol == TRIPLES_PROTOCOL:
        output = ("Return an arcs array of three-element arrays. "
                  "Each entry must be [token_id, head_id, deprel]. ")
    else:
        raise ValueError("unsupported experimental protocol")
    return SEMANTIC_INSTRUCTION + output + "Return only JSON, without explanations or extra fields."


def response_schema(protocol=PROTOCOL) -> dict:
    # Cardinality is checked after generation, as in the current object-arc protocol.
    if protocol == TRIPLES_PROTOCOL:
        return {"type": "object", "additionalProperties": False, "required": ["arcs"],
                "properties": {"arcs": {"type": "array", "minItems": 1, "items": {
                    "type": "array", "minItems": 3, "maxItems": 3, "items": False,
                    "prefixItems": [{"type": "integer", "minimum": 1},
                                    {"type": "integer", "minimum": 0},
                                    {"type": "string", "minLength": 1}],
                }}}}
    if protocol != PROTOCOL:
        raise ValueError('unknown compact protocol')
    return {"type": "object", "additionalProperties": False, "required": ["heads", "deprels"],
            "properties": {
                "heads": {"type": "array", "minItems": 1,
                          "items": {"type": "integer", "minimum": 0}},
                "deprels": {"type": "array", "minItems": 1,
                            "items": {"type": "string", "minLength": 1}},
            }}


def experiment_messages(request: ModelRequest, protocol: str):
    if request.task is not TaskType.DEPENDENCY:
        raise ValueError("compact dependency experiments require dependency input")
    envelope = PromptEnvelope.from_request(request)
    messages = envelope.to_messages()
    if protocol == BASELINE_PROTOCOL:
        return messages
    if protocol not in (PROTOCOL, TRIPLES_PROTOCOL):
        raise ValueError("unsupported experimental protocol")
    payload = envelope.to_dict()
    payload["envelope_version"] = "experimental-" + protocol
    payload["required_output_schema"] = response_schema(protocol)
    return (messages[0], {"role": "user", "content": json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON value")


def parse_compact_dependency(raw: str, *, expected_token_ids: Sequence[int],
                             max_utf8_bytes: int = 32768, protocol=PROTOCOL) -> ResponseParseResult:
    """Reject malformed output, then map positions to IDs without repair or gold access."""
    ids = tuple(expected_token_ids)
    if (not ids or any(type(value) is not int for value in ids)
            or ids != tuple(range(1, len(ids) + 1))):
        raise ValueError("experimental input IDs must be contiguous and ordered from one")
    if type(max_utf8_bytes) is not int or max_utf8_bytes <= 0:
        raise ValueError("response byte limit must be positive")
    if protocol not in (PROTOCOL, TRIPLES_PROTOCOL):
        raise ValueError('unknown compact protocol')

    def fail(code, message):
        return ResponseParseResult(TaskType.DEPENDENCY, error=ResponseParseError(code, message))

    if not isinstance(raw, str):
        return fail(ParseErrorCode.WRONG_TYPE, "response must be text")
    try:
        if len(raw.encode("utf-8")) > max_utf8_bytes:
            return fail(ParseErrorCode.INVALID_VALUE, "response exceeds byte limit")
        payload = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return fail(ParseErrorCode.INVALID_JSON, "response must be unambiguous JSON")
    if not isinstance(payload, dict):
        return fail(ParseErrorCode.TOP_LEVEL_NOT_OBJECT, "response must be an object")
    if protocol == TRIPLES_PROTOCOL:
        if 'arcs' not in payload:
            return fail(ParseErrorCode.MISSING_FIELD, 'arcs are required')
        if set(payload) != {'arcs'}:
            return fail(ParseErrorCode.EXTRA_FIELD, 'unexpected response field')
        if not isinstance(payload['arcs'], list):
            return fail(ParseErrorCode.WRONG_TYPE, 'arcs must be an array')
        if not payload['arcs']:
            return fail(ParseErrorCode.EMPTY_VALUE, 'arcs must not be empty')
        if any(not isinstance(arc, list) or len(arc) != 3 for arc in payload['arcs']):
            return fail(ParseErrorCode.WRONG_TYPE, 'each arc must be a three-element array')
        legacy = {'arcs': [{'token_id': arc[0], 'head_id': arc[1], 'deprel': arc[2]}
                           for arc in payload['arcs']]}
        return parse_model_response(TaskType.DEPENDENCY, json.dumps(legacy),
                                    expected_count=len(ids), expected_token_ids=ids)
    if not {"heads", "deprels"} <= set(payload):
        return fail(ParseErrorCode.MISSING_FIELD, "heads and deprels are required")
    if set(payload) != {"heads", "deprels"}:
        return fail(ParseErrorCode.EXTRA_FIELD, "unexpected response field")
    heads, labels = payload["heads"], payload["deprels"]
    if not isinstance(heads, list) or not isinstance(labels, list):
        return fail(ParseErrorCode.WRONG_TYPE, "heads and deprels must be arrays")
    if not heads or not labels:
        return fail(ParseErrorCode.EMPTY_VALUE, "response arrays must not be empty")
    if len(heads) != len(ids) or len(labels) != len(ids):
        return fail(ParseErrorCode.LENGTH_MISMATCH, "one head and relation per token required")
    if (any(type(head) is not int for head in heads)
            or any(type(label) is not str for label in labels)):
        return fail(ParseErrorCode.WRONG_TYPE, "expected integer heads and string relations")
    if any(not label for label in labels):
        return fail(ParseErrorCode.EMPTY_VALUE, "relations must not be empty")
    if any(head < 0 or head > len(ids) for head in heads):
        return fail(ParseErrorCode.INVALID_HEAD_ID, "head ID outside the input token IDs")
    legacy = {"arcs": [{"token_id": token_id, "head_id": head, "deprel": label}
                       for token_id, head, label in zip(ids, heads, labels, strict=True)]}
    # Existing parser remains the final validator. Do not add tree constraints, normalize
    # relation names, insert missing arcs, or consult correct answers here.
    return parse_model_response(TaskType.DEPENDENCY, json.dumps(legacy), expected_count=len(ids),
                                expected_token_ids=ids)
