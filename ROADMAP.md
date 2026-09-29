# Conductor roadmap

This root file is the **canonical roadmap**. Milestones are operational capability gates, not a
collection of disconnected features. Code completion alone does not promote execution software.

Status vocabulary:

- **DONE** — implemented and verified for the stated scope.
- **ACTIVE** — the current highest-priority promotion milestone.
- **QUEUED** — planned, but its prerequisites are not yet complete.
- **PARKED** — intentionally deferred.

Verification vocabulary used inside milestones:

- **unit** — deterministic automated tests with local doubles or fixtures;
- **paper** — connected to a broker/exchange paper environment;
- **shadow** — consuming live state and calculating actions without execution authority;
- **production** — executing with real authority and reconciled operational evidence.

## M0 — Portfolio kernel and target protocol — DONE

- strict Linear Target Snapshot and Structure Target Snapshot v1 models;
- schema validation with unknown-field rejection;
- idempotent event acceptance and payload-collision detection;
- per-book revision and replay rules;
- staleness enforcement;
- filesystem SDK/inbox transport;
- desired-state portfolio construction and route-aware netting;
- deterministic risk scaling, order planning, and reconciliation;
- paper execution adapter;
- durable SQLite ledger and audit foundation;
- independent strategy/book ownership.

**Gate passed:** independent strategy targets can enter through the frozen V0.3 protocol, produce a
deterministic desired portfolio, and reconcile idempotently in the local paper runtime.

## M1 — Windows ETSA/RPSchteroids/TLAQ migration — ACTIVE

The active objective is to replace Dagster orchestration and broker execution for the three proven
Windows equity strategies without rewriting their strategy logic.

### Implemented in the V0.4 alpha code

- config-driven node, route, strategy, seed, capital, and risk setup;
- route-backed named capital pools in one TOML, with independent broker NAV, allocator, risk, and trade-buffer budgets per account;
- one-shot strategy subprocess orchestration with persisted inputs, outputs, stdout, and stderr;
- database-atomic one-active-run acquisition for each strategy/book;
- native target-weight, target-quantity, and position-delta adapters;
- immediate conversion of deltas to absolute desired state;
- virtual strategy accounts, positions, allocated capital, and negative cash;
- internal crossing and residual broker-level netting;
- static, inverse-volatility, ERC, and deterministic fallback allocators, with strategy risk-budget inputs and selectable Ledoit-Wolf/sample covariance for ERC;
- explicit versioned `StrategyPolicy` plus `PortfolioPolicyEngine`, including opt-in native-vol scaling, strategy leverage/concentration limits, freshness rules, and fail-closed missing-volatility behavior;
- strategy- and sleeve-level capital-scaled rebalance bands plus portfolio gross and single-instrument caps;
- persisted end-to-end portfolio decision lineage from translated strategy targets through policy, risk, implementation buffering, aggregate broker targets and planned deltas;
- policy/allocator diagnostics surfaced through `status` and authority-aware `doctor`;
- route-aware execution and a persistent SQLite/WAL Nautilus bridge;
- one worker/bridge state boundary per IBKR account, with configured-vs-reported account-ID fail-closed checks;
- route-scoped strategy runtime startup so an unrelated account worker outage does not block another account;
- persistent Nautilus IBKR worker boundary with heartbeat, broker state, instrument resolution,
  request recovery, orders, fills, and commissions;
- exact-account broker-position preflight feeding startup `load_ids`, canonical native-IB-to-Conductor
  stock identity normalization, readiness gated on successful position reconstruction, and a batch
  target-universe API that serializes cold IB contract resolution with request/subscription dedupe
  and persists the authoritative listing-venue instrument ID returned through Nautilus's `on_instrument()` callback before explicitly routing quote subscription through client `IB`;
- explicit shadow mode with live submission disabled by default;
- external-authority Shadow Mirror state separate from durable virtual ownership, with sticky/fail-closed
  shared-account attribution, route-wide fresh-intent capture, authority-aware reconciliation and
  explicit cutover promotion;
