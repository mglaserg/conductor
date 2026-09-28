# ADR 0008 — Explicit strategy and portfolio policy stages

- **Status:** Accepted
- **Date:** 2026-09-28

## Context

Conductor already normalized strategy output, translated it through allocated capital, applied
route-level risk, buffered small rebalances, and netted same-account targets. Those controls were
spread across several services, however, and strategy-specific implementation rules were either
owned by the strategy repository or represented only as comments/config fields. That made it hard
to answer a basic operational question after the fact: why did a strategy's native target become
the broker target that Conductor attempted to implement?

The Windows shared-account migration also makes policy ordering safety-critical. ETSA and
RPSchteroids must remain separate virtual owners even when their physical positions net. A run of
one strategy must never rescale a synthetic hold-current intent for the companion strategy.

## Decision

Add two explicit policy layers without changing the frozen V0.3 target protocol or execution
boundary.

1. `StrategyPolicy` is versioned configuration attached to a strategy. It may define a risk budget,
   target-volatility scaling, strategy gross-leverage and single-position limits, a strategy-level
   rebalance band, target freshness, and explicit missing-volatility behavior.
2. `PortfolioPolicyEngine` composes the strategy-policy engine, route-local portfolio risk engine,
   and pre-netting rebalance buffer. Capital allocation remains route-backed and is resolved before
   tradable target construction; strategy risk budgets are inputs to dynamic allocators when clean
   return history is available.

Native strategy results may optionally return scalar `metadata`. Conductor persists that metadata
on the normalized `StrategyIntent`; target-volatility policy reads only its explicitly configured
metadata key. Missing required volatility fails closed by default.

Synthetic `conductor-hold-current` companion intents are policy-invariant. They are preserved
exactly and cannot be target-vol scaled, leverage scaled, or concentration scaled merely because a
different strategy on the shared route ran.

Strategy and sleeve rebalance bands are measured as notional change divided by the relevant
allocated-capital base and are applied before broker aggregation/netting. Every cycle persists a
`portfolio.decision_lineage` event spanning raw translated strategy targets, policy-adjusted
targets, route-risk-adjusted targets, implementation targets after deadbands, aggregate broker
targets, and planned deltas.

ERC covariance estimation supports Ledoit-Wolf shrinkage or sample covariance. Ledoit-Wolf remains
the default. Dynamic allocators still require clean attributable strategy return history; absent
that history, the existing explicit fallback chain remains authoritative.

## Consequences

- Policy changes are visible, versioned, auditable, and independent from strategy signal code.
- Existing strategy-level `max_gross_leverage` configuration is now enforced instead of ignored.
- ETSA/RPS shared-route ownership remains stable when only one strategy runs.
- `status` exposes policy and allocator diagnostics; `doctor` reports policy blockers and allocator
  fallbacks in addition to authority-aware reconciliation.
- Target-volatility scaling is opt-in. The default maximum scale is `1.0`, so enabling a target
  volatility cannot lever a strategy above its native target unless the operator explicitly raises
  `max_vol_scale`.
- HRP/HERC, RIE covariance, transaction-cost-aware allocation transitions, and automatic allocator
  histories remain later M5 work rather than being hidden inside this policy layer.
