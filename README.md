# Conductor

Conductor is the portfolio control plane for independent trading strategies.

> **Strategies publish desired economic state. Conductor owns capital, implementation, risk,
> reconciliation, execution, and economic ownership.**

Conductor is not a strategy framework. ETSA, RPSchteroids, Futurescope, Crypto YOLO, CleanCarry,
and later strategies remain independent research/production projects. NautilusTrader remains the
intended lower-level execution/runtime kernel where it fits; Conductor owns the portfolio above it.

## V0.3 — Target Snapshot Protocol

V0.3 replaces the loose external `StrategyIntent` boundary with one versioned Conductor protocol.
There is **not** an ETSA schema, RPS schema, YOLO schema, etc. Strategies use thin producer-side
adapters to publish one of two economic snapshot families:

- `LinearTargetSnapshot`: complete signed target weights relative to strategy capital;
- `StructureTargetSnapshot`: complete desired spreads/pairs/options structures with leg ratios and
  structure-level risk intent.

The internal V0.2 portfolio kernel remains in place behind this boundary.

```text
Strategy repo
   native model / signal / portfolio logic
                 |
                 v
          conductor producer SDK
                 |
       complete target snapshot
                 |
                 v
       atomic local JSON inbox
                 |
                 v
 schema + StrategyProfile validation
                 |
                 v
 immutable intent event + desired book state
                 |
                 v
      capital / quantity resolution
                 |
                 v
        portfolio risk + netting
                 |
                 v
 desired broker state <-> actual broker state
                 |
                 v
             execution
                 |
                 v
       committed virtual ownership
```

See [`docs/TARGET_SNAPSHOT_PROTOCOL.md`](docs/TARGET_SNAPSHOT_PROTOCOL.md) for the frozen V1
semantics implemented in this release.

## Key V0.3 properties

- replacement identity is `(strategy_id, book_id)`;
- snapshots are **complete desired state**, never diffs;
- absent targets in a newer snapshot become zero;
- an empty complete snapshot is a normal flatten;
- `(source, event_id)` replay is idempotent;
- same event ID with a different payload hard-rejects;
- inbox publishing never overwrites an unprocessed event ID;
- revisions are monotonic; revision gaps are allowed;
- a book cannot silently change protocol family across revisions;
- duplicate/revision checks and desired-state replacement are transactional in SQLite;
- Pydantic models forbid unknown fields and generate committed JSON Schemas;
- strategy permissions/staleness live in Conductor-owned `StrategyProfile`, not in strategy payloads;
- accepted desired state is checked for freshness again before execution;
- the V0.2 virtual ledger migrates from `(strategy, instrument)` to
  `(strategy, book, instrument)`, assigning existing rows to `book_id="main"`;
- local file transport is atomic and works on Windows and Lubuntu;
- structure snapshots are validated/stored in V0.3 but are **not executable yet**.

## Producer example

ETSA and RPSchteroids use the same linear contract:

```python
from datetime import datetime, timezone
from conductor.protocol.sdk import FilesystemConductorClient

client = FilesystemConductorClient(r"C:\Trading\Conductor\runtime\inbox")
client.submit_linear(
    strategy_id="ETSA",
    book_id="main",
    revision=42,
    as_of=datetime.now(timezone.utc),
    targets={
        "EQ.US.AAPL": "0.08",
        "EQ.US.MSFT": "-0.06",
        "EQ.US.NVDA": "0.04",
    },
)
```

Those are target weights inside ETSA's allocated capital budget. They are not trade deltas.
Conductor resolves quantities and computes broker deltas itself.

See `examples/etsa_producer.py`, `examples/rpschteroids_producer.py`, and
`examples/futurescope_structure_producer.py`.

## Demo

Python 3.12+ is required. From either Windows or Lubuntu:

```text
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest -q
uv run conductor-demo
```

Or:

```text
uv run conductor demo
```

The demo is paper-only. It:

1. has ETSA and RPS publish real V0.3 snapshot files;
2. validates and accepts them into SQLite desired state;
3. turns strategy-budget weights into quantities;
4. nets overlapping AAPL ownership;
5. reconciles a paper broker;
6. publishes ETSA revision 2 with MSFT omitted;
7. proves omission means zero and removes ETSA's MSFT exposure; and
8. proves the next identical cycle creates zero trades.

Nothing in the demo connects to a live account.

## Protocol tools

Validate a snapshot:

```text
uv run conductor validate path/to/snapshot.json
```

Regenerate JSON Schemas:

```text
uv run conductor schemas schemas
```

Committed schemas:

- `schemas/linear-target-snapshot-v1.json`
- `schemas/structure-target-snapshot-v1.json`

## Deployment topology

Conductor is one logical system with two independent runtime nodes:

```text
Windows node                         Lubuntu node
------------                         ------------
Equities                             Crypto
Futures
Options

IBKR / Windows adapters              Hyperliquid / crypto adapters
Windows service/task plumbing        systemd service/timer plumbing
local durable state                  local durable state
```

Both nodes use the same protocol, portfolio semantics, ledger model, and risk vocabulary. Each node
must remain safe and operable with local state; a future global portfolio view can aggregate the two
without making either machine depend on a shared database to trade safely.

## NautilusTrader boundary

NautilusTrader remains optional:

```text
uv pip install -e ".[nautilus]"
uv run conductor-nautilus-smoke
```

Conductor's domain and protocol do not depend on Nautilus types. This lets the Windows node use a
Windows-appropriate execution adapter while the Lubuntu crypto node can use Nautilus/Hyperliquid
where appropriate.

## What V0.3 deliberately does not do

- live broker/exchange routing;
- structure-to-leg quantity sizing;
- options lifecycle/Greeks translation;
- futures spread lifecycle/roll management;
- cross-node shared execution state;
- full canonical instrument registry enforcement;
- strategy P&L/cost-basis attribution.

Those remain portfolio/runtime milestones. The protocol boundary is now stable enough to build them
without requiring strategy repositories to know broker mechanics.
