import json
from types import SimpleNamespace

import pytest

from linguistic_oj import auth_config
from linguistic_oj.challenge import build_challenge
from linguistic_oj.executor_state import ExecutorState, GuardedQwenProvider, executor_lock
from linguistic_oj.generation_stages import (
    HISTOGRAMS,
    REQUIRED,
    MetricsUnavailable,
    measure_one,
    parse_metrics,
    stage_delta,
)
from linguistic_oj.providers import (
    GenerationSettings,
    ModelGeneration,
    ModelIdentity,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderTransportError,
)
from linguistic_oj.responses import TaskType
from linguistic_oj.runner import _prepare_samples


def counters(n=0):
    result = dict.fromkeys(REQUIRED, 0.0)
    result['request_success_total'] = n
    for name in HISTOGRAMS:
        result[name + '_count'] = n
    for name, value in {'time_to_first_token_seconds': .12, 'e2e_request_latency_seconds': 1.13,
        'request_queue_time_seconds': .01, 'request_prefill_time_seconds': .1,
        'request_decode_time_seconds': 1, 'request_inference_time_seconds': 1.1,
        'request_prompt_tokens': 32, 'request_generation_tokens': 5}.items():
        result[name + '_sum'] = value * n
    return result


def exposition(values):
    lines = []
    for name, value in values.items():
        labels = 'engine="0",model_name="Qwen/Qwen3.5-9B"'
        if name == 'request_success_total':
            labels += ',finished_reason="stop"'
        lines.append(f'vllm:{name}{{{labels}}} {value}')
    return '\n'.join(lines)


def test_exact_counter_deltas_measure_server_stages_and_exclude_first_decode_token():
    before, after = (parse_metrics(exposition(counters(n))) for n in (12, 13))
    result = stage_delta(before, after, prompt_tokens=32, output_tokens=5)
    assert result['request_prefill_time_seconds'] == pytest.approx(.1)
    assert result['request_decode_time_seconds'] == pytest.approx(1)
    assert result['decode_tokens_per_second'] == pytest.approx(4)
    assert result['decode_fraction_of_inference'] == pytest.approx(1 / 1.1)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'nan', 'wrong-model', 'wrong-engine'])
def test_metric_parser_rejects_ambiguous_or_invalid_sources(mutation):
    text = exposition(counters())
    if mutation == 'missing':
        text = '\n'.join(text.splitlines()[1:])
    elif mutation == 'duplicate':
        text += '\n' + text.splitlines()[0]
    elif mutation == 'nan':
        text = text.replace(' 0.0', ' NaN', 1)
    elif mutation == 'wrong-model':
        text = text.replace('Qwen/Qwen3.5-9B', 'other')
    else:
        text = text.replace('engine="0"', 'engine="1"')
    with pytest.raises(MetricsUnavailable):
        parse_metrics(text)


@pytest.mark.parametrize('mutation', ['multiple', 'reset', 'busy', 'stale', 'tokens', 'phases'])
def test_counter_attribution_rejects_contamination_reset_and_partial_updates(mutation):
    before, after = counters(5), counters(6)
    if mutation == 'multiple':
        after = counters(7)
    elif mutation == 'reset':
        after = counters(1)
    elif mutation == 'busy':
        after['num_requests_waiting'] = 1
    elif mutation == 'stale':
        after['time_to_first_token_seconds_count'] = 5
    elif mutation == 'tokens':
        after['request_generation_tokens_sum'] += 1
    else:
        after['request_inference_time_seconds_sum'] += .1
    with pytest.raises(MetricsUnavailable):
        stage_delta(before, after, prompt_tokens=32, output_tokens=5)


@pytest.fixture
def case(tmp_path):
    path = tmp_path / 'samples.jsonl'
    path.write_text(json.dumps({'id': 'private-id', 'language': 'English', 'treebank': 'Fixture',
        'text': 'Secret', 'tasks_available': ['upos'],
        'answers': {'segmentation': ['Secret'], 'upos': ['NOUN']}}), encoding='utf-8')
    artifacts = build_challenge(path, language='English', treebank='Fixture', task='upos',
                                count=1, seed=2026, version='test-v1')
    prepared = _prepare_samples(artifacts)[0]
    request = ModelRequest(task=TaskType.UPOS, language='English', treebank='Fixture',
                           student_prompt='Private prompt', model_input=prepared.model_input)
    return SimpleNamespace(position=1, prepared=prepared, request=request, input_tokens=32)


class Provider:
    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return ModelGeneration('{"tags":["NOUN"]}', 5, 'stop', 32)


def test_completed_request_with_unusable_metrics_is_recorded_but_never_resent(case):
    provider = Provider()
    snapshots = iter((counters(), counters(2)))
    row = measure_one(case, provider, lambda: next(snapshots), lambda: True)
    assert provider.calls == 1
    assert not row['attribution_valid'] and row['stages'] is None
    assert row['score_statistics']['accuracy'] == 1
    assert all(private not in json.dumps(row)
               for private in ('Secret', 'Private prompt', 'private-id'))


def test_delayed_metrics_settle_without_resending(case):
    provider = Provider()
    snapshots = iter((counters(), counters(), counters(1)))
    row = measure_one(case, provider, lambda: next(snapshots), lambda: True)
    assert row['attribution_valid'] and provider.calls == 1


def test_busy_before_send_prevents_generation(case):
    provider = Provider()
    with pytest.raises(MetricsUnavailable):
        measure_one(case, provider, lambda: counters(), lambda: False)
    assert provider.calls == 0


def test_changed_online_state_after_completion_discards_attribution(case):
    snapshots, allowed = iter((counters(), counters(1))), iter((True, False))
    row = measure_one(case, Provider(), lambda: next(snapshots), lambda: next(allowed))
    assert not row['attribution_valid'] and row['stages'] is None


def test_transport_uncertainty_preserves_persistent_barrier(case, tmp_path, monkeypatch):
    monkeypatch.setattr(auth_config, '_check_windows_acl', lambda path: None)
    directory = tmp_path / 'state'
    directory.mkdir(mode=0o700)

    def fail(self, request, **kwargs):
        raise ProviderTransportError('lost connection', termination_confirmed=False)

    monkeypatch.setattr(OpenAICompatibleProvider, 'generate', fail)
    with executor_lock(directory, create=True):
        state = ExecutorState.initialize(directory, 'a' * 64)
        provider = GuardedQwenProvider(executor_state=state, challenge_id='fixture',
            base_url='http://127.0.0.1:8000/v1', identity=ModelIdentity(
                runtime='fixture', model='fixture', revision='a' * 40, runtime_version='fixture'),
            settings=GenerationSettings())
        with pytest.raises(ProviderTransportError):
            measure_one(case, provider, lambda: counters(), lambda: True)
        assert state.snapshot()['pending'] is not None
