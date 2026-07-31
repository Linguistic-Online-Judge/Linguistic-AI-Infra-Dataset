import json
from pathlib import Path

import pytest

from scripts.build_standard_dataset import (
    load_treebank_name_map,
    resolve_treebank_name,
    treebank_name_map_sha256,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TREEBANK_MAP_PATH = PROJECT_ROOT / "config" / "treebank_names.json"
TARGET_CONLLUS_PATH = PROJECT_ROOT / "Target_Conllus"


def test_treebank_name_map_covers_every_target_conllu_file() -> None:
    configured_names = load_treebank_name_map(TREEBANK_MAP_PATH)
    target_filenames = {
        path.name for path in TARGET_CONLLUS_PATH.glob("*/*.conllu") if path.is_file()
    }

    assert len(target_filenames) == 97
    assert set(configured_names) == target_filenames


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
