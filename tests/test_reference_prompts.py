import hashlib
import json
from pathlib import Path

from linguistic_oj.mvp_contract import EvaluationContract
from linguistic_oj.providers import (
    _XPOS_TAG_INVENTORIES,
    HDT_XPOS_TAG_INVENTORY_SHA256,
    HDT_XPOS_TAG_INVENTORY_VERSION,
)

ROOT = Path(__file__).parents[1]


def test_independent_reference_prompt_hashes_are_frozen() -> None:
    prompt_dir = ROOT / "prompts" / "reference"
    expected = {
        "upos-independent-alpha-v1.txt": (
            "bfd9ae0e238564ca0552b08e757a13fb66e3d7ae2de7437766829d0808e07381"
        ),
        "upos-independent-beta-v1.txt": (
            "2ab23d2ae5361ff985929de4c75de6df1d8932868498fcc04ff034a8dfb616db"
        ),
        "upos-independent-gamma-v1.txt": (
            "7a273546197734f471bd7699388d62ff3d2537e13d9e0a55f197166985bed191"
        ),
        "upos-independent-delta-v1.txt": (
            "656656fd5ce5d9741f5c481165e56fde74998e761e8ec769a1c879fe7de71213"
        ),
    }

    actual = {
        name: hashlib.sha256((prompt_dir / name).read_bytes()).hexdigest()
        for name in expected
    }

    assert actual == expected


def test_structured_smoke_prompt_hashes_are_frozen() -> None:
    prompt_dir = ROOT / "prompts" / "reference"
    expected = {
        "transliteration-gsdsimp-smoke-v1.txt": (
            "2101738b319def47eb163bb1d969b5616e02c35cd8fe67d5067d87f55b27a4c1"
        ),
        "transliteration-gsdsimp-smoke-v2.txt": (
            "5a922d6530cdc4c6e791e019c33a0635ecad61423552d1b5d896f2876231dcfd"
        ),
        "transliteration-gsdsimp-smoke-v3.txt": (
            "bce43e380538732a0a9e95bba195558041bcab568f2b16ce220783ba1e36cfae"
        ),
        "xpos-hdt-smoke-v1.txt": (
            "ea106fc052e09f8c3f70409dde906660fce5825cac201efa9f362e6f674b6c2d"
        ),
        "xpos-hdt-smoke-v2.txt": (
            "9c7423ba9b8a5a376b7f2f97b707d594b2d6bd25e124581776c6c87cc3df385a"
        ),
        "xpos-hdt-smoke-v3.txt": (
            "fd9f71cc12b1a0ddbcece10031ac40ae94aafbf9465391388cfc5319e3c7e70e"
        ),
    }

    actual = {
        name: hashlib.sha256((prompt_dir / name).read_bytes()).hexdigest()
        for name in expected
    }

    assert actual == expected


