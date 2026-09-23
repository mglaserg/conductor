# Windows migration: ETSA + RPSchteroids + TLAQ

This is the operator runbook for moving the three existing Windows equity strategies from Dagster
to Conductor without rewriting their strategy logic.

The canonical topology is **one Conductor node, one `conductor.toml`, multiple independently funded
IBKR routes**:

```text
ETSA ---------+                         +--> IBKR account: ibkr_main
              +--> capital pool -------+       (ETSA + RPS share capital/risk/netting)
RPS ----------+    portfolio.ibkr_main |
                                        |
TLAQ ------------> capital pool ------------> IBKR account: ibkr_tlaq
                   portfolio.ibkr_tlaq        (independent capital/risk/netting)
```

A route is both an execution-account identity and a capital-pool boundary. Capital, risk capacity,
trade buffers, reconciliation and internal netting do not move across routes.

## 1. Runtime topology

Run these processes on the Windows trading machine:

1. **TWS or IB Gateway** — the Interactive Brokers endpoint.
2. **one persistent Nautilus worker per configured IBKR route/account**.
3. **short-lived Conductor strategy runs** — launched at each strategy's existing production time.

For the current two-account layout:

```text
Task Scheduler -> conductor run ETSA -----------+--> route ibkr_main worker --> IBKR account A
Task Scheduler -> conductor run RPSchteroids ---+

Task Scheduler -> conductor run TLAQ --------------> route ibkr_tlaq worker --> IBKR account B

                         all durable virtual ownership/audit state
                                      |
                                      v
                              data/conductor.sqlite
```

The strategy subprocesses never connect to IBKR directly.

`conductor run <strategy>` starts only the route-backed runtime needed by that strategy. ETSA and
RPS therefore depend on `ibkr_main`; TLAQ depends on `ibkr_tlaq`. An unrelated account worker being
down must not block a strategy on another route. By contrast, `conductor status` and
`conductor doctor` intentionally inspect the complete configured node and therefore require all
relevant routes to be healthy.

## 2. One-file capital-pool configuration

Each independently funded account gets one named portfolio whose ID matches its route ID:

```toml
[portfolio.ibkr_main]
allocator = "static"

[portfolio.ibkr_main.static.weights]
ETSA = 0.85
RPSchteroids = 0.15

[portfolio.ibkr_tlaq]
allocator = "static"

[portfolio.ibkr_tlaq.static.weights]
TLAQ = 1.0
```

In live/shadow mode, the default NAV source is the route's broker `NetLiquidation`. There is no
global Windows NAV split across independent accounts. If `ibkr_main` reports $200,000 and
`ibkr_tlaq` reports $75,000, the static budgets are $170,000 ETSA, $30,000 RPS and $75,000 TLAQ.

Each pool can select its own allocator and risk overrides. Global `[risk]` values are defaults that
are applied independently to each capital pool.

For fully offline `--paper` with more than one pool, provide synthetic NAVs without changing the
live broker-NAV behavior:

```toml
[paper.portfolio_navs]
ibkr_main = 100000
ibkr_tlaq = 100000
```

## 3. Execution routes and workers

Use one route per physical IBKR account. Routes may share the same TWS/Gateway host and port, but
they must have distinct IB API client IDs and distinct bridge databases.

Example shape:

```toml
[routes.ibkr_main]
adapter = "nautilus_ibkr"
account = "ACCOUNT_A"
host = "127.0.0.1"
port = 7496
data_client_id = 1301
exec_client_id = 1302
account_summary_client_id = 11302
bridge_db = "data/nautilus_ibkr_main_bridge.sqlite"
live_orders_enabled = false

[routes.ibkr_tlaq]
adapter = "nautilus_ibkr"
account = "ACCOUNT_B"
host = "127.0.0.1"
port = 7496
data_client_id = 1311
exec_client_id = 1312
account_summary_client_id = 11312
bridge_db = "data/nautilus_ibkr_tlaq_bridge.sqlite"
live_orders_enabled = false
```

