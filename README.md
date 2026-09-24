# Conductor

Conductor is the portfolio control plane and strategy runtime for independent trading strategies.

> **Strategies decide desired economic state. Conductor owns strategy accounts, capital, portfolio
> risk, economic ownership, netting, audit, and desired broker state. NautilusTrader owns the
> broker/exchange execution plumbing.**

The first production migration is the Windows equities runtime: **ETSA + RPSchteroids + TLAQ**.
ETSA and RPSchteroids share one Interactive Brokers capital pool/account; TLAQ can run in its own
independently funded IBKR account from the same `conductor.toml`.

## V0.4 alpha — Dagster migration runtime

V0.4 builds on the frozen V0.3 Target Snapshot Protocol and adds the operational path needed to move
these three demonstrated strategies from Dagster to Conductor.

```text
Windows Task Scheduler
        |
        v
conductor run <strategy>
        |
        +--> strategy-specific account snapshot
        |       positions + virtual cash + allocated capital
        |
        +--> existing strategy subprocess
        |       ETSA          -> target weights
        |       RPSchteroids  -> absolute target shares
        |       TLAQ          -> share deltas
        |
        +--> normalize to absolute desired strategy state
        +--> route-backed capital pool allocation / risk
        +--> internal crossing across books sharing that route
        +--> aggregate desired position for that IBKR account
        |
        v
route-specific SQLite/WAL bridge
        |
        v
persistent NautilusTrader worker for that account
        |
        v
official Nautilus Interactive Brokers adapter
        |
        v
TWS / IB Gateway
```

### Why the Nautilus worker is persistent

`conductor run ETSA` is intentionally a short-lived Task Scheduler process. It should not open a new
TWS connection, reconstruct order state, and reconnect to market data every time a strategy runs.
One long-lived Nautilus worker owns each configured IBKR route/account and continuously publishes
that account's positions, NetLiquidation, instruments, and execution state into its own durable
bridge. The one-shot Conductor process reads only the route needed by the strategy run and, in live
mode, submits that account's aggregate execution request through the matching bridge.

There is no Redis, RabbitMQ, or network service between them. Both processes run on the same Windows
machine and use SQLite in WAL mode.

## Account-backed capital pools and virtual ownership

A named portfolio is one independently funded capital pool, keyed by the execution route/account:

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

Unless `nav = <number>` is supplied, each pool uses **that route's broker NetLiquidation**. So if
`ibkr_main` has $200k, ETSA is budgeted $170k and RPS $30k. If `ibkr_tlaq` has $75k, TLAQ is
budgeted $75k. Separate accounts never donate NAV/risk capacity to one another.

Within a shared account, Conductor still preserves virtual ownership and internal netting. For
example, if ETSA wants +20 AAPL while RPS reduces AAPL by 15 shares on `ibkr_main`, Conductor can
internally transfer 15 shares between their virtual books and send only **BUY 5 AAPL** to that
account. A strategy on `ibkr_tlaq` is not part of that cross and is not touched by the run.

Negative virtual cash remains permitted and represents strategy financing.

## Native strategy contracts

The migration does **not** rewrite the strategies just to satisfy Conductor.

- ETSA: `target_weights` — target weights are sized against ETSA's persisted allocated capital.
- RPSchteroids: `target_quantities` — absolute desired share positions.
- TLAQ: `position_deltas` — deltas are applied exactly once to TLAQ's starting virtual position and
  immediately converted to absolute Conductor targets.

Delta semantics never escape the TLAQ adapter, so retries cannot double-buy.

Example adapters live in [`examples/migration`](examples/migration).

## Execution boundary

NautilusTrader is the primary IBKR backend in V0.4. Conductor does **not** implement its own TWS
order/fill state machine.

The persistent worker uses the official Nautilus Interactive Brokers data/execution clients and
instrument provider. For the September migration, automatic canonical mapping is intentionally
limited to US equities:

```text
AAPL             -> AAPL=STK.SMART
EQ.US.AAPL       -> AAPL=STK.SMART
```

Futures and options will get explicit contract parsers rather than ambiguous string guessing.

Each IBKR route has its own worker/API client IDs and bridge database. A worker must publish the
configured IBKR account ID; Conductor fails closed if the worker is connected to a different
account. Strategy ownership remains exclusively in Conductor's virtual ledger.

### Account NAV fallback

