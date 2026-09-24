import json
from pathlib import Path

import pytest

from scripts.build_standard_dataset import (
    load_treebank_name_map,
    make_sample,
    parse_conllu,
    resolve_treebank_name,
    treebank_name_map_sha256,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TREEBANK_MAP_PATH = PROJECT_ROOT / "config" / "treebank_names.json"
TARGET_CONLLUS_PATH = PROJECT_ROOT / "Target_Conllus"


def _token(
    token_id: int,
    form: str,
    *,
    upos: str | None = "NOUN",
    xpos: str | None = "NN",
    head: int | None = 0,
    deprel: str | None = "root",
    translit: str | None = "a",
) -> dict[str, object]:
    return {
        "id": token_id,
        "form": form,
        "upos": upos,
        "xpos": xpos,
        "head": head,
        "deprel": deprel,
        "misc": {} if translit is None else {"Translit": translit},
    }


def _sentence(tokens: list[dict[str, object]]) -> dict[str, object]:
    return {
        "comments": {"sent_id": "test-1", "text": "AB"},
        "tokens": tokens,
        "block_index": 1,
    }


def _make(tokens: list[dict[str, object]]) -> dict:
    return make_sample(_sentence(tokens), Path("test.conllu"), "English", "Test")


def test_treebank_name_map_covers_every_target_conllu_file() -> None:
    configured_names = load_treebank_name_map(TREEBANK_MAP_PATH)
    target_filenames = {
        path.name for path in TARGET_CONLLUS_PATH.glob("*/*.conllu") if path.is_file()
    }

    assert len(target_filenames) == 97
    assert set(configured_names) == target_filenames


def test_repository_metadata_records_strict_task_eligibility_counts() -> None:
    metadata = json.loads(
        (PROJECT_ROOT / "Standard_Dataset" / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    assert metadata["total_samples"] == 135_180
    assert metadata["total_languages"] == 18
    assert metadata["by_task"] == {
        "dependency": 114_300,
        "segmentation": 114_320,
        "transliteration": 14_582,
        "upos": 114_287,
        "xpos": 78_423,
    }


def test_gsdsimp_name_is_reproducible_without_original_treebanks() -> None:
    configured_names = load_treebank_name_map(TREEBANK_MAP_PATH)
    source_path = Path("zh_gsdsimp-ud-test.conllu")

    result = resolve_treebank_name("Chinese", source_path, configured_names, {}, {})

    assert result == "GSDSimp"


def test_invalid_treebank_name_map_is_rejected(tmp_path: Path) -> None:
    invalid_map = tmp_path / "invalid.json"
    invalid_map.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid treebank name map"):
        load_treebank_name_map(invalid_map)


def test_treebank_map_hash_is_based_on_semantic_json_content(tmp_path: Path) -> None:
    lf_path = tmp_path / "lf.json"
    crlf_path = tmp_path / "crlf.json"
    lf_path.write_bytes(b'{"b":"B","a":"A"}\n')
    crlf_path.write_bytes(b'{\r\n  "a": "A",\r\n  "b": "B"\r\n}\r\n')

    lf_names = load_treebank_name_map(lf_path)
    crlf_names = load_treebank_name_map(crlf_path)

    assert treebank_name_map_sha256(lf_names) == treebank_name_map_sha256(crlf_names)


def test_complete_visible_sentence_advertises_all_tasks() -> None:
    sample = _make(
        [
            _token(1, "A", head=0, deprel="root", translit="a"),
            _token(2, "B", upos="VERB", xpos="VB", head=1, deprel="dep", translit="b"),
        ]
    )

    assert sample["tasks_available"] == [
        "segmentation",
        "upos",
        "xpos",
        "dependency",
        "transliteration",
    ]
    assert set(sample["answers"]) == set(sample["tasks_available"])
    assert all(len(answer) == 2 for answer in sample["answers"].values())


def test_missing_form_disables_every_task() -> None:
    sample = _make([_token(1, "_")])

    assert sample["tasks_available"] == []
    assert sample["answers"] == {}


@pytest.mark.parametrize(
    ("changes", "unavailable_task"),
    [
        ({"upos": None}, "upos"),
        ({"upos": "CONJ"}, "upos"),
        ({"xpos": None}, "xpos"),
        ({"head": None}, "dependency"),
        ({"head": 3}, "dependency"),
        ({"deprel": None}, "dependency"),
        ({"misc": {}}, "transliteration"),
    ],
)
def test_incomplete_task_gold_disables_only_that_task(
    changes: dict[str, object],
    unavailable_task: str,
) -> None:
    token = _token(1, "A")
    token.update(changes)
    sample = _make([token])

    assert "segmentation" in sample["tasks_available"]
    assert unavailable_task not in sample["tasks_available"]
    assert unavailable_task not in sample["answers"]
    assert set(sample["answers"]) == set(sample["tasks_available"])


def test_noncontiguous_token_ids_disable_dependency_only() -> None:
    sample = _make([_token(1, "A"), _token(3, "B", head=1)])

    assert "segmentation" in sample["tasks_available"]
    assert "upos" in sample["tasks_available"]
    assert "dependency" not in sample["tasks_available"]


def test_multiword_and_empty_nodes_do_not_affect_alignment(tmp_path: Path) -> None:
    source = tmp_path / "test.conllu"
    source.write_text(
        "\n".join(
            [
                "# sent_id = test-1",
                "# text = AB",
                "1-2\tAB\t_\t_\t_\t_\t_\t_\t_\t_",
                "1\tA\t_\tNOUN\tNN\t_\t0\troot\t_\tTranslit=a",
                "2\tB\t_\tVERB\tVB\t_\t1\tdep\t_\tTranslit=b",
                "2.1\tghost\t_\tX\tXX\t_\t_\t_\t_\t_",
                "",
            ]
        ),
        encoding="utf-8",
    )

    sentences = parse_conllu(source)
    sample = make_sample(sentences[0], source, "English", "Test")

    assert [token["id"] for token in sentences[0]["tokens"]] == [1, 2]
    assert sample["answers"]["segmentation"] == ["A", "B"]
    assert set(sample["answers"]) == set(sample["tasks_available"])
