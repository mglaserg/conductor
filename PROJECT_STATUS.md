# Conductor project status

Last reviewed: **2026-09-23**

This is the canonical handoff document for the repository's current state. `ROADMAP.md` describes
where the product is going; this file describes what is true now and what should happen next.

## Current objective

Promote the V0.4 Windows migration runtime from locally implemented alpha code to an operationally
proven replacement for Dagster orchestration and broker execution for ETSA, RPSchteroids, and TLAQ.

- NautilusTrader `2.0.0rc4` and Nautech's `nautilus_ibapi` account-summary client are **required core runtime dependencies**. Plain `uv sync` installs both; there is no separate Nautilus extra.

The current Windows topology is intentionally multi-account in one configuration:

- `ibkr_main`: ETSA + RPSchteroids share one independently funded IBKR capital pool;
- `ibkr_tlaq`: TLAQ uses a separate independently funded IBKR capital pool;
- both live under one `conductor.toml` and one Conductor virtual-ownership ledger;
- one persistent Nautilus worker/bridge is required per physical IBKR route/account.

Conductor must not receive production execution authority until the M1 gate in `ROADMAP.md` and the
cutover checklist in `docs/WINDOWS_ETSA_RPS_TLAQ_MIGRATION.md` are satisfied.

## What works now

### Frozen V0.3 foundation

- strict Linear Target Snapshot and Structure Target Snapshot models;
- schema validation, profile validation, staleness checks, and payload hashing;
- idempotent acceptance with event-ID collision detection;
- independent per-book revision handling and complete-snapshot semantics;
- filesystem producer SDK and inbox processing;
- durable SQLite ledger and deterministic local paper execution path.

### V0.4 alpha runtime

- TOML-driven node, route, strategy, risk, seed and execution configuration;
- **route-backed named capital pools** using `[portfolio.<route_id>]` in one file;
- broker `NetLiquidation` as the default live/shadow NAV source independently for each route;
- exact-account direct IB `reqAccountSummary(NetLiquidation)` fallback when Nautilus has the right
  account but no usable NAV, with off-loop refresh, staleness expiry and strict account filtering;
- per-pool static/inverse-vol/ERC selection and deterministic fallback configuration;
- allocator-derived strategy capital budgets refreshed from each route's NAV;
- per-route gross, net and single-instrument risk scaling;
- per-route trade-size/dust thresholds;
- route-scoped strategy startup/reconciliation so an unrelated account worker is not a dependency
  of another account's strategy run;
- all-node `status` and `doctor` paths that intentionally inspect every configured route;
- one worker/bridge per IBKR account with distinct client IDs/bridge DBs;
- configured-vs-worker-reported IBKR account-ID validation that fails closed;
- one-shot strategy subprocess orchestration with persisted run inputs and outputs;
- native `target_weights`, `target_quantities`, and `position_deltas` result modes;
- conversion of strategy deltas to absolute desired state at the adapter boundary;
- virtual strategy accounts with positions, allocated capital, and positive or negative cash;
- internal crossing and aggregate broker netting **only within a route/account**;
- route-scoped ledger target/position commits so a run cannot delete ownership on another account;
- persisted-route protection: changing a live strategy's `route_id` requires explicit migration and
  is refused as a hot config edit;
- separate multi-account offline paper NAVs through `[paper.portfolio_navs]` without touching live
  broker-NAV behavior;
- strategy lifecycle, status, doctor, worker-status, dashboard, Windows scheduler and migration
  helpers;
- `start_nautilus_workers.bat` for the current two-route Windows topology.

These statements describe implemented code and local automated verification, not completed IBKR
paper, shadow, or production validation.

## Verification state

- Full automated suite on 2026-09-23: **72 passed**.
- New multi-account coverage verifies:
  - independent broker NAVs and 85/15 + 100% strategy budgeting;
  - exact strategy membership/weight validation per route;
  - no cross-account execution or virtual-position deletion;
  - route-specific risk scaling and trade buffers;
  - route-scoped startup that does not construct an unrelated account adapter;
  - multi-account offline-paper NAV overrides without constructing live adapters;
  - rejection of a Nautilus worker connected to the wrong configured account;
  - namespaced Nautilus `AccountId` resolution from the live cache before account-scoped NAV lookup;
  - raw `NetLiquidation` extraction from the reported account event before portfolio valuation, so
    an empty initial IB margin snapshot cannot trigger Nautilus's currency panic;
  - direct IB `reqAccountSummary(NetLiquidation)` fallback accepting only the exact configured
    native account, including rejection of other-account and conflicting-account-summary values;
  - account-balance fallback and fail-closed behavior when the configured IB account is not the
    account registered in Nautilus.
