import json
from pathlib import Path

from linguistic_oj.contracts import (
    AGGREGATION_VERSION,
    RESPONSE_SCHEMA_VERSIONS,
    SCORER_VERSION,
)
from linguistic_oj.mvp_contract import (
    EvaluationContract,
    canonical_sha256,
    load_qwen_worker_contract,
)
from linguistic_oj.providers import PROMPT_ENVELOPE_VERSION
from linguistic_oj.responses import TaskType

ROOT = Path(__file__).parents[1]


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_mvp_evaluation_config_matches_frozen_artifacts() -> None:
    config = _load_json(ROOT / "config" / "mvp_evaluation.json")
    challenge = _load_json(ROOT / "challenges" / "public" / "en-ewt-upos-v1.json")
    benchmark = _load_json(
        ROOT / "benchmarks" / "qwen3.5-9b-en-ewt-upos-mvp-v1.json"
    )
    runtime = _load_json(
        ROOT / "benchmarks" / "observations" / "qwen3.5-9b-en-ewt-upos-mvp-v1.json"
    )

    catalog = config["catalog"]
    identity = config["evaluation_identity"]
    assert isinstance(catalog, dict)
    assert isinstance(identity, dict)
    assert config["contract_version"] == identity["contract_version"]
    assert config["contract_version"] == "mvp-evaluation-v1"
    assert catalog["challenge_id"] == identity["challenge_id"]
    assert identity["challenge_id"] == challenge["challenge_id"]
    assert identity["challenge_id"] == benchmark["challenge_id"]
    assert identity["dataset_sha256"] == challenge["dataset_sha256"]
    assert identity["dataset_sha256"] == benchmark["dataset_sha256"]
    assert identity["selection_sha256"] == challenge["selection_sha256"]
    assert identity["selection_sha256"] == benchmark["selection_sha256"]
    assert catalog["security_level"] == challenge["security_level"]
    assert catalog["status"] == challenge["status"] == "draft"

    assert identity["task"] == challenge["task"] == benchmark["task"]
    assert identity["model_identity"] == benchmark["model_identity"]
    assert identity["model_identity"] == runtime["model_identity"]
    assert identity["generation_settings"] == benchmark["generation_settings"]
    assert identity["prompt_envelope_version"] == PROMPT_ENVELOPE_VERSION
    assert identity["prompt_envelope_version"] == benchmark["prompt_envelope_version"]
    assert identity["response_schema_version"] == challenge["response_schema_version"]
    assert identity["response_schema_version"] == RESPONSE_SCHEMA_VERSIONS[TaskType.UPOS]
    assert identity["scorer_version"] == SCORER_VERSION
    assert identity["scorer_version"] == benchmark["scorer_version"]
    assert identity["aggregation_version"] == AGGREGATION_VERSION
    assert identity["aggregation_version"] == benchmark["aggregation_version"]

    assert benchmark["samples_valid"] + benchmark["samples_invalid"] == 50
    assert benchmark["samples_total"] == challenge["sample_count"] == 50
    assert sum(benchmark["errors"].values()) == benchmark["samples_invalid"]
    assert benchmark["primary_metric"] == challenge["primary_metric"]
    assert benchmark["score"] == benchmark["metrics"][benchmark["primary_metric"]]

    assert runtime["benchmark"] == "qwen3.5-9b-en-ewt-upos-mvp-v1.json"
    assert runtime["challenge_id"] == identity["challenge_id"]
    assert runtime["generation_settings"] == identity["generation_settings"]
    assert runtime["student_prompt_sha256"] == benchmark["student_prompt_sha256"]
    assert runtime["runs"] == 2
    assert runtime["request_count"] == 2 * challenge["sample_count"] == 100
    assert sum(runtime["finish_reasons"].values()) == runtime["request_count"]
    assert runtime["finish_reasons"] == {
        "abort": 0,
        "error": 0,
        "length": 0,
        "repetition": 0,
        "stop": 100,
    }
    assert runtime["aggregate_canonicalization"] == "python-json-v1"
    assert runtime["aggregate_sha256"] == canonical_sha256(benchmark)
    assert runtime["run_aggregate_sha256s"] == [runtime["aggregate_sha256"]] * 2

    limits = config["limits"]
    assert isinstance(limits, dict)
    assert (
        limits["max_rendered_input_tokens"]
        + identity["generation_settings"]["max_tokens"]
        == limits["model_context_tokens"]
    )
    assert limits["student_prompt_tokens"] < limits["max_rendered_input_tokens"]

    partition = config["leaderboard_partition"]
    assert isinstance(partition, dict)
    assert partition["algorithm"] == "sha256"
    assert partition["canonicalization"] == "python-json-v1"
    assert partition["canonicalization_parameters"] == {
        "allow_nan": False,
        "ensure_ascii": False,
        "separators": [",", ":"],
        "sort_keys": True,
    }
    assert partition["canonical_json_source"] == "evaluation_identity"
    assert canonical_sha256(identity) == partition["expected_sha256"]
    assert runtime["evaluation_identity_sha256"] == partition["expected_sha256"]

    feedback = config["feedback"]
    assert isinstance(feedback, dict)
    owner_fields = set(feedback["owner_result_fields"])
    public_fields = set(feedback["public_leaderboard_fields"])
    assert owner_fields == {
        "aggregation_version",
        "challenge_id",
        "dataset_sha256",
        "errors",
        "generation_settings",
        "metrics",
        "model_identity",
        "primary_metric",
        "prompt_envelope_version",
        "samples_invalid",
        "samples_total",
        "samples_valid",
        "score",
        "scorer_version",
        "selection_sha256",
        "student_prompt_sha256",
        "task",
    }
    assert public_fields == {
        "evaluation_identity_sha256",
        "public_handle",
        "rank",
        "samples_invalid",
        "samples_total",
        "samples_valid",
        "score",
        "succeeded_at",
    }
    assert set(feedback["owner_failure_fields"]) == {
        "code",
        "failure_contract_version",
        "retryable",
    }
    assert owner_fields <= set(benchmark)
    assert "student_prompt_sha256" in owner_fields
    assert "student_prompt_sha256" not in public_fields
    assert {
        "answers",
        "gold_items",
        "model_input",
        "raw_responses",
        "sample_ids",
        "student_prompt",
    }.isdisjoint(owner_fields | public_fields)

    access = config["access_policy"]
    assert isinstance(access, dict)
    assert access == {
        "draft_submission_policy": "development_override_only",
        "external_activation_requirements": [
            "recorded_source_release_or_commit",
            "recorded_source_file_sha256s",
            "recorded_attribution_requirements",
            "reviewed_share_alike_requirements",
            "reviewed_underlying_text_rights",
        ],
        "leaderboard_identity": "non_email_public_handle",
        "submission_identity": "server_verified_subject",
        "submission_result_access": "owner_only",
    }
    assert catalog["source_release"] == catalog["source_commit"] == "unrecorded"
    assert catalog["source_file_sha256s"] == []
    assert catalog["annotation_license"] == "CC BY-SA 4.0"
    assert catalog["attribution_requirements"] == "unrecorded"
    assert catalog["share_alike_requirements"] == "review_required"
    assert catalog["underlying_text_rights"] == "review_required"

    assert config["idempotency"] == {
        "key_ascii_pattern": "^[A-Za-z0-9._~-]{1,128}$",
        "key_match_semantics": "ascii_fullmatch",
        "key_max_utf8_bytes": 128,
        "request_hash_fields": [
            "challenge_id",
            "contract_version",
            "student_prompt_exact_utf8",
        ],
        "scope": ["user_id", "idempotency_key"],
    }

    failure_contract = config["failure_contract"]
    assert isinstance(failure_contract, dict)
    assert failure_contract == {
        "codes": {
            "DATASET_INTEGRITY": {"retryable": False},
            "JOB_DEADLINE": {"retryable": False},
            "MODEL_IDENTITY_MISMATCH": {"retryable": False},
            "PROVIDER_TIMEOUT": {"retryable": True},
            "PROVIDER_TRANSPORT": {"retryable": True},
            "RUNTIME_MISCONFIGURATION": {"retryable": False},
            "WORKER_CRASH": {"retryable": False},
        },
        "version": "platform-failure-v1",
    }
    retryable_codes = {
        code
        for code, policy in failure_contract["codes"].items()
        if policy["retryable"]
    }
    assert retryable_codes == {
        "PROVIDER_TIMEOUT",
        "PROVIDER_TRANSPORT",
    }

    job_policy = config["job_policy"]
    assert isinstance(job_policy, dict)
    assert job_policy == {
        "job_deadline_seconds": 300,
        "max_attempts": 2,
        "provider_request_timeout_seconds": 120,
        "retryable_failure_codes": sorted(retryable_codes),
        "retry_requires_prior_request_terminated": True,
        "states": ["queued", "running", "rejected", "succeeded", "failed"],
        "token_limit_terminal_state": "rejected",
    }
    assert limits == {
        "api_request_body_bytes": 16384,
        "global_queue_depth": 100,
        "max_outstanding_submissions_per_user": 3,
        "max_rendered_input_tokens": 3840,
        "max_running_submissions_per_user": 1,
        "model_context_tokens": 4096,
        "provider_response_body_bytes": 32768,
        "student_prompt_tokens": 2048,
        "student_prompt_utf8_bytes": 8192,
        "submissions_per_user_per_challenge_per_24h": 5,
        "worker_model_concurrency": 1,
    }
    assert config["request_policy"] == {
        "api_request_body_enforcement": (
            "edge_and_asgi_streaming_before_buffer_or_json_decode"
        ),
        "oversized_request_status": 413,
    }

    runtime_contract = EvaluationContract.from_mapping(config)
    assert runtime_contract.evaluation_identity_sha256 == partition["expected_sha256"]
    assert runtime_contract.job_deadline_seconds == 300
    assert runtime_contract.external_activation_ready is False


