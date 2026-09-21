# Conductor project status

Last reviewed: **2026-09-21**

This is the canonical handoff document for the repository's current state. `ROADMAP.md` describes
where the product is going; this file describes what is true now and what should happen next.

## Current objective

Promote the V0.4 Windows migration runtime from locally implemented alpha code to an operationally
proven replacement for Dagster orchestration and broker execution for ETSA, RPSchteroids, and TLAQ.

The immediate phase is **paper connectivity and execution proof**, followed by live-account shadow.
Conductor must not receive production execution authority until the M1 gate in `ROADMAP.md` and the
cutover checklist in `docs/WINDOWS_ETSA_RPS_TLAQ_MIGRATION.md` are satisfied.

## What works now

### Frozen V0.3 foundation

- strict Linear Target Snapshot and Structure Target Snapshot models;
- schema validation, profile validation, staleness checks, and payload hashing;
- idempotent acceptance with event-ID collision detection;
- independent per-book revision handling and complete-snapshot semantics;
- filesystem producer SDK and inbox processing;
- route-aware portfolio construction, risk scaling, order planning, and reconciliation;
- durable SQLite ledger and paper execution path.

### Implemented in the V0.4 alpha working tree

- TOML-driven node, portfolio, route, strategy, risk, and seed configuration;
- one-shot strategy subprocess orchestration with persisted run inputs and outputs;
- native `target_weights`, `target_quantities`, and `position_deltas` result modes;
- conversion of strategy deltas to absolute desired state at the adapter boundary;
- virtual strategy accounts with positions, allocated capital, and positive or negative cash;
- internal crosses, fill allocation, and aggregate external netting;
- static, inverse-volatility, ERC, and fallback allocation implementations;
- sleeve rebalance bands, gross exposure limits, and single-instrument limits;
- route registry and initial canonical US-equity resolution;
- persistent local Nautilus bridge with worker heartbeat, broker positions, instruments, requests,
  responses, and restart requeue behavior;
- Nautilus IBKR worker integration code for account state, instrument resolution, orders, fills,
  commissions, and execution reports;
- shadow-by-default execution configuration;
- strategy lifecycle, status, doctor, worker-status, and local dashboard commands;
- Windows Task Scheduler scripts and migration adapters/runbook.

These statements describe implemented code, not completed paper, shadow, or production validation.

## Verification state

- The repository currently contains **48 automated tests** covering the protocol, ledger, portfolio,
  risk, allocation, routing, accounting, bridge, orchestration, rebalance, and native-result paths.
- The V0.3 protocol foundation was tagged as `conductor-v0.3-target-snapshot-protocol`.
- The documented environment workflow completed with `uv 0.12.17`: core `uv sync --extra dev`,
  followed by `uv sync --extra dev --extra nautilus --prerelease allow`.
- The full suite passed with the Nautilus extra installed on 2026-09-21: **48 passed in 5.47s**.
- `uv run conductor demo` completed successfully, including the Windows CP-1252 regression path.
- `uv run conductor-nautilus-smoke` passed locally with NautilusTrader **2.0.0rc4**. This does not
  replace the required smoke on the actual Windows trading machine.
- The concurrent-run acquisition regression passed 20 consecutive race-test repetitions.
- Unit tests verify that a nonzero strategy exit, subprocess timeout, and malformed native output
  preserve committed ownership and broker state while recording a failed/timed-out run.
- Unit tests verify that terminal rejection and partial-fill-then-cancel outcomes become blocked
  runs, return a non-success orchestration status, and do not commit virtual ownership.
- The V0.3 demo CLI passes an end-to-end smoke under a forced Windows CP-1252 output stream.
- The current Ruff baseline is not clean; `ruff check src tests --statistics` reported 252 findings.
  The new failure-test module passes its focused Ruff check. Treat the remaining cleanup as
  engineering debt, not as evidence that tests failed.
