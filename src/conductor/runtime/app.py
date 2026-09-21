from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from conductor.accounting import VirtualAccountingEngine
from conductor.adapters.nautilus_bridge import NautilusBridgeExecutionAdapter
from conductor.adapters.paper import PaperExecutionAdapter
from conductor.adapters.router import RoutedExecutionAdapter
from conductor.config import RuntimeConfig, load_runtime_config
from conductor.domain.models import AggregateTarget, BrokerPosition, InstrumentSpec, ZERO
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import PortfolioBuilder
from conductor.rebalance import VirtualRebalanceBuffer
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine
from conductor.runtime.orchestrator import StrategyRunOrchestrator, StrategyRunOutcome


class ConductorRuntimeApp:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        config.state_db.parent.mkdir(parents=True, exist_ok=True)
        config.run_root.mkdir(parents=True, exist_ok=True)
        self.ledger = ConductorLedger(config.state_db)

        for strategy_id, profile in config.strategies.items():
            self.ledger.ensure_strategy(strategy_id, book_id=profile.book_id)
            if self.ledger.strategy_account(strategy_id, book_id=profile.book_id) is None:
                seed = config.seeds[strategy_id]
                self.ledger.seed_strategy_account(
                    strategy_id,
                    book_id=profile.book_id,
                    route_id=profile.route_id,
                    allocated_capital=seed.allocated_capital,
                    cash=seed.cash,
                )
                self.ledger.seed_virtual_book(
                    strategy_id=strategy_id,
                    book_id=profile.book_id,
                    sleeve_id=profile.sleeve_id,
                    route_id=profile.route_id,
                    positions=seed.positions,
                )

        self.route_adapters: dict[str, object] = {}
        lazy_providers: dict[str, object] = {}
        for route_id, route_cfg in config.routes.items():
            adapter_name = route_cfg.route.adapter.lower()
            if adapter_name == "paper":
                self.route_adapters[route_id] = PaperExecutionAdapter(
                    self._paper_broker_positions(route_id)
                )
            elif adapter_name == "nautilus_ibkr":
                if route_cfg.bridge_db is None:
                    raise ValueError(f"route {route_id} requires bridge_db")
                adapter = NautilusBridgeExecutionAdapter(
                    bridge_db=route_cfg.bridge_db,
                    route_id=route_id,
                    live_orders_enabled=route_cfg.live_orders_enabled,
                    worker_stale_after_seconds=route_cfg.worker_stale_after_seconds,
                    request_timeout_seconds=route_cfg.request_timeout_seconds,
                )
                self.route_adapters[route_id] = adapter
                lazy_providers[route_id] = adapter.instrument_spec
            else:
                raise ValueError(f"unsupported adapter {route_cfg.route.adapter!r}")

        def resolve_spec(instrument: str) -> InstrumentSpec:
            # A canonical instrument must resolve on exactly one local route for V0.4.
            candidate_routes = {
                profile.route_id
                for profile in config.strategies.values()
                if profile.route_id in lazy_providers
            }
            if len(candidate_routes) == 1:
                route_id = next(iter(candidate_routes))
                return lazy_providers[route_id](instrument)  # type: ignore[operator]
            if instrument in config.paper_prices:
                return InstrumentSpec(instrument, config.paper_prices[instrument])
            raise KeyError(
                f"cannot resolve {instrument}: configure paper price or a unique live route provider"
            )

        initial_specs = {
            instrument: InstrumentSpec(instrument, price)
            for instrument, price in config.paper_prices.items()
        }
        # A strategy must be able to receive its current account view before its first
        # Conductor run. Resolve marks for every seeded/current holding up front.
        for row in self.ledger.virtual_positions():
            instrument = row["instrument"]
            if instrument in initial_specs:
                continue
            provider = lazy_providers.get(row["route_id"])
            if provider is None:
                raise KeyError(
                    f"no price/spec available for seeded position {row['route_id']}/{instrument}"
                )
            initial_specs[instrument] = provider(instrument)  # type: ignore[operator]

        portfolio_nav = self._portfolio_nav()
        def strategy_capital(_sleeve_id: str, strategy_id: str, book_id: str) -> Decimal:
            account = self.ledger.strategy_account(strategy_id, book_id=book_id)
            if account is None:
                raise KeyError(f"strategy account not seeded: {strategy_id}/{book_id}")
            return Decimal(account["allocated_capital"])

        self.portfolio = PortfolioBuilder(
            config.allocations,
            initial_specs,
            portfolio_nav=portfolio_nav,
            instrument_provider=resolve_spec,
            strategy_capital_provider=strategy_capital,
        )
        self.accounting = VirtualAccountingEngine(self.ledger, self.portfolio.instruments)
        execution = RoutedExecutionAdapter(self.route_adapters)  # type: ignore[arg-type]
        self.engine = ConductorEngine(
            portfolio=self.portfolio,
            reconciler=DesiredStateReconciler(),
            execution=execution,
            risk=PortfolioRiskEngine(
                portfolio_nav,
                max_gross_leverage=config.max_gross_leverage,
                max_instrument_nav=config.max_instrument_nav,
            ),
            order_planner=OrderPlanner(
                portfolio_nav=portfolio_nav,
                instruments=self.portfolio.instruments,
                min_trade_nav_bps=config.min_trade_nav_bps,
                instrument_provider=resolve_spec,
            ),
            ledger=self.ledger,
            accounting=self.accounting,
            rebalance_buffer=VirtualRebalanceBuffer(
                ledger=self.ledger,
                allocations=config.allocations,
                instruments=self.portfolio.instruments,
            ),
        )
        self.orchestrator = StrategyRunOrchestrator(
            ledger=self.ledger,
            accounting=self.accounting,
            engine=self.engine,
            run_root=config.run_root,
            profiles=config.strategies,
        )

    def _portfolio_nav(self) -> Decimal:
        if self.config.nav_source_route:
            adapter = self.route_adapters.get(self.config.nav_source_route)
            if adapter is None or not hasattr(adapter, "net_liquidation"):
                raise ValueError(
                    f"nav_source_route {self.config.nav_source_route} cannot provide NetLiquidation"
                )
            return adapter.net_liquidation()  # type: ignore[no-any-return]
        if self.config.portfolio_nav is None:
            raise ValueError("node.portfolio_nav or node.nav_source_route is required")
        return self.config.portfolio_nav

    def _paper_broker_positions(self, route_id: str) -> list[BrokerPosition]:
        aggregate: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for row in self.ledger.virtual_positions():
            if row["route_id"] == route_id:
                aggregate[row["instrument"]] += Decimal(row["quantity"])
        return [
            BrokerPosition(instrument, quantity, route_id)
            for instrument, quantity in sorted(aggregate.items())
            if quantity != ZERO
        ]

    @classmethod
    def from_path(cls, path: str | Path) -> "ConductorRuntimeApp":
        return cls(load_runtime_config(path))

    def run_strategy(self, strategy_id: str, *, trigger: str = "manual") -> StrategyRunOutcome:
        try:
            profile = self.config.strategies[strategy_id]
        except KeyError as exc:
            raise KeyError(f"unknown configured strategy: {strategy_id}") from exc
        return self.orchestrator.run(profile, trigger=trigger)

    def status(self) -> dict:
        runtime_by_key = {
            (intent.strategy_id, intent.book_id): intent for intent in self.ledger.runtime_intents()
        }
        strategies: list[dict] = []
        for strategy_id, profile in sorted(self.config.strategies.items()):
            account = self.accounting.account_view(strategy_id, book_id=profile.book_id)
            current = runtime_by_key.get((strategy_id, profile.book_id))
            strategies.append(
                {
                    "strategy_id": strategy_id,
                    "book_id": profile.book_id,
                    "lifecycle": self.ledger.strategy_lifecycle(
                        strategy_id, book_id=profile.book_id
                    ),
                    "route_id": profile.route_id,
                    "result_mode": profile.result_mode.value,
                    "allocated_capital": str(account.allocated_capital),
                    "cash": str(account.cash),
                    "equity": str(account.equity),
                    "gross_exposure": str(account.gross_exposure),
                    "net_exposure": str(account.net_exposure),
                    "positions": {k: str(v) for k, v in account.positions.items()},
                    "target_revision": None if current is None else current.revision,
                    "target_as_of": None if current is None else current.as_of.isoformat(),
                }
            )
        return {
            "node_id": self.config.node_id,
            "portfolio_nav": str(self.portfolio.portfolio_nav),
            "strategies": strategies,
            "recent_runs": self.ledger.strategy_runs(limit=20),
        }

    def bootstrap_reconciliation(self) -> dict:
        quantities: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for row in self.ledger.virtual_positions():
            quantities[(row["route_id"], row["instrument"])] += Decimal(row["quantity"])
        desired = [
            AggregateTarget(
                instrument=instrument,
                route_id=route_id,
                target=quantity,
                notional=ZERO,
            )
            for (route_id, instrument), quantity in sorted(quantities.items())
            if quantity != ZERO
        ]
        actual = self.engine.execution.positions()
        deltas = self.engine.reconciler.reconcile(desired, actual)
        result = {
            "reconciled": not deltas,
            "virtual_position_count": len(desired),
            "broker_position_count": len(actual),
            "differences": [
                {
                    "route_id": delta.route_id,
                    "instrument": delta.instrument,
                    "virtual_expected": str(delta.desired),
                    "broker_actual": str(delta.current),
                    "difference": str(delta.delta),
                }
                for delta in deltas
            ],
        }
        self.ledger.append_event("bootstrap.reconciliation_checked", result)
        return result

    def close(self) -> None:
        for adapter in self.route_adapters.values():
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
