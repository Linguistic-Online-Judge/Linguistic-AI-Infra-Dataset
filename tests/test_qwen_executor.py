import json
import os
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from linguistic_oj import auth_config, qwen_executor
from linguistic_oj.challenge import PublicChallenge, build_challenge, write_challenge
from linguistic_oj.executor_state import (
    ExecutorState,
    GuardedQwenProvider,
    executor_lock,
    read_state,
)
from linguistic_oj.mvp_contract import canonical_sha256

ROOT = Path(__file__).parents[1]


def write_private(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    if os.name == "nt":
        monkeypatch.setattr(auth_config, "_check_windows_acl", lambda path: None)
    monkeypatch.setattr(qwen_executor, "getproxies", lambda: {})
    root = tmp_path / "release"
    root.mkdir()
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(json.dumps({
        "id": "fixture-1", "language": "English", "treebank": "Tiny", "text": "Hi",
        "tasks_available": ["upos"], "answers": {"segmentation": ["Hi"], "upos": ["INTJ"]},
    }) + "\n", encoding="utf-8")
    challenge = build_challenge(dataset, language="English", treebank="Tiny", task="upos",
                                count=1, seed=2026, version="executor-fixture-v1")
    public = challenge.public.model_dump(mode="json")
    public.update(status="active", source_release="handwritten-test-fixture-v1",
                  source_file_sha256s=[{"path": "fixture.jsonl", "sha256": "a" * 64}],
                  attribution_requirements="Synthetic test fixture only",
                  share_alike_requirements="Synthetic test fixture only",
                  underlying_text_rights="Handwritten synthetic test fixture only")
    challenge = replace(challenge, public=PublicChallenge.model_validate_json(json.dumps(public)))
    write_challenge(challenge, public_dir=root / "public", private_dir=tmp_path / "private")
    key = challenge.public.challenge_id
    config = json.loads((ROOT / "config/mvp_evaluation_v2.json").read_text(encoding="utf-8"))
    for field in config["catalog"]:
        config["catalog"][field] = public[field]
    config["evaluation_identity"].update(
        challenge_id=key, dataset_sha256=challenge.public.dataset_sha256,
        selection_sha256=challenge.public.selection_sha256,
    )
    config["leaderboard_partition"]["expected_sha256"] = canonical_sha256(
        config["evaluation_identity"])
    write_private(root / "contract.json", config)
    write_private(root / "registry.json", {
        "schema_version": "challenge-contract-registry-v1", "entries": [{
            "public_descriptor_path": f"public/{key}.json",
            "evaluation_contract_path": "contract.json",
        }],
    })
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    pg = tmp_path / "pg.url"
    redis = tmp_path / "redis.url"
    pg.write_text("postgresql://operator:fixture-password@127.0.0.1/fixture", encoding="utf-8")
    redis.write_text("redis://127.0.0.1/15", encoding="utf-8")
    pg.chmod(0o600)
    redis.chmod(0o600)
    settings = {
        "version": "qwen-serial-executor-v1", "root": str(root), "registry": "registry.json",
        "state_dir": str(state_dir), "postgres_database_url_file": str(pg),
        "redis_url_file": str(redis), "namespace": "fixture-serial",
        "vllm_base_url": "http://127.0.0.1:8000/v1",
        "tokenizer_snapshot": str(tmp_path / "unused-tokenizer"),
        "launch_evidence": str(tmp_path / "unused-evidence"),
        "artifacts": {key: {"public_challenge": str(root / "public" / f"{key}.json"),
                            "private_challenge": str(tmp_path / "private" / f"{key}.json"),
                            "dataset": str(dataset)}},
    }
    path = tmp_path / "executor.json"
    write_private(path, settings)
    return SimpleNamespace(path=path, settings=settings, root=root, state=state_dir, key=key,
                           pg=pg, dataset=dataset)


def test_plan_checks_actual_artifact_binding_without_connecting(deployment, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline plan attempted to construct live services")

    monkeypatch.setattr(qwen_executor, "build_submission_store", forbidden)
    monkeypatch.setattr(qwen_executor, "RedisJobQueue", forbidden)
    plan = qwen_executor.load_plan(deployment.path)
    assert set(plan.contracts) == {deployment.key}
    assert "fixture-password" not in repr(plan)
    deployment.pg.write_text("postgresql://operator:new-password@127.0.0.1/fixture")
    assert qwen_executor.load_plan(deployment.path).binding_sha256 == plan.binding_sha256
    deployment.pg.write_text("postgresql://operator:new-password@127.0.0.1/other_database")
    assert qwen_executor.load_plan(deployment.path).binding_sha256 != plan.binding_sha256
    deployment.dataset.write_bytes(deployment.dataset.read_bytes() + b" ")
    with pytest.raises(ValueError):
        qwen_executor.load_plan(deployment.path)


def test_draft_configuration_is_rejected_before_any_artifact_load(deployment, monkeypatch):
    contract = json.loads((deployment.root / "contract.json").read_text())
    contract["catalog"]["status"] = "draft"
    write_private(deployment.root / "contract.json", contract)
    descriptor = deployment.root / "public" / f"{deployment.key}.json"
    public = json.loads(descriptor.read_text())
    public["status"] = "draft"
    write_private(descriptor, public)
    monkeypatch.setattr(qwen_executor, "load_challenge_artifacts",
                        lambda *args, **kwargs: pytest.fail("draft loaded into production"))
    with pytest.raises(ValueError, match="public activation"):
        qwen_executor.load_plan(deployment.path)


def test_cli_init_is_explicit_and_pending_state_blocks_before_worker_build(deployment, monkeypatch):
    def forbidden(*args):
        pytest.fail("pending or missing state reached worker startup")

    monkeypatch.setattr(qwen_executor, "build_workers", forbidden)
    assert qwen_executor.main(["check", "--config", str(deployment.path)]) == 0
    assert not (deployment.state / "state.json").exists()
    assert qwen_executor.main(["run", "--config", str(deployment.path), "--once"]) == 78
    assert qwen_executor.main(["init", "--config", str(deployment.path)]) == 0
    assert qwen_executor.main(["init", "--config", str(deployment.path)]) == 78
    plan = qwen_executor.load_plan(deployment.path)
    with executor_lock(deployment.state):
        ExecutorState(deployment.state, plan.binding_sha256).begin(deployment.key)
    assert qwen_executor.main(["run", "--config", str(deployment.path), "--once"]) == 75
    assert read_state(deployment.state)["pending"] is not None


def test_worker_construction_uses_guarded_provider_and_closes_queue_on_failure(
    deployment, monkeypatch,
):
    plan = qwen_executor.load_plan(deployment.path)
    queues, providers = [], []

    class Queue:
        def __init__(self, **kwargs):
            self.closed = False
            queues.append(self)

        def health_check(self):
            pass

        def close(self):
            self.closed = True

    def worker(**kwargs):
        providers.append(kwargs["provider"])
        raise RuntimeError("fixture attestation failure")

    monkeypatch.setattr(qwen_executor, "RedisJobQueue", Queue)
    monkeypatch.setattr(qwen_executor, "build_submission_store", lambda **kwargs: SimpleNamespace(
        health_check=lambda: None))
    monkeypatch.setattr(qwen_executor, "QwenSubmissionWorker", worker)
    with executor_lock(deployment.state, create=True):
        state = ExecutorState.initialize(deployment.state, plan.binding_sha256)
        with pytest.raises(RuntimeError, match="attestation"), ExitStack() as stack:
            qwen_executor.build_workers(plan, state, stack, Event())
    assert len(queues) == 1 and queues[0].closed
    assert isinstance(providers[0], GuardedQwenProvider)


def test_executor_rejects_implicit_proxy(deployment, monkeypatch):
    monkeypatch.setattr(qwen_executor, "getproxies", lambda: {"http": "http://fixture-proxy"})
    with pytest.raises(ValueError, match="proxy"):
        qwen_executor.load_plan(deployment.path)


def test_large_executor_file_does_not_relax_authentication_file_limits(deployment):
    payload = deployment.path.read_text(encoding="utf-8")
    deployment.path.write_text(payload + " " * 20000, encoding="utf-8")
    assert deployment.key in qwen_executor.load_plan(deployment.path).contracts
    with pytest.raises(ValueError, match="private regular"):
        auth_config._read_protected(deployment.path)
