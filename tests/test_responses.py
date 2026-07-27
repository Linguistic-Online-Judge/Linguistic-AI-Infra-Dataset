import json

import pytest

from linguistic_oj import (
    DependencyResponse,
    ParseErrorCode,
    SegmentationResponse,
    TaggingResponse,
    TaskType,
    TransliterationResponse,
    parse_model_response,
    response_json_schema,
)


def test_parse_segmentation_response() -> None:
    result = parse_model_response("segmentation", '{"tokens":["然而","，","这样"]}')

    assert result.is_valid is True
    assert isinstance(result.value, SegmentationResponse)
    assert result.value.tokens == ["然而", "，", "这样"]


@pytest.mark.parametrize("task", [TaskType.UPOS, TaskType.XPOS])
def test_parse_tagging_response(task: TaskType) -> None:
    result = parse_model_response(task, '{"tags":["SCONJ","PUNCT"]}', expected_count=2)

    assert result.is_valid is True
    assert isinstance(result.value, TaggingResponse)


def test_parse_dependency_response() -> None:
    raw = json.dumps(
        {
            "arcs": [
                {"token_id": 1, "head_id": 2, "deprel": "nsubj"},
                {"token_id": 2, "head_id": 0, "deprel": "root"},
            ]
        }
    )

    result = parse_model_response(
        TaskType.DEPENDENCY,
        raw,
        expected_count=2,
        expected_token_ids=[1, 2],
    )

    assert result.is_valid is True
    assert isinstance(result.value, DependencyResponse)


def test_parse_transliteration_response() -> None:
    raw = json.dumps({"transliterations": ["rán'ér", ",", "zhèyàng"]})

    result = parse_model_response(TaskType.TRANSLITERATION, raw, expected_count=3)

    assert result.is_valid is True
    assert isinstance(result.value, TransliterationResponse)


def test_markdown_code_fence_is_invalid_json() -> None:
    result = parse_model_response("segmentation", '```json\n{"tokens":["test"]}\n```')

    assert result.error is not None
    assert result.error.code is ParseErrorCode.INVALID_JSON


def test_top_level_array_is_rejected() -> None:
    result = parse_model_response("segmentation", '["test"]')

    assert result.error is not None
    assert result.error.code is ParseErrorCode.TOP_LEVEL_NOT_OBJECT


def test_missing_required_field_is_rejected() -> None:
    result = parse_model_response("upos", "{}")

    assert result.error is not None
    assert result.error.code is ParseErrorCode.MISSING_FIELD


def test_extra_field_is_rejected() -> None:
    result = parse_model_response("upos", '{"tags":["NOUN"],"reasoning":"..."}')

    assert result.error is not None
    assert result.error.code is ParseErrorCode.EXTRA_FIELD


def test_wrong_item_type_is_rejected_without_coercion() -> None:
    result = parse_model_response("upos", '{"tags":[1]}')

    assert result.error is not None
    assert result.error.code is ParseErrorCode.WRONG_TYPE


def test_unknown_upos_tag_is_rejected() -> None:
    result = parse_model_response("upos", '{"tags":["NOT_A_TAG"]}')

    assert result.error is not None
    assert result.error.code is ParseErrorCode.INVALID_TAG


def test_xpos_accepts_language_specific_tag() -> None:
    result = parse_model_response("xpos", '{"tags":["NN-custom"]}')

    assert result.is_valid is True


def test_empty_token_is_rejected() -> None:
    result = parse_model_response("segmentation", '{"tokens":[""]}')

    assert result.error is not None
    assert result.error.code is ParseErrorCode.EMPTY_VALUE


def test_non_segmentation_length_mismatch_is_rejected() -> None:
    result = parse_model_response(
        "transliteration", '{"transliterations":["wǒ"]}', expected_count=2
    )

    assert result.error is not None
    assert result.error.code is ParseErrorCode.LENGTH_MISMATCH


def test_segmentation_does_not_enforce_gold_token_count() -> None:
    result = parse_model_response("segmentation", '{"tokens":["研究生","命"]}', expected_count=3)

    assert result.is_valid is True


def test_duplicate_dependency_token_id_is_rejected() -> None:
    raw = json.dumps(
        {
            "arcs": [
                {"token_id": 1, "head_id": 0, "deprel": "root"},
                {"token_id": 1, "head_id": 1, "deprel": "dep"},
            ]
        }
    )

    result = parse_model_response("dependency", raw)

    assert result.error is not None
    assert result.error.code is ParseErrorCode.DUPLICATE_TOKEN_ID


def test_dependency_token_id_mismatch_is_rejected() -> None:
    raw = '{"arcs":[{"token_id":2,"head_id":0,"deprel":"root"}]}'

    result = parse_model_response("dependency", raw, expected_token_ids=[1])

    assert result.error is not None
    assert result.error.code is ParseErrorCode.TOKEN_ID_MISMATCH


def test_dependency_head_id_must_reference_input_token() -> None:
    raw = '{"arcs":[{"token_id":1,"head_id":99,"deprel":"dep"}]}'

    result = parse_model_response("dependency", raw, expected_token_ids=[1])

    assert result.error is not None
    assert result.error.code is ParseErrorCode.INVALID_HEAD_ID


def test_unknown_task_is_rejected() -> None:
    result = parse_model_response("lemma", "{}")

    assert result.error is not None
    assert result.error.code is ParseErrorCode.UNKNOWN_TASK


def test_schema_forbids_additional_properties() -> None:
    schema = response_json_schema(TaskType.TRANSLITERATION)

    assert schema["additionalProperties"] is False
