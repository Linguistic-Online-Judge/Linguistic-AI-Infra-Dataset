"""Read-only task-pool audit; never creates challenges or prints answer text."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from linguistic_oj.challenge import InvalidGoldAnswerError, validated_gold_item_count
from linguistic_oj.dataset import iter_dataset_samples
from linguistic_oj.responses import TaskType


def audit(directory: Path, minimum: int = 50) -> dict:
    languages = {}
    for path in sorted(directory.glob('*.jsonl')):
        counts = defaultdict(Counter)
        invalid = Counter()
        total = 0
        language = None
        for sample in iter_dataset_samples(path):
            if language is not None and sample.language != language:
                raise ValueError('mixed languages in one source file')
            language = sample.language
            total += 1
            for task in TaskType:
                if task.value not in sample.tasks_available:
                    continue
                try:
                    validated_gold_item_count(sample, task)
                except InvalidGoldAnswerError:
                    invalid[task.value] += 1
                else:
                    counts[task.value][sample.treebank] += 1
        if language:
            languages[language] = {
                'total_samples': total,
                'tasks': {
                    task.value: {
                        'eligible_total': sum(counts[task.value].values()),
                        'invalid_advertised': invalid[task.value],
                        'candidate_treebanks': {
                            name: count for name, count in counts[task.value].most_common()
                            if count >= minimum
                        },
                    } for task in TaskType
                },
            }
    return {'minimum_pool': minimum, 'language_count': len(languages), 'languages': languages}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-dir', type=Path, default=Path('Standard_Dataset/by_language'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.output.parent.is_dir():
        parser.error('output parent must exist')
    report = audit(args.dataset_dir)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    for language, entry in report['languages'].items():
        print(language, ' '.join(
            f"{task}:{info['eligible_total']}({len(info['candidate_treebanks'])} pools)"
            for task, info in entry['tasks'].items()
        ))
