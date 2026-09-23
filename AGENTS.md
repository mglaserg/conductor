# Conductor agent guide

This file is the operating contract for Codex and other coding agents working in this repository.

## Read this first

Before changing code, read these files in order:

1. `PROJECT_STATUS.md` — current state, verification level, known gaps, and next objective.
2. `ROADMAP.md` — prioritized capability gates and the active milestone.
3. `docs/ARCHITECTURE.md` — system boundaries, data flow, state ownership, and extension rules.
4. `docs/SAFETY_INVARIANTS.md` — rules that execution-related changes must preserve.
5. `README.md` — operator-facing runtime overview and deployment topology.
6. `docs/WINDOWS_ETSA_RPS_TLAQ_MIGRATION.md` — production migration and safety procedure.
7. `docs/TARGET_SNAPSHOT_PROTOCOL.md` — the frozen V0.3 strategy-to-Conductor contract.
8. Relevant decisions under `docs/adr/` and tests beside the subsystem being changed.

The repository is the durable source of truth. Do not rely on chat history when repository
documentation disagrees with it.

## Product mission

Conductor is the portfolio control plane and strategy runtime for independent trading strategies.

Strategies decide their desired economic state. Conductor owns strategy accounts, allocated
capital, portfolio risk, virtual ownership, internal crossing, broker-level netting, lifecycle,
audit, and desired broker state. NautilusTrader owns broker/exchange connectivity, instrument
loading, orders, fills, and broker reconciliation.

The current production objective is the Windows equities migration for ETSA, RPSchteroids, and
TLAQ. ETSA/RPSchteroids share one IBKR capital pool/account while TLAQ may use a separate IBKR
account; both are configured on the same Windows node.

## Non-negotiable architecture boundaries

- Strategies emit targets or native results; they do not place broker orders through Conductor
  integrations.
- Strategy ownership is virtual and must remain attributable even when physical broker positions
  are netted.
- The aggregate broker account is never used to guess strategy ownership.
- Native target weights, target quantities, and position deltas are normalized at the adapter
  boundary. Delta semantics must not escape that boundary or be replayed twice.
- Internal crossing happens before external execution. Only the residual aggregate delta reaches
  the broker.
- Desired state and revision handling must remain idempotent.
- Virtual positions and cash move only from reconciled execution facts. Never invent fills or
  commit ownership merely because an order was submitted.
- Every physical position on a Conductor-controlled account must be modeled, preloaded for
  reconciliation, or treated as a blocking exception.
- NautilusTrader is the primary IBKR execution kernel. Do not build a second TWS order/fill state
  machine inside Conductor.
- The local Conductor-to-Nautilus bridge is durable SQLite/WAL IPC. Do not add Redis, RabbitMQ, or a
  network service without a demonstrated workload that requires it.
- Windows and Lubuntu nodes remain independently operable. A future global dashboard receives
  outbound telemetry and must never become an execution dependency or shared source of truth.
- The V0.3 Target Snapshot Protocol is frozen. Make compatible extensions deliberately; do not
  silently change its schemas or replay semantics.

## Safety rules

- Keep `live_orders_enabled = false` in examples and new configurations. Enabling live execution
  is an explicit operator action after the migration gates pass.
- Preserve the audit ledger, run artifacts, bridge history, and reconciliation evidence during
  failures and rollback. Recovery is not a database reset.
- Treat lifecycle operations as economic actions: disabling preserves ownership; retirement must
  flatten and reconcile before becoming final.
- Default to fail-closed behavior for stale workers, missing prices, unknown instruments,
  unmodeled broker positions, malformed strategy output, and ambiguous route ownership.
- Never put account numbers, credentials, tokens, or production-only paths into committed examples.
- Do not weaken a safety check merely to make a smoke test or dashboard appear healthy.

## Design principles

- Prefer explicit domain models and small interfaces over framework-heavy abstractions.
- Keep strategy logic independent from orchestration and execution infrastructure.
- Model desired state, not imperative trade sequences.
- Make retries, restarts, and repeated identical runs safe by construction.
- Persist important decisions and transitions; nothing important should exist only in memory or on
  the dashboard.
- Keep routing explicit. Positions on different routes do not net or share capital/risk capacity
  merely because their display symbols match.
