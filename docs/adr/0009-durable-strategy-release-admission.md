# ADR 0009 — Durable strategy-release admission

- **Status:** Accepted
- **Date:** 2026-09-29

## Context

Conductor already distinguishes strategy operational lifecycle (`active`, `disabled`, `retiring`,
`retired`) from immutable strategy-version identity and evidence. A persisted `StrategyRelease`
record exists for each `(strategy_id, version)`, but its admission state previously had no controlled
transition rules and did not constrain shadow or live authority.

That creates two failure modes. First, configuration could describe a release state without a
reviewable transition history. Second, execution authority could be enabled while a strategy version
was still only research or under review.

## Decision

Conductor owns a durable release-admission state machine, separate from the operational lifecycle:

`RESEARCH -> VALIDATED -> SHADOW -> LIVE`

with terminal/exception states `KILLED` and `REVIEW`.

Allowed transitions are explicit and fail closed. `REVIEW` can return only to `VALIDATED` or move to
`KILLED`; it cannot jump directly back to `SHADOW` or `LIVE`. `KILLED` is terminal for that version.
Configured evidence references remain immutable within a version. ADR 0010 later adds separately
persisted, append-only portable evidence attachments without allowing configured evidence mutation.

Every release transition is stored in `strategy_release_transitions` and mirrored to the append-only
event ledger with actor, reason, prior state, target state, and timestamp. Repeating the current state
is idempotent and does not create duplicate transition history.

Release admission gates authority but does not grant it by itself:

- shadow cycles require every release on the route to be at least `VALIDATED`;
- cutover via `shadow-promote` requires every release on the route to be exactly `SHADOW`;
- any runtime started with `live_orders_enabled=true` requires every configured release on that route
  to be exactly `LIVE`;
- `REVIEW` and `KILLED` transitions disable the strategy's operational lifecycle; explicit
  re-activation is still required after review;
- `live_orders_enabled` remains an independent operator-controlled switch and stays false by default.

Release-management CLI commands operate directly on configuration plus the Conductor ledger and do
not start broker workers merely to inspect or change admission state.

## Consequences

- Strategy-version admission is durable, auditable, and restart-safe.
- A route cannot acquire live broker authority while any configured strategy release is not LIVE.
- Shadow cutover cannot accidentally treat a merely validated release as shadow-proven.
- Operational disable/retire semantics remain independent from research/admission semantics.
- Existing persisted releases are not silently promoted by editing configuration; operators must use
  an explicit transition or publish a new version.
- Specialist research systems can later attach evidence to new immutable releases without becoming
  execution authorities themselves.
