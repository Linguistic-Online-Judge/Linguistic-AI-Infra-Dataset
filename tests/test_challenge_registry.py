import json
from pathlib import Path

import pytest

from linguistic_oj.challenge_registry import load_challenge_contract_registry

ROOT = Path(__file__).parents[1]
V1_LANGUAGE_CHALLENGE_IDS = {
    "ar-pud-upos-v1",
    "zh-beginner-upos-v1",
    "da-ddt-upos-v1",
    "nl-lassysmall-upos-v1",
    "en-childes-upos-v1",
    "fr-fqb-upos-v1",
    "de-hdt-upos-v1",
    "he-htb-upos-v1",
    "hi-hdtb-upos-v1",
    "hu-szeged-upos-v1",
    "it-kiparlaforest-upos-v1",
    "ja-pud-upos-v1",
    "ko-kaist-upos-v1",
    "pt-cintil-upos-v1",
    "ru-syntagrus-upos-v1",
    "es-ancora-upos-v1",
    "sv-talbanken-upos-v1",
    "th-pud-upos-v1",
}
V1_TASK_CHALLENGE_IDS = {
    "de-hdt-dependency-v1",
    "de-hdt-xpos-v1",
    "zh-gsdsimp-segmentation-v2",
    "zh-gsdsimp-transliteration-v1",
}


def test_repository_registry_loads_safe_catalog_and_executable_contracts() -> None:
    registry = load_challenge_contract_registry(
        ROOT,
        ROOT / "config" / "challenge_contract_registry_v1.json",
    )

    assert len(registry.public_challenges) == 26
    assert len(registry.contracts) == 22
    assert V1_LANGUAGE_CHALLENGE_IDS | V1_TASK_CHALLENGE_IDS == set(registry.contracts)
    contract = registry.contracts["en-childes-upos-v1"]
    assert contract.contract_version == "mvp-evaluation-v2"
    assert registry.public_challenges[contract.challenge_id].task == "upos"
    representatives = [
        registry.public_challenges[challenge_id]
        for challenge_id in V1_LANGUAGE_CHALLENGE_IDS
    ]
    assert {challenge.language for challenge in representatives} == {
        "Arabic",
        "Chinese",
        "Danish",
        "Dutch",
        "English",
        "French",
        "German",
        "Hebrew",
        "Hindi",
        "Hungarian",
        "Italian",
        "Japanese",
        "Korean",
        "Portuguese",
        "Russian",
        "Spanish",
        "Swedish",
        "Thai",
    }
    assert all(challenge.sample_count == 50 for challenge in representatives)
    assert all(challenge.status == "draft" for challenge in representatives)
    task_representatives = [
        registry.public_challenges[challenge_id] for challenge_id in V1_TASK_CHALLENGE_IDS
    ]
    assert {challenge.task for challenge in task_representatives} == {
        "dependency",
        "segmentation",
        "transliteration",
        "xpos",
    }


def test_registry_rejects_paths_outside_the_project_root(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "challenge-contract-registry-v1",
                "entries": [
                    {
                        "public_descriptor_path": "../challenge.json",
                        "evaluation_contract_path": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stay below the project root"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_rejects_empty_normalized_paths(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "challenge-contract-registry-v1",
                "entries": [
                    {
                        "public_descriptor_path": ".",
                        "evaluation_contract_path": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stay below the project root"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_rejects_ntfs_alternate_data_stream_paths(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "challenge-contract-registry-v1",
                "entries": [
                    {
                        "public_descriptor_path": "challenges/public.json:descriptor",
                        "evaluation_contract_path": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stay below the project root"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_rejects_duplicate_challenge_ids(tmp_path: Path) -> None:
    (tmp_path / "challenges").mkdir()
    public = json.loads(
        (ROOT / "challenges" / "public" / "en-ewt-upos-v1.json").read_text(
            encoding="utf-8"
        )
    )
    (tmp_path / "challenges" / "first.json").write_text(
        json.dumps(public),
        encoding="utf-8",
    )
    (tmp_path / "challenges" / "second.json").write_text(
        json.dumps(public),
        encoding="utf-8",
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "challenge-contract-registry-v1",
                "entries": [
                    {
                        "public_descriptor_path": "challenges/first.json",
                        "evaluation_contract_path": None,
                    },
                    {
                        "public_descriptor_path": "challenges/second.json",
                        "evaluation_contract_path": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate challenge ID"):
        load_challenge_contract_registry(tmp_path, registry_path)


def test_registry_validates_public_only_descriptors(tmp_path: Path) -> None:
    (tmp_path / "challenges").mkdir()
    public = json.loads(
        (ROOT / "challenges" / "public" / "en-ewt-upos-v1.json").read_text(
            encoding="utf-8"
        )
    )
    public["primary_metric"] = "invalid_metric"
    (tmp_path / "challenges" / "invalid.json").write_text(
        json.dumps(public),
        encoding="utf-8",
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "challenge-contract-registry-v1",
                "entries": [
                    {
                        "public_descriptor_path": "challenges/invalid.json",
                        "evaluation_contract_path": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="metrics do not match"):
        load_challenge_contract_registry(tmp_path, registry_path)