- initial US-equity canonical mapping to IBKR SMART instruments;
- strategy activate, disable, retire, status, doctor, worker-status, and dry-run-first route
  ownership bootstrap commands;
- durable strategy-release admission with audited `RESEARCH -> VALIDATED -> SHADOW -> LIVE`
  transitions, REVIEW/KILLED handling, shadow/cutover gates, and fail-closed live-authority startup;
- append-only portable evidence ingestion for `clockwork.research.v1`, `edgelab.validation.v1`,
  and descriptive `factorstrip.decomposition.v1`, including independent content-hash verification,
  surfaced factor decomposition, and a hard validation-evidence gate on
  `RESEARCH/REVIEW -> VALIDATED` transitions;
- read-only local dashboard;
- Windows Task Scheduler helpers and an operator migration runbook.
- portable offline `run --paper`/`status --paper` execution with a separate SQLite source of truth,
  durable synthetic broker state, deterministic marks, allocated/funded strategy books, and the
  real configured subprocess/native-result boundary, without Nautilus or IBKR.

### Remaining promotion work

1. Install and import the selected NautilusTrader 2.x build on the actual Windows trading machine.
2. Wire the production ETSA, RPSchteroids, and TLAQ adapters without changing their proven signal
   calculations or schedules.
3. Run the create-only route bootstrap workflow, review any shared-account ambiguity, and approve each strategy's initial virtual positions/cash against allocator-derived capital and broker NAV.
4. Account for every physical IBKR position on the correct route/account and make `conductor doctor` clean across all configured routes.
5. Prove both workers' heartbeat, account-ID match, per-account NAV publication, automatic broker
   position preflight/startup reconstruction, serialized target-universe contract qualification, price availability,
   and restart recovery against IBKR paper.
6. Prove a small paper order end to end, including fill quantity, average price, commission,
   reconciliation, and an empty second cycle.
7. Close any gaps exposed by the required failure drills, including proving that an unrelated route
   outage does not block another account. Concurrent acquisition, subprocess
   failure/timeout, malformed output, terminal rejection, and partial-fill-then-cancel have unit
   coverage. Stale/disconnected worker, delta replay, cross-strategy overlap, broker mismatch, and
   all operational repetitions remain part of the promotion gate.
8. Run `shadow-cycle ibkr_main` / `shadow-cycle ibkr_tlaq` at the real production schedule while
   Dagster remains authoritative; explain every proposed-trade mismatch and exercise ambiguous
   ownership failure/recovery.
9. Stop legacy execution, obtain final clean Shadow Mirror reconciliation, promote one route at a
   time with `shadow-promote`, then enable Conductor live orders only after the promotion evidence is
   preserved.
10. Complete a controlled execution-authority cutover with an exercised rollback procedure.

### Gate

All three strategies complete repeated live-account shadow runs with attributable virtual books,
per-route broker NAV/capital budgets, clean multi-account bootstrap reconciliation, deterministic
proposed trades, and no unexplained mismatch. Paper
failure drills pass. Execution authority can then move once, with rollback preserving all ledger
and bridge evidence.

## M2 — Production operations and recovery — QUEUED

- explicit process/run locking that survives crashes and prevents overlapping ownership changes;
- ledger and bridge schema versioning with tested additive migrations;
- precise partial-fill, cancellation, rejection, timeout, and late-event state transitions;
- durable execution idempotency across CLI, worker, TWS, and machine restarts;
- operator-visible pending/claimed/stuck request inspection and safe recovery commands;
- backup, restore, corruption detection, and disaster-recovery rehearsal for both SQLite stores;
- structured logs, health checks, actionable alerts, and retention policy;
- daily ownership/cash/broker reconciliation reports;
- explicit operational handling for dividends, fees, splits, symbol changes, and manual trades;
- tested upgrade and rollback procedure.

**Gate:** kill either process or restart the machine at every execution phase without losing audit
history, duplicating economic intent, or silently changing virtual ownership. Daily reconciliation
can be operated without reading the database manually.

