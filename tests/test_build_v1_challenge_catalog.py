import json
from pathlib import Path

import pytest

from linguistic_oj.challenge import LANGUAGE_CODES, ChallengeExistsError
from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.responses import TaskType
from scripts.build_v1_challenge_catalog import (
    _catalog_lock,
    build_v1_catalog,
    select_largest_eligible_treebank,
)

ROOT = Path(__file__).parents[1]


def _sample(language: str, treebank: str, index: int) -> dict[str, object]:
    return {
        "id": f"{language}-{treebank}-{index}",
        "language": language,
        "treebank": treebank,
        "text": "AB",
        "answers": {
            "segmentation": ["A", "B"],
            "upos": ["NOUN", "VERB"],
            "xpos": ["NN", "VB"],
            "dependency": [[1, "A", 2, "B", "dep"], [2, "B", 0, "ROOT", "root"]],
            "transliteration": ["a", "b"],
        },
        "tasks_available": [
            "segmentation",
            "upos",
            "xpos",
            "dependency",
            "transliteration",
        ],
    }


def _write_dataset(path: Path, samples: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            f"{json.dumps(sample, ensure_ascii=False, separators=(',', ':'))}\n"
            for sample in samples
        ),
        encoding="utf-8",
        newline="\n",
    )


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")


def _build(tmp_path: Path, *, contract_dir: Path | None = None):
    dataset_dir = tmp_path / "Standard_Dataset" / "by_language"
    for language in LANGUAGE_CODES:
        dataset_path = dataset_dir / f"{language}_test.jsonl"
        if dataset_path.exists():
            continue
        if language == "Arabic":
            samples = [_sample("Arabic", "Gamma", index) for index in range(51)]
        elif language == "English":
            samples = [
                *[_sample("English", "Alpha", index) for index in range(50)],
                *[_sample("English", "Beta", index) for index in range(51)],
            ]
        elif language == "Chinese":
            samples = [_sample(language, "GSDSimp", index) for index in range(50)]
        elif language == "German":
            samples = [_sample(language, "HDT", index) for index in range(50)]
        else:
            samples = [_sample(language, "Test", index) for index in range(50)]
        _write_dataset(
            dataset_path,
            samples,
        )
    return build_v1_catalog(
        tmp_path,
        dataset_dir=dataset_dir,
        public_dir=tmp_path / "challenges" / "public",
        private_dir=tmp_path / "runtime" / "private" / "challenges",
        contract_template_path=ROOT / "config" / "mvp_evaluation_v2.json",
        contract_dir=(
            tmp_path / "config" / "evaluation_contracts" / "v1"
            if contract_dir is None
            else contract_dir
        ),
        registry_path=tmp_path / "config" / "challenge_contract_registry_v1.json",
        count=50,
    )


def test_largest_treebank_uses_deterministic_name_tie_break(tmp_path: Path) -> None:
    dataset_path = tmp_path / "English_test.jsonl"
    unrelated_invalid = _sample("English", "Broken", 1)
    unrelated_invalid["answers"]["upos"] = ["CONJ", "VERB"]
    _write_dataset(
        dataset_path,
        [
            *[_sample("English", "Zulu", index) for index in range(2)],
            *[_sample("English", "Alpha", index) for index in range(2)],
            unrelated_invalid,
        ],
    )

    selected = select_largest_eligible_treebank(
        dataset_path,
        language="English",
        task=TaskType.UPOS,
        count=2,
    )

    assert selected == ("Alpha", 2)


def test_largest_treebank_must_have_valid_gold(tmp_path: Path) -> None:
    dataset_path = tmp_path / "English_test.jsonl"
    invalid = _sample("English", "Largest", 3)
    invalid["answers"]["upos"] = ["CONJ", "VERB"]
    _write_dataset(
        dataset_path,
        [
            *[_sample("English", "Smaller", index) for index in range(2)],
            *[_sample("English", "Largest", index) for index in range(3)],
            invalid,
        ],
    )

    with pytest.raises(ValueError, match="invalid UPOS"):
        select_largest_eligible_treebank(
            dataset_path,
            language="English",
            task=TaskType.UPOS,
            count=2,
        )


