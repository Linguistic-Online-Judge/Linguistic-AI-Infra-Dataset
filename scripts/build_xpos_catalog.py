"""Build non-duplicate language-specific POS tasks as an additive registry."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linguistic_oj.challenge import build_challenge  # noqa: E402
from linguistic_oj.challenge_registry import load_challenge_contract_registry  # noqa: E402
from linguistic_oj.dataset import load_dataset_samples_by_id  # noqa: E402
from linguistic_oj.model_inputs import build_model_input  # noqa: E402
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256  # noqa: E402
from linguistic_oj.providers import ModelRequest, PromptEnvelope  # noqa: E402
from linguistic_oj.qwen_runtime import TokenizerIdentity, load_huggingface_tokenizer  # noqa: E402
from linguistic_oj.responses import TaskType, parse_model_response  # noqa: E402
from scripts.audit_xpos_pools import audit  # noqa: E402
from scripts.build_foundation_catalog import (  # noqa: E402
    CATALOG_FIELDS,
    compact,
    output_budget,
    token_count,
)

BASE_REGISTRY = Path('config/challenge_contract_registry_foundation_v1.json')
REGISTRY = Path('config/challenge_contract_registry_xpos_v1.json')


def read_profiles(path):
    text = path.read_text(encoding='utf-8')
    return json.loads(text.partition('const XPOS_LESSONS =')[2].strip().removesuffix(';'))


def instruction(profile):
    return (f"为输入 tokens 中的每个词元标注{profile['tagset']}。{profile['guide']}"
            '标签区分大小写，保留标签中的竖线、加号、连接符及全部组成部分。'
            '不要重新分词。输出顺序和数量必须与输入一致。只返回一个 JSON 对象，'
            '唯一字段 tags 为非空字符串数组，不输出解释或额外字段。')


def prompts(profile):
    zero = f"任务：{profile['language']} / {profile['treebank']} / 树库词性 XPOS。\n"
    zero += instruction(profile)
    few = zero + '\n\n以下公开手写例子仅说明任务与格式，不是待评测输入。请处理随后给出的实际输入：'
    for example in profile['examples']:
        few += '\n输入：' + compact({'tokens': example['tokens']})
        few += '\n输出：' + compact({'tags': example['tags']})
    return zero, few


def validate_profile(profile, pool):
    if pool['same_as_upos'] == pool['tokens']:
        raise ValueError('XPOS duplicates UPOS; do not manufacture a second task')
    if pool['samples'] < 50:
        raise ValueError('insufficient complete XPOS samples')
    for example in profile['examples']:
        if len(example['tokens']) != len(example['tags']):
            raise ValueError('teaching example counts differ')
        unknown = set(example['tags']) - set(pool['tags'])
        if unknown:
            raise ValueError(f"{profile['language']}: unknown example tags {sorted(unknown)}")


def build(root, data_root, destination, tokenizer_path, profiles_path, inspect_only=False):
    profiles = read_profiles(profiles_path)
    pools = audit(data_root / 'Standard_Dataset/by_language')
    for profile in profiles.values():
        pool = pools[profile['language']][profile['treebank']]
        validate_profile(profile, pool)
        print(profile['language'], profile['treebank'], pool['samples'],
              len(pool['tags']), 'labels', flush=True)
    if inspect_only:
        return
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('destination must be new, with an existing parent')
    registry = load_challenge_contract_registry(root, BASE_REGISTRY)
    document = json.loads((root / BASE_REGISTRY).read_text(encoding='utf-8'))
    expected = TokenizerIdentity.from_mapping(next(iter(registry.contracts.values())).
                                             evaluation_identity['tokenizer_identity'])
    actual = TokenizerIdentity.from_snapshot(tokenizer_path, repository=expected.repository,
                                            revision=expected.revision)
    if expected != actual:
        raise ValueError('fixed tokenizer identity changed')
    tokenizer = load_huggingface_tokenizer(tokenizer_path)
    present = {(public.language, public.task) for key, public in registry.public_challenges.items()
               if key in registry.contracts}
    report = {'schema_version': 'xpos-catalog-build-v1', 'new_tasks': [],
              'old_contracts': {key: value.contract_snapshot_sha256
                                for key, value in registry.contracts.items()}}
    generated = []
    inventories = {}
    for profile in profiles.values():
        language, treebank = profile['language'], profile['treebank']
        if (language, 'xpos') in present:
            continue
        paths = list((data_root / 'Standard_Dataset/by_language').glob(f'{language}_*.jsonl'))
        if len(paths) != 1:
            raise ValueError('missing or ambiguous dataset')
        artifacts = build_challenge(paths[0], language=language, treebank=treebank,
                                    task='xpos', count=50, seed=2026, version='specialized-v1')
        samples = load_dataset_samples_by_id(paths[0], artifacts.private.sample_ids)
        teaching = prompts(profile)
        max_input = max_output = 0
        for sample in samples:
            output = compact({'tags': sample.answers['xpos']})
            if parse_model_response('xpos', output,
                                    expected_count=len(sample.answers['segmentation'])).error:
                raise ValueError('gold output fails the current schema')
            max_output = max(max_output, len(tokenizer.encode(output, add_special_tokens=False)))
            for prompt in teaching:
                request = ModelRequest(task=TaskType.XPOS, language=language, treebank=treebank,
                                       student_prompt=prompt,
                                       model_input=build_model_input(sample, TaskType.XPOS))
                tokens = tokenizer.apply_chat_template(
                    list(PromptEnvelope.from_request(request).to_messages()), tokenize=True,
                    add_generation_prompt=True, enable_thinking=False)
                max_input = max(max_input, token_count(tokens))
        budget = output_budget(max_output)
        public = artifacts.public.model_copy(update={
            'benchmark_limitations': 'Public-data specialized POS practice, 50 fixed samples. '
                                    'Source-specific XPOS strings, not interchangeable with UPOS. '
                                    'Source-rights review and public activation remain separate.'})
        mapping = json.loads((root / 'config/mvp_evaluation_v2.json').read_text(encoding='utf-8'))
        mapping['catalog'].update(public.model_dump(mode='json', include=CATALOG_FIELDS))
        identity = mapping['evaluation_identity']
        identity.update(challenge_id=public.challenge_id, task='xpos',
                        dataset_sha256=public.dataset_sha256,
                        selection_sha256=public.selection_sha256,
                        response_schema_version=public.response_schema_version,
                        aggregation_version=public.aggregation_version,
                        scorer_version=public.scorer_version)
        identity['generation_settings']['max_tokens'] = budget
        mapping['limits']['max_rendered_input_tokens'] = 4096 - budget
        mapping['job_policy']['job_deadline_seconds'] = 900 if budget > 768 else 600
        mapping['leaderboard_partition']['expected_sha256'] = canonical_sha256(identity)
        generated.append((public, artifacts.private, mapping))
        inventories[f'{language}/{treebank}'] = sorted(pools[language][treebank]['tags'])
        row = {'challenge_id': public.challenge_id, 'language': language, 'treebank': treebank,
               'max_template_input_tokens': max_input, 'max_canonical_output_tokens': max_output,
               'output_budget': budget, 'fits': max_input + budget <= 4096,
               'samples': 50, 'tag_count': len(inventories[f'{language}/{treebank}']),
               'templates': list(teaching)}
        report['new_tasks'].append(row)
        print(public.challenge_id, 'input', max_input, 'output', max_output, 'budget', budget,
              'fits', row['fits'], flush=True)
    destination.mkdir(mode=0o700)
    (destination / 'build-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                  encoding='utf-8')
    if not all(row['fits'] for row in report['new_tasks']):
        raise ValueError('some task budgets do not fit the fixed context')
    for entry in document['entries']:
        for name in (entry['public_descriptor_path'], entry['evaluation_contract_path']):
            if name:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((root / name).read_bytes())
    files = {str(REGISTRY): document}
    for public, private, mapping in generated:
        EvaluationContract.from_mapping(mapping)
        files[f'challenges/public/{public.challenge_id}.json'] = public.model_dump(mode='json')
        files[f'runtime/private/challenges/{public.challenge_id}.json'] = private.model_dump(
            mode='json')
        contract = f'config/evaluation_contracts/xpos-v1/{public.challenge_id}.json'
        files[contract] = mapping
        document['entries'].append({'public_descriptor_path':
                                   f'challenges/public/{public.challenge_id}.json',
                                   'evaluation_contract_path': contract})
    document['entries'].sort(key=lambda entry: entry['public_descriptor_path'])
    for name, payload in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
    target = destination / 'src/linguistic_oj/web/assets/xpos-labels.js'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('"use strict";\nconst XPOS_LABEL_INVENTORIES = ' +
                      json.dumps(inventories, ensure_ascii=False, indent=2) + ';\n',
                      encoding='utf-8')
    loaded = load_challenge_contract_registry(destination, REGISTRY)
    assert all(loaded.contracts[key].contract_snapshot_sha256 == value
               for key, value in report['old_contracts'].items())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'data-root', 'destination', 'tokenizer-snapshot', 'profiles'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--inspect-only', action='store_true')
    args = parser.parse_args()
    build(args.root.resolve(), args.data_root.resolve(), args.destination.resolve(),
          args.tokenizer_snapshot.resolve(), args.profiles.resolve(), args.inspect_only)
