# ADR 0003: Multi-Challenge Runtime Routing

## Status

Accepted on 2026-09-04.

## Context

The first online slice injected one evaluation contract, one outbox dispatcher,
and one queue into the API. Submission rows already store the complete canonical
contract snapshot and both identity hashes, and queue routes already use the
contract snapshot hash. The missing piece was selecting among several trusted
contracts without allowing a request or job message to choose arbitrary runtime
configuration.

## Decision

The deployment supplies one immutable challenge registry to both the API and
Workers. The API loads and validates the complete registry before serving and
creates one outbox dispatcher and queue route for every executable contract.
Submission requests select only by registered challenge ID.

Each Worker process selects one registry challenge at startup. It remains bound
to that challenge's public description, evaluation contract, private manifest,
dataset, provider, and contract-snapshot queue. A Worker never loads a contract
from a job message or database row; it compares those stored identities with its
trusted startup contract.

Public-only registry entries cannot accept submissions. Executable contracts in
one API process must share request-body, global queue, per-user outstanding, and
per-user running limits because those policies are enforced above or across
challenge partitions. Challenge-specific prompt, deadline, provider, and rolling
per-challenge quota limits may differ.

Stored contract snapshots remain authoritative when returning historical owner
results and leaderboards. The current registry is used for new admission, not for
reinterpreting old scores.

## Consequences

- Supporting more challenges does not require a database migration or queue
  message-format change.
- A bad registry entry, missing dispatcher, mismatched global policy, or artifact
  mismatch prevents startup.
- A changed contract snapshot must receive a new versioned challenge ID.
  Deployments keep the previous registry entry and Worker alive until its outbox
  and queue drain; replacing a snapshot under one ID is unsupported.
- If a platform-wide admission limit changes, old and new contracts cannot coexist.
  The deployment must stop admission, drain all old outbox and queue work in a
  maintenance window, and then replace the registry. A future platform-policy
  layer may separate these limits from evaluation contracts.
- A production registry is deployment-owned and is not committed until source
  rights, activation, private artifacts, and runtime evidence are approved.
