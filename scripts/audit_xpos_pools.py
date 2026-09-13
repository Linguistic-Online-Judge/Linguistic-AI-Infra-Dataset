"""Summarize XPOS label inventories without exporting sentence/answer pairs."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from linguistic_oj.challenge import validated_gold_item_count
from linguistic_oj.dataset import iter_dataset_samples
from linguistic_oj.responses import TaskType


def audit(directory):
    result = {}
    for path in sorted(directory.glob('*.jsonl')):
        pools = defaultdict(lambda: {'samples': 0, 'tags': Counter(), 'same_as_upos': 0,
                                     'tokens': 0})
        language = None
        for sample in iter_dataset_samples(path):
            language = sample.language
            if 'xpos' not in sample.tasks_available:
                continue
            validated_gold_item_count(sample, TaskType.XPOS)
            pool = pools[sample.treebank]
            pool['samples'] += 1
            pool['tags'].update(sample.answers['xpos'])
            pool['tokens'] += len(sample.answers['xpos'])
            pool['same_as_upos'] += sum(a == b for a, b in zip(
                sample.answers['xpos'], sample.answers.get('upos', []), strict=False))
        if language:
            result[language] = {
                name: {**pool, 'tags': dict(pool['tags'].most_common())}
                for name, pool in pools.items() if pool['samples'] >= 50
            }
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-dir', type=Path, default=Path('Standard_Dataset/by_language'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.dataset_dir)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    for language, pools in report.items():
        for treebank, pool in pools.items():
            print(language, treebank, pool['samples'],
                  f"{len(pool['tags'])} tags",
                  f"UPOS-equal {pool['same_as_upos']}/{pool['tokens']}",
                  list(pool['tags'])[:12])
