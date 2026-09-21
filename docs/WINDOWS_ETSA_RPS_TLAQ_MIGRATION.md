# Windows migration: ETSA + RPSchteroids + TLAQ

This is the operational runbook for moving the three existing Windows equity strategies from
Dagster to Conductor while preserving their proven strategy logic.

The goal is not to redesign ETSA/RPS/TLAQ. The goal is to replace orchestration, shared-account
bookkeeping, portfolio control and broker execution with Conductor + NautilusTrader.

## 1. Runtime topology

Run three kinds of process on the Windows trading machine:

1. **TWS or IB Gateway** — the Interactive Brokers endpoint.
2. **one persistent Nautilus worker** — owns the IBKR API connection, instrument cache, market data,
   orders/fills and reconciliation.
3. **short-lived Conductor strategy runs** — launched by Task Scheduler at each strategy's existing
   production time.

```text
Task Scheduler -> conductor run ETSA -----------+
Task Scheduler -> conductor run RPSchteroids ---+--> local Conductor SQLite
Task Scheduler -> conductor run TLAQ -----------+          |
                                                           v
                                                 local Nautilus bridge SQLite
                                                           |
                                                           v
                                                 persistent Nautilus worker
                                                           |
                                                           v
                                                      TWS / Gateway
```

The strategy processes never connect to IBKR.

## 2. Prepare the strategy adapters

Copy the relevant templates from `examples/migration/` into the existing strategy repositories.
Keep `_emit.py` beside each adapter.

### ETSA

The included ETSA adapter reuses the current RobotWealth fetch and
`latest_tri_stat_arb_weights(...)` calculation and emits target weights. Adjust imports only if the
production ETSA package path differs.

### RPSchteroids

Set:

```toml
[strategies.RPSchteroids.environment]
RPS_TARGET_CALLABLE = "your.module:target_positions"
```

The callable returns `{ticker: absolute_target_shares}`.

### TLAQ

Set:

```toml
[strategies.TLAQ.environment]
TLAQ_DELTA_CALLABLE = "your.module:trade_deltas"
```

The callable receives the Conductor strategy-account snapshot and returns
`{ticker: signed_share_delta}`. Negative virtual cash is preserved in that snapshot.

The adapter converts the deltas to a run output; Conductor applies them once to the starting TLAQ
virtual positions and stores only the resulting absolute desired state.

## 3. Install Conductor and Nautilus

From the Conductor repo:

```powershell
uv python install 3.12
uv venv --python 3.12
uv pip install -e ".[dev,nautilus]"
uv run pytest -q
```

Before relying on the Windows trading machine, verify that the installed Nautilus wheel imports:

```powershell
uv run conductor-nautilus-smoke
```

Nautilus officially tests Windows Server rather than ordinary Windows desktop editions. The
actual Windows trading machine therefore needs a paper smoke before Conductor depends on it.

## 4. Build the initial virtual subledger

This is the most important migration step.

For every overlapping or non-overlapping current position, determine economic ownership before
Conductor receives execution authority.

Example:

```text
AAPL broker actual = 150

TLAQ virtual        = 100
RPS virtual         =  50
ETSA virtual        =   0
                     ----
expected broker     = 150
```

Populate each strategy's `seed.positions`, `seed.cash`, and `seed.allocated_capital` in the TOML.
TLAQ cash may be negative.

Conductor does not guess initial ownership from the aggregate broker account.

### Current physical holdings outside those seeds

Add every other current equity in the IBKR account to:

```toml
preload_instruments = ["SYMBOL1", "SYMBOL2"]
```

These are reconciliation-only. If they are non-zero at IBKR and have no virtual owner,
`conductor doctor` must fail. Assign them to a real/synthetic book or remove them before go-live.

**Production invariant:** all positions in the controlled IBKR account are modeled by Conductor.

## 5. Paper connectivity smoke

Start TWS paper trading or IB Gateway paper and configure the appropriate API port/account in
`conductor.toml`.

Start the worker:

```powershell
uv run conductor nautilus-worker windows_ibkr_equities --config conductor.toml
```

In another terminal:

```powershell
uv run conductor worker-status windows_ibkr_equities --config conductor.toml
uv run conductor status --config conductor.toml
uv run conductor doctor --config conductor.toml
```

The smoke must prove:

- worker heartbeat stays fresh;
- IB account/NAV are visible;
- all seeded instruments resolve;
- prices are available;
- broker positions are visible;
- restart of the worker recovers bridge state;
- no unknown live position is silently mapped.

