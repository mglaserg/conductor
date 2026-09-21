# Changelog

All notable changes to Conductor are recorded here. Until a stable release process exists, entries
follow a lightweight Keep a Changelog structure and use repository tags as the historical anchors.

## Unreleased

### Added

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
