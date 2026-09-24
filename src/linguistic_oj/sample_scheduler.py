"""Isolated request-level scheduler; no API/queue claims or production activation.

Jobs must already have verified ownership, a full manifest and their original absolute deadline.
One caller holds executor_lock for the entire scheduler lifetime.
"""

import hashlib
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Event, RLock

from .aggregation import aggregate_challenge
from .bounded_executor_state import BoundedExecutorState, BoundedGuardedProvider
from .challenge_registry import validate_contract_matches_public
from .providers import (
    ModelGeneration,
    ModelRequest,
    ProviderContractError,
    ProviderRequestError,
    ProviderTimeoutError,
)
from .responses import TaskType
from .runner import JobDeadline, JobDeadlineExceeded, _prepare_samples, evaluate_raw_response

_PREPARED_JOB_TOKEN = object()


class SampleClaimLost(RuntimeError):
    """The caller no longer owns the database claim; do not send another sample."""


@dataclass(frozen=True, repr=False)
class PreparedJob:
    submission_id: str
    owner_id: str
    contract: object
    artifacts: object
    samples: tuple
    requests: tuple
    deadline: JobDeadline
    before_sample: object = field(default=None, compare=False)
    _token: object = field(default=None, compare=False)

    def __post_init__(self):
        if self._token is not _PREPARED_JOB_TOKEN:
            raise TypeError('jobs must be constructed through complete prepare_job preflight')


def prepare_job(*, submission_id, owner_id, contract, artifacts, student_prompt,
                deadline, request_preflight, selection_cache=None, before_sample=None):
    if any(not isinstance(value, str) or not value.strip()
           for value in (submission_id, owner_id, student_prompt)):
        raise ValueError('job ownership and exact nonempty prompt are required')
    if not isinstance(deadline, JobDeadline) or not callable(request_preflight):
        raise TypeError('original absolute deadline and request preflight are required')
    if before_sample is not None and not callable(before_sample):
        raise TypeError('claim observation must be callable')
    if len(student_prompt.encode('utf-8')) > contract.student_prompt_utf8_bytes:
        raise ValueError('student prompt exceeds the existing byte budget')
    validate_contract_matches_public(contract, artifacts.public)
    deadline.require_remaining()
    samples = _prepare_samples(artifacts, selection_cache=selection_cache)
    requests = tuple(ModelRequest(task=TaskType(artifacts.public.task),
        language=artifacts.public.language, treebank=artifacts.public.treebank,
        student_prompt=student_prompt, model_input=sample.model_input) for sample in samples)
    request_preflight(requests)
    deadline.require_remaining()
    return PreparedJob(submission_id, owner_id, contract, artifacts, samples, requests, deadline,
                       before_sample=before_sample, _token=_PREPARED_JOB_TOKEN)


class ScheduledProvider(BoundedGuardedProvider):
    """The correlation record stays in the private ledger, never in model messages."""

    def generate(self, request, /, *, timeout_seconds=None):
        raise ProviderContractError('request-level execution requires explicit sample binding')

    def generate_sample(self, request, *, context, timeout_seconds):
        if context.get('request_sha256') != hashlib.sha256(self._request_body(request)).hexdigest():
            raise ProviderContractError('request correlation does not match exact request bytes')
        return self._generate_guarded(request, timeout_seconds=timeout_seconds,
                                      request_context=context)


@dataclass(repr=False)
class _Progress:
    job: PreparedJob
    cursor: int = 0
    inflight: set = field(default_factory=set)
    outcomes: dict = field(default_factory=dict)
    aggregate: object = None
    error: str | None = None
    error_type: str | None = None
    failure_code: str | None = None
    retry_allowed: bool = False
    terminal_notified: bool = False