Nautilus remains authoritative for execution, positions, fills, instruments and reconciliation.
For broker NAV only, the worker has one narrow fallback for linked/multi-account TWS sessions where
Nautilus has registered the correct account but its typed account state contains no usable
`NetLiquidation`. A separate read-only TWS API client requests:

```text
reqAccountSummary(group="All", tags="NetLiquidation")
```

Conductor accepts only the callback whose native account code exactly matches the route's configured
`account`. Values for any other linked account are ignored. The direct value is refreshed off the
Nautilus event loop, expires when stale, and never enables cross-account NAV fallback.

The fallback connection uses `account_summary_client_id` (default: `exec_client_id + 10000`).
`account_summary_refresh_seconds`, `account_summary_stale_after_seconds`,
`account_summary_timeout_seconds`, and `account_summary_fallback_enabled` are configurable per
route.

## Shadow mode is the default

In the Windows template:

```toml
live_orders_enabled = false
```

In this mode Conductor can use current positions/NAV/prices published by the Nautilus worker and
calculate the exact proposed aggregate broker trades, but it **does not enqueue execution requests**.
This is the mode for Dagster-vs-Conductor comparison before cutover.

For an additional safety layer during live-account shadowing, configure TWS/IB Gateway API access as
read-only at the IB application itself.

## One-file multi-account configuration

The canonical hierarchy is **route/account -> capital pool -> strategies**. The portfolio block and
route ID deliberately share the same name:

```toml
[portfolio.ibkr_main]
allocator = "static"
[portfolio.ibkr_main.static.weights]
ETSA = 0.85
RPSchteroids = 0.15

[routes.ibkr_main]
adapter = "nautilus_ibkr"
account = "REPLACE_WITH_MAIN_ACCOUNT"
bridge_db = "data/ibkr_main_bridge.sqlite"
data_client_id = 1301
exec_client_id = 1302
account_summary_client_id = 11302
live_orders_enabled = false

[portfolio.ibkr_tlaq]
allocator = "static"
[portfolio.ibkr_tlaq.static.weights]
TLAQ = 1.0

[routes.ibkr_tlaq]
adapter = "nautilus_ibkr"
account = "REPLACE_WITH_TLAQ_ACCOUNT"
bridge_db = "data/ibkr_tlaq_bridge.sqlite"
data_client_id = 1311
exec_client_id = 1312
account_summary_client_id = 11312
live_orders_enabled = false
```

A strategy's `route_id` selects both the execution account and the capital pool. All static weights
for a pool must cover exactly the strategies assigned to that route and sum to 1.0. Per-pool risk
overrides may be added under `[portfolio.<route_id>.risk]`; otherwise `[risk]` supplies defaults.
Gross, net and single-instrument limits are enforced per route. `max_margin_utilization` is currently
configuration/status only until the worker publishes the required account margin measurement.

Changing `route_id` for an already-persisted strategy is intentionally **not** a hot config edit:
Conductor refuses the mismatch until the book is explicitly migrated/bootstrap-reconciled on the
destination account.

## Offline paper mode (Windows or Lubuntu)

Offline paper mode exercises the configured strategy subprocess, `CONDUCTOR_OUTPUT` native-result
adapter, target normalization, portfolio/risk path, accounting, fills, reconciliation, and audit
without starting Nautilus, TWS, IB Gateway, or any persistent worker:

```powershell
uv run conductor run etsa --paper --config conductor.toml
uv run conductor run RPSchteroids --paper --config conductor.toml
uv run conductor status --paper --config conductor.toml
```

This is a separate runtime, not shadow mode with submission suppressed. By default it uses
`data/conductor.paper.sqlite` when normal state is `data/conductor.sqlite`, and paper run artifacts
go under `data/runs/paper`. Synthetic broker positions and fills are durable in the paper database.
The configured live routes are never constructed or contacted.

For the canonical multi-account file, give each synthetic capital pool a paper-only NAV:

```toml
[paper]
default_price = 100

[paper.portfolio_navs]
ibkr_main = 100000
ibkr_tlaq = 100000

[paper_prices]
"EQ.US.AAPL" = 250
```

Those values are used only by `--paper`; live/shadow mode still reads each route's broker
NetLiquidation. Legacy one-pool configs may continue to use numeric `node.portfolio_nav`. A new
paper book receives its allocator-derived capital as both allocated capital and initial paper cash.
`strategies.<id>.paper_seed.cash` and `.positions` can explicitly override bootstrap cash/ownership;
normal `seed` values remain isolated from paper state.
The same configuration and commands are portable to Lubuntu; only each strategy's configured
`cwd` and `command` need to be valid on that node.

