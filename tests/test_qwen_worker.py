from pathlib import Path

import pytest

from linguistic_oj.qwen_worker import parse_args


def test_qwen_worker_cli_requires_deployment_owned_inputs() -> None:
    args = parse_args(
        [
            "--root",
            ".",
            "--database",
            "runtime/submissions.db",
            "--redis-url",
            "redis://127.0.0.1:6379/0",
            "--public-challenge",
            "challenges/public/en-ewt-upos-v1.json",
            "--private-challenge",
            "runtime/private/challenges/en-ewt-upos-v1.json",
            "--dataset",
            "Standard_Dataset/standard_dataset.jsonl",
            "--vllm-base-url",
            "http://127.0.0.1:8000/v1",
            "--tokenizer-snapshot",
            "runtime/models/c202236235762e1c871ad0ccb60c8ee5ba337b9a",
            "--launch-evidence",
            "runtime/qwen-launch.json",
            "--once",
        ]
    )

    assert args.database == Path("runtime/submissions.db")
    assert args.once is True


def test_qwen_worker_cli_rejects_non_positive_idle_sleep() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--root",
                ".",
                "--database",
                "runtime/submissions.db",
                "--redis-url",
                "redis://127.0.0.1:6379/0",
                "--public-challenge",
                "challenges/public/en-ewt-upos-v1.json",
                "--private-challenge",
                "runtime/private/challenges/en-ewt-upos-v1.json",
                "--dataset",
                "Standard_Dataset/standard_dataset.jsonl",
                "--vllm-base-url",
                "http://127.0.0.1:8000/v1",
                "--tokenizer-snapshot",
                "runtime/models/c202236235762e1c871ad0ccb60c8ee5ba337b9a",
                "--launch-evidence",
                "runtime/qwen-launch.json",
                "--idle-sleep-seconds",
                "0",
            ]
        )
