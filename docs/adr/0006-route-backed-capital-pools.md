# ADR 0006: Bind independent capital pools to execution routes

- Status: Accepted
- Date: 2026-09-23

## Context

The Windows node now needs to operate more than one independently funded IBKR account from a single
`conductor.toml`. ETSA and RPSchteroids may share one account while TLAQ uses another. A single
node-wide portfolio NAV/allocator would incorrectly let one account's capital and risk capacity
influence another account, even though Conductor cannot transfer cash between them.

Execution routing was already route-aware, but capital sizing, risk limits, trade buffers, and
runtime reconciliation still assumed one node-wide portfolio.

## Decision

A named block `portfolio.<route_id>` represents one independently funded capital pool. By default its
NAV is the NetLiquidation published by the worker for that same route. Static/dynamic allocation,
risk limits, and trade-size buffers are evaluated against that route's NAV only.

A strategy's `route_id` therefore selects both:

1. the execution account/worker; and
2. the capital pool that funds and risk-governs the strategy.

All strategies sharing a route are included in that route's desired-state cycle so virtual
ownership/netting remains complete. Strategies on other routes are excluded from the cycle and
cannot be flattened, crossed, or used as capital/risk capacity.

One persistent worker and one durable bridge database remain required per live IBKR route/account.
The worker-reported broker account ID must match the account configured for that route.

Changing the `route_id` of an already-persisted strategy is treated as an account migration and is
refused until ownership/cash are explicitly migrated and bootstrap reconciliation is clean.

Legacy one-pool configuration remains supported for migration and offline paper fixtures. New
multi-account deployments use named route-backed portfolios.

## Consequences

- A single Conductor node/config can operate several independent broker accounts without pretending
  they are one fungible pool.
- ETSA/RPS can share capital/netting while TLAQ remains economically isolated in another account.
- Broker NAV changes automatically refresh target-weight strategy allocated-capital budgets.
- Risk scaling and dust thresholds are account-local.
- A run on one route does not require or mutate another route's broker/virtual state.
- Account moves require an explicit future migration workflow rather than a hot config edit.
- Per-route workers must use unique API client IDs and bridge state when connected concurrently.
