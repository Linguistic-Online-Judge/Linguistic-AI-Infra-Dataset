"""Measure revised prompts on fixed artifacts; optionally compare independent hand examples."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linguistic_oj.challenge import LANGUAGE_CODES  # noqa: E402
from linguistic_oj.challenge_registry import load_challenge_contract_registry  # noqa: E402
from linguistic_oj.dataset import load_dataset_samples_by_id  # noqa: E402
from linguistic_oj.local_dev import _state_lock  # noqa: E402
from linguistic_oj.model_inputs import (  # noqa: E402
    DependencyModelInput,
    DependencyTokenInput,
    SegmentationModelInput,
    build_model_input,
    response_expectations,
)
from linguistic_oj.providers import (  # noqa: E402
    GenerationSettings,
    ModelIdentity,
    ModelRequest,
    OpenAICompatibleProvider,
    PromptEnvelope,
)
from linguistic_oj.qwen_runtime import load_huggingface_tokenizer  # noqa: E402
from linguistic_oj.responses import TaskType, parse_model_response  # noqa: E402
from scripts.build_foundation_catalog import (  # noqa: E402
    read_examples,
    teaching_prompts,
    token_count,
)


def read_rules(path):
    return json.loads(path.read_text(encoding='utf-8').partition('const TEMPLATE_RULES =')[2].
                      strip().removesuffix(';'))


def revised_prompts(language, treebank, task, example, rules):
    old_zero, old_few = teaching_prompts(language, treebank, task, example)
    prefix = old_zero.partition('\n')[0] + '\n'
    instruction = rules[task.value].replace('{note}', example['note'])
    zero = prefix + instruction
    return zero, zero + old_few[len(old_zero):]


def audit(root, data, tokenizer_path, rules_path, examples_path, output):
    registry = load_challenge_contract_registry(
        root, Path('config/challenge_contract_registry_foundation_v1.json'))
    rules = read_rules(rules_path)
    examples = read_examples(examples_path)
    tokenizer = load_huggingface_tokenizer(tokenizer_path)
    rows = []
    for key, contract in registry.contracts.items():
        public = registry.public_challenges[key]
        task = TaskType(public.task)
        if task not in (TaskType.SEGMENTATION, TaskType.DEPENDENCY):
            continue
        manifest = json.loads((data / 'runtime/private/challenges' / (key + '.json')).read_text())
        sample_ids = [sample['sample_id'] for sample in manifest['samples']]
        path = next((data / 'Standard_Dataset/by_language').glob(f'{public.language}_*.jsonl'))
        prompts = revised_prompts(public.language, public.treebank, task,
                                  examples[LANGUAGE_CODES[public.language]], rules)
        maximum = 0
        for sample in load_dataset_samples_by_id(path, sample_ids):
            for prompt in prompts:
                request = ModelRequest(
                    task=task, language=public.language, treebank=public.treebank,
                    student_prompt=prompt, model_input=build_model_input(sample, task))
                maximum = max(maximum, token_count(tokenizer.apply_chat_template(
                    list(PromptEnvelope.from_request(request).to_messages()), tokenize=True,
                    add_generation_prompt=True, enable_thinking=False)))
        rows.append({'challenge_id': key, 'max_input_tokens': maximum,
                     'limit': contract.max_rendered_input_tokens,
                     'fits': maximum <= contract.max_rendered_input_tokens})
    report = {'kind': 'updated-template-budget-v1', 'rows': rows,
              'passed': all(row['fits'] for row in rows)}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    if not report['passed']:
        raise ValueError('revised template exceeds an existing contract limit')
    print('PASS revised templates: all', len(rows), 'segmentation/dependency tasks fit.')


def probe(root, state_dir, rules_path, examples_path, output):
    """Hold the instance lock: normal application must be stopped during these eight calls."""
    registry = load_challenge_contract_registry(
        root, Path('config/challenge_contract_registry_foundation_v1.json'))
    identity = next(iter(registry.contracts.values())).evaluation_identity['model_identity']
    rules = read_rules(rules_path)
    examples = read_examples(examples_path)
    cases = [
        ('English', 'segmentation', ['Mira', 'carries', 'five', 'yellow', 'cups', '.'], None),
        ('Chinese', 'segmentation', ['小林', '收好', '雨伞', '。'], None),
        ('English', 'dependency', ['Mira', 'sleeps', '.'],
         [(2, 'nsubj'), (0, 'root'), (2, 'punct')]),
        ('Japanese', 'dependency', ['ミナ', 'が', '笑う', '。'],
         [(3, 'nsubj'), (1, 'case'), (0, 'root'), (3, 'punct')]),
    ]
    report = {'kind': 'independent-handwritten-template-probe-v1', 'rows': [],
              'evaluation_samples_used': False, 'database_writes': False}
    with _state_lock(state_dir):
        if (state_dir / 'uncertain-inference.json').exists():
            raise ValueError('prior inference termination unconfirmed')
        provider = OpenAICompatibleProvider(
            base_url='http://127.0.0.1:8000/v1', identity=ModelIdentity(**identity),
            settings=GenerationSettings(max_tokens=512, temperature=0.0, top_p=1.0,
                                        seed=2026, enable_thinking=False), timeout_seconds=120)
        try:
            for language, task_name, tokens, arcs in cases:
                task = TaskType(task_name)
                example = examples[LANGUAGE_CODES[language]]
                if task == TaskType.SEGMENTATION:
                    inputs = SegmentationModelInput(text=''.join(tokens))
                    expected = {'tokens': tokens}
                else:
                    inputs = DependencyModelInput(tokens=tuple(
                        DependencyTokenInput(token_id=i + 1, form=word)
                        for i, word in enumerate(tokens)))
                    expected = {'arcs': [{'token_id': i + 1, 'head_id': head, 'deprel': relation}
                                         for i, (head, relation) in enumerate(arcs)]}
                versions = [('old', teaching_prompts(language, 'Handwritten', task, example)[0]),
                            ('revised', revised_prompts(language, 'Handwritten', task,
                                                       example, rules)[0])]
                for version, prompt in versions:
                    generation = provider.generate(ModelRequest(
                        task=task, language=language, treebank='Handwritten',
                        student_prompt=prompt, model_input=inputs))
                    count, ids = response_expectations(task, inputs)
                    parsed = parse_model_response(task, generation.raw_text,
                                                  expected_count=count, expected_token_ids=ids)
                    report['rows'].append({'language': language, 'task': task_name,
                                           'version': version, 'format_valid': parsed.is_valid,
                                           'exact_match': parsed.is_valid and (
                                               parsed.value.model_dump() == expected),
                                           'output': generation.raw_text})
                    print(language, task_name, version,
                          report['rows'][-1]['exact_match'], flush=True)
        finally:
            if provider.has_active_request:
                with (state_dir / 'uncertain-inference.json').open('x') as stream:
                    json.dump({'reason': 'independent-template-probe-unconfirmed'}, stream)
            with output.open('x', encoding='utf-8') as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['audit', 'probe'])
    for name in ('root', 'data-root', 'tokenizer-snapshot', 'state-dir',
                 'rules', 'examples', 'output'):
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args()
    if args.action == 'audit':
        audit(args.root, args.data_root, args.tokenizer_snapshot,
              args.rules, args.examples, args.output)
    else:
        probe(args.root, args.state_dir, args.rules, args.examples, args.output)
