"""Request-level submission integration for isolated qualification.

No production entry point, model launch, schema migration, lease extension or sample resume.
The operator must hold executor_lock for this executor's complete lifetime.
"""

from collections import Counter, deque
from threading import Event, Lock
from time import monotonic

from .bounded_executor_state import BoundedExecutorState
from .qwen_runtime import QwenTokenLimitExceeded
from .runner import EvaluationPreflightError, JobDeadline, JobDeadlineExceeded
from .runtime_availability import ExecutorAvailability
from .sample_cache import VerifiedSelectionCache
from .sample_scheduler import SampleScheduler, prepare_job
from .submission_jobs import TokenLimitExceeded, _SubmissionWorkerCore


class RequestSubmissionExecutor:
    def __init__(self, *, store, queues, contracts, artifacts, preflights, slots, state,
                 model_capacity, max_jobs=4, stop=None, selection_cache=None,
                 model_healthy=None, history_limit=256):
        if not isinstance(state, BoundedExecutorState):
            raise TypeError('a shared bounded request ledger is required')
        if not contracts or any(set(mapping) != set(contracts)
                                for mapping in (queues, artifacts, preflights)):
            raise ValueError('submission routes must match exactly')
        if any(not callable(preflight) for preflight in preflights.values()):
            raise TypeError('each task requires its original request preflight')
        if any(contract.worker_model_concurrency != model_capacity
               for contract in contracts.values()):
            raise ValueError('contract capacity does not match the isolated runtime declaration')
        if any(contract.max_running_submissions_per_user != 1 for contract in contracts.values()):
            raise ValueError('this integration requires the existing one-running-job user policy')
        if not callable(getattr(store, 'claim_is_current', None)):
            raise TypeError('store must support current-claim observations')
        self._store, self._state = store, state
        self._slots, self._capacity, self._max_jobs = slots, model_capacity, max_jobs
        self._stop = stop if stop is not None else Event()
        if model_healthy is not None and not callable(model_healthy):
            raise TypeError('model health observation must be callable')
        if type(history_limit) is not int or not 1 <= history_limit <= 65536:
            raise ValueError('invalid executor history bound')
        self._model_healthy, self._history_limit = model_healthy, history_limit
        self._fault_signal = Event()
        self.availability = ExecutorAvailability(state=state,
            model_healthy=model_healthy or (lambda: False), stop=self._stop,
            challenge_ids=contracts, fault_signal=self._fault_signal)
        self._run_lock = Lock()
        self._continuous_started = False
        self._cache = selection_cache if selection_cache is not None else VerifiedSelectionCache()
        # Validate provider independence, endpoint, state binding and capacity before queue access.
        validated = self._scheduler()
        for contract in contracts.values():
            validated.validate_contract(contract)
        self._routes = {}
        for key, contract in contracts.items():
            self._routes[key] = _SubmissionWorkerCore(store=store, queue=queues[key],
                contract=contract, artifacts=artifacts[key], provider=slots[0][key],
                lease_seconds=contract.job_deadline_seconds, request_preflight=preflights[key],
                require_termination_confirmation=True, selection_cache=self._cache,
                claim_guard=state.claim_guard)
        self._cursor = 0

    def _scheduler(self, *, continuous=False):
        return SampleScheduler(self._slots, self._state, model_capacity=self._capacity,
            max_jobs=self._max_jobs, history_limit=self._history_limit,
            dispatch_ready=self.availability.dispatch_ready if continuous else None,
            fault_signal=self._fault_signal if continuous else None)

    def run_round(self):
        """Admit a bounded cohort, publish each completed job promptly, leave the rest queued."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError('executor is already running')
        try:
            if self._continuous_started:
                raise RuntimeError('continuous executor lifetimes cannot be replayed')
            return self._run_round()
        finally:
            self._run_lock.release()

    def _run_round(self):
        self._state.require_clean()
        scheduler = self._scheduler()
        contexts, publications = {}, {}
        observed = {key: set() for key in self._routes}
        exhausted = set()
        keys = tuple(self._routes)
        claim_count = attempts = 0
        while (claim_count < self._max_jobs and len(exhausted) < len(keys)
               and not self._stop.is_set()):
            key = keys[self._cursor % len(keys)]
            self._cursor = (self._cursor + 1) % len(keys)
            if key in exhausted:
                continue
            route = self._routes[key]
            with self._state.claim_guard():
                if self._stop.is_set():
                    break
                submission_id, claimed = route._receive_and_claim_attempt()
            attempts += 1
            if submission_id is None or submission_id in observed[key]:
                if claimed is not None:
                    raise RuntimeError('duplicate claim in one request admission round')
                exhausted.add(key)
                continue
            observed[key].add(submission_id)
            if claimed is None:
                continue
            delivery, claim = claimed
            claim_count += 1
            outcome = self._admit(scheduler, contexts, route, delivery, claim)
            if outcome is not None:
                publications[submission_id] = outcome

        def publish(submission_id, observation):
            publications[submission_id] = self._publish(contexts[submission_id], observation)

        computations = scheduler.run(on_terminal=publish) if contexts else {}
        self._state.require_clean()
        return {'claims': claim_count, 'queue_observations': attempts,
                'publications': publications, 'computations': computations,
                'finite_admission_round': True, 'production_qualified': False}

    def run(self):
        """Continuously admit bounded active jobs until stop; return only bounded diagnostics.

        One coordinator owns claims, contexts and terminal publication. Model calls run in
        independent slots. A short queue sweep is interleaved with sample completions; no
        unbounded seen-ID set or lifetime list of prompts/results is retained.
        """
        if self._model_healthy is None:
            raise ValueError('continuous execution requires explicit shared model health')
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError('executor is already running')
        try:
            if self._continuous_started:
                raise RuntimeError('continuous executor lifetimes cannot be replayed')
            self._continuous_started = True
            scheduler = self._scheduler(continuous=True)
            contexts, totals = {}, Counter()
            recent = deque(maxlen=self._history_limit)
            keys = tuple(self._routes)
            next_admission = 0.0
            scan_remaining = len(keys)
            peak_jobs = 0

            def record(submission_id, outcome):
                totals[outcome] += 1
                recent.append((submission_id, outcome))

            def publish(submission_id, observation):
                try:
                    outcome = self._publish(contexts[submission_id], observation)
                    del contexts[submission_id]
                    record(submission_id, outcome)
                except BaseException:
                    self.availability.fail()
                    raise

            def admit():
                nonlocal next_admission, peak_jobs, scan_remaining
                if self._stop.is_set():
                    scheduler.seal()
                    return
                if (monotonic() < next_admission or not scheduler.admission_capacity
                        or not self.availability()):
                    return
                # Back off after a sweep, not after each route. Bound coordinator work so
                # completed samples get processed even with many empty or busy-user queues.
                scan_started = monotonic()
                for _ in range(min(8, scan_remaining)):
                    key = keys[self._cursor % len(keys)]
                    self._cursor = (self._cursor + 1) % len(keys)
                    scan_remaining -= 1
                    route = self._routes[key]
                    with self._state.claim_guard():
                        if self._stop.is_set():
                            return
                        _, claimed = route._receive_and_claim_attempt()
                    totals['queue_observations'] += 1
                    if scan_remaining == 0:
                        scan_remaining = len(keys)
                        next_admission = monotonic() + .05
                    if claimed is not None:
                        delivery, claim = claimed
                        totals['claims'] += 1
                        outcome = self._admit(scheduler, contexts, route, delivery, claim)
                        peak_jobs = max(peak_jobs, len(contexts))
                        if outcome is not None:
                            record(claim.submission_id, outcome)
                        break
                    if monotonic() >= scan_started + .01 or monotonic() < next_admission:
                        break
                return monotonic() >= next_admission and bool(scheduler.admission_capacity)

            def tick():
                try:
                    return admit()
                except BaseException:
                    self.availability.fail()
                    raise

            self.availability.start()
            try:
                scheduler.run(on_tick=tick, on_terminal=publish,
                              keep_open=True, retain_completed=False)
                self._state.require_clean()
            except BaseException:
                self.availability.fail()
                raise
            finally:
                self.availability.close()
            return {'counts': dict(totals), 'recent_publications': tuple(recent),
                'peak_jobs': peak_jobs, 'retained_jobs': len(contexts),
                'retained_dispatches': len(scheduler.dispatch_order),
                'continuous_admission': True, 'production_qualified': False}
        finally:
            self._run_lock.release()

    def _admit(self, scheduler, contexts, route, delivery, claim):
        contract = route._contract
        if (claim.contract_snapshot_json != contract.snapshot_json
                or claim.evaluation_identity_sha256 != contract.evaluation_identity_sha256):
            route._handle_failure(delivery, claim, 'RUNTIME_MISCONFIGURATION', retry_allowed=False)
            return 'failed_preflight'
        if not self._store.claim_is_current(claim):
            self._discard_stale(route, delivery, claim)
            return 'stale_claim'
        try:
            job = prepare_job(submission_id=claim.submission_id, owner_id=claim.user_id,
                contract=contract, artifacts=route._artifacts, student_prompt=claim.student_prompt,
                deadline=JobDeadline.from_timestamp(claim.deadline_at),
                request_preflight=route._request_preflight, selection_cache=self._cache,
                before_sample=lambda: self._store.claim_is_current(claim))
        except (TokenLimitExceeded, QwenTokenLimitExceeded):
            if not self._store.complete_rejected(claim):
                self._store.expire_leases(evaluation_identity_sha256=claim.evaluation_identity_sha256)
            route._queue.ack(delivery)
            return 'rejected'
        except JobDeadlineExceeded:
            route._handle_failure(delivery, claim, 'JOB_DEADLINE', retry_allowed=False)
            return 'deadline_preflight'
        except (EvaluationPreflightError, OSError, ValueError):
            route._handle_failure(delivery, claim, 'DATASET_INTEGRITY', retry_allowed=False)
            return 'failed_preflight'
        except Exception:
            route._handle_failure(delivery, claim, 'RUNTIME_MISCONFIGURATION', retry_allowed=False)
            return 'failed_preflight'
        # Register before admission; no terminal callback can observe a missing claim context.
        contexts[claim.submission_id] = (route, delivery, claim)
        try:
            scheduler.add_job(job)
        except BaseException:
            del contexts[claim.submission_id]
            # Internal admission failure aborts this lifetime. Let the original lease expire;
            # do not misclassify an executor fault as corrupt input or requeue uncertain work.
            raise
        return None

    def _publish(self, context, observation):
        route, delivery, claim = context
        if observation['samples_inflight']:
            raise RuntimeError('cannot finalize a job with requests still in flight')
        if not self._store.claim_is_current(claim):
            self._discard_stale(route, delivery, claim)
            return 'stale_claim'
        if observation['status'] == 'succeeded':
            aggregate = observation['result']
            try:
                if (aggregate is None or observation['samples_completed']
                        != route._artifacts.public.sample_count
                        or aggregate.samples_total != route._artifacts.public.sample_count):
                    raise ValueError('incomplete aggregate cannot be published')
                owner_result = route._contract.owner_result(aggregate,
                    student_prompt_sha256=claim.student_prompt_sha256)
            except Exception:
                route._handle_failure(delivery, claim, 'RUNTIME_MISCONFIGURATION',
                                      retry_allowed=False)
                return 'failed_result_contract'
            if self._store.complete_success(claim, owner_result=owner_result):
                route._queue.ack(delivery)
                return 'succeeded'
            self._discard_stale(route, delivery, claim)
            return 'stale_at_publication'
        retry_allowed = observation['retry_allowed']
        try:
            self._state.require_dispatchable()
        except Exception:
            retry_allowed = False
        route._handle_failure(delivery, claim,
            observation['failure_code'] or 'RUNTIME_MISCONFIGURATION', retry_allowed=retry_allowed)
        record = self._store.submission_for_owner(claim.submission_id, claim.user_id)
        return 'requeued' if record and record.status.value == 'queued' else 'failed'

    def _discard_stale(self, route, delivery, claim):
        # Do not overwrite another lease or an already-terminal record with an old result.
        self._store.expire_leases(evaluation_identity_sha256=claim.evaluation_identity_sha256)
        self._store.expire_queued_deadlines(evaluation_identity_sha256=claim.evaluation_identity_sha256)
        route._queue.ack(delivery)
