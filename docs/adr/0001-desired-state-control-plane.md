# ADR 0001: Use a desired-state portfolio control plane

- Status: Accepted
- Date: 2026-09-21

## Context

Independent strategies use different native outputs: portfolio weights, absolute quantities,
position deltas, and eventually structured exposures. Allowing each strategy to produce broker
orders would prevent consistent portfolio risk, shared-account netting, replay safety, and ownership
attribution.

## Decision

Strategies express desired economic state. Conductor normalizes native outputs to absolute strategy
targets, combines them into a route-aware desired broker state, applies portfolio policy, and
reconciles desired quantity against actual broker quantity.

Delta semantics terminate at the strategy-adapter boundary. Repeated identical desired state must
produce no additional broker trade after reconciliation.

## Consequences

- Strategy logic remains broker-agnostic and independently testable.
- Conductor can apply one consistent allocation, risk, crossing, and audit path.
- Adapters must correctly translate native output and preserve revision identity.
- Imperative execution algorithms remain behind the execution boundary rather than becoming the
  strategy contract.
