# ADR 0004: Use SQLite/WAL for local Conductor-to-worker IPC

- Status: Accepted
- Date: 2026-09-21

## Context

Scheduled Conductor strategy runs are short-lived, while the Nautilus broker connection must remain
persistent. Both processes run on the same Windows machine, request volume is low, and request,
response, and health history must survive process restarts.

## Decision

Use a dedicated SQLite database in WAL mode as the durable local bridge. It stores worker
heartbeat/readiness, broker positions, instrument metadata, and execution requests/responses. A
single persistent worker claims route-scoped requests; abandoned claimed requests are recoverable
after restart.

Do not introduce Redis, RabbitMQ, or a network service until measured workload or deployment
requirements exceed this design.

## Consequences

- Deployment has no additional service dependency.
- Operators retain durable evidence after either process exits.
- Database backup, schema migration, lock contention, request idempotency, and corruption recovery
  must be handled explicitly.
- This bridge is local execution IPC, not the source of strategy ownership or a cross-node bus.
