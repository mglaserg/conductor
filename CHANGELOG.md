# Changelog

All notable changes to Conductor are recorded here. Until a stable release process exists, entries
follow a lightweight Keep a Changelog structure and use repository tags as the historical anchors.

## 0.4.0a6 - 2026-09-23

### Fixed

- Prevented the IBKR worker from entering Nautilus portfolio valuation while the adapter has only
  published an empty/half-initialized margin account state. This avoids the Rust
  `Currency must be specified` process abort observed on a newly connected TLAQ account.
- Broker NAV discovery now prefers the raw venue-reported `NetLiquidation` preserved in the
  account state's `info` bag before asking Nautilus to calculate portfolio equity.
- An explicitly empty typed-balance snapshot is now treated as `not ready` rather than as a
  valuation input.

### Known upstream limitation

- NautilusTrader `2.0.0rc4` (and `rc5`/current develop as checked on 2026-09-23) builds the IB
  historical execution filter from the namespaced Nautilus account ID (for example
  `IB-U123...`) instead of the native IB account code (`U123...`). IB rejects that reconciliation
  request with error 321. Conductor keeps live orders disabled while this upstream fill-history
  reconciliation path remains affected.

## 0.4.0a5 - 2026-09-23

### Changed

- Made NautilusTrader a required Conductor runtime dependency instead of an optional `nautilus`
  extra.
- Pinned the verified NautilusTrader runtime to `2.0.0rc4` so plain `uv sync` installs the execution
  kernel Conductor actually requires.
- Updated runtime diagnostics and operator documentation so a normal sync is the canonical
  installation path.
- Added a packaging regression test that prevents NautilusTrader from silently becoming optional
  again.
- Stopped tracking generated `*.egg-info` metadata so stale package metadata cannot contradict
  `pyproject.toml`.

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

### Fixed

- IBKR worker NAV publication now resolves Nautilus's namespaced live `AccountId` from the cache
  (for example `IB-U123...`) and verifies its broker-native account number before reading NAV.
  This avoids constructing invalid raw `AccountId("U123...")` values on Nautilus 2.x.
- Live workers no longer fall back to venue-wide equity when the configured account cannot be
  resolved, preventing one IBKR account from being sized from another account's NAV.
- IBKR worker NAV publication now queries Nautilus by the configured account ID first, so
  multi-account routes do not depend on ambiguous venue-scoped equity/cache lookups.
- Workers no longer report `ready=true` while `NetLiquidation` is unavailable; startup remains
  fail-closed until account NAV arrives.
- Runtime bootstrap errors caused by unavailable broker NAV now return a concise `REFUSED:` CLI
  diagnostic instead of a raw Python traceback.

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
