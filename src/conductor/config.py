from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from conductor.domain.models import SleeveAllocation
from conductor.routing import ExecutionRoute, RouteRegistry
from conductor.runtime.models import NativeResultMode, StrategyRunnerProfile


@dataclass(frozen=True, slots=True)
class RouteConfig:
    route: ExecutionRoute
    host: str = "127.0.0.1"
    port: int = 7497
    data_client_id: int = 1301
    exec_client_id: int = 1302
    live_orders_enabled: bool = False
    bridge_db: Path | None = None
    instrument_cache_path: Path | None = None
    worker_stale_after_seconds: int = 15
    request_timeout_seconds: int = 60
    order_timeout_seconds: int = 30
    market_data_type: str = "REALTIME"
    preload_instruments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PortfolioRiskConfig:
    max_gross_leverage: Decimal
    max_net_exposure: Decimal
    max_instrument_nav: Decimal
    max_margin_utilization: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    """One independently funded capital pool, keyed by execution route.

    Named portfolio blocks use ``[portfolio.<route_id>]``. Keeping the capital-pool ID identical
    to the route ID makes ownership explicit: a strategy's ``route_id`` selects both the broker
    account and the capital pool that funds/risk-governs that strategy.
    """

    portfolio_id: str
    route_id: str
    allocator: str
    static_weights: dict[str, Decimal]
    nav_source: str = "broker"
    fixed_nav: Decimal | None = None
    fallback_order: tuple[str, ...] = ("erc", "inverse_vol", "static")
    risk: PortfolioRiskConfig | None = None


@dataclass(frozen=True, slots=True)
class StrategySeed:
    allocated_capital: Decimal | None
    cash: Decimal | None
    positions: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class PaperStrategySeed:
    allocated_capital: Decimal | None
    cash: Decimal | None
    positions: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    path: Path
    node_id: str
    state_db: Path
    run_root: Path
    paper_state_db: Path
    paper_run_root: Path
    # Legacy single-pool fields remain for old configs and paper fixtures.
    portfolio_nav: Decimal | None
    nav_source_route: str | None
    routes: dict[str, RouteConfig]
    strategies: dict[str, StrategyRunnerProfile]
    seeds: dict[str, StrategySeed]
    paper_seeds: dict[str, PaperStrategySeed]
    allocations: dict[str, SleeveAllocation]
    portfolios: dict[str, PortfolioConfig]
    allocation_method: str
    allocation_weights: dict[str, Decimal]
    paper_default_price: Decimal
    paper_prices: dict[str, Decimal]
    paper_portfolio_navs: dict[str, Decimal]
    max_gross_leverage: Decimal
    max_net_exposure: Decimal
    max_instrument_nav: Decimal
    max_margin_utilization: Decimal
    min_trade_nav_bps: Decimal

    @property
    def route_registry(self) -> RouteRegistry:
        return RouteRegistry({key: value.route for key, value in self.routes.items()})


_RESERVED_PORTFOLIO_KEYS = frozenset(
    {"allocator", "static", "inverse_vol", "erc", "fallback", "nav", "risk"}
)


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _paper_db_path(state_db: Path) -> Path:
    suffix = state_db.suffix or ".sqlite"
    stem = state_db.stem if state_db.suffix else state_db.name
    return state_db.with_name(f"{stem}.paper{suffix}")


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - configuration boundary
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _risk_config(raw: dict[str, Any], defaults: dict[str, Any]) -> PortfolioRiskConfig:
    return PortfolioRiskConfig(
        max_gross_leverage=_decimal(
            raw.get("max_gross_leverage", defaults.get("max_gross_leverage", "1.5")),
            field="max_gross_leverage",
        ),
        max_net_exposure=_decimal(
            raw.get("max_net_exposure", defaults.get("max_net_exposure", "1.0")),
            field="max_net_exposure",
        ),
        max_instrument_nav=_decimal(
            raw.get("max_instrument_nav", defaults.get("max_instrument_nav", "0.20")),
            field="max_instrument_nav",
        ),
        max_margin_utilization=_decimal(
            raw.get(
                "max_margin_utilization", defaults.get("max_margin_utilization", "0.60")
            ),
            field="max_margin_utilization",
        ),
    )


