import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.mvp_contract import canonical_sha256

ROOT = Path(__file__).parents[1]
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _canonical_lf_sha256(path: Path) -> str:
    payload = path.read_bytes().replace(b"\r\n", b"\n")
    if b"\r" in payload:
        raise AssertionError(f"source contains a non-CRLF carriage return: {path}")
    return hashlib.sha256(payload).hexdigest()


def test_v1_source_provenance_is_complete_and_fail_closed() -> None:
    provenance_path = ROOT / "config" / "v1_source_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    report_sha256 = provenance.pop("report_sha256")
    registry = load_challenge_contract_registry(
        ROOT,
        ROOT / "config" / "challenge_contract_registry_v1.json",
    )

    assert report_sha256 == canonical_sha256(provenance)
    assert provenance["schema_version"] == "v1-source-provenance-v1"
    assert provenance["file_canonicalization"] == (
        "sha256-utf8-normalize-crlf-to-lf-v1"
    )
    assert provenance["release"] == {
        "archive_url": "https://hdl.handle.net/11234/1-6149",
        "name": "Universal Dependencies 2.18",
        "published_on": "2026-05-15",
        "tag": "r2.18",
    }
    assert COMMIT_PATTERN.fullmatch(provenance["local_ingestion_commit"])
    assert provenance["verdict"] == "blocked"
    assert provenance["blocking_reasons"]

    treebanks = provenance["treebanks"]
    assert len(treebanks) == 22
    assert len({(item["language"], item["treebank"]) for item in treebanks}) == 22
    assert len({item["local_source_path"] for item in treebanks}) == 22
    challenges = {
        challenge_id: item
        for item in treebanks
        for challenge_id in item["challenge_ids"]
    }
    assert len(challenges) == sum(len(item["challenge_ids"]) for item in treebanks)
    assert set(challenges) == set(registry.public_challenges)

    for challenge_id, public in registry.public_challenges.items():
        source = challenges[challenge_id]
        assert (source["language"], source["treebank"]) == (
            public.language,
            public.treebank,
        )
        assert source["activation_decision"] in {"hold", "review_required"}
        assert public.status == "draft"

    for source in treebanks:
        relative_path = PurePosixPath(source["local_source_path"])
        assert not relative_path.is_absolute()
        assert ".." not in relative_path.parts
        local_path = ROOT.joinpath(*relative_path.parts)
        assert local_path.is_file()
        assert not local_path.is_symlink()
        assert SHA256_PATTERN.fullmatch(source["source_file_sha256"])
        assert _canonical_lf_sha256(local_path) == source["source_file_sha256"]
        assert COMMIT_PATTERN.fullmatch(source["release_commit"])
        repository_name = source["repository"].rsplit("/", 1)[-1]
        raw_prefix = (
            "https://raw.githubusercontent.com/UniversalDependencies/"
            f"{repository_name}/r2.18/"
        )
        assert source["source_url"].startswith(raw_prefix)
        assert source["license_url"].startswith(raw_prefix)
        assert source["readme_url"].startswith(raw_prefix)
        assert "/r2.18/" in source["license_url"]
        assert "/r2.18/" in source["readme_url"]
        assert SHA256_PATTERN.fullmatch(source["license_document_sha256"])
        assert SHA256_PATTERN.fullmatch(source["readme_document_sha256"])
        assert source["attribution_summary"].strip()
        assert source["share_alike_review"] != "cleared"
        assert source["underlying_text_review"].strip()
