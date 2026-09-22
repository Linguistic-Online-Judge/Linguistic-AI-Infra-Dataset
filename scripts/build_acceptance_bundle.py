"""Package explicitly allowlisted source for isolated server validation, never production."""

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path


def build_bundle(root: Path, output: Path, *, include_catalog: bool = False,
                 registry_name: str = "config/challenge_contract_registry_v1.json",
                 include_performance: bool = False) -> dict:
    root = root.resolve()
    output = output.resolve()
    if not output.is_relative_to(root / "runtime"):
        raise ValueError("acceptance bundles must stay below project runtime/")
    if not output.parent.is_dir():
        raise ValueError("bundle parent must already exist")
    files = [
        root / "pyproject.toml",
        root / "README.md",
        root / "config/mvp_evaluation.json",
        root / "config/mvp_evaluation_v2.json",
        root / "scripts/check_qwen_pipeline.py",
        root / "scripts/check_bounded_pipeline.py",
        root / "scripts/check_request_development.py",
        root / "scripts/qualify_bounded_qwen.py",
        root / "scripts/qualify_sample_scheduling.py",
    ]
    if include_performance:
        files.extend(root / name for name in (
            "prompts/performance/upos-v1.txt", "prompts/performance/dependency-v1.txt",
            "prompts/qualification/upos-b.txt", "prompts/qualification/dependency-b.txt",
            "docs/QWEN_PERFORMANCE.md",
            "config/classroom_capacity_target_v1.json",
        ))
    if include_catalog:
        if (not registry_name.startswith('config/') or '..' in registry_name.split('/')
                or '\\' in registry_name or ':' in registry_name
                or not registry_name.endswith('.json')):
            raise ValueError('registry must be an allowlisted project config JSON')
        registry_path = root / registry_name
        files.append(registry_path)
        for historical in (root / 'config').glob('challenge_contract_registry_*.json'):
            if historical != registry_path:
                files.append(historical)
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for entry in registry["entries"]:
            for field, prefix in (
                ("public_descriptor_path", "challenges/public/"),
                ("evaluation_contract_path", "config/evaluation_contracts/"),
            ):
                name = entry[field]
                if name is None:
                    continue
                if (
                    not name.startswith(prefix)
                    or not name.endswith(".json")
                    or ".." in name.split("/")
                    or "\\" in name
                    or ":" in name
                ):
                    raise ValueError(
                        "catalog bundle references must stay in their allowlisted folders"
                    )
                files.append(root / name)
    files.extend(
        path
        for path in (root / "src/linguistic_oj").rglob("*")
        if path.suffix in {".py", ".html", ".css", ".js"} and "__pycache__" not in path.parts
    )
    files.extend((root / "src/linguistic_oj/web/assets").glob("*.svg"))
    contents = {}
    for path in sorted(files):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("bundle input must be a local regular source file")
        contents[path.relative_to(root).as_posix()] = path.read_bytes()
    manifest = {
        "schema_version": "isolated-acceptance-source-v1",
        "production_release": False,
        "source_state": "working-tree-snapshot-not-a-reviewed-commit",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    }
    contents["acceptance-source.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with output.open("xb") as target, tarfile.open(fileobj=target, mode="w:gz") as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    return {
        "files": len(contents),
        "bundle_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-catalog", action="store_true")
    parser.add_argument("--include-performance", action="store_true")
    parser.add_argument('--registry', default='config/challenge_contract_registry_v1.json')
    args = parser.parse_args()
    print(
        json.dumps(
            build_bundle(
                Path(__file__).resolve().parents[1],
                args.output,
                include_catalog=args.include_catalog,
                registry_name=args.registry,
                include_performance=args.include_performance,
            ),
            indent=2,
        )
    )