def _parse_nav(raw: object | None, *, field: str) -> tuple[str, Decimal | None]:
    if raw is None:
        return "broker", None
    if isinstance(raw, str):
        if raw.strip().lower() != "broker":
            raise ValueError(f"{field} must be 'broker' or a positive numeric NAV")
        return "broker", None
    value = _decimal(raw, field=field)
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return "fixed", value


def _validate_portfolio_weights(
    *,
    portfolio_id: str,
    route_id: str,
    weights: dict[str, Decimal],
    strategies: dict[str, StrategyRunnerProfile],
) -> None:
    if any(weight < 0 for weight in weights.values()):
        raise ValueError(f"portfolio {portfolio_id} static weights cannot be negative")
    unknown = sorted(set(weights) - set(strategies))
    if unknown:
        raise ValueError(
            f"portfolio {portfolio_id} references unknown strategies: {', '.join(unknown)}"
        )
    wrong_route = sorted(
        strategy_id
        for strategy_id in weights
        if strategies[strategy_id].route_id != route_id
    )
    if wrong_route:
        raise ValueError(
            f"portfolio {portfolio_id} contains strategies assigned to another route: "
            + ", ".join(wrong_route)
        )
    route_strategies = {
        strategy_id for strategy_id, profile in strategies.items() if profile.route_id == route_id
    }
    if weights and set(weights) != route_strategies:
        missing = sorted(route_strategies - set(weights))
        extra = sorted(set(weights) - route_strategies)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("extra " + ", ".join(extra))
        raise ValueError(
            f"portfolio {portfolio_id} static weights must cover exactly the strategies on "
            f"route {route_id}: {'; '.join(details)}"
        )
    if weights:
        total = sum(weights.values(), Decimal("0"))
        if abs(total - Decimal("1")) > Decimal("0.00000001"):
            raise ValueError(
                f"portfolio {portfolio_id} static weights sum to {total}; must sum to 1.0"
            )


