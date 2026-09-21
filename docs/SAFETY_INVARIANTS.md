# Conductor safety invariants

These rules are stronger than implementation details. A change that violates one requires an
explicit architecture decision, migration plan, and updated operational gate. Passing unit tests is
not sufficient evidence for weakening an invariant.

## S1 — Strategies express intent, not broker actions

Strategy code emits desired weights, quantities, structures, or adapter-local deltas. It does not
connect to the broker or decide shared-account order flow through a Conductor integration.

**Reason:** portfolio risk, ownership, crossing, and audit must see the complete desired state
before external execution.

## S2 — The core operates on absolute desired state

Position deltas are applied once to the strategy's starting virtual book at the adapter boundary.
Only the resulting absolute target enters the portfolio core.

**Reason:** replaying a delta can double economic exposure; repeating an absolute target is
idempotent.

## S3 — Strategy ownership is explicit

The physical broker position is never used to infer how much belongs to each strategy. Initial
positions, cash, and capital are seeded from reviewed records, and subsequent ownership changes are
derived from internal transfers and reconciled fills.

**Reason:** many virtual ownership decompositions can produce the same aggregate broker position.

## S4 — Submission is not a fill

Virtual positions and cash do not move merely because Conductor planned or submitted an order.
Ownership changes only from internal crosses and execution facts that reconcile to broker state.

**Reason:** orders can be rejected, partially filled, cancelled, delayed, or reported after a
timeout.

## S5 — Internal crossing precedes external execution

Opposing changes between strategy books are transferred internally before the residual broker delta
is calculated. The broker sees only the aggregate physical requirement.

**Reason:** unnecessary round trips add cost and can temporarily misstate shared-account risk.

## S6 — Route identity is part of position identity

Positions net only when both canonical instrument and execution route match. Similar symbols on
different brokers, accounts, venues, or node-local routes remain separate.

**Reason:** they may have different custody, collateral, contract, settlement, or execution risk.

## S7 — Unknown or stale state fails closed

A stale worker, missing mark, unresolved instrument, unknown physical position, malformed strategy
result, or ambiguous route blocks the affected action. Conductor does not substitute guessed state.

**Reason:** trading from incomplete state can create exposure that cannot be attributed or
reconciled.

## S8 — Important events are durable and auditable

Strategy inputs and outputs, target revisions, risk changes, crosses, requests, execution reports,
cash changes, lifecycle transitions, and reconciliation outcomes are persisted with stable identity.

**Reason:** recovery and incident review cannot depend on process memory or dashboard state.

## S9 — Lifecycle preserves economic state

Disabling a strategy blocks future runs but preserves its book. Retirement first targets its book to
zero and becomes final only after reconciliation. Historical state is never deleted as part of a
lifecycle action.

**Reason:** administrative status must not silently abandon or erase exposure.

## S10 — One execution authority per route

At cutover, only one controlled worker/path may submit orders for a Conductor route. Legacy and new
orchestrators must not both retain live authority.

**Reason:** two correct systems acting on the same desired state can still duplicate orders.

## S11 — Live authority is explicit and off by default

Examples and new configurations use `live_orders_enabled = false`. Paper and shadow evidence is
required before an operator explicitly enables submission.

**Reason:** configuration mistakes should produce proposed trades, not live trades.

## S12 — Nodes remain independently safe

Each execution node retains its local source of truth and can trade, stop, reconcile, and recover
without another node or a central dashboard. Fleet telemetry is outbound and read-only.

**Reason:** observability outages must not become execution outages or create split ownership.

## Change-review checklist

For every execution, accounting, routing, lifecycle, or persistence change, reviewers should answer:

1. Can retry or replay change the intended economic state twice?
2. Can a timeout be mistaken for a rejection or a fill?
3. Can ownership move without an internal cross or reconciled external fact?
4. Can two routes or books accidentally net?
5. Does stale, missing, or ambiguous state block safely?
6. Is the decision reconstructable after process or machine restart?
7. Does shadow mode remain incapable of submitting an order?
8. Is rollback possible without deleting evidence?

The operational failure drills in `docs/WINDOWS_ETSA_RPS_TLAQ_MIGRATION.md` are the current concrete
acceptance suite for these invariants.
