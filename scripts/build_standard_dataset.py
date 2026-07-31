#!/usr/bin/env python3
"""Build a standardized JSONL dataset from UD CoNLL-U test files.

Input defaults to Target_Conllus/, where files are grouped by language folders
such as Chinese_中文/. Output defaults to Standard_Dataset/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

MISSING = "_"
DEFAULT_TREEBANK_MAP = Path("config/treebank_names.json")


def parse_kv_field(value: str) -> dict[str, str]:
    if not value or value == MISSING:
        return {}

    items: dict[str, str] = {}
    for part in value.split("|"):
        if "=" in part:
            key, val = part.split("=", 1)
            items[key] = val
    return items


def parse_conllu(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.strip()) if text.strip() else []
    sentences = []

    for block_index, block in enumerate(blocks, start=1):
        comments: dict[str, str] = {}
        tokens = []

        for raw_line in block.splitlines():
            line = raw_line.rstrip("\n")
            if not line:
                continue

            if line.startswith("#"):
                comment = line[1:].strip()
                if "=" in comment:
                    key, value = comment.split("=", 1)
                    comments[key.strip()] = value.strip()
                continue

            cols = line.split("\t")
            if len(cols) != 10:
                raise ValueError(f"Invalid CoNLL-U line in {path}: {line}")

            token_id = cols[0]
            # Skip multiword-token rows (1-2) and empty-node rows (3.1).
            if "-" in token_id or "." in token_id:
                continue

            misc = parse_kv_field(cols[9])
            tokens.append(
                {
                    "id": int(token_id),
                    "form": cols[1],
                    "upos": None if cols[3] == MISSING else cols[3],
                    "xpos": None if cols[4] == MISSING else cols[4],
                    "head": None if cols[6] == MISSING else int(cols[6]),
                    "deprel": None if cols[7] == MISSING else cols[7],
                    "misc": misc,
                }
            )

        if tokens:
            sentences.append(
                {
                    "comments": comments,
                    "tokens": tokens,
                    "block_index": block_index,
                }
            )

    return sentences


def reconstruct_text(tokens: list[dict]) -> str:
    pieces = []
    for token in tokens:
        pieces.append(token["form"])
        if token["misc"].get("SpaceAfter") != "No":
            pieces.append(" ")
    return "".join(pieces).rstrip()


def language_from_dir(path: Path) -> str:
    return path.name.split("_", 1)[0]


def treebank_slug_from_filename(path: Path) -> str | None:
    match = re.match(r"^[^_]+_(.+?)-ud-test(?:-.+)?\.conllu$", path.name)
    if not match:
        return None
    return match.group(1)


def infer_treebank_from_filename(path: Path) -> str:
    raw = treebank_slug_from_filename(path)
    if raw is None:
        return path.stem
    return raw.upper() if len(raw) <= 4 else raw


def load_treebank_name_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"Treebank name map not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(filename, str) and isinstance(treebank, str) and treebank
        for filename, treebank in payload.items()
    ):
        raise ValueError(f"Invalid treebank name map: {path}")
    return payload


def treebank_name_map_sha256(configured_names: dict[str, str]) -> str:
    canonical_payload = json.dumps(
        configured_names,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical_payload).hexdigest()


def build_treebank_lookup(
    treebanks_dir: Path | None,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    by_filename: dict[tuple[str, str], str] = {}
    by_slug: dict[tuple[str, str], str] = {}
    if not treebanks_dir or not treebanks_dir.exists():
        return by_filename, by_slug

    for repo_dir in treebanks_dir.glob("*/UD_*-*"):
        if not repo_dir.is_dir():
            continue
        match = re.match(r"UD_([^-]+)-(.+)$", repo_dir.name)
        if not match:
            continue

        language = match.group(1)
        treebank = match.group(2)
        by_slug[(language, treebank.lower())] = treebank
        for conllu in repo_dir.glob("*-ud-test.conllu"):
            by_filename[(language, conllu.name)] = treebank

    return by_filename, by_slug


def resolve_treebank_name(
    language: str,
    conllu_path: Path,
    configured_names: dict[str, str],
    treebank_by_filename: dict[tuple[str, str], str],
    treebank_by_slug: dict[tuple[str, str], str],
) -> str:
    configured_match = configured_names.get(conllu_path.name)
    if configured_match:
        return configured_match

    filename_match = treebank_by_filename.get((language, conllu_path.name))
    if filename_match:
        return filename_match

    slug = treebank_slug_from_filename(conllu_path)
    if slug:
        slug_match = treebank_by_slug.get((language, slug.lower()))
        if slug_match:
            return slug_match

    return infer_treebank_from_filename(conllu_path)


def make_sample(
    sentence: dict,
    source_file: Path,
    language: str,
    treebank: str,
) -> dict:
    comments = sentence["comments"]
    tokens = sentence["tokens"]
    id_to_form = {token["id"]: token["form"] for token in tokens}

    sent_id = comments.get("sent_id") or f"sent-{sentence['block_index']}"
    sample_id_sent = re.sub(r"[^A-Za-z0-9_.-]+", "_", sent_id)

    dependency = [
        [
            token["id"],
            token["form"],
            token["head"],
            "ROOT" if token["head"] == 0 else id_to_form.get(token["head"]),
            token["deprel"],
        ]
        for token in tokens
    ]

    answers = {
        "segmentation": [token["form"] for token in tokens],
        "upos": [token["upos"] for token in tokens],
    }
    tasks_available = ["segmentation", "upos", "dependency"]

    xpos_values = [token["xpos"] for token in tokens]
    if xpos_values and all(value is not None for value in xpos_values):
        answers["xpos"] = xpos_values
        tasks_available.insert(2, "xpos")

    answers["dependency"] = dependency

    translit_values = [token["misc"].get("Translit") for token in tokens]
    if translit_values and all(value is not None for value in translit_values):
        answers["transliteration"] = translit_values
        tasks_available.append("transliteration")

    sample = {
        "id": f"{language}_{treebank}_{sample_id_sent}",
        "language": language,
        "treebank": treebank,
        "source_file": source_file.name,
        "sent_id": sent_id,
        "text": comments.get("text") or reconstruct_text(tokens),
        "answers": answers,
        "tasks_available": tasks_available,
    }

    if "parallel_id" in comments:
        sample["parallel_id"] = comments["parallel_id"]
    if "translit" in comments:
        sample["sentence_translit"] = comments["translit"]

    preferred_order = [
        "id",
        "language",
        "treebank",
        "source_file",
        "sent_id",
        "parallel_id",
        "text",
        "sentence_translit",
        "answers",
        "tasks_available",
    ]
    return {key: sample[key] for key in preferred_order if key in sample}


def write_jsonl(path: Path, samples: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def build_dataset(
    input_dir: Path,
    output_dir: Path,
    treebanks_dir: Path | None,
    treebank_map_path: Path,
) -> None:
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    by_language_dir = output_dir / "by_language"
    by_language_dir.mkdir(parents=True, exist_ok=True)

    configured_names = load_treebank_name_map(treebank_map_path)
    treebank_by_filename, treebank_by_slug = build_treebank_lookup(treebanks_dir)
    all_samples: list[dict] = []
    samples_by_language: dict[str, list[dict]] = defaultdict(list)

    for language_dir in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        language = language_from_dir(language_dir)
        for conllu_path in sorted(language_dir.glob("*.conllu")):
            treebank = resolve_treebank_name(
                language,
                conllu_path,
                configured_names,
                treebank_by_filename,
                treebank_by_slug,
            )
            for sentence in parse_conllu(conllu_path):
                sample = make_sample(sentence, conllu_path, language, treebank)
                all_samples.append(sample)
                samples_by_language[language_dir.name].append(sample)

    write_jsonl(output_dir / "standard_dataset.jsonl", all_samples)
    for language_dir_name, samples in sorted(samples_by_language.items()):
        write_jsonl(by_language_dir / f"{language_dir_name}.jsonl", samples)

    by_language_counts = {key: len(value) for key, value in sorted(samples_by_language.items())}
    by_task_counts = Counter(task for sample in all_samples for task in sample["tasks_available"])
    by_treebank_counts = Counter(
        f"{sample['language']}_{sample['treebank']}" for sample in all_samples
    )

    metadata = {
        "generated_at": datetime.now(UTC).isoformat(),
        "input_dir": str(input_dir),
        "treebank_name_map": treebank_map_path.as_posix(),
        "treebank_name_map_sha256": treebank_name_map_sha256(configured_names),
        "total_samples": len(all_samples),
        "total_languages": len(samples_by_language),
        "by_language": by_language_counts,
        "by_task": dict(sorted(by_task_counts.items())),
        "by_treebank": dict(sorted(by_treebank_counts.items())),
        "schema": {
            "dependency": [
                "token_id",
                "token_form",
                "head_id",
                "head_form",
                "deprel",
            ],
            "xpos_policy": (
                "answers.xpos is included only when every token in the sentence has a "
                "non-empty XPOS value."
            ),
            "transliteration_policy": (
                "answers.transliteration is included only when every token in the sentence "
                "has MISC.Translit."
            ),
        },
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert UD CoNLL-U test files into standardized JSONL."
    )
    parser.add_argument("--input", default="Target_Conllus", type=Path)
    parser.add_argument("--output", default="Standard_Dataset", type=Path)
    parser.add_argument(
        "--treebanks",
        default="TreeBanks",
        type=Path,
        help="Optional original TreeBanks directory for official treebank-name casing.",
    )
    parser.add_argument(
        "--treebank-map",
        default=DEFAULT_TREEBANK_MAP,
        type=Path,
        help="Tracked source-filename to official treebank-name mapping.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    treebanks_dir = args.treebanks if args.treebanks.exists() else None
    build_dataset(args.input, args.output, treebanks_dir, args.treebank_map)


if __name__ == "__main__":
    main()
