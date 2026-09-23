import json
import tarfile
from pathlib import Path

import pytest

from scripts.build_acceptance_bundle import build_bundle


@pytest.fixture
def source(tmp_path: Path) -> Path:
    for name in (
        "README.md",
        "pyproject.toml",
        "config/mvp_evaluation.json",
        "config/mvp_evaluation_v2.json",
        "scripts/check_qwen_pipeline.py",
        "scripts/check_bounded_pipeline.py",
        "scripts/check_request_development.py",
        "scripts/check_request_backup.py",
        "scripts/qwen_dev_ops.py",
        "scripts/acceptance_model_relay.py",
        "scripts/qualify_bounded_qwen.py",
        "scripts/qualify_sample_scheduling.py",
        "src/linguistic_oj/__init__.py",
        "src/linguistic_oj/web/index.html",
        "src/linguistic_oj/web/assets/brand-option-a.svg",
        "src/linguistic_oj/__pycache__/cached.pyc",
        "runtime/private/secret.txt",
        ".env",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    return tmp_path


def test_bundle_contains_only_allowlisted_sources_with_fingerprints(source):
    output = source / "runtime/test.tar.gz"
    result = build_bundle(source, output)
    assert len(result["bundle_sha256"]) == 64
    with tarfile.open(output) as archive:
        names = set(archive.getnames())
        assert all(not name.startswith(("runtime/", ".env")) for name in names)
        assert all("__pycache__" not in name for name in names)
        assert "src/linguistic_oj/web/index.html" in names
        assert "src/linguistic_oj/web/assets/brand-option-a.svg" in names
        assert "scripts/check_request_development.py" in names
        manifest = json.load(archive.extractfile("acceptance-source.json"))
        assert manifest["production_release"] is False
        assert set(manifest["files"]) == names - {"acceptance-source.json"}


def test_bundle_refuses_overwrite_and_non_runtime_output(source):
    output = source / "runtime/test.tar.gz"
    build_bundle(source, output)
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        build_bundle(source, output)
    assert output.read_bytes() == before
    with pytest.raises(ValueError, match="runtime"):
        build_bundle(source, source / "export.tar.gz")


def test_catalog_bundle_rejects_private_references(source):
    registry = source / "config/challenge_contract_registry_v1.json"
    registry.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "public_descriptor_path": "runtime/private/secret.json",
                        "evaluation_contract_path": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="allowlisted"):
        build_bundle(source, source / "runtime/catalog.tar.gz", include_catalog=True)
    assert not (source / "runtime/catalog.tar.gz").exists()


def test_performance_bundle_includes_only_explicit_prompts_and_guide(source):
    included = {"prompts/performance/upos-v1.txt", "prompts/performance/dependency-v1.txt",
                "prompts/qualification/upos-b.txt", "prompts/qualification/dependency-b.txt",
                "docs/QWEN_PERFORMANCE.md", "config/classroom_capacity_target_v1.json"}
    for name in included | {"prompts/performance/private-prompt.txt",
                            "prompts/qualification/private-prompt.txt"}:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    output = source / "runtime/performance.tar.gz"
    build_bundle(source, output, include_performance=True)
    with tarfile.open(output) as archive:
        names = set(archive.getnames())
        assert included <= names
        assert "prompts/performance/private-prompt.txt" not in names
        assert "prompts/qualification/private-prompt.txt" not in names
