from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from conductor.config import load_runtime_config
from conductor.domain.models import (
    BrokerPosition,
    ExecutionReport,
    ExposureType,
    InstrumentSpec,
    VirtualTarget,
)
from conductor.orders import OrderPlanner
from conductor.risk import PortfolioRiskEngine
from conductor.runtime.app import ConductorRuntimeApp


def _write_strategy(path: Path, payload: dict) -> Path:
    path.write_text(
        "import json, os\n"
        f"payload = {payload!r}\n"
        "with open(os.environ['CONDUCTOR_OUTPUT'], 'w', encoding='utf-8') as h:\n"
        "    json.dump(payload, h)\n",
        encoding="utf-8",
    )
    return path


def _multi_account_config(tmp_path: Path) -> Path:
    etsa = _write_strategy(tmp_path / "etsa.py", {"targets": {"AAPL": "0.50"}})
    rps = _write_strategy(tmp_path / "rps.py", {"targets": {}})
    tlaq = _write_strategy(tmp_path / "tlaq.py", {"deltas": {}})
    command = lambda path: json.dumps([sys.executable, str(path)])
    cwd = json.dumps(str(tmp_path))
    config = tmp_path / "conductor.toml"
    config.write_text(
        f"""
[node]
id = "multi-account-test"
state_db = "data/conductor.sqlite"
run_root = "data/runs"

[portfolio.ibkr_main]
allocator = "static"
[portfolio.ibkr_main.static.weights]
ETSA = 0.85
RPSchteroids = 0.15

[portfolio.ibkr_tlaq]
allocator = "static"
[portfolio.ibkr_tlaq.static.weights]
TLAQ = 1.0

[risk]
max_gross_leverage = 10
max_net_exposure = 10
max_instrument_nav = 10
max_margin_utilization = 0.60

[execution]
min_trade_nav_bps = 0

[routes.ibkr_main]
adapter = "nautilus_ibkr"
account = "MAIN"
bridge_db = "data/main-bridge.sqlite"
live_orders_enabled = false

[routes.ibkr_tlaq]
adapter = "nautilus_ibkr"
account = "TLAQ"
bridge_db = "data/tlaq-bridge.sqlite"
live_orders_enabled = false

[strategies.ETSA]
result_mode = "target_weights"
route_id = "ibkr_main"
cwd = {cwd}
command = {command(etsa)}
[strategies.ETSA.seed]
cash = 0
positions = {{}}

[strategies.RPSchteroids]
result_mode = "target_weights"
route_id = "ibkr_main"
cwd = {cwd}
command = {command(rps)}
[strategies.RPSchteroids.seed]
cash = 0
positions = {{}}

[strategies.TLAQ]
result_mode = "position_deltas"
route_id = "ibkr_tlaq"
cwd = {cwd}
command = {command(tlaq)}
[strategies.TLAQ.seed]
cash = 0
positions = {{ AAPL = 10 }}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def test_named_portfolios_are_route_backed_and_default_to_broker_nav(tmp_path) -> None:
    config = load_runtime_config(_multi_account_config(tmp_path))
    assert set(config.portfolios) == {"ibkr_main", "ibkr_tlaq"}
    assert config.portfolios["ibkr_main"].static_weights == {
        "ETSA": Decimal("0.85"),
        "RPSchteroids": Decimal("0.15"),
    }
    assert config.portfolios["ibkr_tlaq"].allocator == "static"
    assert config.portfolios["ibkr_tlaq"].nav_source == "broker"


def test_named_portfolio_requires_all_strategies_on_route(tmp_path) -> None:
    path = _multi_account_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace("RPSchteroids = 0.15\n", "")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="must cover exactly"):
        load_runtime_config(path)


class _FakeLiveAdapter:
    def __init__(self, route_id: str, nav: Decimal, positions: dict[str, Decimal]) -> None:
        self.route_id = route_id
        self.nav = nav
        self._positions = dict(positions)
        self.submitted = []

    def net_liquidation(self) -> Decimal:
        return self.nav

    def positions(self):
        return [
            BrokerPosition(instrument, quantity, self.route_id)
            for instrument, quantity in sorted(self._positions.items())
            if quantity != 0
        ]

    def instrument_spec(self, instrument: str) -> InstrumentSpec:
        return InstrumentSpec(instrument, Decimal("100"))

    def submit_deltas(self, deltas):
        reports = []
        for delta in deltas:
            self.submitted.append(delta)
            self._positions[delta.instrument] = (
                self._positions.get(delta.instrument, Decimal("0")) + delta.delta
            )
            reports.append(
                ExecutionReport(
                    route_id=self.route_id,
                    instrument=delta.instrument,
                    requested_quantity=delta.delta,
                    filled_quantity=delta.delta,
                    avg_price=Decimal("100"),
                    status="filled",
                )
            )
        return reports

    def close(self) -> None:
        return None


def test_live_multi_account_allocation_and_execution_are_isolated(tmp_path, monkeypatch) -> None:
    config_path = _multi_account_config(tmp_path)
    adapters: dict[str, _FakeLiveAdapter] = {}

    def fake_adapter(**kwargs):
        route_id = kwargs["route_id"]
        nav = Decimal("200000") if route_id == "ibkr_main" else Decimal("75000")
        positions = {} if route_id == "ibkr_main" else {"AAPL": Decimal("10")}
        adapter = _FakeLiveAdapter(route_id, nav, positions)
        adapters[route_id] = adapter
        return adapter

    monkeypatch.setattr("conductor.runtime.app.NautilusBridgeExecutionAdapter", fake_adapter)
    app = ConductorRuntimeApp.from_path(config_path)
    try:
        accounts = {
            row["strategy_id"]: Decimal(row["allocated_capital"])
            for row in app.ledger.strategy_accounts()
        }
        assert accounts == {
            "ETSA": Decimal("170000.00"),
            "RPSchteroids": Decimal("30000.00"),
            "TLAQ": Decimal("75000.0"),
        }

        outcome = app.run_strategy("ETSA", trigger="test")
        assert outcome.status == "succeeded"
        assert outcome.portfolio_result is not None
        assert {delta.route_id for delta in outcome.portfolio_result.deltas} == {"ibkr_main"}
        assert adapters["ibkr_tlaq"].submitted == []
        assert app.ledger.strategy_positions("TLAQ") == {"AAPL": Decimal("10")}

        status = app.status()
        pools = {item["route_id"]: item for item in status["portfolios"]}
        assert pools["ibkr_main"]["nav"] == "200000"
        assert pools["ibkr_main"]["weights"] == {
            "ETSA": "0.85",
            "RPSchteroids": "0.15",
        }
        assert pools["ibkr_tlaq"]["nav"] == "75000"
    finally:
        app.close()



def test_multi_account_paper_navs_are_independent_of_live_broker_nav(tmp_path, monkeypatch) -> None:
    config_path = _multi_account_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    text += "\n[paper.portfolio_navs]\nibkr_main = 120000\nibkr_tlaq = 80000\n"
    config_path.write_text(text, encoding="utf-8")

    def fail_live_adapter(**kwargs):
        raise AssertionError(f"paper mode constructed live route {kwargs['route_id']}")

    monkeypatch.setattr("conductor.runtime.app.NautilusBridgeExecutionAdapter", fail_live_adapter)
    app = ConductorRuntimeApp.from_path(config_path, paper=True)
    try:
        assert app.portfolio_navs == {
            "ibkr_main": Decimal("120000"),
            "ibkr_tlaq": Decimal("80000"),
        }
        accounts = {
            row["strategy_id"]: Decimal(row["allocated_capital"])
            for row in app.ledger.strategy_accounts()
        }
        assert accounts == {
            "ETSA": Decimal("102000.00"),
            "RPSchteroids": Decimal("18000.00"),
            "TLAQ": Decimal("80000.0"),
        }
    finally:
        app.close()

def test_route_risk_scales_only_the_constrained_account() -> None:
    risk = PortfolioRiskEngine(
        {"small": Decimal("100000"), "large": Decimal("1000000")},
        max_gross_leverage={"small": Decimal("1"), "large": Decimal("1")},
        max_net_exposure={"small": Decimal("10"), "large": Decimal("10")},
        max_instrument_nav={"small": Decimal("10"), "large": Decimal("10")},
    )
    targets = [
        VirtualTarget(
            strategy_id="A",
            sleeve_id="equities",
            route_id="small",
            instrument="AAPL",
            target=Decimal("1500"),
            notional=Decimal("150000"),
            source_exposure_type=ExposureType.NAV_WEIGHT,
        ),
        VirtualTarget(
            strategy_id="B",
            sleeve_id="equities",
            route_id="large",
            instrument="AAPL",
            target=Decimal("1500"),
            notional=Decimal("150000"),
            source_exposure_type=ExposureType.NAV_WEIGHT,
        ),
    ]
    scaled, decision = risk.apply(targets)
    got = {(item.route_id, item.strategy_id): item.target for item in scaled}
    assert got[("small", "A")] == Decimal("1000")
    assert got[("large", "B")] == Decimal("1500")
    assert decision.route_scales["small"] < 1
    assert decision.route_scales["large"] == 1


def test_trade_buffer_uses_each_route_nav() -> None:
    planner = OrderPlanner(
        portfolio_nav={"small": Decimal("100000"), "large": Decimal("1000000")},
        instruments={"AAPL": InstrumentSpec("AAPL", Decimal("100"))},
        min_trade_nav_bps=Decimal("10"),
    )
    from conductor.domain.models import TradeDelta

    planned = planner.plan(
        [
            TradeDelta("AAPL", Decimal("0"), Decimal("5"), Decimal("5"), route_id="small"),
            TradeDelta("AAPL", Decimal("0"), Decimal("5"), Decimal("5"), route_id="large"),
        ]
    )
    assert [item.route_id for item in planned] == ["small"]


def test_route_scoped_runtime_does_not_require_unrelated_account(tmp_path, monkeypatch) -> None:
    config_path = _multi_account_config(tmp_path)
    created_routes: list[str] = []

    def fake_adapter(**kwargs):
        route_id = kwargs["route_id"]
        created_routes.append(route_id)
        if route_id == "ibkr_tlaq":
            raise AssertionError("unrelated TLAQ route should not be constructed")
        return _FakeLiveAdapter(route_id, Decimal("200000"), {})

    monkeypatch.setattr("conductor.runtime.app.NautilusBridgeExecutionAdapter", fake_adapter)
    app = ConductorRuntimeApp.from_path(config_path, route_scope={"ibkr_main"})
    try:
        assert created_routes == ["ibkr_main"]
        assert set(app.portfolio_navs) == {"ibkr_main"}
        outcome = app.run_strategy("ETSA", trigger="test")
        assert outcome.status == "succeeded"
        assert {delta.route_id for delta in outcome.portfolio_result.deltas} == {"ibkr_main"}
    finally:
        app.close()


def test_route_account_summary_client_ids_are_derived_per_route(tmp_path):
    config_path = tmp_path / "conductor.toml"
    config_path.write_text(
        '''
[node]
id = "windows"
state_db = "data/conductor.sqlite"
run_root = "data/runs"

[portfolio.ibkr_main]
allocator = "static"
[portfolio.ibkr_main.static.weights]
ETSA = 1.0

[portfolio.ibkr_tlaq]
allocator = "static"
[portfolio.ibkr_tlaq.static.weights]
TLAQ = 1.0

[routes.ibkr_main]
adapter = "nautilus_ibkr"
account = "U_MAIN"
exec_client_id = 1302

[routes.ibkr_tlaq]
adapter = "nautilus_ibkr"
account = "U_TLAQ"
exec_client_id = 1312
account_summary_client_id = 2312

[strategies.ETSA]
result_mode = "target_weights"
route_id = "ibkr_main"
cwd = "."
command = ["python", "etsa.py"]

[strategies.TLAQ]
result_mode = "position_deltas"
route_id = "ibkr_tlaq"
cwd = "."
command = ["python", "tlaq.py"]
''',
        encoding="utf-8",
    )
    from conductor.config import load_runtime_config

    config = load_runtime_config(config_path)
    assert config.routes["ibkr_main"].account_summary_client_id == 11302
    assert config.routes["ibkr_tlaq"].account_summary_client_id == 2312
    assert config.routes["ibkr_tlaq"].account_summary_fallback_enabled is True
