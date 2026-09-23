# Changelog

All notable changes to Conductor are recorded here. Until a stable release process exists, entries
follow a lightweight Keep a Changelog structure and use repository tags as the historical anchors.

## Unreleased

### Added

- Route-backed named capital pools in a single `conductor.toml`, allowing independent broker
  accounts to use their own NAV, allocator, strategy weights, risk budget and trade-size buffer.
- Broker `NetLiquidation`-derived capital allocation per live route, with separate synthetic
  `[paper.portfolio_navs]` overrides for multi-account offline paper runs.
- Route-scoped strategy runtime startup and reconciliation so an unrelated account/worker cannot
  block or be mutated by another account's strategy cycle.
- Per-route gross, net and single-instrument risk scaling, plus route-aware dust filtering.
- Configured-vs-worker-reported IBKR account validation and distinct multi-worker Windows launch
  support.
- ADR 0006 documenting route-backed capital pools and explicit account-migration boundaries.
- True offline `run --paper` and `status --paper` runtime for Windows and Lubuntu, with a separate
  SQLite ledger, durable synthetic broker positions/fills, deterministic default/override prices,
  static capital allocation, funded initial strategy cash, and case-insensitive CLI strategy IDs.
- End-to-end offline-paper coverage proving no Nautilus construction or live bridge request,
  separate state, allocation/cash bootstrapping, price resolution, durable reconciliation, audit
  evidence, and an empty repeated unchanged run.
- V0.4 config-driven runtime for one-shot strategy subprocesses.
- Native target-weight, target-quantity, and position-delta result adapters.
- Persistent virtual strategy accounts, cash, positions, lifecycle, and run records.
- Internal crossing and external-fill allocation across independent strategy books.
- Static, inverse-volatility, ERC, and deterministic fallback allocation implementations.
- Sleeve rebalance-band handling and expanded portfolio risk controls.
- Route-aware execution and canonical instrument resolution foundation.
- SQLite/WAL bridge between short-lived Conductor runs and a persistent Nautilus worker.
- Nautilus IBKR worker foundation for account state, instrument resolution, orders, fills, and
  reconciliation reports.
- Shadow-by-default execution mode, worker health checks, bootstrap reconciliation, and strategy
  lifecycle commands.
- Read-only local dashboard and Windows Task Scheduler helpers.
- ETSA, RPSchteroids, and TLAQ migration templates and operational runbook.
- Canonical roadmap, project status, architecture guide, safety invariants, agent guide, and initial
  architecture decision records.
- Cross-process, database-atomic strategy-run exclusion with a defensive partial unique index.
- Regression coverage proving that concurrent acquisition, nonzero strategy exits, subprocess
  timeouts, and malformed output cannot mutate committed portfolio state.
- Fail-closed `blocked` portfolio/orchestration state for terminal rejection, denial, cancellation,
  expiry, and failure reports, including partial-fill-then-cancel coverage.
- Windows CP-1252-compatible demo output and a console regression test.

### Changed

- Static/inverse-vol/ERC selection and fallback are now configured per capital pool rather than as
  one node-global allocator. Legacy one-pool configuration remains supported where unambiguous.
- Named-pool strategy `allocated_capital` is refreshed from the route NAV and resolved allocator;
  seed capital is no longer the authority for those live pools.
- Clarified that Conductor owns economic state and portfolio decisions while NautilusTrader owns
  broker/exchange execution plumbing.
- Made the Windows ETSA/RPSchteroids/TLAQ migration the active promotion milestone.

## 0.3.0 — 2026-09-11

### Added

- Frozen Target Snapshot Protocol v1 for linear and structured strategy intent.
- Strict schemas, acceptance rules, replay/revision handling, staleness checks, and filesystem SDK.
- Independent strategy books and migration support for the earlier virtual ledger layout.

## 0.1.0 — 2026-09-11

### Added

- Initial portfolio-control kernel, domain models, paper adapter, risk, order planning,
  reconciliation, ledger, tests, and demonstration CLI.