def test_qwen_runtime_v2_adds_pinned_tokenizer_partition() -> None:
    config = _load_json(ROOT / "config" / "mvp_evaluation_v2.json")
    identity = config["evaluation_identity"]
    partition = config["leaderboard_partition"]
    assert isinstance(identity, dict)
    assert isinstance(partition, dict)
    assert config["contract_version"] == identity["contract_version"]
    assert config["contract_version"] == "mvp-evaluation-v2"
    assert partition["expected_sha256"] == (
        "97af30df18b531c1eecdbf6a22f3a7983c8c93eb48e338917d8fd10a9e55483d"
    )
    assert canonical_sha256(identity) == partition["expected_sha256"]
    assert identity["tokenizer_identity"] == {
        "add_generation_prompt": True,
        "chat_template_sha256": (
            "a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715"
        ),
        "counting_method": "hf-apply-chat-template-tokenize-v1",
        "enable_thinking": False,
        "repository": "Qwen/Qwen3.5-9B",
        "revision": "c202236235762e1c871ad0ccb60c8ee5ba337b9a",
        "tokenizer_config_sha256": (
            "316230d6a809701f4db5ea8f8fc862bc3a6f3229c937c174e674ff3ca0a64ac8"
        ),
        "tokenizer_json_sha256": (
            "5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42"
        ),
    }

    contract = EvaluationContract.from_mapping(config)
    assert contract.provider_request_timeout_seconds == 120
    assert contract.provider_response_body_bytes == 32768
    assert contract.worker_model_concurrency == 1
    assert contract.retry_requires_prior_request_terminated is True
    assert load_qwen_worker_contract(ROOT) == contract