## M3 — IBKR futures and options — QUEUED

- explicit canonical futures contract identity, expiry, multiplier, and roll policy;
- explicit option identity including underlying, expiry, strike, right, multiplier, and venue;
- deterministic canonical-to-Nautilus/IBKR resolution without ambiguous symbol guessing;
- asset-aware quantity rounding, tick size, notional, margin, and risk treatment;
- multi-leg structure sizing and execution semantics for accepted structure snapshots;
- lifecycle handling for expiry, exercise, assignment, and futures delivery/roll risk;
- paper reconciliation tests for mixed equity, future, and option books.

**Gate:** a mixed-asset paper portfolio can be reconstructed from persisted state, resolved to exact
broker contracts, risk-checked, executed, and reconciled without symbol ambiguity or ownership
loss.

## M4 — Independent Lubuntu crypto node — QUEUED

- local crypto runtime and persistent Hyperliquid execution route;
- canonical spot/perpetual identifiers and exchange metadata cache;
- funding, fees, leverage, liquidation distance, and collateral-aware accounting;
- venue-native order/fill/reconciliation handling behind the execution-adapter boundary;
- strategy virtual books and internal netting local to the crypto node;
- paper/testnet, shadow where available, failure-drill, and controlled-cutover sequence;
- no dependency on the Windows node for trading or recovery.

**Gate:** the crypto node can operate, reconcile, restart, and recover independently while exposing
the same Conductor ownership and audit invariants.

## M5 — Capital allocation and portfolio risk maturity — QUEUED

- attributable strategy NAV, P&L, cash flow, exposure, turnover, and drawdown histories;
- allocator inputs derived from clean strategy-level histories rather than aggregate account data;
- governed promotion from static allocation to inverse-volatility or ERC/risk budgets (policy/risk-budget plumbing exists; automatic history feed remains queued);
- covariance/data-quality diagnostics and explicit fallback evidence (Ledoit-Wolf/sample selection and fallback evidence are implemented; richer data-quality gates remain queued);
- net exposure, margin utilization, liquidity, concentration, and asset-class limits;
- allocation rebalance scheduling, turnover controls, and before/after attribution;
- scenario and stress testing with audited operator overrides.

**Gate:** an allocation change is reproducible from persisted inputs, respects every portfolio and
strategy constraint, has deterministic fallback behavior, and can be explained after the fact.

## M6 — Fleet telemetry and global read-only board — QUEUED

- outbound telemetry envelope with node identity, freshness, health, positions, risk, runs, and
  reconciliation summaries;
- independently buffered publication from Windows and Lubuntu nodes;
- central read-only portfolio and operational views;
- stale/missing-node indication and alert routing;
- drill-down links to node-local evidence without copying execution authority;
- strict separation between observability and control.

**Gate:** loss or corruption of the global board cannot block, alter, or authorize trading on any
node; each node remains fully operable from local state.

## M7 — Repeatable strategy onboarding — QUEUED

- versioned producer SDK and adapter compatibility matrix;
- strategy contract tests for account input, output mode, canonical instruments, staleness, and
  deterministic replay;
- onboarding checklist for seed ownership, capital, route, schedule, timeout, and rollback;
- standard paper -> shadow -> limited-live -> production promotion evidence;
- per-strategy kill/disable/retire drills;
- documented upgrade path for frozen protocol versions.

**Gate:** a new independent strategy can be onboarded without changing the portfolio kernel and can
prove its safety contract before receiving execution authority.

## Parked ideas / not current priority

These directions require an explicit reprioritization and must not distract from M1:

- distributed queues, microservices, Kubernetes, or a network control plane;
- a shared cross-node execution database;
- high-frequency or latency-sensitive execution;
- strategy-authored broker orders that bypass desired-state normalization;
- direct trading controls in the global dashboard;
- additional brokers before IBKR and the first crypto route are operationally proven;
- machine-learning allocation before clean attributable histories exist.