- Add infrastructure only when the local, low-frequency workload proves it is necessary.
- Favor deterministic behavior and explainable risk decisions over hidden heuristics.

## Repository map

- `src/conductor/domain/` — core domain types and invariants.
- `src/conductor/protocol/` — frozen target-snapshot schemas, acceptance, conversion, and SDK.
- `src/conductor/runtime/` — strategy subprocess orchestration and native-result normalization.
- `src/conductor/adapters/` — paper execution, routing, bridge IPC, and Nautilus worker boundary.
- `src/conductor/ledger.py` — durable strategy, ownership, audit, and runtime state.
- `src/conductor/accounting.py` — virtual books, internal crosses, cash, and fill allocation.
- `src/conductor/portfolio.py`, `risk.py`, `rebalance.py`, `orders.py`, and `reconcile.py` — the
  portfolio decision pipeline.
- `examples/migration/` — adapters for existing production strategies; these are integration
  templates, not replacement strategy implementations.
- `scripts/windows/` — Task Scheduler and persistent-worker helpers.
- `tests/` — executable behavioral contract for the control plane.

## How to choose work

Unless the user explicitly asks for something else:

1. Take the highest-priority unblocked item in `ROADMAP.md`.
2. Prefer completing one end-to-end safety or capability slice over scattering partial changes
   across milestones.
3. Protect the active Windows migration before expanding to more asset classes, brokers, or nodes.
4. Use the failure drills and promotion gate for the active milestone as acceptance criteria.

If a request conflicts with an architecture boundary above, do not silently work around it. Call
out the conflict and update the durable documentation in the same change if the decision changes.

## Definition of done

A meaningful feature is not done merely because code exists. Include, as applicable:

- implementation through the real runtime path;
- focused unit tests and a regression test for the failure mode;
- persistence and audit coverage for important state transitions;
- explicit timeout, retry, empty, stale, and partial-failure behavior;
- safe configuration defaults;
- CLI or dashboard wiring without bypassing domain rules;
- documentation and example updates;
- an updated milestone or scope line in `ROADMAP.md` when capability status changes;
- verification commands actually run and reported accurately.

Keep repository memory current in the same change:

- update `PROJECT_STATUS.md` when implemented, verified, blocked, or next-work state changes;
- update `ROADMAP.md` when milestone scope or promotion status changes;
- add a concise `CHANGELOG.md` entry under `Unreleased` for meaningful behavior changes;
- add or supersede an ADR when changing a durable architectural decision.

Execution-related work also needs restart and idempotency reasoning. A successful mocked submission
is not evidence that fills, commissions, partial fills, rejection, recovery, and reconciliation are
correct.

## Verification

Core baseline:

```powershell
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
```

Protocol and CLI smoke:

```powershell
uv run conductor demo
uv run conductor schemas schemas
uv run conductor validate path\to\target-snapshot.json
```

Use a real generated snapshot path for `validate`.

Windows/Nautilus work additionally requires the optional runtime and the paper/shadow checks in the
migration guide:

```powershell
uv sync --extra dev --extra nautilus --prerelease allow
uv run conductor-nautilus-smoke
uv run conductor worker-status ibkr_main --config conductor.toml
uv run conductor worker-status ibkr_tlaq --config conductor.toml
uv run conductor doctor --config conductor.toml
```

Never claim a check passed unless it was actually run. If the environment prevents a check, report
that limitation separately from code correctness.

## Repository hygiene

- Preserve unrelated user changes in a dirty worktree.
- Keep generated runtime data, SQLite files, logs, secrets, and local account configuration out of
  Git.
- Keep sample and paper data clearly labeled; never let them masquerade as live broker state.
- Avoid compatibility layers for abandoned designs unless an explicit migration requires them.
- Add dependencies intentionally and document architectural additions in the roadmap or a focused
  design note.
- Prefer additive ledger migrations and explicit backfills over destructive state rewrites.

## Handing work to the next agent

Leave the repository clear enough that a fresh agent can answer:

- What is implemented in code?
- What has been verified only in unit tests, in paper, in shadow, or in production?
- What is the next active promotion gate?
- What is blocked and why?
- Which operator action, configuration, or external system is required next?

Do not blur those verification levels in handoff notes or roadmap updates.
