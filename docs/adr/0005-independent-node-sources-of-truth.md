# ADR 0005: Keep execution nodes independently authoritative

- Status: Accepted
- Date: 2026-09-21

## Context

The Windows IBKR runtime and planned Lubuntu crypto runtime operate on different networks and
venues. A central database or dashboard in the critical path would couple their availability,
complicate recovery, and risk ambiguous execution ownership.

## Decision

Each node owns its local Conductor ledger, bridge, worker, configuration, and venue connection. It
must be able to start, stop, trade, reconcile, and recover without another node.

Fleet-level observability may receive buffered outbound telemetry. It is read-only and never grants
execution authority or becomes a shared source of trading truth.

## Consequences

- A global dashboard outage cannot halt or alter local execution.
- Cross-node views are eventually consistent and must display freshness explicitly.
- Strategy and broker ownership cannot span nodes without a future superseding design.
- Operations, backups, reconciliation, and disaster recovery remain node-local responsibilities.