Conductor fails closed when the worker-reported account ID does not match the account configured
for that route. If Nautilus has registered the correct account but its typed account state has no
usable NAV, the worker opens a separate read-only TWS API connection and requests
`reqAccountSummary("All", "NetLiquidation")`. Only the exact configured native account callback is
accepted; linked-account values are ignored. This fallback never handles orders, fills or positions.

Start both workers with:

```powershell
.\start_nautilus_workers.bat
```

or individually:

```powershell
uv run conductor nautilus-worker ibkr_main --config conductor.toml
uv run conductor nautilus-worker ibkr_tlaq --config conductor.toml
```

Check them independently:

```powershell
uv run conductor worker-status ibkr_main --config conductor.toml
uv run conductor worker-status ibkr_tlaq --config conductor.toml
```

Do not continue until both workers report `ready: true`, the configured account ID matches, and
`net_liquidation` is non-null. The worker deliberately remains not-ready while IBKR account state
is still loading.

## 4. Prepare the strategy adapters

Copy the relevant templates from `examples/migration/` into the existing strategy repositories.
Keep `_emit.py` beside each adapter.

### ETSA

The ETSA adapter reuses the existing RobotWealth fetch and `latest_tri_stat_arb_weights(...)`
calculation and emits complete target weights.

### RPSchteroids

Set the configured callable so the adapter returns the complete desired target representation used
by the current production strategy.

### TLAQ

TLAQ remains a position-delta producer. Its adapter receives TLAQ's own virtual account snapshot and
returns signed share deltas. Conductor converts those deltas once into absolute desired ownership
before portfolio aggregation, preserving idempotency.

## 5. Build and approve the initial virtual subledger

This is the most important migration step.

Within a physical broker account, every controlled broker position must have attributable virtual
ownership. ETSA and RPS can overlap because they share `ibkr_main`; TLAQ is reconciled separately on
`ibkr_tlaq`.

Example for the shared account:

```text
ibkr_main AAPL broker actual = 150

ETSA virtual                 = 100
RPS virtual                  =  50
                               ---
expected broker              = 150
```

TLAQ ownership is **not** part of that sum when TLAQ is on `ibkr_tlaq`.

For named portfolios, `allocated_capital` is derived from the route's NAV and allocator on each
runtime startup. Do not manually seed capital just to mirror the weights. `seed.positions` and
`seed.cash` remain bootstrap assertions and must be approved against reality. TLAQ cash may be
negative.

After the initial bootstrap, SQLite owns the virtual state. Changing a strategy's `route_id` is not
a hot account transfer: Conductor refuses a persisted strategy account whose stored route differs
from configuration. Moving a strategy to another account requires an explicit broker transfer or
re-establishment plus a reviewed virtual-book migration/bootstrap.

## 6. Install and verify Conductor

From the Conductor repo:

```powershell
uv python install 3.12
uv venv --python 3.12
uv pip install -e ".[dev,nautilus]"
uv run pytest -q
uv run conductor-nautilus-smoke
```

The actual Windows trading machine still requires its own Nautilus/IBKR paper smoke.

## 7. Paper connectivity smoke

Start TWS paper trading or IB Gateway paper and configure each route with the correct paper account
and API port. Keep live submission disabled.

Then:

```powershell
uv run conductor worker-status ibkr_main --config conductor.toml
uv run conductor worker-status ibkr_tlaq --config conductor.toml
uv run conductor status --config conductor.toml
uv run conductor doctor --config conductor.toml
```

The smoke must prove for **each** route:

- fresh worker heartbeat;
- configured account ID equals worker-reported account ID;
- broker NAV is published;
- seeded/current instruments resolve and have marks;
- broker positions are visible;
- worker restart recovers bridge state;
- no unknown position is silently assigned to a strategy.

`doctor` is read-only and should be clean across all controlled accounts before execution authority
is enabled.

## 8. Strategy-run behavior

Normal commands are:

```powershell
uv run conductor run ETSA --config conductor.toml
uv run conductor run RPSchteroids --config conductor.toml
uv run conductor run TLAQ --config conductor.toml
```

A run is route-scoped but portfolio-complete **within that route**. For example, running ETSA keeps
RPS's current desired ownership in the `ibkr_main` aggregate while replacing only ETSA's new intent.
It does not load, reconcile, flatten or borrow risk capacity from `ibkr_tlaq`.