def test_catalog_builder_is_repeatable_and_loadable(tmp_path: Path) -> None:
    first = _build(tmp_path)
    generated_paths = sorted(
        path
        for path in tmp_path.rglob("*.json")
        if "runtime" not in path.parts
    )
    first_contents = {path: path.read_bytes() for path in generated_paths}

    second = _build(tmp_path)
    registry = load_challenge_contract_registry(
        tmp_path,
        tmp_path / "config" / "challenge_contract_registry_v1.json",
    )

    assert first == second
    plans_by_language = {plan.language: plan for plan in first.plans}
    assert plans_by_language["Arabic"].treebank == "Gamma"
    assert plans_by_language["English"].treebank == "Beta"
    assert len(first.challenge_ids) == 22
    assert {
        "ar-gamma-upos-v1",
        "de-hdt-dependency-v1",
        "de-hdt-xpos-v1",
        "en-beta-upos-v1",
        "zh-gsdsimp-segmentation-v2",
        "zh-gsdsimp-transliteration-v1",
    } <= set(first.challenge_ids)
    assert set(registry.public_challenges) == set(first.challenge_ids)
    assert set(registry.contracts) == set(first.challenge_ids)
    for challenge_id, contract in registry.contracts.items():
        public = registry.public_challenges[challenge_id]
        generation_settings = contract.evaluation_identity["generation_settings"]
        if public.task == "dependency":
            expected_max_tokens = 1024
            expected_job_deadline_seconds = 900
        elif public.task == "transliteration" or public.language in {
            "Hebrew",
            "Japanese",
            "Spanish",
            "Swedish",
        }:
            expected_max_tokens = 512
            expected_job_deadline_seconds = 300
        else:
            expected_max_tokens = 256
            expected_job_deadline_seconds = 300
        assert generation_settings["max_tokens"] == expected_max_tokens
        assert contract.job_deadline_seconds == expected_job_deadline_seconds
        assert (
            contract.max_rendered_input_tokens + expected_max_tokens
            == contract.model_context_tokens
        )
    assert all(path.read_bytes() == first_contents[path] for path in generated_paths)
    for challenge_id in first.challenge_ids:
        assert (
            tmp_path / "runtime" / "private" / "challenges" / f"{challenge_id}.json"
        ).is_file()


def test_catalog_builder_refuses_to_mutate_an_existing_challenge(tmp_path: Path) -> None:
    _build(tmp_path)
    dataset_path = tmp_path / "Standard_Dataset" / "by_language" / "English_test.jsonl"
    with dataset_path.open("a", encoding="utf-8", newline="\n") as dataset_file:
        dataset_file.write(
            f"{json.dumps(_sample('English', 'Beta', 99), separators=(',', ':'))}\n"
        )

    with pytest.raises(ChallengeExistsError, match="different immutable content"):
        _build(tmp_path)


def test_catalog_builder_distinguishes_integer_and_float_contract_values(
    tmp_path: Path,
) -> None:
    result = _build(tmp_path)
    contract_path = (
        tmp_path
        / "config"
        / "evaluation_contracts"
        / "v1"
        / f"{result.challenge_ids[0]}.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    queue_depth = contract["limits"]["global_queue_depth"]
    assert type(queue_depth) is int
    contract["limits"]["global_queue_depth"] = float(queue_depth)
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ChallengeExistsError, match="different immutable content"):
        _build(tmp_path)


def test_catalog_builder_rejects_existing_id_at_another_registry_path(
    tmp_path: Path,
) -> None:
    result = _build(tmp_path)
    challenge_id = next(item for item in result.challenge_ids if item.startswith("en-"))
    public_path = tmp_path / "challenges" / "public" / f"{challenge_id}.json"
    alias_path = tmp_path / "challenges" / "alias.json"
    public_path.replace(alias_path)
    registry_path = tmp_path / "config" / "challenge_contract_registry_v1.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in registry["entries"]
        if item["public_descriptor_path"].endswith(f"/{challenge_id}.json")
    )
    entry["public_descriptor_path"] = "challenges/alias.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ChallengeExistsError, match="another registry path"):
        _build(tmp_path)

    assert not public_path.exists()


