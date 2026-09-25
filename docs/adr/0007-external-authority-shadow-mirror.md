# ADR 0007 — External-authority Shadow Mirror

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

The Windows migration requires Conductor to shadow ETSA, RPSchteroids, and TLAQ while Dagster
continues to submit the real broker orders. Conductor's durable `virtual_positions` ledger is an
implemented economic-ownership ledger and advances only from internal crosses plus reconciled
execution facts. During shadowing, Dagster changes broker positions without producing Conductor
execution facts, so the durable virtual ledger correctly becomes stale relative to the broker.

Rebasing `virtual_positions` from the broker each day would make reconciliation appear clean by
fabricating ownership/fill history. Inferring ownership directly from current targets is also unsafe:
a target is desired state, not implemented state, and can differ because of partial fills, rejects,
buffers, or an exit still in progress.

## Decision

Conductor maintains a separate **Shadow Mirror** while execution authority remains external.

- `virtual_positions` remains the durable Conductor-owned economic ledger and does not move during
  external shadow.
- `shadow_positions` represents the best attributable view of what the legacy executor currently
  owns. It is explicitly migration state, not fill history.
- Single-strategy routes may map broker positions directly to that strategy.
- On shared routes, existing ownership is sticky. A changed quantity for a singly owned symbol stays
  with that owner unless another strategy also claims the changed symbol, in which case Conductor
  fails closed.
- A genuinely new broker symbol may be attributed only when exactly one fresh current intent claims
  it, or when an operator provides an explicit ownership manifest.
- A changed multiply-owned aggregate position is ambiguous without strategy-specific fills and fails
  closed.
- Route-wide `shadow-cycle` captures fresh intents before refreshing a shared route, then runs one
  counterfactual portfolio cycle.
- Position-delta strategies on a single-owner route refresh the mirror before strategy execution so
  deltas are applied to current implemented state. Shared-route position-delta capture is refused
  unless a future strategy-specific fill attribution mechanism removes the ambiguity.
- Active shadow authority hard-refuses `live_orders_enabled=true`.
- Even when a counterfactual target exactly matches broker state, an external-shadow cycle cannot
  commit the durable virtual ledger.
- At cutover, legacy execution is stopped, outstanding orders settle, the mirror must reconcile
  exactly to the broker, and `shadow-promote --confirm <route>` performs the one-time transfer into
  Conductor virtual ownership while live orders are still disabled.

## Consequences

The migration can run for multiple days while strategy universes and broker quantities change
without corrupting Conductor's economic ledger. `doctor` becomes authority-aware: Shadow Mirror vs
broker is the active invariant during external shadow, while virtual-ledger drift remains visible
for audit. After promotion, strict virtual-vs-broker reconciliation resumes.

The snapshot-based shared-route inference is intentionally conservative. Strategy-specific legacy
fill attribution (for example through IB client/order identifiers) can later strengthen the Shadow
Mirror, but it must not weaken the fail-closed ambiguity rules.
