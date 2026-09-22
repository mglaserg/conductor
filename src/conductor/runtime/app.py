from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from conductor.accounting import VirtualAccountingEngine
from conductor.adapters.nautilus_bridge import NautilusBridgeExecutionAdapter
from conductor.adapters.paper import DurablePaperExecutionAdapter, PaperExecutionAdapter
from conductor.adapters.router import RoutedExecutionAdapter
from conductor.allocation import FallbackAllocator
from conductor.config import RuntimeConfig, load_runtime_config
from conductor.domain.models import ZERO, AggregateTarget, BrokerPosition, InstrumentSpec
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import PortfolioBuilder
from conductor.rebalance import VirtualRebalanceBuffer
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine
from conductor.runtime.orchestrator import StrategyRunOrchestrator, StrategyRunOutcome


class ConductorRuntimeApp:
    def __init__(self, config: RuntimeConfig, *, paper: bool = False) -> None:
        self.config = config
        self.paper_mode = paper
        self.state_db = config.paper_state_db if paper else config.state_db
        self.run_root = config.paper_run_root if paper else config.run_root
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.ledger = ConductorLedger(self.state_db)

        paper_nav = self._configured_paper_nav() if paper else None
        allocation_decision = self._paper_allocation() if paper else None
        initialized_accounts: list[dict[str, str]] = []

        for strategy_id, profile in config.strategies.items():
            self.ledger.ensure_strategy(strategy_id, book_id=profile.book_id)
            if self.ledger.strategy_account(strategy_id, book_id=profile.book_id) is None:
                if paper:
                    assert paper_nav is not None and allocation_decision is not None
                    seed = config.paper_seeds[strategy_id]
                    allocated_capital = (
                        seed.allocated_capital
                        if seed.allocated_capital is not None
                        else paper_nav * allocation_decision.weights.get(strategy_id, ZERO)
                    )
                    cash = seed.cash if seed.cash is not None else allocated_capital
                    positions = seed.positions
                else:
                    seed = config.seeds[strategy_id]
                    allocated_capital = seed.allocated_capital
                    cash = seed.cash
                    positions = seed.positions
                self.ledger.seed_strategy_account(
                    strategy_id,
                    book_id=profile.book_id,
                    route_id=profile.route_id,
                    allocated_capital=allocated_capital,
                    cash=cash,
                )
                self.ledger.seed_virtual_book(
                    strategy_id=strategy_id,
                    book_id=profile.book_id,
                    sleeve_id=profile.sleeve_id,
                    route_id=profile.route_id,
                    positions=positions,
                )
                initialized_accounts.append(
                    {
                        "strategy_id": strategy_id,
                        "book_id": profile.book_id,
                        "allocated_capital": str(allocated_capital),
                        "initial_cash": str(cash),
                    }
                )

        if paper and initialized_accounts:
            assert allocation_decision is not None
            self.ledger.append_event(
                "paper.allocation_decision",
                {
                    "configured_method": config.allocation_method,
                    "resolved_method": allocation_decision.method,
                    "weights": {
                        key: str(value) for key, value in allocation_decision.weights.items()
                    },
                    "diagnostics": dict(allocation_decision.diagnostics),
                    "accounts": initialized_accounts,
                },
            )

        self.route_adapters: dict[str, object] = {}
        lazy_providers: dict[str, object] = {}
        if paper:
            paper_routes = sorted({profile.route_id for profile in config.strategies.values()})
            for route_id in paper_routes:
                self.route_adapters[route_id] = DurablePaperExecutionAdapter(
                    ledger=self.ledger,
                    route_id=route_id,
                    price_provider=self._paper_price,
                    initial_positions=self._paper_broker_positions(route_id),
                )
        else:
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
            if paper:
                return InstrumentSpec(instrument, self._paper_price(instrument))
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
            if paper:
                initial_specs[instrument] = resolve_spec(instrument)
                continue
            provider = lazy_providers.get(row["route_id"])
            if provider is None:
                raise KeyError(
                    f"no price/spec available for seeded position {row['route_id']}/{instrument}"
                )
            initial_specs[instrument] = provider(instrument)  # type: ignore[operator]

        portfolio_nav = paper_nav if paper_nav is not None else self._portfolio_nav()
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
            run_root=self.run_root,
            profiles=config.strategies,
        )

    def _configured_paper_nav(self) -> Decimal:
        if self.config.portfolio_nav is None:
            raise ValueError("paper mode requires node.portfolio_nav")
        return self.config.portfolio_nav

    def _paper_allocation(self):
        configured = self.config.allocation_weights
        if not configured:
            configured = {
                strategy_id: seed.allocated_capital
                for strategy_id, seed in self.config.seeds.items()
                if seed.allocated_capital > ZERO
            }
        if not configured:
            raise ValueError(
                "paper mode requires [portfolio.static.weights] or positive strategy seed capital"
            )
        return FallbackAllocator().allocate(
            self.config.allocation_method,
            configured=configured,
            returns=None,
        )

    def _paper_price(self, instrument: str) -> Decimal:
        return self.config.paper_prices.get(instrument, self.config.paper_default_price)

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
    def from_path(cls, path: str | Path, *, paper: bool = False) -> ConductorRuntimeApp:
        return cls(load_runtime_config(path), paper=paper)

    def resolve_strategy_id(self, strategy_id: str) -> str:
        matches = [
            configured
            for configured in self.config.strategies
            if configured.casefold() == strategy_id.casefold()
        ]
        if len(matches) != 1:
            raise KeyError(f"unknown configured strategy: {strategy_id}")
        return matches[0]

    def run_strategy(self, strategy_id: str, *, trigger: str = "manual") -> StrategyRunOutcome:
        canonical_id = self.resolve_strategy_id(strategy_id)
        profile = self.config.strategies[canonical_id]
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
        payload = {
            "node_id": self.config.node_id,
            "portfolio_nav": str(self.portfolio.portfolio_nav),
            "strategies": strategies,
            "recent_runs": self.ledger.strategy_runs(limit=20),
        }
        if self.paper_mode:
            payload["runtime_mode"] = "paper"
            payload["state_db"] = str(self.state_db)
            payload["paper_broker_positions"] = [
                {
                    "route_id": position.route_id,
                    "instrument": position.instrument,
                    "quantity": str(position.quantity),
                }
                for position in self.engine.execution.positions()
            ]
        return payload

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
