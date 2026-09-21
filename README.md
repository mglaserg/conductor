# Conductor

Conductor is the portfolio control plane and strategy runtime for independent trading strategies.

> **Strategies decide desired economic state. Conductor owns strategy accounts, capital, portfolio
> risk, economic ownership, netting, audit, and desired broker state. NautilusTrader owns the
> broker/exchange execution plumbing.**

The first production migration is the Windows equities runtime: **ETSA + RPSchteroids + TLAQ**, all
sharing one Interactive Brokers account while retaining separate virtual positions and cash.

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
        +--> portfolio risk / sleeve rebalance policy
        +--> internal crossing across virtual strategy books
        +--> aggregate desired IBKR position
        |
        v
local SQLite/WAL bridge
        |
        v
persistent NautilusTrader worker
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
One long-lived Nautilus worker owns the IBKR connection and continuously publishes broker state into
a local durable bridge. The one-shot Conductor process reads that state and, in live mode, submits
an aggregate execution request through the bridge.

There is no Redis, RabbitMQ, or network service between them. Both processes run on the same Windows
machine and use SQLite in WAL mode.

## Shared-account virtual ownership

IBKR only knows the physical account position. Conductor knows economic ownership:

```text
TLAQ           AAPL +100     cash -$12,000
RPSchteroids   AAPL  +50     cash  +$5,000
-------------------------------------------
IBKR physical  AAPL +150
```

TLAQ receives only its own positions/cash when it runs. RPS receives only its own. Negative virtual
cash is permitted and represents strategy financing.

If TLAQ wants +20 AAPL while RPS reduces AAPL by 15 shares, Conductor internally transfers 15 shares
between their virtual books and sends only **BUY 5 AAPL** to IBKR. Internal crosses and external fill
settlements are both audited.

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

Nautilus receives one physical execution identity (`ConductorIbkr-001`) for the shared IB account.
ETSA/RPS/TLAQ ownership remains in Conductor's virtual ledger. This avoids split ownership of the
same net IBKR instrument inside Nautilus reconciliation.

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

## Bootstrap scope is explicit

Nautilus reconciliation requires the instruments referenced by broker reports to be loaded. The
Windows route therefore has a bootstrap field:

```toml
preload_instruments = ["AAPL", "MSFT"]
```

Before cutover, every physical IBKR holding in the Conductor-controlled account must be either:

1. present in a strategy's seeded virtual positions; or
2. listed as a reconciliation-only `preload_instrument`.

A non-zero preloaded position with no virtual owner makes `conductor doctor` fail. **No unmodeled
account position is allowed at go-live.** Once Conductor has execution authority, manual trading in
that account should be treated as an operational exception requiring explicit reconciliation.

## Install

Python 3.12+ and `uv` are recommended.

Core + tests:

```powershell
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest -q
```

Windows execution runtime:

```powershell
uv pip install -e ".[nautilus]"
```

Nautilus currently publishes pre-release 2.x wheels, so depending on the package version available
on the machine you may need to allow pre-releases when installing it directly.

## Windows runtime commands

Copy [`examples/windows_etsa_rps_tlaq.toml`](examples/windows_etsa_rps_tlaq.toml) to
`conductor.toml` and replace every placeholder before connecting to the production account.

Start the persistent execution worker:

```powershell
uv run conductor nautilus-worker windows_ibkr_equities --config conductor.toml
```

Check it independently:

```powershell
uv run conductor worker-status windows_ibkr_equities --config conductor.toml
```

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

For the September migration, persisted strategy capital is the sizing source and existing trusted
allocations should remain static until strategy NAV/P&L history is cleanly attributable. The
allocator implementations are present so we can switch later without changing strategy code.

Portfolio risk currently supports deterministic gross and single-instrument caps and preserves the
existing ETSA/RPS sleeve rebalance-band semantics before TLAQ cross-strategy netting. Every risk,
rebalance, execution, accounting and lifecycle decision is written to the audit ledger.

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
