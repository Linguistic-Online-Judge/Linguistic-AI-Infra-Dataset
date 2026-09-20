"""Summarize private PyTorch GPU kernel traces without confusing overlap with wall time."""

import argparse
import gzip
import hashlib
import json
import math
from collections import Counter
from pathlib import Path


def interval_union(intervals):
    total, start, end = 0.0, None, None
    for left, right in sorted(intervals):
        if start is None:
            start, end = left, right
        elif left <= end:
            end = max(end, right)
        else:
            total += end - start
            start, end = left, right
    return total + (end - start if start is not None else 0)


def summarize_events(document):
    if not isinstance(document, dict) or not isinstance(document.get('traceEvents'), list):
        raise ValueError('expected a PyTorch Chrome trace object')
    groups, intervals, devices = {}, [], set()
    for event in document['traceEvents']:
        if not isinstance(event, dict) or event.get('cat') != 'kernel' or event.get('ph') != 'X':
            continue
        name, start, duration = event.get('name'), event.get('ts'), event.get('dur')
        if (not isinstance(name, str) or not name or any(type(value) not in (int, float)
            or not math.isfinite(value) or value < 0 for value in (start, duration))
                or not math.isfinite(start + duration)):
            raise ValueError('invalid kernel event')
        device = event.get('args', {}).get('device')
        if type(device) is not int or device < 0:
            raise ValueError('kernel device identity is missing')
        devices.add(device)
        intervals.append((start, start + duration))
        group = groups.setdefault(name, {'calls': 0, 'summed_duration_us': 0.0})
        group['calls'] += 1
        group['summed_duration_us'] += duration
    if not intervals or len(devices) != 1:
        raise ValueError('expected kernel activity from exactly one device')
    summed = sum(item['summed_duration_us'] for item in groups.values())
    span = max(end for _, end in intervals) - min(start for start, _ in intervals)
    if summed <= 0 or span <= 0:
        raise ValueError('kernel timing is empty')
    union = interval_union(intervals)
    rows = [{'name': name, **values,
             'fraction_of_summed_kernel_duration': values['summed_duration_us'] / summed}
            for name, values in groups.items()]
    rows.sort(key=lambda row: row['summed_duration_us'], reverse=True)
    return {'schema_version': 'gpu-kernel-trace-summary-v1', 'device': next(iter(devices)),
        'kernel_events': len(intervals), 'distinct_kernel_names': len(rows),
        'summed_kernel_duration_us': summed, 'union_kernel_busy_us': union,
        'first_to_last_kernel_span_us': span, 'kernel_union_fraction_of_span': union / span,
        'kernels': rows, 'category_event_counts': dict(Counter(
            str(event.get('cat')) for event in document['traceEvents'] if isinstance(event, dict))),
        'scope': 'instrumented_capture_only_not_unprofiled_throughput',
        'hardware_bandwidth_or_compute_saturation_proven': False}


def summarize_trace(path, *, max_decoded_bytes=128 * 1024 * 1024):
    if type(max_decoded_bytes) is not int or max_decoded_bytes < 1:
        raise ValueError('trace byte limit must be positive')
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as file:
        raw = file.read(max_decoded_bytes + 1)
    if len(raw) > max_decoded_bytes:
        raise ValueError('decoded trace exceeds analysis limit')
    report = summarize_events(json.loads(raw))
    report['decoded_trace_sha256'] = hashlib.sha256(raw).hexdigest()
    report['decoded_trace_bytes'] = len(raw)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize_trace(args.trace), indent=2))