- Existing tests continue to cover protocol, ledger, portfolio construction, accounting,
  orchestration, bridge behavior, failure handling, paper runtime and reconciliation.
- `live_orders_enabled = false` remains the required default in the Windows configuration.
- Windows evidence now establishes that `ibkr_main` can start and `ibkr_tlaq` reaches the intended
  TWS session and native IB account. Clean per-route NAV/readiness, paper fills, live-account shadow
  comparison, and production cutover are still unproven.

Never summarize the current state as production-ready.

## Real, paper, and shadow boundaries

- CLI `--paper` is synthetic/local and uses a separate SQLite state database; it does not construct
  Nautilus/IBKR route adapters.
- In a multi-pool config, `[paper.portfolio_navs]` supplies independent synthetic NAVs per route.
- Live/shadow named pools use broker NetLiquidation unless a fixed numeric pool NAV is configured.
- `live_orders_enabled = false` is shadow/no-submit behavior, not synthetic paper.
- Strategy seed positions/cash are bootstrap assertions supplied by the operator; they are not
  inferred from IBKR.
- For named pools, strategy allocated capital is derived from pool NAV × allocator weight rather
  than being manually seeded.
- The dashboard is read-only and is not an execution control surface.

## Known gaps and risks

1. NautilusTrader `2.0.0rc4` has an upstream IB reconciliation defect in historical fill lookup:
   it submits the namespaced Nautilus account ID (for example `IB-U123`) to IB instead of the
   native account code. IB returns error 321. Keep `live_orders_enabled = false` until that fill
   reconciliation path is fixed or Conductor moves to a verified patched Nautilus build.
2. Both physical IBKR routes still need clean worker smokes proving non-null NAV, positions,
   instruments, marks, restart recovery and stale-worker blocking. TLAQ reaches its intended
   TWS/account; a7 adds a direct exact-account NetLiquidation fallback specifically for the empty
   Nautilus AccountState observed there, but that path still requires Windows/TWS validation.
3. Initial virtual positions and cash require human approval against the correct physical account.
4. `max_margin_utilization` is represented in configuration/status but is **not yet enforced**;
   current worker state does not publish the required margin-utilization measurement.
5. Partial fills, rejection, timeouts, late fills, disconnects and restart boundaries still need
   real Nautilus/IBKR evidence despite deterministic unit coverage for several terminal cases.
6. Ledger and bridge schemas need versioned migration and backup/restore procedures before routine
   production operation.
7. Crash-safe broker execution idempotency after a submitted request still requires operational
   proof.
8. Runtime allocator history is not yet wired to attributable strategy return history, so
   inverse-vol/ERC configured in the live runtime currently rely on fallback when history is absent.
9. Repository-wide Ruff debt predates this change and still needs an intentional cleanup pass.
10. A broker/account move is deliberately not automated. Physical transfer/re-establishment and
    virtual-book migration/bootstrap must be reviewed explicitly before changing a persisted route.

## Next work, in priority order

1. Run plain `uv sync` and pass `conductor-nautilus-smoke` on the actual Windows
   trading machine.
2. Start both IBKR paper workers and make both `worker-status` commands clean, including account-ID
   match.
3. Verify per-account NetLiquidation and resulting ETSA/RPS/TLAQ capital budgets.
4. Approve initial virtual ownership/cash and make all-route `conductor doctor` clean.
5. Prove one deliberately small paper order independently on `ibkr_main` and `ibkr_tlaq`, including
   fills, commissions, reconciliation, restart recovery and an empty repeated cycle.
6. Repeat the failure drills in the migration guide, especially wrong-account, unrelated-worker
   outage and same-symbol/different-account isolation.
7. Wire all production strategy adapters and compare repeated live shadow runs against Dagster at
   the unchanged schedules.
8. Add enforceable account margin-state publication/guard before production promotion.
9. Review the M1 gate and perform controlled execution-authority cutover only after retained paper
   and shadow evidence is complete.

## External/operator dependencies

Progress beyond local verification requires:

- access to the actual Windows trading machine;
- a compatible NautilusTrader 2.x installation;
- TWS or IB Gateway with the intended IBKR paper/live accounts;
- approved physical holdings and per-strategy virtual ownership/cash;
- the real ETSA, RPSchteroids, and TLAQ repositories and unchanged production schedules;
- an operator-approved shadow/cutover window.

Update this file whenever a capability moves from implemented to paper-, shadow-, or
production-verified.