## 6. Paper order smoke

Use a paper account and set:

```toml
live_orders_enabled = true
```

Run a deliberately small test book through Conductor and verify:

- order reaches IBKR through Nautilus;
- fill quantity/price comes back;
- commission is captured when available;
- aggregate broker state reconciles;
- virtual ownership/cash commit only after reconciliation;
- second identical Conductor cycle emits no trade.

Return `live_orders_enabled` to `false` after the paper order smoke.

## 7. Live-account shadow

For the actual production account, use the live TWS/Gateway API port and account ID but keep:

```toml
live_orders_enabled = false
```

Also enable API read-only at TWS/IB Gateway while practical for this phase.

The worker publishes real account positions, NAV and market data. Conductor computes the exact
portfolio/risk/netting result but does not enqueue orders.

Run ETSA, RPS and TLAQ at their real production times while Dagster remains authoritative. Compare:

- native strategy result;
- strategy target state;
- strategy capital/cash input;
- aggregate desired broker quantity;
- proposed broker delta;
- Dagster/legacy actual trade;
- post-run broker position.

Every mismatch gets explained before cutover. Do not normalize away mismatches merely to obtain a
passing report.

## 8. Task Scheduler

Task Scheduler should call Conductor, not the strategy directly.

Example action:

```text
Program:    powershell.exe
Arguments:  -File C:\Trading\Conductor\scripts\windows\run_strategy.ps1 \
            -Strategy ETSA -Config C:\Trading\Conductor\conductor.toml
```

Create one task per strategy at the exact times currently used by the production Dagster jobs.
Use the parameterized registration helper if useful:

```powershell
.\scripts\windows\register_strategy_task.ps1 `
  -Strategy ETSA `
  -At "15:50" `
  -Config "C:\Trading\Conductor\conductor.toml"
```

Do not infer or alter the production schedule during migration. Copy the known schedule exactly.

The persistent Nautilus worker can be registered at machine startup/logon using
`register_nautilus_worker_task.ps1`.

## 9. Failure drills before live cutover

At minimum test these in paper/shadow:

1. run the same strategy twice concurrently — second run must be rejected;
2. kill and restart the Nautilus worker — stale heartbeat must block Conductor;
3. disconnect TWS/Gateway — Conductor must not use stale broker state;
4. strategy subprocess exits non-zero — portfolio must not change;
5. strategy times out — portfolio must not change;
6. strategy returns malformed output — reject and audit;
7. TLAQ delta output replay — absolute target remains idempotent;
8. overlapping TLAQ/RPS symbol — only net aggregate delta reaches broker;
9. manual broker mismatch — `doctor` fails;
10. partial/rejected order — virtual ownership must not be invented as filled.

Automated unit coverage currently exercises the concurrency lock, nonzero subprocess exit,
subprocess timeout, malformed output, terminal rejection, and partial-fill-then-cancel cases while
asserting that committed ownership and broker state remain unchanged. Terminal execution failures
propagate as a blocked run instead of a successful or still-submitted run. This evidence is
necessary but does not replace repeating the drills with the actual Windows scheduler, worker, and
IBKR paper environment.

## 10. Go-live gate

Before turning on production execution:

- all 3 strategy seeds are approved;
- every physical IBKR position is in Conductor reconciliation scope;
- `conductor doctor` is clean;
- worker heartbeat/reconciliation is clean;
- multiple real-time shadow runs match the expected strategy/portfolio behavior;
- failure drills pass;
- run stdout/stderr and audit records are being persisted;
- Windows Task Scheduler tasks are enabled at the exact production times;
- Dagster execution can be disabled without disabling its rollback/reference code.

Then:

1. stop Dagster from submitting live orders;
2. ensure only one Conductor/Nautilus worker owns execution authority;
3. remove API read-only if it was enabled at TWS/IB Gateway;
4. set `live_orders_enabled = true`;
5. restart the Nautilus worker so the mode change is explicit;
6. run `worker-status` and `doctor` again;
7. permit the next scheduled Conductor strategy run.

## 11. Rollback

If Conductor cannot safely reconcile after cutover:

1. `conductor disable <strategy>` for affected strategy jobs;
2. set `live_orders_enabled = false` and restart the worker;
3. preserve all SQLite/run artifacts — do not delete state;
4. reconcile physical IBKR state and virtual ownership explicitly;
5. only re-enable legacy execution after confirming it will not duplicate an already-applied
   Conductor target.

Rollback is an execution-authority change, not a database reset.
