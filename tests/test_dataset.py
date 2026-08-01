import json
from pathlib import Path

import pytest

from linguistic_oj.dataset import (
    DatasetFormatError,
    SelectedSampleSetError,
    iter_dataset_samples,
    iter_matching_samples,
    load_dataset_samples_by_id,
)


def _sample(sample_id: str, *, language: str = "Chinese", treebank: str = "GSDSimp") -> dict:
    return {
        "id": sample_id,
        "language": language,
        "treebank": treebank,
        "text": f"sentence {sample_id}",
        "answers": {"segmentation": ["sentence", sample_id]},
        "tasks_available": ["segmentation", "upos"],
    }


def _write_jsonl(path: Path, samples: list[dict]) -> None:
    path.write_text(
        "".join(f"{json.dumps(sample, ensure_ascii=False)}\n" for sample in samples),
        encoding="utf-8",
    )


def test_iter_dataset_samples_streams_valid_records(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample("s1"), _sample("s2")])

    samples = list(iter_dataset_samples(dataset_path))

    assert [sample.id for sample in samples] == ["s1", "s2"]


def test_iter_matching_samples_filters_language_treebank_and_task(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(
        dataset_path,
        [
            _sample("matching"),
            _sample("wrong-language", language="English"),
            _sample("wrong-treebank", treebank="GSD"),
        ],
    )

    samples = list(
        iter_matching_samples(
            dataset_path,
            language="Chinese",
            treebank="GSDSimp",
            task="segmentation",
        )
    )

    assert [sample.id for sample in samples] == ["matching"]


def test_invalid_json_reports_source_line(tmp_path: Path) -> None:
    dataset_path = tmp_path / "invalid.jsonl"
    dataset_path.write_text(
        f"{json.dumps(_sample('valid'))}\nnot-json\n",
        encoding="utf-8",
    )

    with pytest.raises(DatasetFormatError, match=r"invalid\.jsonl:2"):
        list(iter_dataset_samples(dataset_path))


def test_missing_dataset_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Dataset file not found"):
        list(iter_dataset_samples(tmp_path / "missing.jsonl"))


def test_selected_samples_are_loaded_in_manifest_order(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample("first"), _sample("second"), _sample("unused")])

    selected = load_dataset_samples_by_id(dataset_path, ["second", "first"])

    assert [sample.id for sample in selected] == ["second", "first"]


def test_selected_sample_loader_rejects_invalid_manifest_ids(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample("first")])

    with pytest.raises(SelectedSampleSetError, match="duplicate values"):
        load_dataset_samples_by_id(dataset_path, ["first", "first"])
    with pytest.raises(SelectedSampleSetError, match="missing selected"):
        load_dataset_samples_by_id(dataset_path, ["missing"])


def test_selected_sample_loader_scans_for_duplicate_dataset_ids(tmp_path: Path) -> None:
    dataset_path = tmp_path / "samples.jsonl"
    _write_jsonl(dataset_path, [_sample("selected"), _sample("other"), _sample("selected")])

    with pytest.raises(SelectedSampleSetError, match="duplicate selected"):
        load_dataset_samples_by_id(dataset_path, ["selected"])