For a harmless installation smoke before wiring real strategy repositories, use
[`examples/offline_paper_smoke.toml`](examples/offline_paper_smoke.toml):

```powershell
uv run conductor run etsa --paper --config examples/offline_paper_smoke.toml
uv run conductor status --paper --config examples/offline_paper_smoke.toml
```

## Bootstrap scope is explicit

Nautilus reconciliation requires the instruments referenced by broker reports to be loaded *before*
startup reconciliation runs. The IBKR worker now performs an exact-account read-only portfolio
preflight through TWS, captures every non-zero stock holding plus its broker-reported mark, converts
the holding into a RAW Nautilus instrument ID, and adds those IDs to the provider's startup
`load_ids`. The worker stays `ready=false` until Nautilus reconstructs the same broker position set.
The broker marks are persisted into the bridge so ownership bootstrap can value already-held
positions without opening a second quote warm-up cycle.

`preload_instruments` remains available for reconciliation-only instruments that should be loaded
even when they are not currently held:

```toml
preload_instruments = ["AAPL", "MSFT"]
```

A non-zero broker/preloaded position with no virtual owner makes `conductor doctor` fail. **No
unmodeled account position is allowed at go-live.** If the equities-only worker discovers a held
non-stock contract during its preflight, it fails closed rather than silently skipping that position.
Once Conductor has execution authority, manual trading in that account should be treated as an
operational exception requiring explicit reconciliation.

Initial ownership is established with a dry-run-first bootstrap command. A route with one configured
strategy is mechanically assignable; a shared route uses the latest persisted strategy intents only
to identify unique owners and refuses any unclaimed or overlapping instrument:

```powershell
uv run conductor bootstrap ibkr_tlaq --config conductor.toml
uv run conductor bootstrap ibkr_tlaq --config conductor.toml --commit --confirm ibkr_tlaq

uv run conductor bootstrap ibkr_main --config conductor.toml `
  --write-template data/bootstrap/ibkr_main.json
```

If `ibkr_main` is fully unambiguous, commit it directly:

```powershell
uv run conductor bootstrap ibkr_main --config conductor.toml --commit --confirm ibkr_main
```

If the dry run reports an overlap or unclaimed holding, edit the generated JSON so the `positions`
object contains the exact approved per-strategy quantities. The manifest format is intentionally
small and contains only one-time starting ownership:

```json
{
  "route_id": "ibkr_main",
  "positions": {
    "ETSA": {
      "EQ.US.AEP": "-59"
    },
    "RPSchteroids": {
      "EQ.US.EMB": "86",
      "EQ.US.IEF": "109"
    }
  }
}
```

Every non-zero broker position on the route must be assigned, and the quantities across strategies
must sum exactly to the broker quantity for each instrument. Extra fields such as `unresolved` from a
generated template may remain in the file; the command reads the `positions` object. Then commit it:

```powershell
uv run conductor bootstrap ibkr_main --config conductor.toml `
  --ownership data/bootstrap/ibkr_main.json `
  --commit --confirm ibkr_main
