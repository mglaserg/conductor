# Conductor

Conductor is the portfolio control plane for independent trading strategies.

**Strategies decide what they want. Conductor decides how to express, size, constrain,
execute, reconcile, and attribute it.**

## V0.1 architecture

```text
Strategy projects
    ETSA / RPSchteroids / Futurescope / Crypto YOLO / ...
                         |
                         v
                  StrategyIntent
                         |
                         v
                 PortfolioBuilder
                         |
              virtual ownership ledger
                         |
                         v
                 AggregateTarget
                         |
                         v
             DesiredStateReconciler
                 /               \
        desired state          actual state
                 \               /
                         v
                     TradeDelta
                         |
                         v
                 ExecutionAdapter
                 /             \
             Paper          Nautilus v2
                                |
                         IBKR / Hyperliquid
```

The Conductor ledger remains authoritative for **economic strategy ownership** even when
multiple strategies net to one broker position.

## Deliberate boundary with NautilusTrader

NautilusTrader owns venue/execution plumbing: order lifecycle, adapter routing, lower-level
risk, fills, and venue reconciliation. Conductor owns strategy intent, sleeves, capital
allocation, cross-strategy risk, desired state, virtual ownership, and attribution.

NautilusTrader v2 is currently pre-release. Conductor therefore keeps it behind an optional
adapter boundary rather than importing Nautilus types into the domain model.

## Lubuntu setup

Use Python 3.12+ and `uv`.

```bash
sudo apt update
sudo apt install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh

cd conductor
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
pytest -q
conductor-demo
```

To experiment with the NautilusTrader v2 bridge separately:

```bash
uv pip install -e '.[nautilus]'
python -c "from conductor.adapters.nautilus import check_nautilus_v2; print(check_nautilus_v2())"
```

Do **not** route production capital through the v2 release candidate merely because the
optional dependency installs successfully.

## V0.1 rules

1. Strategy projects never place broker orders through Conductor internals.
2. Strategy outputs enter through `StrategyIntent`.
3. Conductor preserves per-strategy virtual ownership before cross-strategy netting.
4. Execution compares desired state with actual venue state; it does not assume prior orders succeeded.
5. A restart should converge to the same desired state.
6. Research logic stays in the source strategy project.
7. Broker/exchange specifics stay behind execution adapters.

## Next milestone

V0.2 should add:

- intent ingestion schema/versioning and staleness handling;
- instrument registry and translation (`VTI -> shares`, `TLT duration -> ZB DV01` later);
- strategy/sleeve NAV and capital budgets;
- order-plan objects with buffering and netting;
- fill allocation back to virtual strategy ownership;
- explicit run/reconciliation states and kill conditions;
- Nautilus v2 paper/testnet bridge first, before live IBKR or Hyperliquid.
