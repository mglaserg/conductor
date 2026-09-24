# Changelog

## 0.4.0a12 - 2026-09-24

### Fixed

- Kept the public route-level target-universe warm-up API, but changed cold IBKR/Nautilus
  instrument discovery from one fan-out bridge request into strictly serialized single-instrument
  resolution. The next missing symbol is not enqueued until the previous one has a usable mark.
- Each cold symbol now receives the full configured bridge request timeout independently. A stalled
  contract therefore fails with the exact canonical instrument (for example `EQ.US.BUSE`) instead
  of one opaque batch request ID after the whole universe hangs.
- Existing startup-seeded marks still bypass dynamic resolution entirely, and quote-subscription /
  in-flight request deduplication remains unchanged.

### Validation

- Added regressions proving that the second cold symbol is not enqueued before the first completes,
  and that a stalled first symbol is named in the error while later symbols are never started.
- This directly covers the Windows shadow failure where eight new ETSA symbols were fanned out
  together and the Nautilus worker stopped making progress until Conductor's bridge timeout.

## 0.4.0a11 - 2026-09-24

### Fixed

- Replaced the startup quantity-only IBKR position preflight used by the live worker with an
  exact-account portfolio snapshot that also captures broker-reported market prices/market values.
- Seeded those broker marks into the Nautilus bridge alongside canonical startup positions.
  Ownership bootstrap therefore reuses the already-reconciled broker snapshot instead of issuing a
  second batch quote warm-up merely to back-solve starting virtual cash.
- This removes the observed bootstrap timeout after a healthy worker had already reconciled the
  route's positions.

### Safety

- Portfolio snapshots remain filtered to the configured native IB account and still fail closed on
  any non-stock holding in the equities-only worker.
- The bootstrap manifest remains one-time migration state; no strategy-specific ownership universe
  was added to `conductor.toml`.
- Bootstrap still requires exact broker-vs-manifest quantities and remains trade-free/create-only.

## 0.4.0a10 - 2026-09-24

### Added

- Added `conductor bootstrap <route_id>` as a first-class, dry-run-by-default ownership bootstrap
  workflow for live/shadow broker routes.
- Single-strategy routes automatically propose ownership of every current broker position. Shared
  routes infer only unambiguous ownership claims from each strategy's latest persisted runtime
  intent; unclaimed or multiply-claimed instruments remain unresolved and block commit.
- Added explicit JSON ownership manifests and `--write-template` support for resolving shared-route
  ambiguities without editing SQLite by hand.
- Bootstrap cash is derived from each strategy's allocator-assigned capital minus the marked net
  notional of its approved starting positions, so each virtual book starts with equity equal to its
  assigned capital.

### Safety

- Bootstrap is create-only: it refuses to overwrite any route that already has virtual ownership.
- `--commit` requires `--confirm <route_id>`, and the proposed strategy quantities must sum exactly
  to every live broker quantity before the ledger can be written.
- The position and cash writes are one SQLite transaction, followed by immediate broker-vs-virtual
  reconciliation. Bootstrap never places orders and never interprets allocator weights as position
  ownership.

## 0.4.0a9 - 2026-09-24

### Fixed

- Normalized exact-account IBKR stock-position preflight symbols into Conductor canonical IDs
  (for example `AEP` -> `EQ.US.AEP`) before startup reconciliation. This preserves the strict
  set-and-quantity equality gate while preventing the same holdings from appearing as both missing
  native symbols and extra canonical symbols.
- Live bridge position mapping now applies the same US-equity canonicalization, including when an
  older bridge database still contains native-symbol aliases from a previous worker version.

### Added

- Added `nautilus_start.bat`, `nautilus_status.bat`, and a cleaned-up `nautilus_doctor.bat` for the
  normal two-route Windows operator workflow.
- Ignored new runtime run artifacts, worker logs, and the local IB instrument cache in Git. Existing
  historical run artifacts already tracked by older commits are intentionally left untouched.

### Safety

- Startup reconciliation still requires exact canonical instrument/quantity equality; this change
  normalizes identity rather than weakening the readiness gate.
- `live_orders_enabled = false` remains unchanged.

## 0.4.0a8 - 2026-09-23

### Fixed

- Added an exact-account IBKR position preflight before Nautilus node construction. Existing stock
  holdings are now discovered through the read-only TWS API and injected into the instrument
  provider's startup `load_ids`, preventing broker positions from being skipped simply because the
  instrument cache was cold.
- Worker readiness is now gated on startup broker-position reconciliation. A worker cannot publish
  `ready=true` while the broker preflight says positions exist but Nautilus has not reconstructed
  those positions.
- Added route-level batch instrument warm-up before portfolio construction. Strategy target universes
  are resolved together instead of serially blocking once per symbol.
- Deduplicated in-flight instrument requests and quote subscriptions inside the persistent worker,
  eliminating repeated `Subscribe(Quotes(...))` calls from the 250 ms bridge poll loop.
- Raised the default bridge request timeout from 60 seconds to 180 seconds as a cold-cache safety
  margin. The timeout is no longer the primary instrument-discovery mechanism because route
  universes are warmed in one batch first.

### Safety

- The broker-position preflight filters by the exact configured native IB account code and fails
  closed if the equities-only worker encounters a non-stock holding it cannot model safely.
- The known upstream Nautilus `IB-U...` historical-execution reconciliation warning remains
  unchanged; `live_orders_enabled = false` is still required for shadow validation.

## 0.4.0a7 - 2026-09-23

### Added

- Added an exact-account Interactive Brokers `reqAccountSummary` fallback for `NetLiquidation` when
  Nautilus has registered the configured account but its account state still has no usable NAV.
- The fallback runs on a separate TWS API client/thread, filters callbacks by the native configured
  account code, caches only fresh values, and fails closed on ambiguity, timeout, staleness, or
  account mismatch.
- Added per-route account-summary client/refresh/staleness settings and status diagnostics.
- Added Nautech's `nautilus_ibapi` mirror as a required runtime dependency so plain `uv sync`
  installs the direct account-summary client together with NautilusTrader.

### Safety

- Nautilus remains the sole execution/position/fill/reconciliation backend. The direct IB API path
  is read-only and is used only for the scalar broker NAV.
- A linked account's NAV can never be substituted for another route: only an exact native account
  callback match is accepted.

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