```

Bootstrap is create-only, writes positions and cash atomically, and immediately verifies that the
virtual route sums back to the broker. It never submits orders.

## Install

Python 3.12+ and `uv` are recommended.

Conductor includes NautilusTrader as a required execution dependency. A normal sync installs the
complete runtime; there is no separate Nautilus extra.

```powershell
uv venv --python 3.12
uv sync
```

For development and tests:

```powershell
uv sync --extra dev
uv run pytest -q
```

Conductor pins the NautilusTrader version it is verified against so `uv sync` produces a runnable
execution environment instead of a core-only installation.

## Windows runtime commands

Copy [`examples/windows_etsa_rps_tlaq.toml`](examples/windows_etsa_rps_tlaq.toml) to
`conductor.toml` and replace every placeholder before connecting to the production account.

Start one persistent worker per IBKR account route:

```powershell
uv run conductor nautilus-worker ibkr_main --config conductor.toml
uv run conductor nautilus-worker ibkr_tlaq --config conductor.toml
```

Check each independently:

```powershell
uv run conductor worker-status ibkr_main --config conductor.toml
uv run conductor worker-status ibkr_tlaq --config conductor.toml
```

For the normal Windows operator loop, the root convenience wrappers avoid retyping the CLI:

```powershell
.\nautilus_start.bat
.\nautilus_status.bat
.\nautilus_doctor.bat
```

Nautilus namespaces IB account IDs internally (for example `IB-U123...`) even though the IB adapter
configuration uses the native account number (`U123...`). The worker resolves the authoritative
namespaced ID from Nautilus's live cache and verifies its native portion before publishing NAV.
It never uses venue-wide equity as a live fallback across accounts.

`worker-status` also verifies that the worker-reported IBKR account matches the account configured
for that route. It must show `ready: true` **and** a non-null `net_liquidation` before `doctor` or a
live/shadow strategy run. During IBKR startup the worker stays not-ready until account state, NAV, and the broker-position
preflight/reconciliation gate have completed. Before each portfolio cycle Conductor also warms the
route's complete target universe in one worker request; the worker deduplicates instrument requests
and quote subscriptions while waiting for usable marks.

> **Current Nautilus rc4/rc5 IB limitation:** startup historical-fill reconciliation sends the
> namespaced Nautilus account ID back to IB, which IB rejects with error 321. This is an upstream
> adapter bug, not a Conductor route/account mismatch. Keep `live_orders_enabled = false` until a
> verified Nautilus build fixes that request path. The Conductor worker itself avoids the separate
> empty-account currency panic by reading venue-reported `NetLiquidation` before portfolio equity.


Run a strategy manually:

```powershell
uv run conductor run ETSA --config conductor.toml --trigger manual
uv run conductor run RPSchteroids --config conductor.toml --trigger manual
uv run conductor run TLAQ --config conductor.toml --trigger manual
```

Check virtual vs physical ownership:

```powershell
uv run conductor doctor --config conductor.toml
```

Show runtime/strategy state:

```powershell
uv run conductor status --config conductor.toml
```

Read-only local board (optional):

```powershell
uv pip install -e ".[dashboard]"
uv run conductor dashboard --config conductor.toml
```

## Strategy lifecycle

```powershell
uv run conductor disable ETSA --config conductor.toml
uv run conductor activate ETSA --config conductor.toml
uv run conductor retire ETSA --config conductor.toml --confirm ETSA
```

`disable` preserves current ownership and blocks future runs. `retire` targets the strategy book to
zero and only becomes retired after reconciliation. History is never deleted.

## Capital allocation and risk

The V0.4 alpha contains a common allocator interface for:

- static allocation;
- inverse-vol allocation;
- ERC/risk-budget allocation with Ledoit-Wolf covariance;
- deterministic fallbacks.

For the September migration, each account's broker NetLiquidation is the capital base and the
named portfolio's resolved weights are persisted into each strategy's allocated-capital budget.
Static allocation should remain the default until strategy NAV/P&L history is cleanly attributable;
the inverse-vol/ERC implementations can later be selected independently per capital pool.

Portfolio risk now applies gross, net, and single-instrument caps **per route/account**, and the
final trade-size dust threshold also uses that route's NAV. Existing ETSA/RPS sleeve rebalance-band
semantics remain available before same-account netting. Every allocation, risk, rebalance,
execution, accounting and lifecycle decision is written to the audit ledger.

## Audit rule

**Nothing important should exist only in memory or on the dashboard.**

Conductor persists strategy runs, input account snapshots, native outputs, target revisions, risk
changes, rebalance decisions, execution reports, internal crosses, external fill settlements,
virtual cash changes, lifecycle transitions and bootstrap reconciliation checks. The local Nautilus
bridge also persists request/response history.

## Deployment topology

The Windows and Lubuntu nodes are on different networks and do not depend on each other.

```text
Windows node                           Lubuntu node
------------                           ------------
Equities/futures/options               Crypto
local Conductor state                  local Conductor state
persistent execution worker            local crypto runtime
IBKR                                   Hyperliquid
local dashboard                         local dashboard
```

A future global dashboard should receive outbound telemetry from each node. It must never become an
execution dependency or shared source of truth.

## Migration guide

See [`docs/WINDOWS_ETSA_RPS_TLAQ_MIGRATION.md`](docs/WINDOWS_ETSA_RPS_TLAQ_MIGRATION.md) for the
paper smoke, live-account shadow, Task Scheduler setup, bootstrap/reconciliation procedure, failure
drills, and cutover checklist.

The frozen V0.3 Target Snapshot Protocol remains documented in
[`docs/TARGET_SNAPSHOT_PROTOCOL.md`](docs/TARGET_SNAPSHOT_PROTOCOL.md).
