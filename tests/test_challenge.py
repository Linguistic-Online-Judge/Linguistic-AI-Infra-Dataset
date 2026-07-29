import json
from pathlib import Path

import pytest

from linguistic_oj.challenge import (
    DuplicateSampleIdError,
    InsufficientSamplesError,
    build_challenge,
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
    assert len(private_payload["sample_ids"]) == 5
    assert private_payload["selection_seed"] == 2026


def test_dataset_hash_changes_when_source_changes(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    samples = [_sample(index) for index in range(10)]
    _write_jsonl(dataset_path, samples)
    original = _build(dataset_path)

    samples.append(_sample(99, language="English"))
    _write_jsonl(dataset_path, samples)
    changed = _build(dataset_path)

    assert original.private.dataset_sha256 != changed.private.dataset_sha256


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