- No repository evidence yet establishes a successful IBKR paper connection, paper fill,
  live-account shadow comparison, or production cutover for the V0.4 runtime.

Never summarize the current state as production-ready.

## Real, paper, and synthetic boundaries

- `PaperExecutionAdapter` and unit-test broker state are synthetic/local.
- The Nautilus bridge and worker are real integration code but remain operationally unproven until
  exercised against the selected Nautilus build and IBKR paper environment.
- `live_orders_enabled = false` is the required default and represents shadow/no-submit behavior.
- Strategy seed positions and cash are bootstrap assertions supplied by the operator; they are not
  inferred from IBKR.
- The local dashboard is read-only and must not be treated as an execution control surface.

## Known gaps and risks

1. NautilusTrader 2.0.0rc4 imports locally, but the selected package still needs an import and
   platform smoke on the actual Windows trading machine.
2. Production strategy repositories, callable paths, schedules, and output contracts still require
   machine-local wiring and verification.
3. Initial virtual ownership and cash require human approval against the actual IBKR account.
4. Broker connectivity, instrument preload, live marks, and account/NAV publication need an IBKR
   paper smoke.
5. Partial fills, rejection, timeouts, late fills, disconnects, and restart boundaries need
   end-to-end evidence. Terminal rejection and partial-fill-then-cancel are now fail-closed and
   unit-tested, but the real Nautilus/IBKR transitions remain unproven.
6. Concurrent strategy-run acquisition is now protected by an immediate SQLite write transaction
   and a partial unique index, with unit race coverage. Crash-safe execution idempotency after a
   submitted broker request still requires explicit proof.
7. Ledger and bridge schemas need versioned migration and backup/restore procedures before routine
   production operation.
8. The repository-wide lint baseline needs an intentional cleanup pass.
9. The checked-out worktree contains substantial uncommitted V0.4 work; preserve unrelated changes
   and do not assume `HEAD` represents the alpha runtime shown in the files.

## Next work, in priority order

1. Install/confirm the Nautilus extra and pass `conductor-nautilus-smoke` on the actual Windows
   trading machine.
2. Configure IBKR paper with live submission disabled; prove worker health, NAV, positions, prices,
   and instrument resolution.
3. Approve virtual seeds and make `conductor doctor` clean against the controlled account scope.
4. Execute one deliberately small paper order and verify fills, commission, ownership allocation,
   reconciliation, restart recovery, and an empty second cycle.
5. Run every failure drill in the Windows migration guide and fix the discovered gaps. Unit
   coverage exists for concurrent acquisition, nonzero exit, timeout, and malformed output, but
   the operational drills remain required.
6. Wire all three production strategy adapters and compare repeated live-account shadow runs with
   the existing Dagster outputs at the unchanged production schedule.
7. Review the M1 gate and perform a controlled cutover only after all evidence is retained.

## External/operator dependencies

Progress beyond local tests requires:

- access to the actual Windows trading machine;
- a compatible NautilusTrader 2.x installation;
- TWS or IB Gateway and an IBKR paper account;
- approved current holdings, strategy ownership, cash, and allocated-capital records;
- the real ETSA, RPSchteroids, and TLAQ repositories and their production schedules;
- an operator-approved live-account shadow and eventual cutover window.

## Likely files for the next implementation pass

- `src/conductor/adapters/nautilus_ibkr_worker.py`
- `src/conductor/adapters/nautilus_bridge.py`
- `src/conductor/runtime/orchestrator.py`
- `src/conductor/engine.py`
- `src/conductor/accounting.py`
- `src/conductor/ledger.py`
- `tests/test_nautilus_bridge.py`
- `tests/test_orchestrator.py`
- `tests/test_accounting.py`
- `docs/WINDOWS_ETSA_RPS_TLAQ_MIGRATION.md`

Update this file whenever any item moves from implemented to paper-, shadow-, or
production-verified, or when the highest-priority next task changes.