def _build_portfolios(
    *,
    raw_portfolio: dict[str, Any],
    node: dict[str, Any],
    risk_defaults: dict[str, Any],
    strategies: dict[str, StrategyRunnerProfile],
    routes: dict[str, RouteConfig],
) -> tuple[dict[str, PortfolioConfig], str, dict[str, Decimal]]:
    """Parse canonical named capital pools and retain legacy single-pool compatibility."""

    global_allocator = str(raw_portfolio.get("allocator", "static"))
    global_static = raw_portfolio.get("static", {})
    global_weights = {
        str(strategy): _decimal(weight, field=f"portfolio.static.weights.{strategy}")
        for strategy, weight in global_static.get("weights", {}).items()
    }
    global_fallback = tuple(
        str(item) for item in raw_portfolio.get("fallback", {}).get(
            "order", ["erc", "inverse_vol", "static"]
        )
    )

    named_keys = [
        key
        for key, value in raw_portfolio.items()
        if key not in _RESERVED_PORTFOLIO_KEYS and isinstance(value, dict)
    ]
    portfolios: dict[str, PortfolioConfig] = {}

    if named_keys:
        for portfolio_id in sorted(named_keys):
            item = raw_portfolio[portfolio_id]
            route_id = str(item.get("route_id", portfolio_id))
            if route_id not in routes and route_id not in {
                profile.route_id for profile in strategies.values()
            }:
                raise ValueError(
                    f"portfolio {portfolio_id} references unknown route {route_id}"
                )
            static = item.get("static", {})
            weights = {
                str(strategy): _decimal(
                    weight, field=f"portfolio.{portfolio_id}.static.weights.{strategy}"
                )
                for strategy, weight in static.get("weights", {}).items()
            }
            allocator = str(item.get("allocator", "static"))
            if allocator not in {"static", "inverse_vol", "erc"}:
                raise ValueError(
                    f"portfolio {portfolio_id} has unknown allocator {allocator!r}"
                )
            nav_source, fixed_nav = _parse_nav(
                item.get("nav"), field=f"portfolio.{portfolio_id}.nav"
            )
            fallback_order = tuple(
                str(value)
                for value in item.get("fallback", {}).get("order", global_fallback)
            )
            _validate_portfolio_weights(
                portfolio_id=portfolio_id,
                route_id=route_id,
                weights=weights,
                strategies=strategies,
            )
            portfolios[portfolio_id] = PortfolioConfig(
                portfolio_id=portfolio_id,
                route_id=route_id,
                allocator=allocator,
                static_weights=weights,
                nav_source=nav_source,
                fixed_nav=fixed_nav,
                fallback_order=fallback_order,
                risk=_risk_config(item.get("risk", {}), risk_defaults),
            )

        routes_with_strategies = {profile.route_id for profile in strategies.values()}
        covered_routes = {portfolio.route_id for portfolio in portfolios.values()}
        missing_routes = sorted(routes_with_strategies - covered_routes)
        if missing_routes:
            raise ValueError(
                "named portfolio mode requires one [portfolio.<route_id>] capital pool for every "
                "strategy route; missing: " + ", ".join(missing_routes)
            )
        duplicate_routes = sorted(
            route_id
            for route_id in covered_routes
            if sum(1 for item in portfolios.values() if item.route_id == route_id) > 1
        )
        if duplicate_routes:
            raise ValueError(
                "V0.4 supports one capital pool per route; duplicate route pool(s): "
                + ", ".join(duplicate_routes)
            )
        return portfolios, global_allocator, global_weights

    # Legacy shape: one global [portfolio] block. Derive the only route when possible.
    strategy_routes = sorted({profile.route_id for profile in strategies.values()})
    nav_source_route = node.get("nav_source_route")
    if len(strategy_routes) > 1 and (global_weights or nav_source_route is not None):
        raise ValueError(
            "legacy [portfolio] cannot allocate multiple strategy routes; use "
            "[portfolio.<route_id>] blocks"
        )

    for route_id in strategy_routes:
        if len(strategy_routes) == 1:
            weights = dict(global_weights)
            allocator = global_allocator
        else:
            weights = {}
            allocator = "static"
        _validate_portfolio_weights(
            portfolio_id=route_id,
            route_id=route_id,
            weights=weights,
            strategies=strategies,
        )
        if nav_source_route is not None and str(nav_source_route) == route_id:
            nav_source, fixed_nav = "broker", None
        elif node.get("portfolio_nav") is not None:
            nav_source, fixed_nav = "fixed", _decimal(
                node["portfolio_nav"], field="node.portfolio_nav"
            )
        elif route_id in routes:
            nav_source, fixed_nav = "broker", None
        else:
            nav_source, fixed_nav = "broker", None
        portfolios[route_id] = PortfolioConfig(
            portfolio_id=route_id,
            route_id=route_id,
            allocator=allocator,
            static_weights=weights,
            nav_source=nav_source,
            fixed_nav=fixed_nav,
            fallback_order=global_fallback,
            risk=_risk_config({}, risk_defaults),
        )
    return portfolios, global_allocator, global_weights


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    config_path = Path(path).expanduser().resolve()
    base = config_path.parent
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    node: dict[str, Any] = raw.get("node", {})
    node_id = str(node.get("id", "windows"))
    state_db = _resolve_path(base, str(node.get("state_db", "data/conductor.sqlite")))
    run_root = _resolve_path(base, str(node.get("run_root", "data/runs")))
    paper = raw.get("paper", {})
    paper_state_raw = paper.get("state_db")
    paper_run_root_raw = paper.get("run_root")
    paper_state_db = (
        _resolve_path(base, str(paper_state_raw))
        if paper_state_raw is not None
        else _paper_db_path(state_db)
    )
    if paper_state_db == state_db:
        raise ValueError("paper.state_db must be different from node.state_db")
    paper_run_root = (
        _resolve_path(base, str(paper_run_root_raw))
        if paper_run_root_raw is not None
        else run_root / "paper"
    )
    portfolio_nav_raw = node.get("portfolio_nav")
    portfolio_nav = (
        _decimal(portfolio_nav_raw, field="node.portfolio_nav")
        if portfolio_nav_raw is not None
        else None
    )
    if portfolio_nav is not None and portfolio_nav <= 0:
        raise ValueError("node.portfolio_nav must be positive")
    nav_source_route = node.get("nav_source_route")

    routes: dict[str, RouteConfig] = {}
    for route_id, item in raw.get("routes", {}).items():
        route = ExecutionRoute(
            route_id=route_id,
            node_id=str(item.get("node_id", node_id)),
            adapter=str(item["adapter"]),
            account=str(item.get("account", "paper")),
            venue=item.get("venue"),
        )
        bridge_raw = item.get("bridge_db")
        cache_raw = item.get("instrument_cache_path")
        legacy_client_id = int(item.get("client_id", 1301))
        routes[route_id] = RouteConfig(
            route=route,
            host=str(item.get("host", "127.0.0.1")),
            port=int(item.get("port", 7497)),
            data_client_id=int(item.get("data_client_id", legacy_client_id)),
            exec_client_id=int(item.get("exec_client_id", legacy_client_id + 1)),
            live_orders_enabled=bool(item.get("live_orders_enabled", False)),
            bridge_db=(
                _resolve_path(base, str(bridge_raw))
                if bridge_raw is not None
                else _resolve_path(base, f"data/{route_id}_nautilus_bridge.sqlite")
            ),
            instrument_cache_path=(
                _resolve_path(base, str(cache_raw))
                if cache_raw is not None
                else _resolve_path(base, f"data/{route_id}_ib_instruments.json")
            ),
            worker_stale_after_seconds=int(item.get("worker_stale_after_seconds", 15)),
            request_timeout_seconds=int(item.get("request_timeout_seconds", 60)),
            order_timeout_seconds=int(item.get("order_timeout_seconds", 30)),
            market_data_type=str(item.get("market_data_type", "REALTIME")),
            preload_instruments=tuple(str(value) for value in item.get("preload_instruments", [])),
        )

    strategies: dict[str, StrategyRunnerProfile] = {}
    seeds: dict[str, StrategySeed] = {}
    paper_seeds: dict[str, PaperStrategySeed] = {}
    for strategy_id, item in raw.get("strategies", {}).items():
        cwd = _resolve_path(base, str(item["cwd"]))
        profile = StrategyRunnerProfile(
            strategy_id=strategy_id,
            sleeve_id=str(item.get("sleeve_id", "equities")),
            result_mode=NativeResultMode(str(item["result_mode"])),
            command=tuple(str(value) for value in item["command"]),
            cwd=cwd,
            route_id=str(item["route_id"]),
            book_id=str(item.get("book_id", "main")),
            timeout_seconds=int(item.get("timeout_seconds", 600)),
            environment={str(k): str(v) for k, v in item.get("environment", {}).items()},
        )
        strategies[strategy_id] = profile
        seed = item.get("seed", {})
        seeds[strategy_id] = StrategySeed(
            allocated_capital=(
                _decimal(
                    seed["allocated_capital"],
                    field=f"strategies.{strategy_id}.seed.allocated_capital",
                )
                if "allocated_capital" in seed
                else None
            ),
            cash=(
                _decimal(seed["cash"], field=f"strategies.{strategy_id}.seed.cash")
                if "cash" in seed
                else None
            ),
            positions={
                str(instrument): _decimal(
                    quantity,
                    field=f"strategies.{strategy_id}.seed.positions.{instrument}",
                )
                for instrument, quantity in seed.get("positions", {}).items()
            },
        )
        paper_seed = item.get("paper_seed", {})
        paper_seeds[strategy_id] = PaperStrategySeed(
            allocated_capital=(
                _decimal(
                    paper_seed["allocated_capital"],
                    field=f"strategies.{strategy_id}.paper_seed.allocated_capital",
                )
                if "allocated_capital" in paper_seed
                else None
            ),
            cash=(
                _decimal(
                    paper_seed["cash"], field=f"strategies.{strategy_id}.paper_seed.cash"
                )
                if "cash" in paper_seed
                else None
            ),
            positions={
                str(instrument): _decimal(
                    quantity,
                    field=f"strategies.{strategy_id}.paper_seed.positions.{instrument}",
                )
                for instrument, quantity in paper_seed.get("positions", {}).items()
            },
        )

    casefolded_ids = [strategy_id.casefold() for strategy_id in strategies]
    if len(casefolded_ids) != len(set(casefolded_ids)):
        raise ValueError("configured strategy IDs must be unique ignoring case")

    unknown_strategy_routes = sorted(
        {
            profile.route_id
            for profile in strategies.values()
            if profile.route_id not in routes and profile.route_id != "offline"
        }
    )
    # A paper-only config may intentionally omit a live route. Any explicitly configured live
    # routes remain strict; route existence is also validated by named capital pools below.
    if unknown_strategy_routes and routes:
        raise ValueError(
            "strategy route(s) have no [routes.<id>] configuration: "
            + ", ".join(unknown_strategy_routes)
        )

    allocations: dict[str, SleeveAllocation] = {}
    for sleeve_id, item in raw.get("sleeves", {}).items():
        allocations[sleeve_id] = SleeveAllocation(
            sleeve_id=sleeve_id,
            portfolio_weight=_decimal(
                item.get("portfolio_weight", "1"), field=f"sleeves.{sleeve_id}.portfolio_weight"
            ),
            leverage=_decimal(
                item.get("leverage", "1"), field=f"sleeves.{sleeve_id}.leverage"
            ),
            rebalance_band=_decimal(
                item.get("rebalance_band", "0"), field=f"sleeves.{sleeve_id}.rebalance_band"
            ),
            strategy_weights={
                str(strategy): _decimal(
                    weight, field=f"sleeves.{sleeve_id}.strategy_weights.{strategy}"
                )
                for strategy, weight in item.get("strategy_weights", {}).items()
            },
        )

    risk = raw.get("risk", {})
    execution = raw.get("execution", {})
    portfolio = raw.get("portfolio", {})
    portfolios, allocation_method, allocation_weights = _build_portfolios(
        raw_portfolio=portfolio,
        node=node,
        risk_defaults=risk,
        strategies=strategies,
        routes=routes,
    )

    paper_default_price = _decimal(paper.get("default_price", "100"), field="paper.default_price")
    paper_portfolio_navs = {
        str(route_id): _decimal(nav, field=f"paper.portfolio_navs.{route_id}")
        for route_id, nav in paper.get("portfolio_navs", {}).items()
    }
    unknown_paper_navs = sorted(
        set(paper_portfolio_navs) - {item.route_id for item in portfolios.values()}
    )
    if unknown_paper_navs:
        raise ValueError(
            "paper.portfolio_navs references unknown capital pools: "
            + ", ".join(unknown_paper_navs)
        )
    invalid_paper_navs = [
        route_id for route_id, nav in paper_portfolio_navs.items() if nav <= 0
    ]
    if invalid_paper_navs:
        raise ValueError(
            "paper portfolio NAVs must be positive: " + ", ".join(sorted(invalid_paper_navs))
        )
    paper_prices = {
        str(instrument): _decimal(price, field=f"paper_prices.{instrument}")
        for instrument, price in raw.get("paper_prices", {}).items()
    }
    if paper_default_price <= 0:
        raise ValueError("paper.default_price must be positive")
    invalid_paper_prices = [name for name, price in paper_prices.items() if price <= 0]
    if invalid_paper_prices:
        raise ValueError(
            "paper prices must be positive: " + ", ".join(sorted(invalid_paper_prices))
        )

    global_risk = _risk_config({}, risk)
    return RuntimeConfig(
        path=config_path,
        node_id=node_id,
        state_db=state_db,
        run_root=run_root,
        paper_state_db=paper_state_db,
        paper_run_root=paper_run_root,
        portfolio_nav=portfolio_nav,
        nav_source_route=str(nav_source_route) if nav_source_route is not None else None,
        routes=routes,
        strategies=strategies,
        seeds=seeds,
        paper_seeds=paper_seeds,
        allocations=allocations,
        portfolios=portfolios,
        allocation_method=allocation_method,
        allocation_weights=allocation_weights,
        paper_default_price=paper_default_price,
        paper_prices=paper_prices,
        paper_portfolio_navs=paper_portfolio_navs,
        max_gross_leverage=global_risk.max_gross_leverage,
        max_net_exposure=global_risk.max_net_exposure,
        max_instrument_nav=global_risk.max_instrument_nav,
        max_margin_utilization=global_risk.max_margin_utilization,
        min_trade_nav_bps=_decimal(
            execution.get("min_trade_nav_bps", "1"), field="execution.min_trade_nav_bps"
        ),
    )
