# Conductor architecture

## Product boundary

Conductor is a portfolio control plane and strategy runtime. It converts independent strategy
intent into one risk-controlled desired broker state while preserving economic ownership inside
virtual strategy books.

```text
strategy repositories
  signal logic + native output
              |
              v
strategy adapter / target protocol
  normalize to absolute desired state
              |
              v
Conductor control plane
  virtual accounts -> allocation -> portfolio -> risk
  -> rebalance bands -> internal crossing -> broker delta
              |
              v
execution adapter / durable local bridge
              |
              v
NautilusTrader worker
  instruments + market data + orders + fills + broker reconciliation
              |
              v
broker or exchange
```

Strategies do not own broker connections in this architecture. NautilusTrader does not own
strategy allocation or virtual economic ownership.

## Primary layers

### Strategy contract

`src/conductor/protocol/` contains the frozen V0.3 Target Snapshot Protocol and filesystem SDK.
It is the stable, broker-agnostic contract for producers that can emit Conductor-native snapshots.

`src/conductor/runtime/` supports existing strategies that produce native target weights, absolute
quantities, or deltas. The runtime gives the subprocess only its strategy account view, persists the
artifacts, and converts the result to a `StrategyIntent`.

Delta output is an adapter concern. It is applied exactly once to the starting virtual position and
becomes absolute desired state before entering the portfolio core.

### Portfolio control plane

The main decision path is composed from small domain services:

- `PortfolioBuilder` sizes strategy intent and aggregates route-aware targets;
- `VirtualRebalanceBuffer` applies sleeve-level rebalance-band policy before cross-strategy netting;
- `PortfolioRiskEngine` applies deterministic portfolio limits;
- `DesiredStateReconciler` computes desired minus actual broker quantity;
- `OrderPlanner` suppresses uneconomic residual trades;
- `VirtualAccountingEngine` preserves ownership, internal crosses, cash, and fill allocation;
- `ConductorEngine` orders the pipeline and records audit evidence.

These components operate on domain objects from `src/conductor/domain/models.py`. Execution adapters
must not introduce broker-specific objects into the portfolio layer.

### Execution boundary

`ExecutionAdapter` is the boundary between Conductor's desired broker delta and venue execution.
The paper implementation is synchronous and local. The live IBKR implementation uses two pieces:

1. `NautilusBridgeExecutionAdapter` runs inside the short-lived Conductor process. It checks worker
   freshness, reads broker state, resolves instruments, and writes durable requests.
2. The persistent Nautilus worker owns the actual IBKR/Nautilus connection, claims requests,
   publishes state, submits orders, and writes execution reports.

Conductor intentionally does not implement a parallel TWS order/fill state machine.

## Strategy-run sequence

```text
1. acquire/record strategy run
2. load the strategy's virtual account view
3. persist subprocess input
4. run strategy with timeout and captured stdout/stderr
5. validate native output
6. normalize output to absolute StrategyIntent
7. persist desired strategy state/revision
8. build aggregate route-aware desired portfolio
9. apply sleeve policy and portfolio risk
10. calculate desired broker delta
11. internalize opposing strategy changes where possible
12. submit only residual external deltas, or return shadow reports
13. refresh actual broker state and reconcile
14. commit virtual ownership/cash only from reconciled facts
15. persist run outcome and audit records
```

Failures before reconciliation must leave the prior committed virtual ownership intact. A later run
may observe completed broker facts and finish reconciliation; it must not assume an earlier
submission filled. A terminal rejection, denial, cancellation, expiry, or failure that leaves the
broker away from desired state produces a blocked portfolio cycle and a non-success orchestration
outcome. A merely asynchronous submission remains submitted while broker state may still converge.

## State and consistency

### Conductor ledger

The main SQLite ledger is the durable source for accepted events, desired books, runtime books,
strategy accounts, virtual positions, lifecycle, strategy runs, instrument cache, and audit data.

The ledger distinguishes desired state from committed economic ownership. This distinction is
necessary whenever execution is asynchronous, rejected, partially filled, or interrupted.

### Nautilus bridge

The bridge SQLite database contains worker heartbeat/readiness, broker positions, resolved
instruments, and request/response history. WAL mode supports one persistent worker and occasional
short-lived local Conductor processes at the repository's expected request volume.

The bridge is durable IPC, not a second portfolio ledger. Strategy ownership does not move into it.

### Transaction boundary

There is no distributed transaction spanning the strategy process, Conductor ledger, bridge,
Nautilus, and broker. Correctness therefore depends on:

- stable intent identity and revisions;
- absolute desired state inside the core;
- durable request and response identities;
- refreshing broker facts after submission;
- idempotent reconciliation;
- committing virtual ownership only after reconciliation;
- preserving evidence across restart and timeout.

## Virtual ownership and netting

IBKR exposes the physical account total. Conductor retains the economic decomposition:

```text
ETSA            AAPL  +40
RPSchteroids    AAPL  +25
TLAQ            AAPL  -10
                       ---
IBKR physical   AAPL  +55
```

If strategies make opposing changes, `VirtualAccountingEngine` records an internal transfer. Only
the net residual is eligible for broker execution. Internal crosses change ownership without
pretending that an external fill occurred.

Bootstrap seeds are explicit operator assertions. Conductor never reverse-engineers the ownership
split from the aggregate broker position.

## Routing and instrument identity

Route identity is part of position identity. Equal display symbols on different routes are not
fungible and do not net.

Canonical instrument identifiers remain broker-agnostic in strategy and portfolio code. A route
resolver maps them to exact venue instruments and persists the result. V0.4 automatic mapping is
deliberately narrow: US equities to IBKR SMART. Futures and options require explicit contract
parsers before promotion.

## Deployment topology

The Windows node owns its local strategy runs, ledger, bridge, Nautilus worker, and IBKR connection.
A future Lubuntu crypto node owns an equivalent local source of truth and venue connection.

```text
Windows node                         Lubuntu node
------------                         ------------
local strategies                     local strategies
local Conductor ledger               local Conductor ledger
local execution bridge               local execution bridge
local persistent worker              local persistent worker
IBKR                                 crypto venue
         \                           /
          \-- outbound telemetry ---/
                    |
             read-only global view
```

Neither node may require the other node or the global view to trade, stop, reconcile, or recover.

## Extension rules

When adding a strategy:

- select one supported contract and route;
- provide explicit initial ownership and cash;
- certify replay, timeout, malformed-output, and disable/retire behavior;
- pass paper and shadow promotion stages before execution authority.

When adding an asset class:

- define an unambiguous canonical identity;
- model multiplier, lot/tick size, notional, lifecycle, and margin semantics;
- keep broker translation behind the route boundary;
- add mixed-book risk, accounting, execution, and reconciliation tests.

When adding a broker or venue:

- implement the execution-adapter contract;
- retain Conductor's ownership and desired-state semantics;
- provide durable request identity, execution facts, health/freshness, and restart recovery;
- do not leak venue-specific order logic into strategy adapters.

## Related decisions

- `docs/adr/0001-desired-state-control-plane.md`
- `docs/adr/0002-virtual-strategy-subledger.md`
- `docs/adr/0003-nautilus-execution-kernel.md`
- `docs/adr/0004-sqlite-wal-local-bridge.md`
- `docs/adr/0005-independent-node-sources-of-truth.md`
