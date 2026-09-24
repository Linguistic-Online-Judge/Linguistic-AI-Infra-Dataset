"""Import only validated public expansion artifacts; keep private manifests under runtime."""
import argparse
import json
from pathlib import Path

from linguistic_oj.challenge_registry import load_challenge_contract_registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staging', type=Path, required=True)
    parser.add_argument('--registry', type=Path,
                        default=Path('config/challenge_contract_registry_foundation_v1.json'))
    parser.add_argument('--base-registry', type=Path,
                        default=Path('config/challenge_contract_registry_v1.json'))
    parser.add_argument('--with-xpos-labels', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    staging = args.staging.resolve(strict=True)
    name = args.registry
    if name.is_absolute() or '..' in name.parts or name.parts[0] != 'config':
        raise ValueError('registry must stay in project config')
    old = load_challenge_contract_registry(root, args.base_registry)
    new = load_challenge_contract_registry(staging, name)
    for key, contract in old.contracts.items():
        if new.contracts[key].snapshot_json != contract.snapshot_json:
            raise ValueError('old contract would change')
    document = json.loads((staging / name).read_text(encoding='utf-8'))
    files = [name]
    if args.with_xpos_labels:
        labels = Path('src/linguistic_oj/web/assets/xpos-labels.js')
        text = (staging / labels).read_text(encoding='utf-8')
        prefix = '"use strict";\nconst XPOS_LABEL_INVENTORIES = '
        if not text.startswith(prefix):
            raise ValueError('unexpected label inventory format')
        value = json.loads(text[len(prefix):].strip().removesuffix(';'))
        if not all(isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)
                   for tags in value.values()):
            raise ValueError('invalid label inventory')
        files.append(labels)
    for item in document['entries']:
        for field in ('public_descriptor_path', 'evaluation_contract_path'):
            if item[field]:
                files.append(Path(item[field]))
    for relative in files:
        contents = (staging / relative).read_bytes()
        target = root / relative
        if target.exists() and target.read_bytes() != contents:
            raise ValueError(f'refusing changed existing file: {relative}')
    for relative in files:
        target = root / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write((staging / relative).read_bytes())
    print(f'Public catalog imported: {len(new.public_challenges)} entries, '
          f'{len(new.contracts)} executable contracts; old files preserved.')


if __name__ == '__main__':
    main()
