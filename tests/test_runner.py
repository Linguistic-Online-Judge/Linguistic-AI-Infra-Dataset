import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from linguistic_oj.challenge import (
    ChallengeArtifacts,
    build_challenge,
    make_challenge_id,
    write_challenge,
)
from linguistic_oj.providers import (
    DeterministicMockProvider,
    ModelGeneration,
    ProviderContractError,
)
from linguistic_oj.responses import TaskType
from linguistic_oj.runner import JobDeadline, JobDeadlineExceeded, run_challenge
from linguistic_oj.runner import main as runner_main


def _sample(sample_id: str, text: str = "AB") -> dict:
    tokens = list(text)
    dependency = [
        [
            index,
            token,
            0 if index == 1 else index - 1,
            "ROOT" if index == 1 else tokens[index - 2],
            "root" if index == 1 else "dep",
        ]
        for index, token in enumerate(tokens, start=1)
    ]
    return {
        "id": sample_id,
        "language": "Test",
        "treebank": "Tiny",
        "text": text,
        "answers": {
            "segmentation": tokens,
            "upos": ["X"] * len(tokens),
            "xpos": ["MOCK"] * len(tokens),
            "dependency": dependency,
            "transliteration": tokens,
        },
        "tasks_available": [task.value for task in TaskType],
    }


def _write_jsonl(path: Path, samples: list[dict]) -> None:
    path.write_text(
        "".join(f"{json.dumps(sample, ensure_ascii=False)}\n" for sample in samples),
        encoding="utf-8",
    )


def _artifacts(tmp_path: Path, task: TaskType | str, samples: list[dict] | None = None):
    dataset_path = tmp_path / f"{TaskType(task).value}.jsonl"
    dataset_samples = samples or [_sample("sample-1")]
    _write_jsonl(dataset_path, dataset_samples)
    return build_challenge(
        dataset_path,
        language="Test",
        treebank="Tiny",
        task=task,
        count=len(dataset_samples),
        seed=2026,
        version="v1",
    )


@pytest.mark.parametrize("task", list(TaskType))
def test_mock_runner_completes_every_task_with_perfect_fixture(
    tmp_path: Path,
    task: TaskType,
) -> None:
    artifacts = _artifacts(tmp_path, task)

    result = run_challenge(
        artifacts,
        DeterministicMockProvider(),
        student_prompt="Return the required JSON.",
    )

    assert result.task is task
    assert result.samples_total == 1
    assert result.samples_valid == 1
    assert result.samples_invalid == 0
    assert result.score == 1.0
    assert result.errors == {}


def test_complete_runs_are_deterministic_and_return_only_aggregate_data(tmp_path: Path) -> None:
    artifacts = _artifacts(
        tmp_path,
        "segmentation",
        [_sample("sample-b", "CD"), _sample("sample-a", "AB")],
    )
    provider = DeterministicMockProvider()

    first = run_challenge(artifacts, provider, student_prompt="Segment the text.").to_dict()
    second = run_challenge(artifacts, provider, student_prompt="Segment the text.").to_dict()

    assert first == second
    assert "sample_ids" not in first
    assert "raw_responses" not in first
    assert "answers" not in first


def test_segmentation_runner_uses_boundary_free_scoring_surface(tmp_path: Path) -> None:
    sample = _sample("sample-1")
    sample["text"] = "A B"
    artifacts = _artifacts(tmp_path, "segmentation", [sample])

    result = run_challenge(
        artifacts,
        DeterministicMockProvider(),
        student_prompt="Segment the text.",
    )

    assert result.score == 1.0
    assert result.samples_valid == 1


class _StaticProvider:
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.calls = 0

    def generate(self, request, *, timeout_seconds=None):
        self.calls += 1
        return ModelGeneration(raw_text=self.raw_text)


def test_malformed_model_output_becomes_zero_with_error_code(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path, "segmentation")
    provider = _StaticProvider("not-json")

    result = run_challenge(artifacts, provider, student_prompt="Segment the text.")

    assert result.samples_valid == 0
    assert result.samples_invalid == 1
    assert result.score == 0.0
    assert result.errors == {"INVALID_JSON": 1}


@pytest.mark.parametrize(
    ("task", "raw_text", "error_code"),
    [
        ("upos", '{"tags":["X"]}', "LENGTH_MISMATCH"),
        (
            "dependency",
            '{"arcs":[{"token_id":1,"head_id":0,"deprel":"root"}]}',
            "TOKEN_ID_MISMATCH",
        ),
    ],
)
def test_parser_context_is_derived_from_safe_fixed_tokens(
    tmp_path: Path,
    task: str,
    raw_text: str,
    error_code: str,
) -> None:
    artifacts = _artifacts(tmp_path, task)

    result = run_challenge(
        artifacts,
        _StaticProvider(raw_text),
        student_prompt="Return JSON.",
    )

    assert result.errors == {error_code: 1}


def test_valid_but_wrong_output_is_scored_not_malformed(tmp_path: Path) -> None:
    sample = _sample("sample-1")
    sample["answers"]["upos"] = ["NOUN", "NOUN"]
    artifacts = _artifacts(tmp_path, "upos", [sample])

    result = run_challenge(
        artifacts,
        DeterministicMockProvider(),
        student_prompt="Tag the tokens.",
    )

    assert result.samples_valid == 1
    assert result.samples_invalid == 0
    assert result.score == 0.0
    assert result.errors == {}


class _ExplodingProvider:
    def __init__(self, explode_on_call: int = 1) -> None:
        self.calls = 0
        self.explode_on_call = explode_on_call

    def generate(self, request, *, timeout_seconds=None):
        self.calls += 1
        if self.calls == self.explode_on_call:
            raise RuntimeError("provider unavailable")
        return DeterministicMockProvider().generate(
            request,
            timeout_seconds=timeout_seconds,
        )


