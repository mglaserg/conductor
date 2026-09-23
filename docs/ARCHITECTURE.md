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
  route-backed capital pools -> virtual accounts -> allocation -> risk
  -> rebalance bands -> same-route crossing -> broker delta
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

- `PortfolioBuilder` sizes strategy intent from each strategy's allocated capital and aggregates route-aware targets;
- named `portfolio.<route_id>` capital pools resolve independent broker/fixed NAV and allocation policy;
- `VirtualRebalanceBuffer` applies sleeve-level rebalance-band policy before same-route netting;
- `PortfolioRiskEngine` applies deterministic limits independently per route/account;
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

CLI `--paper` selects a fully offline execution composition before routes are built. It opens a
separate Conductor ledger, substitutes every configured strategy route with a durable synthetic
broker backed by that ledger, and prices instruments from deterministic paper configuration. It
does not construct the Nautilus bridge adapter. This portable composition is available on Windows
and Lubuntu and is distinct from live-account shadow mode.

## Strategy-run sequence

```text
1. acquire/record strategy run
2. load the strategy's virtual account view
3. persist subprocess input
4. run strategy with timeout and captured stdout/stderr
5. validate native output
6. normalize output to absolute StrategyIntent
7. persist desired strategy state/revision
8. select the strategy's route-backed capital pool and same-route companion books
9. build aggregate desired state for that route/account
10. apply sleeve policy and route-local portfolio risk
11. calculate desired broker delta
12. internalize opposing same-route strategy changes where possible
13. submit only residual external deltas, or return shadow reports
14. refresh actual broker state for that route and reconcile
15. commit only that route's virtual ownership/cash from reconciled facts
16. persist run outcome and audit records
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

## Route-backed capital pools, virtual ownership, and netting

Each independently funded broker account is represented by one named capital pool keyed by route.
Its NAV comes from that route's broker NetLiquidation by default, and only strategies assigned to
that route share the pool's allocation and risk budget.

Within a shared account, Conductor retains the economic decomposition:

```text
route ibkr_main
ETSA            AAPL  +40
RPSchteroids    AAPL  +25
                       ---
IBKR physical   AAPL  +65

route ibkr_tlaq
TLAQ            AAPL  -10
                       ---
IBKR physical   AAPL  -10
```

If ETSA and RPS make opposing changes on `ibkr_main`, `VirtualAccountingEngine` records an internal
transfer and only the net residual is eligible for that broker account. TLAQ cannot cross with them
while it is assigned to `ibkr_tlaq`, even if the canonical instrument is identical.

Bootstrap seeds are explicit operator assertions. Conductor never reverse-engineers the ownership
split from aggregate broker positions, and changing an already-persisted strategy's route is refused
until an explicit account migration/bootstrap is performed.

## Routing and instrument identity

Route identity is part of position identity **and capital identity**. Equal display symbols on
different routes are not fungible, do not net, and do not share NAV or risk capacity. A strategy run
reconciles only its own route/account plus companion books assigned to that route.

Canonical instrument identifiers remain broker-agnostic in strategy and portfolio code. A route
resolver maps them to exact venue instruments and persists the result. V0.4 automatic mapping is
deliberately narrow: US equities to IBKR SMART. Futures and options require explicit contract
parsers before promotion.

## Deployment topology

The Windows node owns its local strategy runs and ledger plus one bridge/worker per configured IBKR
route/account. A future Lubuntu crypto node owns an equivalent local source of truth and venue
connection.

```text
Windows node                         Lubuntu node
------------                         ------------
local strategies                     local strategies
local Conductor ledger               local Conductor ledger
route-specific execution bridges     local execution bridge
route-specific persistent workers    local persistent worker
IBKR accounts                        crypto venue
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
- `docs/adr/0006-route-backed-capital-pools.md`
