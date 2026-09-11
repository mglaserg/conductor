# Conductor

Conductor is the portfolio control plane for independent trading strategies.

> **Strategies decide what they want. Conductor decides how to express, size, constrain,
> execute, reconcile, and attribute it.**

Conductor is deliberately *not* another strategy framework. Research projects remain independent.
NautilusTrader is the intended execution/runtime kernel; Conductor owns the economic portfolio.

## V0.2 architecture

```text
Strategy projects
 ETSA / RPSchteroids / Futurescope / Crypto YOLO / CleanCarry / ...
                              |
                              v
                         StrategyIntent
                              |
                   revision + freshness gate
                              |
                              v
                    Hierarchical capital budget
                 portfolio -> sleeve -> strategy
                              |
                              v
                 Instrument / quantity translation
                              |
                              v
                    Virtual strategy targets
                              |
                     portfolio risk governor
                              |
                              v
                 aggregate + cross-strategy netting
                              |
                desired state <-> actual broker state
                              |
                         OrderPlanner
                              |
                   ExecutionAdapter boundary
                       /                \
                    Paper            Nautilus v2
                                         |
                              IBKR / Hyperliquid / ...
                              |
                   post-trade reconciliation
                              |
                   committed virtual ownership
```

The broker sees only aggregate positions. Conductor preserves the economic owner of each position
inside its own virtual ledger.

## What V0.2 adds

- hierarchical portfolio NAV -> sleeve -> strategy capital budgets;
- NAV-weight, notional and quantity intent semantics;
- instrument prices, contract multipliers and lot-size translation;
- strategy intent revisions and staleness refusal;
- portfolio-level gross and single-instrument risk caps;
- trade-buffer/order-planning layer;
- desired-vs-actual broker reconciliation;
- separate **virtual targets** and **committed virtual positions**;
- economic ownership commits only once aggregate broker state reconciles;
- run states and append-only events in SQLite;
- deterministic idempotency: once desired state is reached, the next cycle produces no orders;
- optional NautilusTrader v2 boundary kept outside the Conductor domain model.

## Portfolio semantics

For `NAV_WEIGHT` intents, strategy targets are weights inside the strategy's allocated capital.
For example:

```text
Portfolio NAV                     $250,000
Equities sleeve @ 50%             $125,000
  ETSA @ 85%                      $106,250
  RPSchteroids @ 15%               $18,750
```

An ETSA `AAPL = +0.40` target therefore requests approximately `$42,500` of AAPL before lot-size
rounding. This is very different from multiplying an already-computed share count by `0.85`.

`NOTIONAL` and `QUANTITY` intents are treated as absolute economic requests. Portfolio risk may
still scale them.

## Demo

The included demo is intentionally paper-only:

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
pytest -q
conductor-demo
```

The demo uses ETSA + RPSchteroids in one shared equity sleeve and proves:

1. separate strategy capital budgets;
2. separate virtual ownership of overlapping AAPL exposure;
3. cross-strategy aggregation/netting;
4. portfolio-level risk checks;
5. desired-vs-actual order generation;
6. broker-state reconciliation;
7. virtual ownership commit; and
8. a second cycle with zero trades.

Nothing in the demo connects to a live account.

## NautilusTrader boundary

NautilusTrader owns venue/execution plumbing: live order lifecycle, adapter routing, lower-level
risk, fills, execution algorithms, and venue reconciliation. Conductor owns strategy intent,
sleeves, capital allocation, cross-strategy portfolio risk, desired state, economic ownership,
and attribution.

Install the optional v2 dependency only when testing the bridge:

```bash
uv pip install -e '.[nautilus]'
conductor-nautilus-smoke
```

As of September 11, 2026, the public v2 docs are still on release-candidate builds. Do not route
production capital merely because the v2 package installs successfully.

## Lubuntu target

Conductor's deployment target is Linux/Lubuntu. Production services should eventually use:

- dedicated `uv`/venv environment;
- absolute paths;
- `.env` with restrictive permissions;
- persistent SQLite/Postgres state and structured logs;
- systemd service/timer units;
- startup reconciliation before execution is enabled;
- explicit paper/testnet/live modes.

## Next portfolio work

V0.3 should deepen accounting rather than rush live execution:

- fills and commission ingestion into the Conductor ledger;
- strategy-level cost basis, realized/unrealized P&L and NAV;
- explicit internal crossing when strategies trade opposite directions;
- strategy/sleeve drawdown and capital-utilization reporting;
- portfolio cash/margin reserve accounting;
- contract/instrument translation registry, including Futurescope duration/DV01 translation later;
- lifecycle objects for persistent targets, expiries and futures rolls;
- Nautilus sandbox/paper bridge, then IBKR and Hyperliquid demo accounts;
- only after those reconcile cleanly: guarded live routing.