def test_provider_failure_aborts_without_partial_aggregate(tmp_path: Path) -> None:
    artifacts = _artifacts(
        tmp_path,
        "segmentation",
        [_sample("sample-a", "AB"), _sample("sample-b", "CD")],
    )
    provider = _ExplodingProvider(explode_on_call=2)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        run_challenge(artifacts, provider, student_prompt="Segment the text.")
    assert provider.calls == 2


def test_runner_propagates_decreasing_whole_job_deadline(tmp_path: Path) -> None:
    artifacts = _artifacts(
        tmp_path,
        "segmentation",
        [_sample("sample-a", "AB"), _sample("sample-b", "CD")],
    )
    now = [datetime(2026, 1, 1, tzinfo=UTC)]

    class _DeadlineProvider(DeterministicMockProvider):
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def generate(self, request, *, timeout_seconds=None):
            assert timeout_seconds is not None
            self.timeouts.append(timeout_seconds)
            now[0] += timedelta(seconds=1)
            return super().generate(request, timeout_seconds=timeout_seconds)

    provider = _DeadlineProvider()
    deadline = JobDeadline(
        expires_at=now[0] + timedelta(seconds=5),
        clock=lambda: now[0],
    )

    run_challenge(
        artifacts,
        provider,
        student_prompt="Segment the text.",
        deadline=deadline,
    )

    assert provider.timeouts == [5.0, 4.0]


def test_runner_aborts_when_deadline_expires_during_request(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path, "segmentation")
    now = [datetime(2026, 1, 1, tzinfo=UTC)]

    class _SlowProvider(DeterministicMockProvider):
        calls = 0

        def generate(self, request, *, timeout_seconds=None):
            self.calls += 1
            now[0] += timedelta(seconds=2)
            return super().generate(request, timeout_seconds=timeout_seconds)

    provider = _SlowProvider()
    deadline = JobDeadline(
        expires_at=now[0] + timedelta(seconds=1),
        clock=lambda: now[0],
    )

    with pytest.raises(JobDeadlineExceeded):
        run_challenge(
            artifacts,
            provider,
            student_prompt="Segment the text.",
            deadline=deadline,
        )
    assert provider.calls == 1


class _WrongResultProvider:
    def generate(self, request, *, timeout_seconds=None):
        return "not a ModelGeneration"


def test_provider_contract_failure_is_not_scored_as_model_output(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path, "segmentation")

    with pytest.raises(ProviderContractError, match="must return ModelGeneration"):
        run_challenge(
            artifacts,
            _WrongResultProvider(),
            student_prompt="Segment the text.",
        )


def test_dataset_integrity_failure_happens_before_provider_call(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path, "segmentation")
    provider = _ExplodingProvider()
    original = artifacts.dataset_path.read_text(encoding="utf-8")
    artifacts.dataset_path.write_text(f"{original}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="configured dataset"):
        run_challenge(artifacts, provider, student_prompt="Segment the text.")
    assert provider.calls == 0


class _RecordingProvider(DeterministicMockProvider):
    def __init__(self) -> None:
        self.texts: list[str] = []

    def generate(self, request, *, timeout_seconds=None):
        self.texts.append(request.model_input.text)
        return super().generate(request, timeout_seconds=timeout_seconds)


def test_provider_requests_follow_manifest_order(tmp_path: Path) -> None:
    artifacts = _artifacts(
        tmp_path,
        "segmentation",
        [_sample("sample-z", "CD"), _sample("sample-a", "AB")],
    )
    provider = _RecordingProvider()

    run_challenge(artifacts, provider, student_prompt="Segment the text.")

    assert provider.texts == ["AB", "CD"]


def test_cli_sanitizes_private_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = _artifacts(tmp_path, "segmentation", [_sample("sample-secret")])
    changed_challenge_id = make_challenge_id(
        "Other",
        artifacts.public.treebank,
        TaskType.SEGMENTATION,
        artifacts.public.version,
    )
    private_artifacts = ChallengeArtifacts(
        public=artifacts.public.model_copy(
            update={"language": "Other", "challenge_id": changed_challenge_id}
        ),
        private=artifacts.private.model_copy(update={"challenge_id": changed_challenge_id}),
        dataset_path=artifacts.dataset_path,
    )
    public_path, private_path = write_challenge(
        private_artifacts,
        public_dir=tmp_path / "public",
        private_dir=tmp_path / "private",
    )
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("Segment the text.", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner",
            "--public",
            str(public_path),
            "--private",
            str(private_path),
            "--dataset",
            str(private_artifacts.dataset_path),
            "--prompt-file",
            str(prompt_path),
        ],
    )

    with pytest.raises(SystemExit) as error:
        runner_main()

    captured = capsys.readouterr()
    assert error.value.code == 1
    assert "Evaluation failed: EvaluationPreflightError" in captured.err
    assert "sample-secret" not in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_cli_reports_prompt_identity_without_prompt_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = _artifacts(tmp_path, "segmentation")
    public_path, private_path = write_challenge(
        artifacts,
        public_dir=tmp_path / "public",
        private_dir=tmp_path / "private",
    )
    prompt_text = "Private calibration prompt."
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner",
            "--public",
            str(public_path),
            "--private",
            str(private_path),
            "--dataset",
            str(artifacts.dataset_path),
            "--prompt-file",
            str(prompt_path),
        ],
    )

    runner_main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["student_prompt_sha256"] == hashlib.sha256(
        prompt_text.encode("utf-8")
    ).hexdigest()
    assert prompt_text not in captured.out
    assert captured.err == ""
