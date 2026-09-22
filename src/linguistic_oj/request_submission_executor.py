"""Finite-round request-level submission integration for isolated qualification.

No production entry point, model launch, schema migration, lease extension or sample resume.
The operator must hold executor_lock for this executor's complete lifetime.
"""

from threading import Event

from .bounded_executor_state import BoundedExecutorState
from .qwen_runtime import QwenTokenLimitExceeded
from .runner import EvaluationPreflightError, JobDeadline, JobDeadlineExceeded
from .sample_cache import VerifiedSelectionCache
from .sample_scheduler import SampleScheduler, prepare_job
from .submission_jobs import TokenLimitExceeded, _SubmissionWorkerCore


class RequestSubmissionExecutor:
    def __init__(self, *, store, queues, contracts, artifacts, preflights, slots, state,
                 model_capacity, max_jobs=4, stop=None, selection_cache=None):
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

    def _scheduler(self):
        return SampleScheduler(self._slots, self._state, model_capacity=self._capacity,
                               max_jobs=self._max_jobs)

    def run_round(self):
        """Admit a bounded cohort, publish each completed job promptly, leave the rest queued."""
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
            contract = route._contract
            if (claim.contract_snapshot_json != contract.snapshot_json
                    or claim.evaluation_identity_sha256 != contract.evaluation_identity_sha256):
                route._handle_failure(delivery, claim, 'RUNTIME_MISCONFIGURATION',
                                      retry_allowed=False)
                publications[submission_id] = 'failed_preflight'
                continue
            if not self._store.claim_is_current(claim):
                self._discard_stale(route, delivery, claim)
                publications[submission_id] = 'stale_claim'
                continue
            try:
                job = prepare_job(submission_id=claim.submission_id, owner_id=claim.user_id,
                    contract=contract, artifacts=route._artifacts,
                    student_prompt=claim.student_prompt,
                    deadline=JobDeadline.from_timestamp(claim.deadline_at),
                    request_preflight=route._request_preflight, selection_cache=self._cache,
                    before_sample=lambda selected=claim: self._store.claim_is_current(selected))
                scheduler.add_job(job)
            except (TokenLimitExceeded, QwenTokenLimitExceeded):
                if not self._store.complete_rejected(claim):
                    self._store.expire_leases(evaluation_identity_sha256=claim.evaluation_identity_sha256)
                route._queue.ack(delivery)
                publications[submission_id] = 'rejected'
                continue
            except JobDeadlineExceeded:
                route._handle_failure(delivery, claim, 'JOB_DEADLINE', retry_allowed=False)
                publications[submission_id] = 'deadline_preflight'
                continue
            except (EvaluationPreflightError, OSError, ValueError):
                route._handle_failure(delivery, claim, 'DATASET_INTEGRITY', retry_allowed=False)
                publications[submission_id] = 'failed_preflight'
                continue
            except Exception:
                route._handle_failure(delivery, claim, 'RUNTIME_MISCONFIGURATION',
                                      retry_allowed=False)
                publications[submission_id] = 'failed_preflight'
                continue
            contexts[submission_id] = (route, delivery, claim)

        def publish(submission_id, observation):
            route, delivery, claim = contexts[submission_id]
            if observation['samples_inflight']:
                raise RuntimeError('cannot finalize a job with requests still in flight')
            if not self._store.claim_is_current(claim):
                self._discard_stale(route, delivery, claim)
                publications[submission_id] = 'stale_claim'
                return
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
                    publications[submission_id] = 'failed_result_contract'
                    return
                if self._store.complete_success(claim, owner_result=owner_result):
                    publications[submission_id] = 'succeeded'
                    route._queue.ack(delivery)
                else:
                    # A pre-write observation never substitutes for the transactional lease fence.
                    self._discard_stale(route, delivery, claim)
                    publications[submission_id] = 'stale_at_publication'
                return
            retry_allowed = observation['retry_allowed']
            try:
                self._state.require_dispatchable()
            except Exception:
                retry_allowed = False
            route._handle_failure(delivery, claim,
                observation['failure_code'] or 'RUNTIME_MISCONFIGURATION',
                retry_allowed=retry_allowed)
            record = self._store.submission_for_owner(submission_id, claim.user_id)
            publications[submission_id] = ('requeued'
                if record and record.status.value == 'queued' else 'failed')

        computations = scheduler.run(on_terminal=publish) if contexts else {}
        self._state.require_clean()
        return {'claims': claim_count, 'queue_observations': attempts,
                'publications': publications, 'computations': computations,
                'finite_admission_round': True, 'production_qualified': False}

    def _discard_stale(self, route, delivery, claim):
        # Do not overwrite another lease or an already-terminal record with an old result.
        self._store.expire_leases(evaluation_identity_sha256=claim.evaluation_identity_sha256)
        self._store.expire_queued_deadlines(evaluation_identity_sha256=claim.evaluation_identity_sha256)
        route._queue.ack(delivery)
