"""Add missing segmentation/dependency tasks without changing any existing contract."""
from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from pathlib import Path

from linguistic_oj.challenge import LANGUAGE_CODES, build_challenge
from linguistic_oj.challenge_registry import load_challenge_contract_registry
from linguistic_oj.dataset import load_dataset_samples_by_id
from linguistic_oj.model_inputs import build_model_input, response_expectations
from linguistic_oj.mvp_contract import EvaluationContract, canonical_sha256
from linguistic_oj.providers import ModelRequest, PromptEnvelope
from linguistic_oj.qwen_runtime import TokenizerIdentity, load_huggingface_tokenizer
from linguistic_oj.responses import TaskType, parse_model_response

REGISTRY = Path('config/challenge_contract_registry_foundation_v1.json')
CATALOG_FIELDS = {
    'annotation_license', 'attribution_requirements', 'challenge_id', 'security_level',
    'share_alike_requirements', 'source_commit', 'source_file_sha256s', 'source_release',
    'status', 'underlying_text_rights',
}
DEPENDENCY_INSTRUCTION = (
    '按 UD 规范分析输入的词元序列。保留每个 token_id，恰好输出一条对应依存弧。'
    'head_id 为中心词的 token_id，唯一句根的 head_id 为 0，deprel 为适当的 UD 关系。'
    '形成单根、连通、无环的树。只返回 JSON 对象，唯一字段 arcs 为非空数组，每项仅含'
    '整数 token_id、整数 head_id 和非空字符串 deprel。不要输出解释、Markdown 或额外字段。'
)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def read_examples(path):
    text = path.read_text(encoding='utf-8')
    return json.loads(text.partition('const LANGUAGE_TASK_EXAMPLES =')[2].strip().removesuffix(';'))


def teaching_prompts(language, treebank, task, entry):
    if task == TaskType.SEGMENTATION:
        instruction = ('输入 text 由当前树库的词元拼接而成。恢复词元边界，保留全部字符与原顺序，'
                       '不补写原始句子中的空格或缩合形式。' + entry['note'] +
                       '只返回 JSON 对象，唯一字段 tokens 为非空字符串数组；不输出解释或额外字段。')
        label = '分词'
    else:
        instruction, label = DEPENDENCY_INSTRUCTION, '依存句法'
    zero = f'任务：{language} / {treebank} / {label}。\n{instruction}'
    few = zero + '\n\n以下公开手写例子仅说明任务与格式，不是待评测输入。请处理随后给出的实际输入：'
    for example in entry['examples']:
        if task == TaskType.SEGMENTATION:
            inputs = {'text': ''.join(example['tokens'])}
            output = {'tokens': example['tokens']}
        else:
            inputs = {'tokens': [{'token_id': i + 1, 'form': word}
                                 for i, word in enumerate(example['tokens'])]}
            output = {'arcs': [{'token_id': i + 1, 'head_id': head, 'deprel': relation}
                               for i, (head, relation) in enumerate(zip(
                                   example['heads'], example['relations'], strict=True))]}
        few += '\n输入：' + compact(inputs) + '\n输出：' + compact(output)
    return zero, few


def output_budget(max_gold_tokens):
    # Measured canonical reference + 25% formatting reserve and termination space.
    return max(256, math.ceil((max_gold_tokens * 1.25 + 16) / 128) * 128)


def token_count(value):
    if isinstance(value, Mapping):
        value = value['input_ids']
    if not isinstance(value, (list, tuple)) or any(type(item) is not int for item in value):
        raise ValueError('unexpected tokenizer output')
    return len(value)


