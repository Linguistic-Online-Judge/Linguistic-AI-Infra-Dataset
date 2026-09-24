#!/usr/bin/env python3
"""Audit registered challenge inputs and canonical outputs with the pinned tokenizer."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from linguistic_oj.challenge import (
    PublicChallenge,
    load_challenge_artifacts,
    validated_gold_item_count,
)
from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.dataset import DatasetSample, load_dataset_samples_by_id
from linguistic_oj.model_inputs import build_model_input, response_expectations
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.providers import GenerationSettings, ModelRequest, PromptEnvelope
from linguistic_oj.qwen_runtime import TokenizerIdentity, load_huggingface_tokenizer
from linguistic_oj.responses import UD_UPOS_TAGS, TaskType, parse_model_response


def _token_ids(value: object) -> tuple[int, ...]:
    if isinstance(value, Mapping):
        value = value.get("input_ids")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("tokenizer returned an invalid token sequence")
    tokens = tuple(value)
    if any(type(token) is not int or token < 0 for token in tokens):
        raise ValueError("tokenizer returned an invalid token sequence")
    return tokens


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_gold_response(sample: DatasetSample, task: TaskType) -> str:
    gold = sample.answers.get(task.value)
    if not isinstance(gold, list) or not gold:
        raise ValueError(f"sample {sample.id} has no {task.value} gold")
    if task is TaskType.SEGMENTATION:
        payload: dict[str, object] = {"tokens": gold}
    elif task in {TaskType.UPOS, TaskType.XPOS}:
        payload = {"tags": gold}
    elif task is TaskType.DEPENDENCY:
        payload = {
            "arcs": [
                {
                    "deprel": arc[4],
                    "head_id": arc[2],
                    "token_id": arc[0],
                }
                for arc in gold
            ]
        }
    else:
        payload = {"transliterations": gold}
    return _compact_json(payload)


def _completion_tokens(tokenizer: object, response: str) -> int:
    encode = getattr(tokenizer, "encode", None)
    if not callable(encode):
        raise TypeError("tokenizer must provide encode")
    return len(_token_ids(encode(response, add_special_tokens=False)))


def _rendered_tokens(
    tokenizer: object,
    request: ModelRequest,
    identity: TokenizerIdentity,
) -> int:
    apply_chat_template = getattr(tokenizer, "apply_chat_template", None)
    if not callable(apply_chat_template):
        raise TypeError("tokenizer must provide apply_chat_template")
    return len(
        _token_ids(
            apply_chat_template(
                list(PromptEnvelope.from_request(request).to_messages()),
                tokenize=True,
                add_generation_prompt=identity.add_generation_prompt,
                enable_thinking=identity.enable_thinking,
            )
        )
    )


def _next_power_of_two(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("value must be a positive integer")
    return 1 << (value - 1).bit_length()


def _dataset_path(root: Path, language: str) -> Path:
    matches = sorted((root / "Standard_Dataset" / "by_language").glob(f"{language}_*.jsonl"))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one dataset file for {language}")
    return matches[0]


def _generation_settings(contract: EvaluationContract) -> GenerationSettings:
    value = contract.evaluation_identity.get("generation_settings")
    if not isinstance(value, dict):
        raise ValueError(f"contract {contract.challenge_id} has no generation settings")
    return GenerationSettings(**value)


def _tokenizer_identity(contract: EvaluationContract) -> TokenizerIdentity:
    value = contract.evaluation_identity.get("tokenizer_identity")
    if not isinstance(value, dict):
        raise ValueError(f"contract {contract.challenge_id} has no tokenizer identity")
    return TokenizerIdentity.from_mapping(value)


def _audit_challenge(
    root: Path,
    contract: EvaluationContract,
    public: PublicChallenge,
    tokenizer: object,
    tokenizer_identity: TokenizerIdentity,
    student_prompt: str,
) -> dict[str, object]:
    private_path = root / "runtime" / "private" / "challenges" / f"{contract.challenge_id}.json"
    language = public.language
    public_path = root / "challenges" / "public" / f"{contract.challenge_id}.json"
    dataset_path = _dataset_path(root, language)
    artifacts = load_challenge_artifacts(
        public_path,
        private_path,
        dataset_path=dataset_path,
    )
    samples = load_dataset_samples_by_id(dataset_path, artifacts.private.sample_ids)
    task = TaskType(artifacts.public.task)
    settings = _generation_settings(contract)
    max_items = 0
    max_rendered = 0
    max_canonical_gold = 0

    for manifest_sample, sample in zip(artifacts.private.samples, samples, strict=True):
        if task.value not in sample.tasks_available:
            raise ValueError(f"sample {sample.id} does not advertise {task.value}")
        gold_items = validated_gold_item_count(sample, task)
        if gold_items != manifest_sample.gold_items:
            raise ValueError(f"sample {sample.id} gold denominator changed")
        model_input = build_model_input(sample, task)
        request = ModelRequest(
            task=task,
            language=artifacts.public.language,
            treebank=artifacts.public.treebank,
            student_prompt=student_prompt,
            model_input=model_input,
        )
        response = canonical_gold_response(sample, task)
        expected_count, expected_token_ids = response_expectations(task, model_input)
        parsed = parse_model_response(
            task,
            response,
            expected_count=expected_count,
            expected_token_ids=expected_token_ids,
        )
        if parsed.error is not None or parsed.value is None:
            raise ValueError(f"canonical gold response failed to parse for {sample.id}")
        max_items = max(max_items, gold_items)
        max_rendered = max(
            max_rendered,
            _rendered_tokens(tokenizer, request, tokenizer_identity),
        )
        max_canonical_gold = max(
            max_canonical_gold,
            _completion_tokens(tokenizer, response),
        )

    structural_bounded = task is TaskType.UPOS
    domain_worst = None
    if structural_bounded:
        domain_worst = max(
            _completion_tokens(
                tokenizer,
                _compact_json({"tags": [tag] * max_items}),
            )
            for tag in sorted(UD_UPOS_TAGS)
        )
    required_completion = domain_worst or max_canonical_gold
    minimum_with_termination = required_completion + 1
    fits_output = settings.max_tokens >= minimum_with_termination
    fits_rendered = max_rendered <= contract.max_rendered_input_tokens
    fits_context = max_rendered + settings.max_tokens <= contract.model_context_tokens
    contract_limits_fit_context = (
        contract.max_rendered_input_tokens + settings.max_tokens
        <= contract.model_context_tokens
    )
    return {
        "challenge_id": contract.challenge_id,
        "contract_snapshot_sha256": contract.contract_snapshot_sha256,
        "dataset_sha256": artifacts.public.dataset_sha256,
        "evaluation_identity_sha256": contract.evaluation_identity_sha256,
        "configured": {
            "max_rendered_input_tokens": contract.max_rendered_input_tokens,
            "max_tokens": settings.max_tokens,
            "model_context_tokens": contract.model_context_tokens,
        },
        "maxima": {
            "canonical_gold_content_tokens": max_canonical_gold,
            "canonical_upos_domain_worst_tokens": domain_worst,
            "gold_items": max_items,
            "rendered_input_tokens_for_prompt": max_rendered,
        },
        "minimum_max_tokens_with_termination": minimum_with_termination,
        "recommended_power_of_two_max_tokens": _next_power_of_two(
            minimum_with_termination
        ),
        "sample_count": len(samples),
        "selection_sha256": artifacts.public.selection_sha256,
        "structural_output_bound": {
            "bounded": structural_bounded,
            "method": (
                "canonical compact JSON with the longest uniform allowed UPOS tag"
                if structural_bounded
                else "unbounded strings; canonical selected gold is measured"
            ),
        },
        "task": task.value,
        "verdict": (
            "pass"
            if fits_output and fits_rendered and fits_context and contract_limits_fit_context
            else "fail"
        ),
        "checks": {
            "configured_output_budget_fits": fits_output,
            "contract_limits_fit_context": contract_limits_fit_context,
            "prompt_render_fits_context": fits_context,
            "prompt_render_fits_limit": fits_rendered,
        },
    }


def build_report(
    root: Path,
    tokenizer_snapshot: Path,
    student_prompt: str,
) -> dict[str, object]:
    root = root.resolve()
    registry_path = root / "config" / "challenge_contract_registry_v1.json"
    registry = load_challenge_contract_registry(root, registry_path)
    contracts = tuple(registry.contracts[key] for key in sorted(registry.contracts))
    if not contracts:
        raise ValueError("registry has no executable contracts")
    expected_identity = _tokenizer_identity(contracts[0])
    if any(_tokenizer_identity(contract) != expected_identity for contract in contracts):
        raise ValueError("registered contracts do not share one tokenizer identity")
    actual_identity = TokenizerIdentity.from_snapshot(
        tokenizer_snapshot.resolve(),
        repository=expected_identity.repository,
        revision=expected_identity.revision,
        add_generation_prompt=expected_identity.add_generation_prompt,
        enable_thinking=expected_identity.enable_thinking,
    )
    if actual_identity != expected_identity:
        raise ValueError("tokenizer snapshot does not match registered contracts")
    tokenizer = load_huggingface_tokenizer(tokenizer_snapshot)
    prompt_tokens = _completion_tokens(tokenizer, student_prompt)
    challenges = [
        _audit_challenge(
            root,
            contract,
            registry.public_challenges[contract.challenge_id],
            tokenizer,
            actual_identity,
            student_prompt,
        )
        for contract in contracts
    ]
    report: dict[str, Any] = {
        "schema_version": "qwen-token-budget-audit-v1",
        "registry_sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        "tokenizer_identity": actual_identity.to_dict(),
        "prompt": {
            "sha256": hashlib.sha256(student_prompt.encode("utf-8")).hexdigest(),
            "tokens": prompt_tokens,
            "universal_student_prompt_certified": False,
        },
        "challenges": challenges,
        "verdict": "pass" if all(item["verdict"] == "pass" for item in challenges) else "fail",
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--tokenizer-snapshot", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    student_prompt = args.prompt_file.read_text(encoding="utf-8")
    report = build_report(args.root, args.tokenizer_snapshot, student_prompt)
    payload = f"{json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)}\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8", newline="\n")
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
