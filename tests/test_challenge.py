import json
from pathlib import Path

import pytest

from linguistic_oj import AGGREGATION_VERSION, SCORER_VERSION
from linguistic_oj.challenge import (
    ChallengeArtifacts,
    ChallengeExistsError,
    DuplicateSampleIdError,
    InsufficientSamplesError,
    InvalidGoldAnswerError,
    PublicChallenge,
    build_challenge,
    load_challenge_artifacts,
    write_challenge,
)


def _sample(
    index: int,
    *,
    language: str = "Chinese",
    treebank: str = "GSDSimp",
    tasks: list[str] | None = None,
) -> dict:
    return {
        "id": f"sample-{index:03d}",
        "language": language,
        "treebank": treebank,
        "text": f"测试句子{index}",
        "answers": {
            "segmentation": ["测试", f"句子{index}"],
            "upos": ["NOUN", "NOUN"],
        },
        "tasks_available": tasks or ["segmentation", "upos"],
    }


def _write_jsonl(path: Path, samples: list[dict]) -> None:
    path.write_text(
        "".join(f"{json.dumps(sample, ensure_ascii=False)}\n" for sample in samples),
        encoding="utf-8",
    )


def _build(dataset_path: Path, *, seed: int = 2026, count: int = 5):
    return build_challenge(
        dataset_path,
        language="Chinese",
        treebank="GSDSimp",
        task="segmentation",
        count=count,
        seed=seed,
        version="v1",
    )


def test_challenge_selection_is_deterministic(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(30)])

    first = _build(dataset_path, seed=11)
    second = _build(dataset_path, seed=11)

    assert first.private.sample_ids == second.private.sample_ids
    assert first.private.selection_sha256 == second.private.selection_sha256


def test_different_seeds_select_different_samples(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(50)])

    first = _build(dataset_path, seed=11)
    second = _build(dataset_path, seed=29)

    assert first.private.sample_ids != second.private.sample_ids


def test_challenge_filters_pool_and_has_unique_ids(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    samples = [_sample(index) for index in range(10)]
    samples.extend(
        [
            _sample(100, language="English"),
            _sample(101, treebank="GSD"),
            _sample(102, tasks=["upos"]),
        ]
    )
    _write_jsonl(dataset_path, samples)

    artifacts = _build(dataset_path, count=10)

    assert artifacts.public.challenge_id == "zh-gsdsimp-segmentation-v1"
    assert artifacts.public.security_level == "public_reproducible"
    assert artifacts.public.status == "draft"
    assert artifacts.public.scorer_version == SCORER_VERSION
    assert artifacts.public.aggregation_version == AGGREGATION_VERSION
    assert artifacts.public.secondary_metrics == ("micro_precision", "micro_recall")
    assert len(artifacts.private.sample_ids) == 10
    assert len(set(artifacts.private.sample_ids)) == 10
    assert all(sample_id.startswith("sample-0") for sample_id in artifacts.private.sample_ids)


def test_public_output_does_not_leak_private_fields_or_answers(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(10)])
    artifacts = _build(dataset_path)

    public_path, private_path = write_challenge(
        artifacts,
        public_dir=tmp_path / "public",
        private_dir=tmp_path / "private",
    )
    public_payload = json.loads(public_path.read_text(encoding="utf-8"))
    private_payload = json.loads(private_path.read_text(encoding="utf-8"))

    assert "answers" not in public_payload
    assert "sample_ids" not in public_payload
    assert "selection_seed" not in public_payload
    assert public_payload["dataset_sha256"] == private_payload["dataset_sha256"]
    assert public_payload["selection_sha256"] == private_payload["selection_sha256"]
    assert len(private_payload["samples"]) == 5
    assert all(sample["gold_items"] == 2 for sample in private_payload["samples"])
    assert private_payload["selection_seed"] == 2026

    loaded = load_challenge_artifacts(
        public_path,
        private_path,
        dataset_path=dataset_path,
    )
    assert loaded.private.sample_ids == artifacts.private.sample_ids


def test_dataset_hash_changes_when_source_changes(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    samples = [_sample(index) for index in range(10)]
    _write_jsonl(dataset_path, samples)
    original = _build(dataset_path)

    samples.append(_sample(99, language="English"))
    _write_jsonl(dataset_path, samples)
    changed = _build(dataset_path)

    assert original.private.dataset_sha256 != changed.private.dataset_sha256


def test_identical_challenge_can_be_written_again(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(10)])
    artifacts = _build(dataset_path)
    public_dir = tmp_path / "public"
    private_dir = tmp_path / "private"

    first_paths = write_challenge(
        artifacts,
        public_dir=public_dir,
        private_dir=private_dir,
    )
    second_paths = write_challenge(
        artifacts,
        public_dir=public_dir,
        private_dir=private_dir,
    )

    assert first_paths == second_paths


