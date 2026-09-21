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
class StrategySeed:
    allocated_capital: Decimal
    cash: Decimal
    positions: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    path: Path
    node_id: str
    state_db: Path
    run_root: Path
    portfolio_nav: Decimal | None
    nav_source_route: str | None
    routes: dict[str, RouteConfig]
    strategies: dict[str, StrategyRunnerProfile]
    seeds: dict[str, StrategySeed]
    allocations: dict[str, SleeveAllocation]
    paper_prices: dict[str, Decimal]
    max_gross_leverage: Decimal
    max_instrument_nav: Decimal
    min_trade_nav_bps: Decimal

    @property
    def route_registry(self) -> RouteRegistry:
        return RouteRegistry({key: value.route for key, value in self.routes.items()})


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    config_path = Path(path).expanduser().resolve()
    base = config_path.parent
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    node: dict[str, Any] = raw.get("node", {})
    node_id = str(node.get("id", "windows"))
    state_db = _resolve_path(base, str(node.get("state_db", "data/conductor.sqlite")))
    run_root = _resolve_path(base, str(node.get("run_root", "data/runs")))
    portfolio_nav_raw = node.get("portfolio_nav")
    portfolio_nav = Decimal(str(portfolio_nav_raw)) if portfolio_nav_raw is not None else None
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
            allocated_capital=Decimal(str(seed.get("allocated_capital", "0"))),
            cash=Decimal(str(seed.get("cash", "0"))),
            positions={
                str(instrument): Decimal(str(quantity))
                for instrument, quantity in seed.get("positions", {}).items()
            },
        )

    allocations: dict[str, SleeveAllocation] = {}
    for sleeve_id, item in raw.get("sleeves", {}).items():
        allocations[sleeve_id] = SleeveAllocation(
            sleeve_id=sleeve_id,
            portfolio_weight=Decimal(str(item.get("portfolio_weight", "1"))),
            leverage=Decimal(str(item.get("leverage", "1"))),
            rebalance_band=Decimal(str(item.get("rebalance_band", "0"))),
            strategy_weights={
                str(strategy): Decimal(str(weight))
                for strategy, weight in item.get("strategy_weights", {}).items()
            },
        )

    risk = raw.get("risk", {})
    execution = raw.get("execution", {})
    return RuntimeConfig(
        path=config_path,
        node_id=node_id,
        state_db=state_db,
        run_root=run_root,
        portfolio_nav=portfolio_nav,
        nav_source_route=str(nav_source_route) if nav_source_route is not None else None,
        routes=routes,
        strategies=strategies,
        seeds=seeds,
        allocations=allocations,
        paper_prices={
            str(instrument): Decimal(str(price))
            for instrument, price in raw.get("paper_prices", {}).items()
        },
        max_gross_leverage=Decimal(str(risk.get("max_gross_leverage", "1.5"))),
        max_instrument_nav=Decimal(str(risk.get("max_instrument_nav", "0.20"))),
        min_trade_nav_bps=Decimal(str(execution.get("min_trade_nav_bps", "1"))),
    )
