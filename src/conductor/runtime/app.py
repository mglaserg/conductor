from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from conductor.accounting import VirtualAccountingEngine
from conductor.adapters.nautilus_bridge import NautilusBridgeExecutionAdapter
from conductor.adapters.paper import DurablePaperExecutionAdapter, PaperExecutionAdapter
from conductor.adapters.router import RoutedExecutionAdapter
from conductor.allocation import AllocationDecision, FallbackAllocator
from conductor.config import PortfolioConfig, RuntimeConfig, load_runtime_config
from conductor.domain.models import (
    ZERO,
    AggregateTarget,
    BrokerPosition,
    InstrumentSpec,
    VirtualTarget,
)
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import PortfolioBuilder
from conductor.rebalance import VirtualRebalanceBuffer
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine
from conductor.runtime.orchestrator import StrategyRunOrchestrator, StrategyRunOutcome


class ConductorRuntimeApp:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        paper: bool = False,
        route_scope: set[str] | None = None,
    ) -> None:
        self.config = config
        self.paper_mode = paper
        configured_strategy_routes = {profile.route_id for profile in config.strategies.values()}
        if route_scope is None:
            self.route_scope = configured_strategy_routes
        else:
            unknown_routes = set(route_scope) - set(config.routes)
            if unknown_routes:
                raise ValueError(f"unknown route scope: {sorted(unknown_routes)}")
            self.route_scope = set(route_scope)
        self.state_db = config.paper_state_db if paper else config.state_db
        self.run_root = config.paper_run_root if paper else config.run_root
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.ledger = ConductorLedger(self.state_db)

        self.portfolios_by_route: dict[str, PortfolioConfig] = {
            item.route_id: item
            for item in config.portfolios.values()
            if item.route_id in self.route_scope
        }
        self.route_adapters: dict[str, object] = {}
        lazy_providers: dict[str, object] = {}

        # Live broker adapters must exist before broker-backed NAV can be resolved. Paper adapters
        # are created after virtual seed ownership so their synthetic broker starts reconciled.
        if not paper:
            for route_id, route_cfg in config.routes.items():
                if route_id not in self.route_scope:
                    continue
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
                        expected_account_id=route_cfg.route.account,
                    )
                    self.route_adapters[route_id] = adapter
                    lazy_providers[route_id] = adapter.instrument_spec
                else:
                    raise ValueError(f"unsupported adapter {route_cfg.route.adapter!r}")

        self.portfolio_navs = self._resolve_portfolio_navs(paper=paper)
        self.allocation_decisions = self._resolve_allocations()
        initialized_accounts = self._initialize_strategy_accounts(paper=paper)
        self._record_allocation_decisions(initialized_accounts, paper=paper)

        if paper:
            paper_routes = sorted(self.route_scope)
            for route_id in paper_routes:
                self.route_adapters[route_id] = DurablePaperExecutionAdapter(
                    ledger=self.ledger,
                    route_id=route_id,
                    price_provider=self._paper_price,
                    initial_positions=self._paper_broker_positions(route_id),
                )

        def resolve_spec(route_id: str, instrument: str) -> InstrumentSpec:
            if paper:
                return InstrumentSpec(instrument, self._paper_price(instrument))
            provider = lazy_providers.get(route_id)
            if provider is not None:
                return provider(instrument)  # type: ignore[operator]
            if instrument in config.paper_prices:
                return InstrumentSpec(instrument, config.paper_prices[instrument])
            raise KeyError(f"cannot resolve {route_id}/{instrument}: no route provider or price")

        initial_specs = {
            instrument: InstrumentSpec(instrument, price)
            for instrument, price in config.paper_prices.items()
        }
        # A strategy must receive its current account view before its first run. Resolve marks for
        # every seeded/current holding through the route that actually owns that position.
        for row in self.ledger.virtual_positions():
            if row["route_id"] not in self.route_scope:
                continue
            instrument = row["instrument"]
            if instrument in initial_specs:
                continue
            initial_specs[instrument] = resolve_spec(row["route_id"], instrument)

        aggregate_nav = sum(self.portfolio_navs.values(), ZERO)
        if aggregate_nav <= ZERO:
            raise ValueError("configured capital pools must have positive aggregate NAV")

        def strategy_capital(_sleeve_id: str, strategy_id: str, book_id: str) -> Decimal:
            account = self.ledger.strategy_account(strategy_id, book_id=book_id)
            if account is None:
                raise KeyError(f"strategy account not seeded: {strategy_id}/{book_id}")
            return Decimal(account["allocated_capital"])

        self.portfolio = PortfolioBuilder(
            config.allocations,
            initial_specs,
            portfolio_nav=aggregate_nav,
            route_navs=self.portfolio_navs,
            route_instrument_provider=resolve_spec,
            strategy_capital_provider=strategy_capital,
        )
        self.accounting = VirtualAccountingEngine(self.ledger, self.portfolio.instruments)
        execution = RoutedExecutionAdapter(self.route_adapters)  # type: ignore[arg-type]

        gross_limits = {
            route_id: self._pool(route_id).risk.max_gross_leverage
            for route_id in self.portfolio_navs
        }
        net_limits = {
            route_id: self._pool(route_id).risk.max_net_exposure
            for route_id in self.portfolio_navs
        }
        instrument_limits = {
            route_id: self._pool(route_id).risk.max_instrument_nav
            for route_id in self.portfolio_navs
        }
        self.engine = ConductorEngine(
            portfolio=self.portfolio,
            reconciler=DesiredStateReconciler(),
            execution=execution,
            risk=PortfolioRiskEngine(
                self.portfolio_navs,
                max_gross_leverage=gross_limits,
                max_net_exposure=net_limits,
                max_instrument_nav=instrument_limits,
            ),
            order_planner=OrderPlanner(
                portfolio_nav=self.portfolio_navs,
                instruments=self.portfolio.instruments,
                min_trade_nav_bps=config.min_trade_nav_bps,
                route_instrument_provider=resolve_spec,
            ),
            ledger=self.ledger,
            accounting=self.accounting,
            rebalance_buffer=VirtualRebalanceBuffer(
                ledger=self.ledger,
                allocations=config.allocations,
                instruments=self.portfolio.instruments,
            ),
        )
        scoped_profiles = {
            strategy_id: profile
            for strategy_id, profile in config.strategies.items()
            if profile.route_id in self.route_scope
        }
        self.orchestrator = StrategyRunOrchestrator(
            ledger=self.ledger,
            accounting=self.accounting,
            engine=self.engine,
            run_root=self.run_root,
            profiles=scoped_profiles,
        )

    def _pool(self, route_id: str) -> PortfolioConfig:
        try:
            pool = self.portfolios_by_route[route_id]
        except KeyError as exc:
            raise KeyError(f"no capital pool configured for strategy route {route_id}") from exc
        if pool.risk is None:  # defensive; loader always materializes risk defaults
            raise ValueError(f"portfolio {pool.portfolio_id} has no risk configuration")
        return pool

    def _resolve_portfolio_navs(self, *, paper: bool) -> dict[str, Decimal]:
        navs: dict[str, Decimal] = {}
        pools = list(self.portfolios_by_route.values())
        for pool in pools:
            if paper and pool.route_id in self.config.paper_portfolio_navs:
                nav = self.config.paper_portfolio_navs[pool.route_id]
            elif pool.fixed_nav is not None:
                nav = pool.fixed_nav
            elif paper:
                if len(pools) == 1 and self.config.portfolio_nav is not None:
                    nav = self.config.portfolio_nav
                else:
                    raise ValueError(
                        f"paper mode needs paper.portfolio_navs.{pool.route_id} for this "
                        "capital pool (or a numeric portfolio NAV / legacy node.portfolio_nav)"
                    )
            else:
                adapter = self.route_adapters.get(pool.route_id)
                if adapter is not None and hasattr(adapter, "net_liquidation"):
                    nav = adapter.net_liquidation()  # type: ignore[no-any-return]
                elif len(pools) == 1 and self.config.portfolio_nav is not None:
                    # Legacy one-pool configurations may still use node.portfolio_nav.
                    nav = self.config.portfolio_nav
                else:
                    raise ValueError(
                        f"portfolio {pool.portfolio_id} requires broker NAV from route "
                        f"{pool.route_id}, but that route cannot provide NetLiquidation"
                    )
            if nav <= ZERO:
                raise ValueError(f"portfolio {pool.portfolio_id} NAV must be positive")
            navs[pool.route_id] = nav
        return navs

    def _resolve_allocations(self) -> dict[str, AllocationDecision]:
        decisions: dict[str, AllocationDecision] = {}
        for route_id, pool in self.portfolios_by_route.items():
            if not pool.static_weights:
                continue
            decisions[route_id] = FallbackAllocator().allocate(
                pool.allocator,
                configured=pool.static_weights,
                returns=None,
                fallback_order=pool.fallback_order,
            )
        return decisions

    def _initialize_strategy_accounts(self, *, paper: bool) -> list[dict[str, str]]:
        initialized: list[dict[str, str]] = []
        for strategy_id, profile in self.config.strategies.items():
            if profile.route_id not in self.route_scope:
                continue
            self.ledger.ensure_strategy(strategy_id, book_id=profile.book_id)
            live_seed = self.config.seeds[strategy_id]
            paper_seed = self.config.paper_seeds[strategy_id]
            decision = self.allocation_decisions.get(profile.route_id)

            if decision is not None:
                allocated_capital = self.portfolio_navs[profile.route_id] * decision.weights.get(
                    strategy_id, ZERO
                )
            elif paper and paper_seed.allocated_capital is not None:
                allocated_capital = paper_seed.allocated_capital
            elif live_seed.allocated_capital is not None:
                allocated_capital = live_seed.allocated_capital
            else:
                allocated_capital = ZERO

            existing = self.ledger.strategy_account(strategy_id, book_id=profile.book_id)
            if existing is not None:
                if decision is not None:
                    self.ledger.update_strategy_allocation(
                        strategy_id,
                        book_id=profile.book_id,
                        route_id=profile.route_id,
                        allocated_capital=allocated_capital,
                    )
                elif existing["route_id"] != profile.route_id:
                    raise RuntimeError(
                        f"strategy {strategy_id}/{profile.book_id} is persisted on route "
                        f"{existing['route_id']} but config assigns {profile.route_id}; perform an "
                        "explicit account migration/bootstrap before changing route_id"
                    )
                continue

            if paper:
                cash = paper_seed.cash if paper_seed.cash is not None else allocated_capital
                positions = paper_seed.positions or live_seed.positions
            else:
                cash = live_seed.cash if live_seed.cash is not None else ZERO
                positions = live_seed.positions

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
            initialized.append(
                {
                    "strategy_id": strategy_id,
                    "book_id": profile.book_id,
                    "route_id": profile.route_id,
                    "allocated_capital": str(allocated_capital),
                    "initial_cash": str(cash),
                }
            )
        return initialized

    def _record_allocation_decisions(
        self, initialized_accounts: list[dict[str, str]], *, paper: bool
    ) -> None:
        for route_id, decision in sorted(self.allocation_decisions.items()):
            pool = self._pool(route_id)
            event_type = "paper.allocation_decision" if paper else "portfolio.allocation_decision"
            self.ledger.append_event(
                event_type,
                {
                    "portfolio_id": pool.portfolio_id,
                    "route_id": route_id,
                    "portfolio_nav": str(self.portfolio_navs[route_id]),
                    "configured_method": pool.allocator,
                    "resolved_method": decision.method,
                    "weights": {key: str(value) for key, value in decision.weights.items()},
                    "diagnostics": dict(decision.diagnostics),
                    "accounts": [
                        item for item in initialized_accounts if item["route_id"] == route_id
                    ],
                },
            )

    def _paper_price(self, instrument: str) -> Decimal:
        return self.config.paper_prices.get(instrument, self.config.paper_default_price)

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
    def from_path(
        cls,
        path: str | Path,
        *,
        paper: bool = False,
        route_scope: set[str] | None = None,
    ) -> ConductorRuntimeApp:
        return cls(load_runtime_config(path), paper=paper, route_scope=route_scope)

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
        if profile.route_id not in self.route_scope:
            raise RuntimeError(
                f"strategy {canonical_id} is on route {profile.route_id}, outside runtime scope "
                f"{sorted(self.route_scope)}"
            )
        return self.orchestrator.run(profile, trigger=trigger)

    def status(self) -> dict:
        runtime_by_key = {
            (intent.strategy_id, intent.book_id): intent for intent in self.ledger.runtime_intents()
        }
        strategies: list[dict] = []
        for strategy_id, profile in sorted(self.config.strategies.items()):
            if profile.route_id not in self.route_scope:
                continue
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

        portfolios = []
        for route_id, pool in sorted(self.portfolios_by_route.items()):
            decision = self.allocation_decisions.get(route_id)
            portfolios.append(
                {
                    "portfolio_id": pool.portfolio_id,
                    "route_id": route_id,
                    "nav": str(self.portfolio_navs[route_id]),
                    "nav_source": pool.nav_source,
                    "configured_allocator": pool.allocator,
                    "resolved_allocator": None if decision is None else decision.method,
                    "weights": (
                        {}
                        if decision is None
                        else {key: str(value) for key, value in decision.weights.items()}
                    ),
                    "risk": {
                        "max_gross_leverage": str(pool.risk.max_gross_leverage),
                        "max_net_exposure": str(pool.risk.max_net_exposure),
                        "max_instrument_nav": str(pool.risk.max_instrument_nav),
                        "max_margin_utilization": str(pool.risk.max_margin_utilization),
                    },
                }
            )
        payload = {
            "node_id": self.config.node_id,
            "portfolio_nav": str(sum(self.portfolio_navs.values(), ZERO)),
            "portfolios": portfolios,
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

    def bootstrap_route_ownership(
        self,
        route_id: str,
        *,
        ownership: dict[str, dict[str, Decimal]] | None = None,
        commit: bool = False,
    ) -> dict:
        """Plan or commit initial virtual ownership for one live broker route.

        A single-strategy route owns every broker position automatically. Shared routes infer only
        unambiguous ownership from each strategy's latest persisted intent. Any unclaimed or
        multiply-claimed instrument remains unresolved until the operator supplies an explicit
        ownership manifest.
        """
        if self.paper_mode:
            raise ValueError("bootstrap is for live/shadow broker routes, not paper mode")
        if route_id not in self.route_scope:
            raise ValueError(f"bootstrap route {route_id} is outside runtime scope")
        if route_id not in self.route_adapters:
            raise ValueError(f"bootstrap route {route_id} has no execution adapter")

        profiles = {
            strategy_id: profile
            for strategy_id, profile in self.config.strategies.items()
            if profile.route_id == route_id
        }
        if not profiles:
            raise ValueError(f"route {route_id} has no configured strategies")

        existing = [
            row for row in self.ledger.virtual_positions() if row["route_id"] == route_id
        ]
        if existing:
            raise RuntimeError(
                f"route {route_id} already has {len(existing)} virtual positions; bootstrap is "
                "create-only and will not replace existing ownership"
            )

        broker_quantities: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for position in self.route_adapters[route_id].positions():  # type: ignore[attr-defined]
            if position.route_id == route_id:
                broker_quantities[position.instrument] += position.quantity
        broker_quantities = {
            instrument: quantity
            for instrument, quantity in broker_quantities.items()
            if quantity != ZERO
        }

        assigned: dict[str, dict[str, Decimal]] = {strategy_id: {} for strategy_id in profiles}
        unresolved: list[dict[str, object]] = []
        inference = "explicit_manifest" if ownership is not None else "runtime_intents"

        if ownership is not None:
            unknown_strategies = set(ownership) - set(profiles)
            if unknown_strategies:
                raise ValueError(
                    "ownership manifest references strategies outside route "
                    f"{route_id}: {', '.join(sorted(unknown_strategies))}"
                )
            for strategy_id, positions in ownership.items():
                for instrument, quantity in positions.items():
                    quantity = Decimal(str(quantity))
                    if quantity != ZERO:
                        assigned[strategy_id][instrument] = quantity
        elif len(profiles) == 1:
            inference = "single_owner_route"
            strategy_id = next(iter(profiles))
            assigned[strategy_id] = dict(sorted(broker_quantities.items()))
        else:
            latest = {
                intent.strategy_id: intent
                for intent in self.ledger.runtime_intents()
                if intent.route_id == route_id and intent.strategy_id in profiles
            }
            missing_intents = sorted(set(profiles) - set(latest))
            if missing_intents:
                unresolved.append(
                    {
                        "reason": "missing_runtime_intent",
                        "strategies": missing_intents,
                    }
                )

            for instrument, quantity in sorted(broker_quantities.items()):
                claimants = [
                    strategy_id
                    for strategy_id, intent in sorted(latest.items())
                    if intent.targets.get(instrument, ZERO) != ZERO
                ]
                if len(claimants) == 1:
                    assigned[claimants[0]][instrument] = quantity
                else:
                    unresolved.append(
                        {
                            "reason": "unclaimed" if not claimants else "ambiguous",
                            "instrument": instrument,
                            "broker_quantity": str(quantity),
                            "claimants": claimants,
                            "source_targets": {
                                strategy_id: str(latest[strategy_id].targets[instrument])
                                for strategy_id in claimants
                            },
                        }
                    )

        assigned_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for positions in assigned.values():
            for instrument, quantity in positions.items():
                assigned_totals[instrument] += quantity
        manifest_differences = []
        for instrument in sorted(set(broker_quantities) | set(assigned_totals)):
            broker_quantity = broker_quantities.get(instrument, ZERO)
            assigned_quantity = assigned_totals.get(instrument, ZERO)
            if broker_quantity != assigned_quantity:
                manifest_differences.append(
                    {
                        "instrument": instrument,
                        "broker_quantity": str(broker_quantity),
                        "assigned_quantity": str(assigned_quantity),
                        "difference": str(broker_quantity - assigned_quantity),
                    }
                )

        committable = not unresolved and not manifest_differences
        cash_by_owner: dict[tuple[str, str], Decimal] = {}
        position_rows: list[VirtualTarget] = []
        strategy_details: dict[str, dict[str, object]] = {}

        if committable:
            adapter = self.route_adapters[route_id]
            warm = getattr(adapter, "warm_instruments", None)
            if callable(warm) and broker_quantities:
                warm(sorted(broker_quantities))

            specs: dict[str, InstrumentSpec] = {}
            provider = getattr(adapter, "instrument_spec", None)
            if not callable(provider):
                raise ValueError(f"route {route_id} cannot provide instrument marks for bootstrap")
            for instrument in sorted(broker_quantities):
                spec = provider(instrument)
                specs[instrument] = spec
                self.portfolio.instruments[instrument] = spec

            for strategy_id, profile in sorted(profiles.items()):
                account = self.ledger.strategy_account(strategy_id, book_id=profile.book_id)
                if account is None:
                    raise KeyError(f"strategy account not seeded: {strategy_id}/{profile.book_id}")
                allocated_capital = Decimal(account["allocated_capital"])
                net_notional = ZERO
                for instrument, quantity in sorted(assigned[strategy_id].items()):
                    notional = quantity * specs[instrument].unit_notional
                    net_notional += notional
                    position_rows.append(
                        VirtualTarget(
                            strategy_id=strategy_id,
                            book_id=profile.book_id,
                            sleeve_id=profile.sleeve_id,
                            route_id=route_id,
                            instrument=instrument,
                            target=quantity,
                            notional=notional,
                        )
                    )
                cash = allocated_capital - net_notional
                cash_by_owner[(strategy_id, profile.book_id)] = cash
                strategy_details[strategy_id] = {
                    "book_id": profile.book_id,
                    "allocated_capital": str(allocated_capital),
                    "position_count": len(assigned[strategy_id]),
                    "net_position_notional": str(net_notional),
                    "bootstrap_cash": str(cash),
                    "positions": {
                        instrument: str(quantity)
                        for instrument, quantity in sorted(assigned[strategy_id].items())
                    },
                }

        result: dict[str, object] = {
            "route_id": route_id,
            "mode": "commit" if commit else "dry_run",
            "inference": inference,
            "broker_position_count": len(broker_quantities),
            "strategies": strategy_details,
            "ownership": {
                strategy_id: {
                    instrument: str(quantity)
                    for instrument, quantity in sorted(positions.items())
                }
                for strategy_id, positions in sorted(assigned.items())
            },
            "unresolved": unresolved,
            "manifest_differences": manifest_differences,
            "committable": committable,
            "committed": False,
        }

        if commit:
            if not committable:
                raise ValueError(
                    f"bootstrap plan for {route_id} is not committable; resolve every ownership "
                    "ambiguity and quantity difference first"
                )
            self.ledger.bootstrap_route_ownership(
                route_id=route_id,
                positions=position_rows,
                cash_by_owner=cash_by_owner,
            )
            reconciliation = self.bootstrap_reconciliation()
            if not reconciliation["reconciled"]:
                raise RuntimeError(
                    f"bootstrap commit for {route_id} did not reconcile to broker positions"
                )
            result["committed"] = True
            result["reconciliation"] = reconciliation
            self.ledger.append_event(
                "bootstrap.route_completed",
                {
                    "route_id": route_id,
                    "inference": inference,
                    "broker_position_count": len(broker_quantities),
                    "strategies": strategy_details,
                },
            )
        else:
            self.ledger.append_event("bootstrap.route_planned", result)
        return result

    def bootstrap_reconciliation(self) -> dict:
        quantities: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for row in self.ledger.virtual_positions():
            if row["route_id"] not in self.route_scope:
                continue
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