def test_xpos_candidate_inventories_match_complete_hdt_source() -> None:
    hdt_path = next((ROOT / "Target_Conllus").glob("German_*/de_hdt-ud-test.conllu"))
    source_tags = set()
    for line in hdt_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if "-" not in fields[0] and "." not in fields[0]:
            source_tags.add(fields[4])

    for name in ("xpos-hdt-smoke-v2.txt", "xpos-hdt-smoke-v3.txt"):
        prompt = (ROOT / "prompts" / "reference" / name).read_text(encoding="utf-8")
        inventory_line = next(
            line for line in prompt.splitlines() if line.startswith("Allowed tags (49): ")
        )
        prompt_tags = set(inventory_line.removeprefix("Allowed tags (49): ").split())
        assert len(prompt_tags) == 49
        assert prompt_tags == source_tags

    runtime_tags = _XPOS_TAG_INVENTORIES[("German", "HDT")]
    runtime_hash = hashlib.sha256(
        json.dumps(
            runtime_tags,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert HDT_XPOS_TAG_INVENTORY_VERSION == "de-hdt-xpos-tags-v1"
    assert HDT_XPOS_TAG_INVENTORY_SHA256 == runtime_hash
    assert set(runtime_tags) == source_tags


def test_structured_v2_prompts_require_exact_token_alignment() -> None:
    prompt_dir = ROOT / "prompts" / "reference"
    expectations = {
        "transliteration-gsdsimp-smoke-v2.txt": (
            'one JSON object in this shape: {"transliterations":',
            "the array length equals N",
        ),
        "transliteration-gsdsimp-smoke-v3.txt": (
            "with a transliterations array",
            "exactly the same number of items as input.tokens",
        ),
        "xpos-hdt-smoke-v2.txt": (
            'one JSON object in this shape: {"tags":',
            "the array length equals N",
        ),
        "xpos-hdt-smoke-v3.txt": (
            "with a tags array",
            "exactly the same number of items as input.tokens",
        ),
    }

    for name, required_fragments in expectations.items():
        prompt = (prompt_dir / name).read_text(encoding="utf-8")
        assert all(fragment in prompt for fragment in required_fragments)


def test_structured_output_calibration_report_is_self_verifying() -> None:
    report_path = (
        ROOT
        / "benchmarks"
        / "observations"
        / "qwen3.5-9b-structured-output-calibration-v1.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected_sha256 = report.pop("report_sha256")
    canonical = json.dumps(
        report,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert hashlib.sha256(canonical).hexdigest() == expected_sha256
    assert report["decision"]["production_activation"] is False
    assert report["protocol"]["hdt_xpos_tag_inventory_sha256"] == (
        HDT_XPOS_TAG_INVENTORY_SHA256
    )
    assert report["protocol"]["hdt_xpos_tag_inventory_version"] == (
        HDT_XPOS_TAG_INVENTORY_VERSION
    )
    for comparison in report["comparisons"]:
        contract = EvaluationContract.from_path(
            ROOT
            / "config"
            / "evaluation_contracts"
            / "v1"
            / f"{comparison['challenge_id']}.json"
        )
        identity = contract.evaluation_identity
        control = comparison["control"]
        assert control["contract_snapshot_sha256"] == contract.contract_snapshot_sha256
        assert control["dataset_sha256"] == identity["dataset_sha256"]
        assert control["enable_thinking"] == identity["generation_settings"][
            "enable_thinking"
        ]
        assert control["max_tokens"] == identity["generation_settings"]["max_tokens"]
        assert control["response_schema_version"] == identity["response_schema_version"]
        assert control["selection_sha256"] == identity["selection_sha256"]
        assert control["source_evaluation_identity_sha256"] == (
            contract.evaluation_identity_sha256
        )
        assert report["protocol"]["tokenizer_identity"] == identity["tokenizer_identity"]

        observations = [
            comparison["baseline"],
            comparison["final_candidate"],
            *comparison["prompt_only_candidates"],
        ]
        for observation in observations:
            assert len(observation["artifact_paths"]) == len(
                observation["artifact_sha256s"]
            )
            for artifact_index, (path, expected_artifact_sha256) in enumerate(
                zip(
                    observation["artifact_paths"],
                    observation["artifact_sha256s"],
                    strict=True,
                )
            ):
                artifact_path = ROOT / path
                assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == (
                    expected_artifact_sha256
                )
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                assert artifact["challenge_id"] == comparison["challenge_id"]
                assert artifact["dataset_sha256"] == control["dataset_sha256"]
                assert artifact["selection_sha256"] == control["selection_sha256"]
                assert artifact["generation_settings"] == identity["generation_settings"]
                assert artifact["model_identity"] == identity["model_identity"]
                assert artifact["prompt_envelope_version"] == identity[
                    "prompt_envelope_version"
                ]
                assert artifact["scorer_version"] == identity["scorer_version"]
                assert artifact["aggregation_version"] == identity["aggregation_version"]
                assert artifact["student_prompt_sha256"] == observation["prompt_sha256"]
                assert artifact["samples_valid"] == observation["samples_valid"]
                assert artifact["samples_invalid"] == observation.get(
                    "samples_invalid", 50 - observation["samples_valid"]
                )
                assert artifact["score"] == observation["score"]
                assert artifact["errors"] == observation["errors"]
                diagnostics = artifact["generation_diagnostics"]
                assert diagnostics["execution_seconds"] == observation[
                    "execution_seconds"
                ][artifact_index]
                assert diagnostics["finish_reasons"] == observation["finish_reasons"]
                assert diagnostics["generated_tokens"]["total"] == observation[
                    "generated_tokens"
                ]
                if observation is comparison["final_candidate"]:
                    assert artifact["structured_output_contract_version"] == (
                        observation["mode"]
                    )
                else:
                    assert "structured_output_contract_version" not in artifact
        final_candidate = comparison["final_candidate"]
        assert final_candidate["mode"] == "dynamic-response-constraint-v5"
        assert final_candidate["samples_valid"] == 50
        assert final_candidate["samples_invalid"] == 0
        assert final_candidate["errors"] == {}