Internal crossing/netting is therefore allowed between ETSA and RPS when they want opposite changes
in the same instrument. It never crosses ETSA/RPS against TLAQ because those trades belong to
different physical accounts.

## 9. Paper order smoke

Use paper accounts and enable live submission only on the route being tested:

```toml
live_orders_enabled = true
```

Prove a deliberately small order on each route independently and verify:

- order reaches the intended IBKR account through its Nautilus worker;
- fill quantity/price returns;
- commission is captured when available;
- broker state reconciles on that route;
- virtual ownership/cash commit only after reconciliation;
- a repeated unchanged cycle emits no trade;
- the other account receives no order.

Return `live_orders_enabled` to `false` after the smoke.

## 10. Live-account shadow

For the actual live accounts, use the live TWS/Gateway API port and real account IDs while keeping:

```toml
live_orders_enabled = false
```

Run ETSA, RPS and TLAQ at their normal production times while Dagster remains authoritative. Compare
for every strategy:

- native strategy result;
- assigned route/account and broker NAV;
- resolved strategy capital budget;
- strategy target state;
- aggregate desired quantity **within its route**;
- proposed broker delta;
- Dagster/legacy actual trade;
- post-run broker position.

Explain every mismatch before cutover.

## 11. Task Scheduler

Task Scheduler calls Conductor, not the underlying strategy directly:

```text
Program:    powershell.exe
Arguments:  -File C:\Trading\Conductor\scripts\windows\run_strategy.ps1 \
            -Strategy ETSA -Config C:\Trading\Conductor\conductor.toml
```

Create one task per strategy at the exact existing production schedule. Register/start one Nautilus
worker per route/account. Do not collapse the workers into one route simply because they share a TWS
process.

## 12. Required failure drills

At minimum test these in paper/shadow:

1. run the same strategy twice concurrently — second run must be rejected;
2. kill its route worker — that route's strategy run must fail closed;
3. kill the *other* route worker — unrelated strategy runs must remain operable;
4. connect a worker to the wrong IBKR account — account check must fail closed;
5. disconnect TWS/Gateway — stale broker state must not be used;
6. strategy subprocess exits non-zero — portfolio ownership must not change;
7. strategy times out or returns malformed output — portfolio ownership must not change;
8. TLAQ delta output replay — absolute target remains idempotent;
9. overlapping ETSA/RPS symbol — only their net `ibkr_main` delta reaches that broker account;
10. same symbol held on TLAQ — it remains independent and is not cross-routed;
11. manual broker mismatch — `doctor` fails;
12. partial/rejected order — virtual ownership must not be invented as filled.

Unit coverage is necessary but does not replace repeating these drills with the actual Windows
scheduler, Nautilus workers and IBKR paper environment.

## 13. Go-live gate

Before enabling production execution:

- both route/account mappings are approved;
- all strategy bootstrap ownership/cash is approved;
- every controlled physical position is represented on the correct route;
- both workers pass `worker-status` including account-ID match;
- `conductor doctor` is clean across all routes;
- paper order and restart drills pass independently for each route;
- repeated live shadow runs have no unexplained mismatch;
- Task Scheduler uses the unchanged production schedules;
- Dagster can be disabled without destroying rollback/reference evidence.

Then transfer execution authority deliberately, route by route. Do not enable both accounts merely
because one account has passed its gate.

## 14. Rollback

If Conductor cannot safely reconcile after cutover:

1. disable the affected strategy/route from scheduled execution;
2. set that route's `live_orders_enabled = false` and restart its worker;
3. preserve all Conductor and bridge SQLite/run artifacts;
4. reconcile that physical account against virtual ownership explicitly;
5. only re-enable legacy execution after confirming it will not duplicate an already-applied target.

Rollback is an execution-authority change, not a database reset.

## 15. Current limitation

`max_margin_utilization` is represented in configuration/status but the current worker does not yet
publish enough margin-utilization state for Conductor to enforce that limit. Gross, net and
single-instrument risk are enforced per route; margin-utilization enforcement remains a promotion
gap and must not be described as active protection yet.