def build(root, data_root, destination, tokenizer_path, examples_path):
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('destination must be new and its parent must exist')
    registry = load_challenge_contract_registry(
        root, Path('config/challenge_contract_registry_v1.json'))
    document = json.loads((root / 'config/challenge_contract_registry_v1.json').read_text(
        encoding='utf-8'))
    examples = read_examples(examples_path)
    expected = TokenizerIdentity.from_mapping(next(iter(registry.contracts.values())).
                                             evaluation_identity['tokenizer_identity'])
    actual = TokenizerIdentity.from_snapshot(tokenizer_path, repository=expected.repository,
                                            revision=expected.revision)
    if expected != actual:
        raise ValueError('tokenizer differs from the existing fixed model')
    tokenizer = load_huggingface_tokenizer(tokenizer_path)
    generated = []
    report = {'schema_version': 'foundation-catalog-build-v1', 'new_tasks': [],
              'old_contracts': {key: value.contract_snapshot_sha256
                                for key, value in registry.contracts.items()}}
    present = {(registry.public_challenges[key].language, contract.evaluation_identity['task'])
               for key, contract in registry.contracts.items()}
    for language, code in LANGUAGE_CODES.items():
        base = next(public for key, public in registry.public_challenges.items()
                    if key in registry.contracts and public.language == language
                    and public.task == 'upos')
        paths = list((data_root / 'Standard_Dataset/by_language').glob(f'{language}_*.jsonl'))
        if len(paths) != 1:
            raise ValueError('missing/ambiguous dataset')
        for task in (TaskType.SEGMENTATION, TaskType.DEPENDENCY):
            if (language, task.value) in present:
                continue
            treebank = ('PUD' if language == 'Spanish' and task == TaskType.DEPENDENCY
                        else base.treebank)
            artifact = build_challenge(paths[0], language=language, treebank=treebank,
                                       task=task, count=50, seed=2026, version='foundation-v1')
            samples = load_dataset_samples_by_id(paths[0], artifact.private.sample_ids)
            prompts = teaching_prompts(language, treebank, task, examples[code])
            max_input, max_output, max_items = 0, 0, 0
            for sample in samples:
                inputs = build_model_input(sample, task)
                if task == TaskType.SEGMENTATION:
                    output = {'tokens': sample.answers['segmentation']}
                else:
                    output = {'arcs': [{'token_id': arc[0], 'head_id': arc[2], 'deprel': arc[4]}
                                       for arc in sample.answers['dependency']]}
                response = compact(output)
                count, ids = response_expectations(task, inputs)
                if parse_model_response(task, response, expected_count=count,
                                        expected_token_ids=ids).error is not None:
                    raise ValueError('canonical output failed the existing response contract')
                max_items = max(max_items, len(sample.answers['segmentation']))
                max_output = max(max_output, len(tokenizer.encode(
                    response, add_special_tokens=False)))
                for prompt in prompts:
                    request = ModelRequest(task=task, language=language, treebank=treebank,
                                           student_prompt=prompt, model_input=inputs)
                    tokens = tokenizer.apply_chat_template(
                        list(PromptEnvelope.from_request(request).to_messages()), tokenize=True,
                        add_generation_prompt=True, enable_thinking=False)
                    max_input = max(max_input, token_count(tokens))
            budget = output_budget(max_output)
            row = {'challenge_id': artifact.public.challenge_id, 'language': language,
                   'treebank': treebank, 'task': task.value, 'samples': 50,
                   'max_items': max_items, 'max_template_input_tokens': max_input,
                   'max_canonical_output_tokens': max_output, 'output_budget': budget,
                   'fits': max_input + budget <= 4096, 'templates': list(prompts)}
            report['new_tasks'].append(row)
            template = json.loads((root / 'config/mvp_evaluation_v2.json').read_text(
                encoding='utf-8'))
            public = artifact.public.model_copy(update={
                'benchmark_limitations': (
                    'Public-data foundation practice, 50 fixed samples; not a secret assessment. '
                    + ('Input concatenates treebank tokens for boundary restoration. '
                       if task == TaskType.SEGMENTATION else '')
                    + 'Source-rights review and production activation remain separate.'),
            })
            template['catalog'].update(public.model_dump(mode='json', include=CATALOG_FIELDS))
            identity = template['evaluation_identity']
            identity.update(challenge_id=public.challenge_id, task=task.value,
                            dataset_sha256=public.dataset_sha256,
                            selection_sha256=public.selection_sha256,
                            response_schema_version=public.response_schema_version,
                            aggregation_version=public.aggregation_version,
                            scorer_version=public.scorer_version)
            identity['generation_settings']['max_tokens'] = budget
            template['limits']['max_rendered_input_tokens'] = 4096 - budget
            template['job_policy']['job_deadline_seconds'] = (
                900 if task == TaskType.DEPENDENCY else 300)
            template['leaderboard_partition']['expected_sha256'] = canonical_sha256(identity)
            generated.append((public, artifact.private, template))
            print(f'{public.challenge_id}: input={max_input} output={max_output} '
                  f'budget={budget} fit={row["fits"]}', flush=True)
    destination.mkdir(mode=0o700)
    with (destination / 'build-report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    if not all(row['fits'] for row in report['new_tasks']):
        raise ValueError('some fixed sample sets do not fit the model; see build-report.json')
    files = {str(REGISTRY): document}
    for entry in document['entries']:
        for name in (entry['public_descriptor_path'], entry['evaluation_contract_path']):
            if name:
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((root / name).read_bytes())
    for public, private, mapping in generated:
        contract = EvaluationContract.from_mapping(mapping)
        files[f'challenges/public/{public.challenge_id}.json'] = public.model_dump(mode='json')
        files[f'runtime/private/challenges/{public.challenge_id}.json'] = private.model_dump(
            mode='json')
        contract_path = f'config/evaluation_contracts/foundation-v1/{public.challenge_id}.json'
        files[contract_path] = mapping
        document['entries'].append({'public_descriptor_path':
                                   f'challenges/public/{public.challenge_id}.json',
                                   'evaluation_contract_path': contract_path})
        assert contract.challenge_id == public.challenge_id
    document['entries'].sort(key=lambda entry: entry['public_descriptor_path'])
    for name, payload in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
    loaded = load_challenge_contract_registry(destination, REGISTRY)
    if any(loaded.contracts[key].contract_snapshot_sha256 != value
           for key, value in report['old_contracts'].items()):
        raise ValueError('an old contract changed')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'data-root', 'destination', 'tokenizer-snapshot', 'examples'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    build(args.root.resolve(), args.data_root.resolve(), args.destination.resolve(),
          args.tokenizer_snapshot.resolve(), args.examples.resolve())