def test_catalog_builder_enforces_frozen_v1_parameters(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "Standard_Dataset" / "by_language"
    _write_dataset(
        dataset_dir / "Arabic_test.jsonl",
        [_sample("Arabic", "Gamma", index) for index in range(50)],
    )

    with pytest.raises(ValueError, match="count 50, seed 2026"):
        build_v1_catalog(
            tmp_path,
            dataset_dir=dataset_dir,
            public_dir=tmp_path / "challenges" / "public",
            private_dir=tmp_path / "runtime" / "private" / "challenges",
            contract_template_path=ROOT / "config" / "mvp_evaluation_v2.json",
            contract_dir=tmp_path / "config" / "evaluation_contracts" / "v1",
            registry_path=tmp_path / "config" / "challenge_contract_registry_v1.json",
            languages=("Arabic",),
            count=49,
        )

    assert not (tmp_path / "challenges").exists()


def test_catalog_builder_requires_all_18_languages(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly the ordered 18 languages"):
        build_v1_catalog(
            tmp_path,
            dataset_dir=tmp_path / "Standard_Dataset" / "by_language",
            public_dir=tmp_path / "challenges" / "public",
            private_dir=tmp_path / "runtime" / "private" / "challenges",
            contract_template_path=ROOT / "config" / "mvp_evaluation_v2.json",
            contract_dir=tmp_path / "config" / "evaluation_contracts" / "v1",
            registry_path=tmp_path / "config" / "challenge_contract_registry_v1.json",
            languages=("Arabic",),
        )


def test_catalog_builder_rejects_another_contract_path(tmp_path: Path) -> None:
    result = _build(tmp_path)
    alternate_dir = tmp_path / "config" / "alternate_contracts"

    with pytest.raises(ChallengeExistsError, match="another contract path"):
        _build(tmp_path, contract_dir=alternate_dir)

    assert all(
        not (alternate_dir / f"{challenge_id}.json").exists()
        for challenge_id in result.challenge_ids
    )


def test_catalog_builder_uses_an_exclusive_process_lock(tmp_path: Path) -> None:
    with _catalog_lock(tmp_path):
        with pytest.raises(RuntimeError, match="another.*in progress"):
            _build(tmp_path)


def test_catalog_builder_rejects_dangling_artifact_symlinks(tmp_path: Path) -> None:
    public_dir = tmp_path / "challenges" / "public"
    public_dir.mkdir(parents=True)
    public_path = public_dir / "ar-gamma-upos-v1.json"
    _symlink_or_skip(public_path, tmp_path / "missing-public.json")

    with pytest.raises(ChallengeExistsError, match="must not be a symbolic link"):
        _build(tmp_path)

    assert public_path.is_symlink()
    assert not (tmp_path / "config" / "challenge_contract_registry_v1.json").exists()


def test_catalog_lock_rejects_symbolic_links(tmp_path: Path) -> None:
    lock_path = tmp_path / "config" / ".v1-catalog.lock"
    lock_path.parent.mkdir(parents=True)
    target = tmp_path / "lock-target"
    target.write_bytes(b"unchanged")
    _symlink_or_skip(lock_path, target)

    with pytest.raises(RuntimeError, match="lock must not be a symbolic link"):
        with _catalog_lock(tmp_path):
            pass

    assert target.read_bytes() == b"unchanged"


def test_catalog_lock_rejects_symlinked_directories(tmp_path: Path) -> None:
    target = tmp_path / "alternate-config"
    target.mkdir()
    _symlink_or_skip(tmp_path / "config", target)

    with pytest.raises(RuntimeError, match="must not traverse symbolic links"):
        with _catalog_lock(tmp_path):
            pass