def test_semantically_identical_json_formatting_is_allowed(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(10)])
    artifacts = _build(dataset_path)
    public_dir = tmp_path / "public"
    private_dir = tmp_path / "private"
    public_path, _ = write_challenge(
        artifacts,
        public_dir=public_dir,
        private_dir=private_dir,
    )
    public_path.write_text(
        json.dumps(artifacts.public.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    write_challenge(
        artifacts,
        public_dir=public_dir,
        private_dir=private_dir,
    )


def test_conflicting_challenge_version_cannot_be_overwritten(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(20)])
    original = _build(dataset_path, seed=1)
    conflicting = _build(dataset_path, seed=2)
    public_dir = tmp_path / "public"
    private_dir = tmp_path / "private"
    write_challenge(original, public_dir=public_dir, private_dir=private_dir)

    with pytest.raises(ChallengeExistsError, match="Create a new challenge version"):
        write_challenge(conflicting, public_dir=public_dir, private_dir=private_dir)


def test_public_only_clone_still_rejects_different_selection(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(20)])
    original = _build(dataset_path, seed=1)
    conflicting = _build(dataset_path, seed=2)
    public_dir = tmp_path / "public"
    private_dir = tmp_path / "private"
    _, private_path = write_challenge(
        original,
        public_dir=public_dir,
        private_dir=private_dir,
    )
    private_path.unlink()

    with pytest.raises(ChallengeExistsError, match="Create a new challenge version"):
        write_challenge(conflicting, public_dir=public_dir, private_dir=private_dir)


def test_public_and_private_artifacts_cannot_be_mixed(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(20)])
    first = _build(dataset_path, seed=1)
    second = _build(dataset_path, seed=2)

    with pytest.raises(ValueError, match="do not match"):
        ChallengeArtifacts(
            public=first.public,
            private=second.private,
            dataset_path=dataset_path,
        )


def test_manifest_denominators_are_immutable_and_hashed(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(10)])
    artifacts = _build(dataset_path)
    first_sample = artifacts.private.samples[0]
    changed_sample = first_sample.model_copy(update={"gold_items": first_sample.gold_items + 1})
    changed_samples = (changed_sample, *artifacts.private.samples[1:])
    tampered = artifacts.private.model_copy(update={"samples": changed_samples})

    assert isinstance(artifacts.private.samples, tuple)
    with pytest.raises(ValueError, match="selection_sha256"):
        ChallengeArtifacts(
            public=artifacts.public,
            private=tampered,
            dataset_path=dataset_path,
        )


def test_selected_sample_requires_gold_answer_for_task(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    sample = _sample(1, tasks=["upos"])
    sample["answers"].pop("upos", None)
    _write_jsonl(dataset_path, [sample])

    with pytest.raises(InvalidGoldAnswerError, match="non-empty upos gold list"):
        build_challenge(
            dataset_path,
            language="Chinese",
            treebank="GSDSimp",
            task="upos",
            count=1,
            seed=2026,
            version="v1",
        )


@pytest.mark.parametrize(
    ("task", "answer"),
    [
        ("upos", ["NOUN"]),
        ("xpos", ["NN"]),
        ("transliteration", ["cèshì"]),
        ("dependency", [[1, "测试", 0, "ROOT", "root"]]),
    ],
)
def test_fixed_token_gold_count_must_match_segmentation(
    tmp_path: Path,
    task: str,
    answer: list,
) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    sample = _sample(1, tasks=[task])
    sample["answers"][task] = answer
    _write_jsonl(dataset_path, [sample])

    with pytest.raises(InvalidGoldAnswerError, match="gold count must match segmentation"):
        build_challenge(
            dataset_path,
            language="Chinese",
            treebank="GSDSimp",
            task=task,
            count=1,
            seed=2026,
            version="v1",
        )


def test_upos_gold_tags_must_use_ud_inventory(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    sample = _sample(1, tasks=["upos"])
    sample["answers"]["upos"] = ["NOT_UD", "NOUN"]
    _write_jsonl(dataset_path, [sample])

    with pytest.raises(InvalidGoldAnswerError, match="invalid UPOS"):
        build_challenge(
            dataset_path,
            language="Chinese",
            treebank="GSDSimp",
            task="upos",
            count=1,
            seed=2026,
            version="v1",
        )


def test_dependency_gold_forms_must_align_with_tokens(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    sample = _sample(1, tasks=["dependency"])
    sample["answers"]["dependency"] = [
        [1, "WRONG", 0, "ROOT", "root"],
        [2, "句子1", 1, "测试", "dep"],
    ]
    _write_jsonl(dataset_path, [sample])

    with pytest.raises(InvalidGoldAnswerError, match="token form is misaligned"):
        build_challenge(
            dataset_path,
            language="Chinese",
            treebank="GSDSimp",
            task="dependency",
            count=1,
            seed=2026,
            version="v1",
        )


def test_legacy_public_challenge_remains_catalog_readable() -> None:
    path = (
        Path(__file__).parents[1]
        / "challenges"
        / "public"
        / "zh-gsdsimp-segmentation-v1.json"
    )

    legacy = PublicChallenge.model_validate_json(path.read_text(encoding="utf-8"))

    assert legacy.challenge_id == "zh-gsdsimp-segmentation-v1"
    assert legacy.scorer_version is None
    assert legacy.aggregation_version is None


def test_insufficient_matching_samples_is_reported(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample(index) for index in range(2)])

    with pytest.raises(InsufficientSamplesError, match="only 2 match"):
        _build(dataset_path, count=3)


def test_duplicate_matching_sample_id_is_rejected(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    duplicate = _sample(1)
    _write_jsonl(dataset_path, [duplicate, duplicate])

    with pytest.raises(DuplicateSampleIdError, match="sample-001"):
        _build(dataset_path, count=1)
