# ADR 0003: Use NautilusTrader as the execution kernel

- Status: Accepted
- Date: 2026-09-21

## Context

Reliable broker execution requires instrument loading, market data, order lifecycle, fills, account
state, and reconciliation. Reimplementing those concerns directly against TWS would create a second
execution engine and blur responsibility between portfolio control and broker plumbing.

## Decision

NautilusTrader owns broker/exchange connectivity and execution mechanics. Conductor owns strategy
accounts, capital, portfolio policy, virtual ownership, aggregate desired state, and audit.

For IBKR, one persistent Nautilus worker owns the connection and one physical execution identity for
the shared account. Strategy identity remains exclusively in Conductor's virtual ledger.

## Consequences

- Conductor avoids maintaining its own TWS order/fill state machine.
- Nautilus version compatibility and platform behavior become explicit operational dependencies.
- Broker-specific translation stays in the worker/adapter layer.
- Paper connectivity, fills, restart behavior, and reconciliation must be proven on the actual
  Windows deployment before cutover.
