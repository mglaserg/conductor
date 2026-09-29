from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from conductor.accounting import VirtualAccountingEngine
from conductor.adapters.nautilus_bridge import NautilusBridgeExecutionAdapter
from conductor.adapters.paper import DurablePaperExecutionAdapter, PaperExecutionAdapter
from conductor.adapters.router import RoutedExecutionAdapter
from conductor.allocation import (
    AllocationDecision,
    ERCAllocator,
    FallbackAllocator,
    InverseVolAllocator,
)
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
from conductor.policy import StrategyPolicyEngine
from conductor.protocol.release import StrategyRelease, StrategyReleaseState
from conductor.portfolio_policy import PortfolioPolicyEngine
from conductor.portfolio import PortfolioBuilder
from conductor.rebalance import VirtualRebalanceBuffer
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine
from conductor.runtime.models import NativeResultMode
from conductor.runtime.orchestrator import StrategyRunOrchestrator, StrategyRunOutcome
from conductor.shadow import ShadowOwnerProfile, infer_shadow_ownership


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

        self._initialize_strategy_releases()
        if not paper:
            for row in self.ledger.shadow_routes(active_only=True):
                route_id = row["route_id"]
                if route_id not in self.route_scope:
                    continue
                route_cfg = config.routes[route_id]
                if route_cfg.live_orders_enabled:
                    raise ValueError(
                        f"route {route_id} has active external shadow authority but "
                        "live_orders_enabled=true; disable Conductor orders before shadowing"
                    )
            self._validate_live_release_authority()

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
        for row in self.ledger.implementation_positions(route_ids=self.route_scope):
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
        risk_engine = PortfolioRiskEngine(
            self.portfolio_navs,
            max_gross_leverage=gross_limits,
            max_net_exposure=net_limits,
            max_instrument_nav=instrument_limits,
        )
        rebalance_buffer = VirtualRebalanceBuffer(
            ledger=self.ledger,
            allocations=config.allocations,
            instruments=self.portfolio.instruments,
            strategy_policies=config.strategy_policies,
        )
        strategy_policy = StrategyPolicyEngine(
            config.strategy_policies,
            capital_provider=strategy_capital,
        )
        portfolio_policy = PortfolioPolicyEngine(
            strategy_policy=strategy_policy,
            risk=risk_engine,
            rebalance_buffer=rebalance_buffer,
        )
        self.engine = ConductorEngine(
            portfolio=self.portfolio,
            reconciler=DesiredStateReconciler(),
            execution=execution,
            risk=risk_engine,
            order_planner=OrderPlanner(
                portfolio_nav=self.portfolio_navs,
                instruments=self.portfolio.instruments,
                min_trade_nav_bps=config.min_trade_nav_bps,
                route_instrument_provider=resolve_spec,
            ),
            ledger=self.ledger,
            accounting=self.accounting,
            rebalance_buffer=rebalance_buffer,
            strategy_policy=strategy_policy,
            portfolio_policy=portfolio_policy,
            external_authority_routes={
                row["route_id"]
                for row in self.ledger.shadow_routes(active_only=True)
                if row["route_id"] in self.route_scope
            },
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
            shadow_refresh=self._refresh_active_shadow_after_intent,
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
            route_strategies = {
                strategy_id
                for strategy_id, profile in self.config.strategies.items()
                if profile.route_id == route_id
            }
            risk_budgets = {
                strategy_id: self.config.strategy_policies[strategy_id].risk_budget
                for strategy_id in route_strategies
            }
            allocator = FallbackAllocator(
                inverse_vol=InverseVolAllocator(
                    ewma_lambda=pool.inverse_vol_ewma_lambda,
                    min_observations=pool.inverse_vol_min_observations,
                    lookback_observations=pool.inverse_vol_lookback_observations,
                ),
                erc=ERCAllocator(
                    min_observations=pool.erc_min_observations,
                    lookback_observations=pool.erc_lookback_observations,
                    covariance_estimator=pool.covariance_estimator,
                ),
            )
            decisions[route_id] = allocator.allocate(
                pool.allocator,
                configured=pool.static_weights,
                returns=None,
                risk_budgets=risk_budgets,
                fallback_order=pool.fallback_order,
            )
        return decisions

    def _initialize_strategy_releases(self) -> None:
        for strategy_id, profile in self.config.strategies.items():
            if profile.route_id not in self.route_scope:
                continue
            configured = StrategyRelease.from_metadata(strategy_id, profile.metadata)
            self.ledger.ensure_strategy_release(configured)

    def _strategy_release(self, strategy_id: str) -> StrategyRelease:
        profile = self.config.strategies[strategy_id]
        release = self.ledger.strategy_release(strategy_id, profile.metadata.version)
        if release is None:
            raise RuntimeError(
                f"strategy release {strategy_id}/{profile.metadata.version} is not registered"
            )
        return release

    def _validate_live_release_authority(self) -> None:
        """Fail closed before constructing a runtime with live order authority."""
        for route_id in sorted(self.route_scope):
            route = self.config.routes.get(route_id)
            if route is None or not route.live_orders_enabled:
                continue
            blocked = []
            for strategy_id, profile in sorted(self.config.strategies.items()):
                if profile.route_id != route_id:
                    continue
                lifecycle = self.ledger.strategy_lifecycle(
                    strategy_id, book_id=profile.book_id
                )
                if lifecycle in {"disabled", "retired"}:
                    continue
                release = self._strategy_release(strategy_id)
                if not release.can_trade_live():
                    blocked.append(
                        f"{strategy_id}/{release.version}={release.state.value}"
                    )
            if blocked:
                raise ValueError(
                    f"route {route_id} has live_orders_enabled=true but strategy releases are "
                    f"not LIVE: {', '.join(blocked)}"
                )

    def _require_shadow_admission(self, route_id: str) -> None:
        blocked = []
        for strategy_id, profile in sorted(self.config.strategies.items()):
            if profile.route_id != route_id:
                continue
            if self.ledger.strategy_lifecycle(strategy_id, book_id=profile.book_id) != "active":
                continue
            release = self._strategy_release(strategy_id)
            if not release.can_shadow():
                blocked.append(f"{strategy_id}/{release.version}={release.state.value}")
        if blocked:
            raise ValueError(
                f"route {route_id} has releases not admitted for shadow: {', '.join(blocked)}"
            )

    def _require_shadow_cutover_state(self, route_id: str) -> None:
        blocked = []
        for strategy_id, profile in sorted(self.config.strategies.items()):
            if profile.route_id != route_id:
                continue
            if self.ledger.strategy_lifecycle(strategy_id, book_id=profile.book_id) != "active":
                continue
            release = self._strategy_release(strategy_id)
            if release.state is not StrategyReleaseState.SHADOW:
                blocked.append(f"{strategy_id}/{release.version}={release.state.value}")
        if blocked:
            raise ValueError(
                f"route {route_id} cannot cut over until every release is SHADOW: "
                f"{', '.join(blocked)}"
            )

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

    def activate_strategy(self, strategy_id: str) -> str:
        canonical_id = self.resolve_strategy_id(strategy_id)
        profile = self.config.strategies[canonical_id]
        release = self._strategy_release(canonical_id)
        route = self.config.routes[profile.route_id]
        shadow = self.ledger.shadow_route(profile.route_id)
        if route.live_orders_enabled and not release.can_trade_live():
            raise ValueError(
                f"strategy release {canonical_id}/{release.version} must be LIVE before "
                f"activation on live route {profile.route_id}"
            )
        if shadow is not None and shadow["active"] and not release.can_shadow():
            raise ValueError(
                f"strategy release {canonical_id}/{release.version} is not admitted for shadow"
            )
        if release.state in {StrategyReleaseState.REVIEW, StrategyReleaseState.KILLED}:
            raise ValueError(
                f"strategy release {canonical_id}/{release.version} is {release.state.value.upper()}"
            )
        self.orchestrator.activate(profile)
        return canonical_id

    def run_strategy(self, strategy_id: str, *, trigger: str = "manual") -> StrategyRunOutcome:
        canonical_id = self.resolve_strategy_id(strategy_id)
        profile = self.config.strategies[canonical_id]
        if profile.route_id not in self.route_scope:
            raise RuntimeError(
                f"strategy {canonical_id} is on route {profile.route_id}, outside runtime scope "
                f"{sorted(self.route_scope)}"
            )
        release = self._strategy_release(canonical_id)
        if release.state is StrategyReleaseState.KILLED:
            raise RuntimeError(
                f"strategy release {canonical_id}/{release.version} is KILLED"
            )
        if release.state is StrategyReleaseState.REVIEW and not self.paper_mode:
            raise RuntimeError(
                f"strategy release {canonical_id}/{release.version} is in REVIEW; "
                "only offline paper runs are permitted"
            )
        shadow = self.ledger.shadow_route(profile.route_id)
        if shadow is not None and shadow["active"] and len(self._route_profiles(profile.route_id)) == 1:
            # Single-owner routes (TLAQ) can refresh from broker state exactly before a delta-native
            # strategy receives its account snapshot. Shared routes refresh only after fresh intents.
            self.refresh_shadow_mirror(profile.route_id, commit=True)
        return self.orchestrator.run(profile, trigger=trigger)

    def _route_profiles(self, route_id: str) -> dict[str, object]:
        return {
            strategy_id: profile
            for strategy_id, profile in self.config.strategies.items()
            if profile.route_id == route_id
        }

    def _refresh_active_shadow_after_intent(self, route_id: str) -> None:
        shadow = self.ledger.shadow_route(route_id)
        if shadow is not None and shadow["active"]:
            self.refresh_shadow_mirror(route_id, commit=True)

    def _broker_quantities(self, route_id: str) -> dict[str, Decimal]:
        if route_id not in self.route_adapters:
            raise ValueError(f"route {route_id} has no execution adapter")
        quantities: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for position in self.route_adapters[route_id].positions():  # type: ignore[attr-defined]
            if position.route_id == route_id:
                quantities[position.instrument] += position.quantity
        return {
            instrument: quantity
            for instrument, quantity in quantities.items()
            if quantity != ZERO
        }

    def refresh_shadow_mirror(
        self,
        route_id: str,
        *,
        ownership: dict[str, dict[str, Decimal]] | None = None,
        commit: bool = False,
    ) -> dict:
        """Plan or refresh the external-authority shadow mirror for one broker route.

        Existing single-owner assignments are sticky. A genuinely new symbol on a shared route may
        be attributed only when exactly one current strategy intent claims it. Ambiguity fails closed.
        The durable ``virtual_positions`` ledger is never modified here.
        """
        if self.paper_mode:
            raise ValueError("shadow mirror is for live broker routes, not paper mode")
        if route_id not in self.route_scope:
            raise ValueError(f"shadow route {route_id} is outside runtime scope")
        if self.config.routes[route_id].live_orders_enabled:
            raise ValueError(
                f"route {route_id} cannot enter external shadow while live_orders_enabled=true"
            )
        profiles = self._route_profiles(route_id)
        if not profiles:
            raise ValueError(f"route {route_id} has no configured strategies")

        broker = self._broker_quantities(route_id)
        existing_shadow = self.ledger.shadow_route(route_id)
        source_rows = (
            self.ledger.shadow_positions(route_id=route_id)
            if existing_shadow is not None and existing_shadow["active"]
            else [row for row in self.ledger.virtual_positions() if row["route_id"] == route_id]
        )
        previous: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(dict)
        for row in source_rows:
            previous[(row["strategy_id"], row["book_id"])][row["instrument"]] = Decimal(
                row["quantity"]
            )

        intents = {
            intent.strategy_id: {key: Decimal(value) for key, value in intent.targets.items()}
            for intent in self.ledger.runtime_intents()
            if intent.route_id == route_id and intent.strategy_id in profiles
        }
        owner_profiles = {
            strategy_id: ShadowOwnerProfile(
                strategy_id=strategy_id,
                book_id=profile.book_id,
                sleeve_id=profile.sleeve_id,
            )
            for strategy_id, profile in profiles.items()
        }
        inferred = infer_shadow_ownership(
            broker_positions=broker,
            profiles=owner_profiles,
            previous=previous,
            intents=intents,
            explicit=ownership,
        )

        assigned: dict[str, dict[str, Decimal]] = {strategy_id: {} for strategy_id in profiles}
        sources: dict[tuple[str, str, str], str] = {}
        for assignment in inferred.assignments:
            assigned[assignment.owner[0]][assignment.instrument] = assignment.quantity
            sources[(assignment.owner[0], assignment.owner[1], assignment.instrument)] = assignment.source

        position_rows: list[VirtualTarget] = []
        strategy_details: dict[str, dict[str, object]] = {}
        if inferred.committable:
            adapter = self.route_adapters[route_id]
            warm = getattr(adapter, "warm_instruments", None)
            if callable(warm) and broker:
                warm(sorted(broker))
            provider = getattr(adapter, "instrument_spec", None)
            if not callable(provider):
                raise ValueError(f"route {route_id} cannot provide instrument marks for shadowing")
            specs: dict[str, InstrumentSpec] = {}
            for instrument in sorted(broker):
                spec = provider(instrument)
                specs[instrument] = spec
                self.portfolio.instruments[instrument] = spec
            for strategy_id, profile in sorted(profiles.items()):
                net_notional = ZERO
                for instrument, quantity in sorted(assigned[strategy_id].items()):
                    spec = specs[instrument]
                    notional = quantity * spec.unit_notional
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
                strategy_details[strategy_id] = {
                    "book_id": profile.book_id,
                    "position_count": len(assigned[strategy_id]),
                    "net_position_notional": str(net_notional),
                    "positions": {
                        instrument: str(quantity)
                        for instrument, quantity in sorted(assigned[strategy_id].items())
                    },
                }

        result: dict[str, object] = {
            "route_id": route_id,
            "mode": "commit" if commit else "dry_run",
            "authority": "external_shadow",
            "baseline": "shadow" if existing_shadow is not None and existing_shadow["active"] else "virtual",
            "broker_position_count": len(broker),
            "strategies": strategy_details,
            "ownership": {
                strategy_id: {
                    instrument: str(quantity)
                    for instrument, quantity in sorted(positions.items())
                }
                for strategy_id, positions in sorted(assigned.items())
            },
            "ownership_sources": {
                f"{strategy_id}/{book_id}/{instrument}": source
                for (strategy_id, book_id, instrument), source in sorted(sources.items())
            },
            "warnings": list(inferred.warnings),
            "unresolved": list(inferred.unresolved),
            "committable": inferred.committable,
            "committed": False,
        }
        if commit:
            if not inferred.committable:
                raise ValueError(
                    f"shadow mirror for {route_id} is unresolved; do not guess strategy ownership"
                )
            self.ledger.replace_shadow_route(
                route_id=route_id,
                positions=position_rows,
                ownership_sources=sources,
                details={
                    "broker_position_count": len(broker),
                    "warnings": list(inferred.warnings),
                },
            )
            self.engine.external_authority_routes.add(route_id)
            result["committed"] = True
            result["reconciliation"] = self.shadow_reconciliation(route_id)
        else:
            self.ledger.append_event("shadow.route_refresh_planned", result)
        return result

    def shadow_reconciliation(self, route_id: str | None = None) -> dict:
        routes = {route_id} if route_id is not None else {
            row["route_id"] for row in self.ledger.shadow_routes(active_only=True)
            if row["route_id"] in self.route_scope
        }
        differences: list[dict[str, str]] = []
        shadow_count = 0
        broker_count = 0
        for selected_route in sorted(routes):
            shadow = self.ledger.shadow_route(selected_route)
            if shadow is None or not shadow["active"]:
                differences.append({
                    "route_id": selected_route,
                    "instrument": "*",
                    "shadow_expected": "missing",
                    "broker_actual": "unknown",
                    "difference": "shadow_not_active",
                })
                continue
            expected: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for row in self.ledger.shadow_positions(route_id=selected_route):
                expected[row["instrument"]] += Decimal(row["quantity"])
            actual = self._broker_quantities(selected_route)
            shadow_count += len([value for value in expected.values() if value != ZERO])
            broker_count += len(actual)
            for instrument in sorted(set(expected) | set(actual)):
                wanted = expected.get(instrument, ZERO)
                got = actual.get(instrument, ZERO)
                if wanted != got:
                    differences.append({
                        "route_id": selected_route,
                        "instrument": instrument,
                        "shadow_expected": str(wanted),
                        "broker_actual": str(got),
                        "difference": str(wanted - got),
                    })
        return {
            "reconciled": not differences,
            "shadow_position_count": shadow_count,
            "broker_position_count": broker_count,
            "differences": differences,
        }

    def promote_shadow_route(self, route_id: str) -> dict:
        if self.config.routes[route_id].live_orders_enabled:
            raise ValueError("promotion must occur while Conductor live orders are still disabled")
        self._require_shadow_cutover_state(route_id)
        check = self.shadow_reconciliation(route_id)
        if not check["reconciled"]:
            raise ValueError(f"shadow route {route_id} does not reconcile to broker; refusing promotion")
        profiles = self._route_profiles(route_id)
        cash_by_owner: dict[tuple[str, str], Decimal] = {}
        for strategy_id, profile in profiles.items():
            account = self.ledger.strategy_account(strategy_id, book_id=profile.book_id)
            if account is None:
                raise KeyError(f"strategy account not seeded: {strategy_id}/{profile.book_id}")
            net = ZERO
            for row in self.ledger.shadow_positions(route_id=route_id):
                if row["strategy_id"] == strategy_id and row["book_id"] == profile.book_id:
                    net += Decimal(row["notional"])
            cash_by_owner[(strategy_id, profile.book_id)] = Decimal(account["allocated_capital"]) - net
        self.ledger.promote_shadow_route(route_id=route_id, cash_by_owner=cash_by_owner)
        self.engine.external_authority_routes.discard(route_id)
        strict = self.bootstrap_reconciliation()
        return {
            "route_id": route_id,
            "promoted": True,
            "cash_by_owner": {
                f"{strategy_id}/{book_id}": str(cash)
                for (strategy_id, book_id), cash in sorted(cash_by_owner.items())
            },
            "reconciliation": strict,
        }

    def shadow_cycle(self, route_id: str, *, trigger: str = "shadow-cycle") -> dict:
        self._require_shadow_admission(route_id)
        shadow = self.ledger.shadow_route(route_id)
        if shadow is None or not shadow["active"]:
            raise ValueError(f"route {route_id} has no active shadow mirror")
        if self.config.routes[route_id].live_orders_enabled:
            raise ValueError(f"route {route_id} shadow cycle requires live_orders_enabled=false")
        profiles = self._route_profiles(route_id)
        active_profiles = [
            profile for _, profile in sorted(profiles.items())
            if self.ledger.strategy_lifecycle(profile.strategy_id, book_id=profile.book_id) == "active"
        ]
        if not active_profiles:
            raise ValueError(f"route {route_id} has no active strategies")
        if len(active_profiles) > 1 and any(
            profile.result_mode is NativeResultMode.POSITION_DELTAS for profile in active_profiles
        ):
            raise ValueError(
                "shared-route shadow-cycle cannot safely capture position-delta strategies before "
                "current ownership is known; use strategy-specific external fill attribution"
            )
        if len(active_profiles) == 1:
            self.refresh_shadow_mirror(route_id, commit=True)

        captures = []
        for profile in active_profiles:
            outcome = self.orchestrator.run(profile, trigger=trigger, defer_portfolio=True)
            captures.append({
                "strategy_id": profile.strategy_id,
                "run_id": outcome.run_id,
                "status": outcome.status,
                "revision": outcome.revision,
                "error": outcome.error,
            })
            if outcome.status != "captured":
                return {
                    "route_id": route_id,
                    "status": "failed",
                    "captures": captures,
                    "error": f"intent capture failed for {profile.strategy_id}: {outcome.error}",
                }

        mirror = self.refresh_shadow_mirror(route_id, commit=True)
        cycle_run_id = uuid4().hex
        portfolio_result = self.engine.run_cycle(
            self.orchestrator._portfolio_intents(route_id), run_id=cycle_run_id
        )
        return {
            "route_id": route_id,
            "status": portfolio_result.state.value,
            "run_id": cycle_run_id,
            "captures": captures,
            "mirror": mirror,
            "reconciled": portfolio_result.reconciled,
            "trade_count": len(portfolio_result.deltas),
            "trades": [
                {
                    "instrument": delta.instrument,
                    "current": str(delta.current),
                    "desired": str(delta.desired),
                    "delta": str(delta.delta),
                }
                for delta in portfolio_result.deltas
            ],
        }

    def _release_status_payload(self, release: StrategyRelease) -> dict[str, object]:
        transitions = self.ledger.strategy_release_transitions(
            release.strategy_id, release.version
        )
        return {
            "strategy_id": release.strategy_id,
            "version": release.version,
            "state": release.state.value,
            "evidence_ids": list(release.evidence_ids),
            "allowed_transitions": [state.value for state in release.allowed_transitions()],
            "can_shadow": release.can_shadow(),
            "can_trade_live": release.can_trade_live(),
            "transition_count": len(transitions),
            "last_transition": transitions[-1] if transitions else None,
        }

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
            release = self.ledger.strategy_release(strategy_id, profile.metadata.version)
            if release is None:
                raise RuntimeError(
                    f"strategy release {strategy_id}/{profile.metadata.version} is not registered"
                )
            strategies.append(
                {
                    "strategy_id": strategy_id,
                    "book_id": profile.book_id,
                    "lifecycle": self.ledger.strategy_lifecycle(
                        strategy_id, book_id=profile.book_id
                    ),
                    "route_id": profile.route_id,
                    "result_mode": profile.result_mode.value,
                    "metadata": {
                        "version": profile.metadata.version,
                        "family": profile.metadata.family,
                        "mechanism": profile.metadata.mechanism,
                        "research_source": profile.metadata.research_source,
                        "validation_state": profile.metadata.validation_state,
                        "evidence": [
                            {
                                "producer": item.producer,
                                "artifact_type": item.artifact_type,
                                "location": item.location,
                                "version": item.version,
                            }
                            for item in profile.metadata.evidence
                        ],
                    },
                    "release": self._release_status_payload(release),
                    "allocated_capital": str(account.allocated_capital),
                    "cash": str(account.cash),
                    "equity": str(account.equity),
                    "gross_exposure": str(account.gross_exposure),
                    "net_exposure": str(account.net_exposure),
                    "positions": {k: str(v) for k, v in account.positions.items()},
                    "target_revision": None if current is None else current.revision,
                    "target_as_of": None if current is None else current.as_of.isoformat(),
                    "policy": self._strategy_policy_status(strategy_id),
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
                    "allocation_diagnostics": (
                        {} if decision is None else dict(decision.diagnostics)
                    ),
                    "authority": (
                        "external_shadow"
                        if (self.ledger.shadow_route(route_id) or {}).get("active")
                        else "conductor"
                    ),
                    "shadow": self.ledger.shadow_route(route_id),
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

    def _strategy_policy_status(self, strategy_id: str) -> dict:
        policy = self.config.strategy_policies[strategy_id]
        return {
            "version": policy.version,
            "source": policy.source,
            "risk_budget": str(policy.risk_budget),
            "target_volatility": (
                None if policy.target_volatility is None else str(policy.target_volatility)
            ),
            "volatility_metadata_key": policy.volatility_metadata_key,
            "min_vol_scale": str(policy.min_vol_scale),
            "max_vol_scale": str(policy.max_vol_scale),
            "max_gross_leverage": (
                None if policy.max_gross_leverage is None else str(policy.max_gross_leverage)
            ),
            "max_position_nav": (
                None if policy.max_position_nav is None else str(policy.max_position_nav)
            ),
            "rebalance_band": str(policy.rebalance_band),
            "max_target_age_seconds": policy.max_target_age_seconds,
            "on_missing_volatility": policy.on_missing_volatility.value,
        }

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
        """Doctor check using the route's current execution authority.

        External-shadow routes compare the shadow mirror to broker state and report the durable
        virtual ledger separately as informational drift. Conductor-authority routes retain the
        strict virtual-vs-broker invariant.
        """
        effective_quantities: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        virtual_quantities: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        authority_by_route: dict[str, str] = {}
        for route_id in sorted(self.route_scope):
            shadow = self.ledger.shadow_route(route_id)
            authority_by_route[route_id] = (
                "external_shadow" if shadow is not None and shadow["active"] else "conductor"
            )
        for row in self.ledger.implementation_positions(route_ids=self.route_scope):
            effective_quantities[(row["route_id"], row["instrument"])] += Decimal(row["quantity"])
        for row in self.ledger.virtual_positions():
            if row["route_id"] in self.route_scope:
                virtual_quantities[(row["route_id"], row["instrument"])] += Decimal(row["quantity"])

        actual = [
            position for position in self.engine.execution.positions()
            if position.route_id in self.route_scope
        ]
        desired = [
            AggregateTarget(
                instrument=instrument,
                route_id=route_id,
                target=quantity,
                notional=ZERO,
            )
            for (route_id, instrument), quantity in sorted(effective_quantities.items())
            if quantity != ZERO
        ]
        virtual_desired = [
            AggregateTarget(
                instrument=instrument,
                route_id=route_id,
                target=quantity,
                notional=ZERO,
            )
            for (route_id, instrument), quantity in sorted(virtual_quantities.items())
            if quantity != ZERO
        ]
        deltas = self.engine.reconciler.reconcile(desired, actual)
        virtual_deltas = self.engine.reconciler.reconcile(virtual_desired, actual)
        runtime_intents = {
            (intent.strategy_id, intent.book_id): intent for intent in self.ledger.runtime_intents()
        }
        policy_diagnostics: list[dict] = []
        blocking_issues: list[str] = []
        now = datetime.now(timezone.utc)
        for strategy_id, profile in sorted(self.config.strategies.items()):
            if profile.route_id not in self.route_scope:
                continue
            policy = self.config.strategy_policies[strategy_id]
            intent = runtime_intents.get((strategy_id, profile.book_id))
            issues: list[str] = []
            state = "ready"
            if intent is None:
                state = "no_intent"
            else:
                if policy.max_target_age_seconds is not None:
                    age = max(0.0, (now - intent.as_of).total_seconds())
                    if age > policy.max_target_age_seconds:
                        issues.append(
                            f"stale target {age:.1f}s > {policy.max_target_age_seconds}s"
                        )
                if policy.target_volatility is not None:
                    raw_vol = intent.metadata.get(policy.volatility_metadata_key)
                    if raw_vol is None and policy.on_missing_volatility.value == "block":
                        issues.append(
                            f"missing volatility metadata {policy.volatility_metadata_key!r}"
                        )
            if issues:
                state = "blocked"
                blocking_issues.extend(
                    f"{strategy_id}/{profile.book_id}: {issue}" for issue in issues
                )
            policy_diagnostics.append(
                {
                    "strategy_id": strategy_id,
                    "book_id": profile.book_id,
                    "route_id": profile.route_id,
                    "state": state,
                    "issues": issues,
                    "policy": self._strategy_policy_status(strategy_id),
                }
            )

        allocation_diagnostics = []
        warnings: list[str] = []
        for route_id, pool in sorted(self.portfolios_by_route.items()):
            decision = self.allocation_decisions.get(route_id)
            allocation_diagnostics.append(
                {
                    "route_id": route_id,
                    "configured_allocator": pool.allocator,
                    "resolved_allocator": None if decision is None else decision.method,
                    "covariance_estimator": pool.covariance_estimator,
                    "diagnostics": {} if decision is None else dict(decision.diagnostics),
                }
            )
            if decision is not None and decision.method != pool.allocator:
                warnings.append(
                    f"{route_id}: allocator fell back from {pool.allocator} to {decision.method}"
                )
            if pool.risk.max_margin_utilization is not None:
                warnings.append(
                    f"{route_id}: max_margin_utilization is configured but not yet enforced"
                )

        result = {
            "reconciled": not deltas,
            "healthy": not deltas and not blocking_issues,
            "authority_by_route": authority_by_route,
            "implementation_position_count": len(desired),
            "virtual_position_count": len(virtual_desired),
            "broker_position_count": len(actual),
            "differences": [
                {
                    "route_id": delta.route_id,
                    "instrument": delta.instrument,
                    "expected": str(delta.desired),
                    "broker_actual": str(delta.current),
                    "difference": str(delta.delta),
                }
                for delta in deltas
            ],
            "virtual_ledger_reconciled": not virtual_deltas,
            "virtual_ledger_differences": [
                {
                    "route_id": delta.route_id,
                    "instrument": delta.instrument,
                    "virtual_expected": str(delta.desired),
                    "broker_actual": str(delta.current),
                    "difference": str(delta.delta),
                }
                for delta in virtual_deltas
            ],
            "strategy_policy_diagnostics": policy_diagnostics,
            "allocation_diagnostics": allocation_diagnostics,
            "blocking_issues": blocking_issues,
            "warnings": warnings,
        }
        self.ledger.append_event("doctor.reconciliation_checked", result)
        return result

    def close(self) -> None:
        for adapter in self.route_adapters.values():
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