class SampleScheduler:
    """Fewest in-flight samples first, round-robin ties; idle capacity can serve one job.

    This is a prototype policy, not a change to deployed FIFO/user admission rules.
    Graceful stop rejects new jobs but drains admitted jobs. Incidents stop sample dispatch.
    """

    def __init__(self, slots, state, *, model_capacity, stop=None, max_jobs=32, per_job_limit=None,
                 dispatch_ready=None, history_limit=4096, fault_signal=None):
        if (not isinstance(state, BoundedExecutorState) or type(model_capacity) is not int
                or not 1 <= model_capacity <= 32 or not slots
                or len(slots) > min(model_capacity, state.max_inflight)
                or type(max_jobs) is not int or not 1 <= max_jobs <= 32):
            raise ValueError('invalid request scheduler capacity')
        if dispatch_ready is not None and not callable(dispatch_ready):
            raise TypeError('dispatch readiness must be callable')
        if fault_signal is not None and not isinstance(fault_signal, Event):
            raise TypeError('scheduler fault signal must be an Event')
        if type(history_limit) is not int or not 1 <= history_limit <= 65536:
            raise ValueError('invalid dispatch history bound')
        per_job_limit = len(slots) if per_job_limit is None else per_job_limit
        if type(per_job_limit) is not int or not 1 <= per_job_limit <= len(slots):
            raise ValueError('per-job request limit must fit the global slots')
        keys = tuple(slots[0])
        if not keys or any(tuple(slot) != keys for slot in slots):
            raise ValueError('all slots must offer the same task routes')
        providers = [provider for slot in slots for provider in slot.values()]
        if (any(not isinstance(provider, ScheduledProvider) or provider.executor_state is not state
                for provider in providers) or len({id(p) for p in providers}) != len(providers)):
            raise ValueError('slots require independent sample-bound providers sharing one ledger')
        if (len({p.base_url for p in providers}) != 1
                or any(p.identity != providers[0].identity for p in providers)
                or any(provider.challenge_id != key
                       for slot in slots for key, provider in slot.items())):
            raise ValueError('provider route or model identity mismatch')
        state.require_clean()
        self._slots = tuple(dict(slot) for slot in slots)
        self._state = state
        self._capacity = model_capacity
        self._stop = stop if stop is not None else Event()
        self._max_jobs = max_jobs
        self._per_job_limit = per_job_limit
        self._lock = RLock()
        self._jobs = {}
        self._last_job = None
        self._started = False
        self._closed = False
        self._halted = False
        self._accepting = True
        self._wake = Event()
        self._dispatch_ready = dispatch_ready
        self._fault_signal = fault_signal if fault_signal is not None else Event()
        self._dispatches = deque(maxlen=history_limit)

    def add_job(self, job):
        if not isinstance(job, PreparedJob):
            raise TypeError('a prepared, preflighted job is required')
        with self._lock:
            if self._closed or self._halted or not self._accepting or self._stop.is_set():
                raise RuntimeError('scheduler no longer accepts jobs')
            self._state.require_dispatchable()
            if job.submission_id in self._jobs or len(self._jobs) >= self._max_jobs:
                raise ValueError('duplicate job or finite-session job limit reached')
            if any(progress.job.owner_id == job.owner_id and progress.aggregate is None
                   and (progress.error is None or progress.inflight)
                   for progress in self._jobs.values()):
                raise ValueError('owner already has an active admitted job')
            self.validate_contract(job.contract)
            self._jobs[job.submission_id] = _Progress(job)
            self._wake.set()

    @property
    def admission_capacity(self):
        with self._lock:
            if self._closed or self._halted or not self._accepting or self._stop.is_set():
                return 0
            return self._max_jobs - len(self._jobs)

    def seal(self):
        """Close job admission while allowing all already-admitted work to drain."""
        with self._lock:
            self._accepting = False
            self._wake.set()

    def validate_contract(self, contract):
        """Static provider checks, usable by an adapter before it claims a submission."""
        if contract.worker_model_concurrency != self._capacity:
            raise ValueError('job contract differs from declared model capacity')
        for slot in self._slots:
            provider = slot.get(contract.challenge_id)
            identity = contract.evaluation_identity
            if (provider is None or provider.structured_json
                    or provider.identity.to_dict() != identity['model_identity']
                    or provider.settings.to_dict() != identity['generation_settings']
                    or provider.timeout_seconds != contract.provider_request_timeout_seconds
                    or provider.max_response_body_bytes != contract.provider_response_body_bytes):
                raise ValueError('provider does not preserve the job configuration')

    def _next(self):
        candidates = []
        keys = list(self._jobs)
        if self._last_job in keys:
            start = keys.index(self._last_job) + 1
            keys = keys[start:] + keys[:start]
        for key in keys:
            progress = self._jobs[key]
            if (progress.error is not None or progress.cursor == len(progress.job.samples)
                    or len(progress.inflight) >= self._per_job_limit):
                continue
            try:
                progress.job.deadline.require_remaining()
            except JobDeadlineExceeded:
                progress.error = 'JOB_DEADLINE'
                progress.error_type = 'JobDeadlineExceeded'
                progress.failure_code = 'JOB_DEADLINE'
                continue
            candidates.append(key)
        if not candidates:
            return None
        key = min(candidates, key=lambda value: len(self._jobs[value].inflight))
        progress = self._jobs[key]
        position = progress.cursor
        progress.cursor += 1
        progress.inflight.add(position)
        self._last_job = key
        self._dispatches.append((key, position + 1))
        return key, position

    def _evaluate(self, slot, key, position):
        job = self._jobs[key].job
        sample, request = job.samples[position], job.requests[position]
        if job.before_sample is not None and job.before_sample() is not True:
            raise SampleClaimLost('submission claim is no longer current')
        provider = self._slots[slot][job.contract.challenge_id]
        context = {'submission_sha256': hashlib.sha256(key.encode()).hexdigest(),
            'sample_id_sha256': hashlib.sha256(
                sample.manifest_sample.sample_id.encode()).hexdigest(),
            'sample_position': position + 1,
            'request_sha256': hashlib.sha256(provider._request_body(request)).hexdigest()}
        generation = provider.generate_sample(request, context=context,
            timeout_seconds=job.deadline.require_remaining())
        if not isinstance(generation, ModelGeneration):
            raise ProviderContractError('provider must return a complete generation')
        job.deadline.require_remaining()
        outcome = evaluate_raw_response(sample=sample.dataset_sample,
            manifest_sample=sample.manifest_sample, task=request.task,
            model_input=sample.model_input, raw_response=generation.raw_text)
        return key, position, outcome

    def snapshot(self):
        """Private observation: incomplete jobs have no aggregate or zero-filled result."""
        with self._lock:
            return {key: {'status': ('succeeded' if p.aggregate is not None else
                'failed' if p.error is not None else 'running'), 'error': p.error,
                'error_type': p.error_type,
                'failure_code': p.failure_code, 'retry_allowed': p.retry_allowed,
                'samples_dispatched': p.cursor, 'samples_completed': len(p.outcomes),
                'samples_inflight': len(p.inflight), 'result': p.aggregate}
                for key, p in self._jobs.items()}

    @property
    def dispatch_order(self):
        with self._lock:
            return tuple(self._dispatches)

    def abort(self):
        """Stop new sample dispatch; do not cancel already-sent calls or invent partial grades."""
        with self._lock:
            self._halted = True
            self._fault_signal.set()
            self._wake.set()

    def run(self, *, on_terminal=None, on_tick=None, keep_open=False, retain_completed=True):
        if on_terminal is not None and not callable(on_terminal):
            raise TypeError('terminal publication callback must be callable')
        if on_tick is not None and not callable(on_tick):
            raise TypeError('admission callback must be callable')
        if type(keep_open) is not bool or type(retain_completed) is not bool:
            raise TypeError('scheduler lifecycle flags must be booleans')
        if not retain_completed and on_terminal is None:
            raise ValueError('retiring jobs requires an authoritative publication callback')
        with self._lock:
            if self._started:
                raise RuntimeError('scheduler sessions cannot be replayed')
            self._state.require_clean()
            self._started = True
        active, free = {}, list(range(len(self._slots)))

        def notify_terminal():
            if on_terminal is None:
                return
            with self._lock:
                completed = [(key, value) for key, value in self.snapshot().items()
                    if not self._jobs[key].terminal_notified and value['status'] != 'running'
                    and value['samples_inflight'] == 0]
                for key, _ in completed:
                    self._jobs[key].terminal_notified = True
            try:
                for key, value in completed:
                    on_terminal(key, value)
                    if not retain_completed:
                        with self._lock:
                            del self._jobs[key]
                            self._wake.set()
            except BaseException:
                self.abort()
                raise

        try:
            with ThreadPoolExecutor(max_workers=len(self._slots)) as pool:
                while True:
                    admission_pending = False
                    # A single coordinator admits jobs and publishes results outside the lock.
                    # Worker threads only execute samples; no admission/publication lock inversion.
                    if on_tick is not None and not self._halted:
                        try:
                            admission_pending = on_tick() is True
                        except BaseException:
                            self.abort()
                            raise
                    ready = True
                    if self._dispatch_ready is not None:
                        try:
                            ready = self._dispatch_ready() is True
                        except Exception:
                            self.abort()
                            ready = False
                    with self._lock:
                        self._wake.clear()
                        if self._stop.is_set():
                            self._accepting = False
                        if not self._halted:
                            try:
                                self._state.require_dispatchable()
                            except Exception:
                                self.abort()
                        for progress in self._jobs.values():
                            if progress.aggregate is None and progress.error is None:
                                try:
                                    progress.job.deadline.require_remaining()
                                except JobDeadlineExceeded:
                                    progress.error = 'JOB_DEADLINE'
                                    progress.error_type = 'JobDeadlineExceeded'
                                    progress.failure_code = 'JOB_DEADLINE'
                                    progress.retry_allowed = False
                        while free and not self._halted and ready:
                            selected = self._next()
                            if selected is None:
                                break
                            key, position = selected
                            slot = free.pop(0)
                            active[pool.submit(self._evaluate, slot, key, position)] = (
                                slot, key, position)
                        if self._halted:
                            for progress in self._jobs.values():
                                if progress.aggregate is None and progress.error is None:
                                    progress.error = 'EXECUTOR_BLOCKED'
                                    progress.failure_code = 'RUNTIME_MISCONFIGURATION'
                    if not active:
                        notify_terminal()
                        with self._lock:
                            drained = all(p.aggregate is not None or p.error is not None
                                          for p in self._jobs.values())
                            if drained and (self._halted or not keep_open or not self._accepting):
                                self._closed = True
                                break
                        self._wake.wait(0 if admission_pending else .1)
                        continue
                    done, _ = wait(active, timeout=0 if admission_pending else .05,
                                   return_when=FIRST_COMPLETED)
                    with self._lock:
                        for future in done:
                            slot, key, position = active.pop(future)
                            free.append(slot)
                            progress = self._jobs[key]
                            progress.inflight.remove(position)
                            try:
                                result_key, result_position, outcome = future.result()
                                if ((result_key, result_position) != (key, position)
                                        or outcome.sample_id != progress.job.samples[
                                            position].manifest_sample.sample_id
                                        or position in progress.outcomes):
                                    raise RuntimeError('sample response binding mismatch')
                                progress.outcomes[position] = outcome
                            except JobDeadlineExceeded:
                                progress.error = 'JOB_DEADLINE'
                                progress.error_type = 'JobDeadlineExceeded'
                                progress.failure_code = 'JOB_DEADLINE'
                                progress.retry_allowed = False
                            except SampleClaimLost:
                                progress.error = 'CLAIM_LOST'
                                progress.error_type = 'SampleClaimLost'
                                progress.failure_code = 'WORKER_CRASH'
                                progress.retry_allowed = False
                            except ProviderRequestError as error:
                                if progress.error in (None, 'PROVIDER_REQUEST_FAILED'):
                                    progress.retry_allowed = (error.termination_confirmed and (
                                        progress.error is None or progress.retry_allowed))
                                    if progress.error is None:
                                        progress.failure_code = ('PROVIDER_TIMEOUT'
                                            if isinstance(error, ProviderTimeoutError)
                                            else 'PROVIDER_TRANSPORT')
                                        progress.error_type = type(error).__name__
                                    progress.error = 'PROVIDER_REQUEST_FAILED'
                            except BaseException as error:
                                progress.error = 'EXECUTION_FAILED'
                                progress.error_type = type(error).__name__
                                progress.failure_code = 'RUNTIME_MISCONFIGURATION'
                                progress.retry_allowed = False
                                self.abort()
                            if (progress.error is None and len(progress.outcomes)
                                    == len(progress.job.samples)):
                                try:
                                    progress.job.deadline.require_remaining()
                                    aggregate = aggregate_challenge(progress.job.artifacts,
                                        [progress.outcomes[index]
                                         for index in range(len(progress.job.samples))])
                                    progress.job.deadline.require_remaining()
                                    progress.aggregate = aggregate
                                except JobDeadlineExceeded:
                                    progress.error = 'JOB_DEADLINE'
                                    progress.error_type = 'JobDeadlineExceeded'
                                    progress.failure_code = 'JOB_DEADLINE'
                                except Exception as error:
                                    progress.error = 'AGGREGATION_FAILED'
                                    progress.error_type = type(error).__name__
                                    progress.failure_code = 'RUNTIME_MISCONFIGURATION'
                                    self.abort()
                        if self._halted:
                            for progress in self._jobs.values():
                                if progress.aggregate is None and progress.error is None:
                                    progress.error = 'EXECUTOR_BLOCKED'
                                    progress.failure_code = 'RUNTIME_MISCONFIGURATION'
                    notify_terminal()
        finally:
            with self._lock:
                self._closed = True
                for progress in self._jobs.values():
                    if progress.aggregate is None and progress.error is None:
                        progress.error = 'EXECUTOR_BLOCKED'
                        progress.failure_code = 'RUNTIME_MISCONFIGURATION'
        return self.snapshot()
