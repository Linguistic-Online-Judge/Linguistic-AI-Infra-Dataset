import gzip
import json

import pytest

from linguistic_oj.kernel_trace import summarize_events, summarize_trace


def event(name, start, duration, device=0):
    return {'name': name, 'cat': 'kernel', 'ph': 'X', 'ts': start, 'dur': duration,
            'args': {'device': device}}


def test_overlapping_kernels_are_not_summed_into_wall_occupancy():
    report = summarize_events({'traceEvents': [event('A', 0, 10), event('B', 5, 10),
        event('A', 20, 5), {'name': 'cpu', 'cat': 'cpu_op', 'ph': 'X', 'ts': 0, 'dur': 100}]})
    assert report['summed_kernel_duration_us'] == 25
    assert report['union_kernel_busy_us'] == 20
    assert report['first_to_last_kernel_span_us'] == 25
    assert report['kernel_union_fraction_of_span'] == .8
    assert report['kernels'][0]['calls'] == 2
    assert report['kernels'][0]['fraction_of_summed_kernel_duration'] == .6


@pytest.mark.parametrize('events', [[], [event('A', 0, 1, 0), event('B', 0, 1, 1)],
    [event('A', 0, float('nan'))], [event('A', 0, -1)], [event('A', 0, 0)],
    [event('A', 0, 1, None)]])
def test_rejects_missing_multidevice_or_invalid_kernel_activity(events):
    with pytest.raises(ValueError):
        summarize_events({'traceEvents': events})


def test_gzip_trace_is_size_bounded_and_fingerprinted(tmp_path):
    path = tmp_path / 'trace.json.gz'
    payload = json.dumps({'traceEvents': [event('A', 3, 7)]}).encode()
    with gzip.open(path, 'wb') as file:
        file.write(payload)
    report = summarize_trace(path)
    assert report['decoded_trace_bytes'] == len(payload)
    assert len(report['decoded_trace_sha256']) == 64
    with pytest.raises(ValueError, match='limit'):
        summarize_trace(path, max_decoded_bytes=len(payload) - 1)
