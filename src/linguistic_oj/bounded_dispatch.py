"""Isolated whole-job slot coordinator; no production wiring or sample fan-out."""

import math
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event
from time import monotonic

from .bounded_executor_state import BoundedGuardedProvider


def run_bounded(lanes, state, stop, *, model_capacity, idle_seconds=0.25):
    """Each lane owns independent workers/providers; DB claims still enforce user limits.

    model_capacity is declared by the isolated caller, NOT live runtime attestation.
    Normal stop drains claimed jobs. Failure blocks new claims and drains running calls.
    """
    if (type(model_capacity) is not int or not 1 <= model_capacity <= 32
            or not lanes or len(lanes) > model_capacity or len(lanes) > state.max_inflight
            or not math.isfinite(idle_seconds) or idle_seconds <= 0):
        raise ValueError('invalid bounded dispatcher capacity or idle interval')
    keys = tuple(lanes[0])
    if not keys or any(tuple(lane) != keys for lane in lanes):
        raise ValueError('lanes must use the same ordered task routes')
    workers = [worker for lane in lanes for worker in lane.values()]
    if len({id(worker) for worker in workers}) != len(workers):
        raise ValueError('each lane must own independent worker instances')
    providers = [getattr(worker, '_provider', None) for worker in workers]
    if (any(not isinstance(provider, BoundedGuardedProvider)
            or provider.executor_state is not state for provider in providers)
            or len({id(provider) for provider in providers}) != len(providers)):
        raise ValueError(
            'workers require independent providers bound to this shared request ledger')
    if (len({provider.base_url for provider in providers}) != 1
            or any(provider.identity != providers[0].identity for provider in providers)):
        raise ValueError('all lanes must target the same model endpoint and identity')
    if any(getattr(worker, '_contract', None) is not None
           and worker._contract.worker_model_concurrency != model_capacity for worker in workers):
        raise ValueError('worker contract capacity differs from declared model capacity')
    if any(getattr(worker, '_contract', None) is not None
           and getattr(worker, '_claim_guard', None) != state.claim_guard for worker in workers):
        raise ValueError('submission workers require the shared durable claim gate')
    state.require_clean()
    failed = Event()
    cursors, available = [index % len(keys) for index in range(len(lanes))], [0.] * len(lanes)

    def consume(lane_index):
        for _ in keys:
            if stop.is_set() or failed.is_set():
                return False
            state.require_dispatchable()
            key = keys[cursors[lane_index]]
            cursors[lane_index] = (cursors[lane_index] + 1) % len(keys)
            worked = lanes[lane_index][key].run_once()
            state.require_dispatchable()
            if worked:
                return True
        return False

    error = None
    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        active = {}
        while True:
            if not stop.is_set() and not failed.is_set():
                try:
                    state.require_dispatchable()
                    occupied = set(active.values())
                    for index in range(len(lanes)):
                        if index not in occupied and monotonic() >= available[index]:
                            active[pool.submit(consume, index)] = index
                except BaseException as caught:
                    error = caught
                    failed.set()
            if not active:
                if stop.is_set() or failed.is_set():
                    break
                stop.wait(max(0, min(available) - monotonic()))
                continue
            done, _ = wait(active, timeout=min(idle_seconds, 0.05), return_when=FIRST_COMPLETED)
            for future in done:
                index = active.pop(future)
                try:
                    worked = future.result()
                    available[index] = monotonic() + (0 if worked else idle_seconds)
                except BaseException as caught:
                    error = error or caught
                    failed.set()
    if error is not None:
        raise error
    state.require_clean()
