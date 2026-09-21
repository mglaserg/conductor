# ADR 0002: Preserve virtual strategy ownership in shared accounts

- Status: Accepted
- Date: 2026-09-21

## Context

ETSA, RPSchteroids, and TLAQ share one physical IBKR account, but require independent positions,
cash, capital, P&L, lifecycle, and risk attribution. The broker exposes only aggregate physical
positions and cannot reconstruct the economic ownership split.

## Decision

Conductor maintains a durable virtual account and position book for each strategy/book. Initial
ownership is explicitly seeded and reviewed. Opposing strategy changes cross internally before
external execution. External fills and commissions are allocated back to owners, and committed
ownership moves only when execution facts reconcile.

Negative virtual cash is permitted and represents strategy financing rather than a data error.

## Consequences

- Shared-account netting reduces unnecessary broker turnover without losing attribution.
- Bootstrap requires a human-approved decomposition of every controlled physical position.
- Manual trading creates an explicit reconciliation exception rather than silently changing books.
- Accounting and recovery logic must preserve both aggregate broker truth and virtual ownership
  truth.
