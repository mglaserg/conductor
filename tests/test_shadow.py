from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from conductor.domain.models import BrokerPosition, ExecutionReport, InstrumentSpec, StrategyIntent
from conductor.runtime.app import ConductorRuntimeApp
from conductor.shadow import ShadowOwnerProfile, infer_shadow_ownership


def _profiles() -> dict[str, ShadowOwnerProfile]:
    return {
        "ETSA": ShadowOwnerProfile("ETSA", "main", "equities"),
        "RPSchteroids": ShadowOwnerProfile("RPSchteroids", "main", "equities"),
    }


def test_shadow_inference_keeps_sticky_ownership_and_assigns_only_new_unique_claims() -> None:
    result = infer_shadow_ownership(
        broker_positions={"AEP": Decimal("0"), "AUB": Decimal("-142"), "EMB": Decimal("77")},
        profiles=_profiles(),
        previous={
            ("ETSA", "main"): {"AEP": Decimal("-59")},
            ("RPSchteroids", "main"): {"EMB": Decimal("86")},
        },
        intents={
            "ETSA": {"AUB": Decimal("0.1")},
            "RPSchteroids": {"EMB": Decimal("0.2")},
        },
    )
    assert result.committable
    got = {(row.owner[0], row.instrument): row.quantity for row in result.assignments}
    assert got == {
        ("ETSA", "AUB"): Decimal("-142"),
        ("RPSchteroids", "EMB"): Decimal("77"),
    }
    assert all(row.instrument != "AEP" for row in result.assignments)


def test_shadow_inference_refuses_ambiguous_new_symbol() -> None:
    result = infer_shadow_ownership(
        broker_positions={"AAPL": Decimal("10")},
        profiles=_profiles(),
        previous={},
        intents={
            "ETSA": {"AAPL": Decimal("0.1")},
            "RPSchteroids": {"AAPL": Decimal("0.1")},
        },
    )
    assert not result.committable
    assert result.unresolved[0]["reason"] == "ambiguous_new_symbol"


def test_shadow_inference_refuses_changed_multi_owner_aggregate() -> None:
    result = infer_shadow_ownership(
        broker_positions={"AAPL": Decimal("12")},
        profiles=_profiles(),
        previous={
            ("ETSA", "main"): {"AAPL": Decimal("5")},
            ("RPSchteroids", "main"): {"AAPL": Decimal("5")},
        },
        intents={"ETSA": {"AAPL": Decimal("1")}, "RPSchteroids": {"AAPL": Decimal("1")}},
    )
    assert not result.committable
    assert result.unresolved[0]["reason"] == "multi_owner_quantity_changed"


class _ShadowAdapter:
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

    def warm_instruments(self, instruments) -> None:
        return None

    def submit_deltas(self, deltas):
        self.submitted.extend(deltas)
        return [
            ExecutionReport(
                route_id=self.route_id,
                instrument=delta.instrument,
                requested_quantity=delta.delta,
                filled_quantity=Decimal("0"),
                avg_price=None,
                status="shadow",
            )
            for delta in deltas
        ]

    def close(self) -> None:
        return None


def _write_strategy(path: Path, payload: dict) -> Path:
    path.write_text(
        "import json, os\n"
        f"payload = {payload!r}\n"
        "with open(os.environ['CONDUCTOR_OUTPUT'], 'w', encoding='utf-8') as h:\n"
        "    json.dump(payload, h)\n",
        encoding="utf-8",
    )
    return path


def _shadow_config(tmp_path: Path) -> Path:
    etsa = _write_strategy(tmp_path / "etsa.py", {"targets": {"AUB": "0.10"}})
    rps = _write_strategy(tmp_path / "rps.py", {"targets": {"EMB": "0.20"}})
    tlaq = _write_strategy(tmp_path / "tlaq.py", {"deltas": {"SVXY": "1"}})
    command = lambda path: json.dumps([sys.executable, str(path)])
    cwd = json.dumps(str(tmp_path))
    path = tmp_path / "conductor.toml"
    path.write_text(
        f"""
[node]
id = "shadow-test"
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
bridge_db = "data/main.sqlite"
live_orders_enabled = false

[routes.ibkr_tlaq]
adapter = "nautilus_ibkr"
account = "TLAQ"
bridge_db = "data/tlaq.sqlite"
live_orders_enabled = false

[strategies.ETSA]
result_mode = "target_weights"
route_id = "ibkr_main"
cwd = {cwd}
command = {command(etsa)}
[strategies.ETSA.seed]
cash = 0
positions = {{ AEP = -59 }}

[strategies.RPSchteroids]
result_mode = "target_weights"
route_id = "ibkr_main"
cwd = {cwd}
command = {command(rps)}
[strategies.RPSchteroids.seed]
cash = 0
positions = {{ EMB = 86 }}

[strategies.TLAQ]
result_mode = "position_deltas"
route_id = "ibkr_tlaq"
cwd = {cwd}
command = {command(tlaq)}
[strategies.TLAQ.seed]
cash = 0
positions = {{ SVXY = 772 }}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def test_shadow_mirror_is_separate_from_virtual_ledger_and_doctor_uses_authority(tmp_path, monkeypatch) -> None:
    config = _shadow_config(tmp_path)
    adapters: dict[str, _ShadowAdapter] = {}

    def fake_adapter(**kwargs):
        route_id = kwargs["route_id"]
        positions = (
            {"AUB": Decimal("-142"), "EMB": Decimal("77")}
            if route_id == "ibkr_main"
            else {"SVXY": Decimal("558")}
        )
        adapter = _ShadowAdapter(route_id, Decimal("100000"), positions)
        adapters[route_id] = adapter
        return adapter

    monkeypatch.setattr("conductor.runtime.app.NautilusBridgeExecutionAdapter", fake_adapter)
    app = ConductorRuntimeApp.from_path(config)
    try:
        app.ledger.replace_runtime_intent(
            StrategyIntent("ETSA", {"AUB": Decimal("0.1")}, route_id="ibkr_main"),
            run_id="etsa-current",
        )
        app.ledger.replace_runtime_intent(
            StrategyIntent("RPSchteroids", {"EMB": Decimal("0.2")}, route_id="ibkr_main"),
            run_id="rps-current",
        )
        result = app.refresh_shadow_mirror("ibkr_main", commit=True)
        assert result["committed"] is True
        assert app.ledger.strategy_positions("ETSA") == {"AEP": Decimal("-59")}
        assert app.ledger.implementation_strategy_positions("ETSA") == {"AUB": Decimal("-142")}
        assert app.accounting.account_view("ETSA").positions == {"AUB": Decimal("-142")}

        doctor = app.bootstrap_reconciliation()
        # TLAQ has not entered shadow authority yet, so the all-route doctor still detects its drift.
        assert doctor["reconciled"] is False
        main_diffs = [x for x in doctor["differences"] if x["route_id"] == "ibkr_main"]
        assert main_diffs == []
        assert doctor["authority_by_route"]["ibkr_main"] == "external_shadow"
        assert doctor["virtual_ledger_reconciled"] is False
    finally:
        app.close()


def test_single_owner_shadow_refresh_then_promotion_is_exact(tmp_path, monkeypatch) -> None:
    config = _shadow_config(tmp_path)

    def fake_adapter(**kwargs):
        route_id = kwargs["route_id"]
        positions = {"SVXY": Decimal("558")} if route_id == "ibkr_tlaq" else {}
        return _ShadowAdapter(route_id, Decimal("100000"), positions)

    monkeypatch.setattr("conductor.runtime.app.NautilusBridgeExecutionAdapter", fake_adapter)
    app = ConductorRuntimeApp.from_path(config, route_scope={"ibkr_tlaq"})
    try:
        result = app.refresh_shadow_mirror("ibkr_tlaq", commit=True)
        assert result["ownership"]["TLAQ"] == {"SVXY": "558"}
        assert app.ledger.strategy_positions("TLAQ") == {"SVXY": Decimal("772")}
        assert app.ledger.implementation_strategy_positions("TLAQ") == {"SVXY": Decimal("558")}
        promoted = app.promote_shadow_route("ibkr_tlaq")
        assert promoted["promoted"] is True
        assert app.ledger.strategy_positions("TLAQ") == {"SVXY": Decimal("558")}
        assert app.ledger.shadow_route("ibkr_tlaq")["active"] is False
        assert app.bootstrap_reconciliation()["reconciled"] is True
    finally:
        app.close()


def test_shadow_cycle_captures_shared_route_intents_before_new_symbol_attribution(tmp_path, monkeypatch) -> None:
    config = _shadow_config(tmp_path)

    def fake_adapter(**kwargs):
        route_id = kwargs["route_id"]
        positions = (
            {"AUB": Decimal("-142"), "EMB": Decimal("77")}
            if route_id == "ibkr_main"
            else {"SVXY": Decimal("558")}
        )
        return _ShadowAdapter(route_id, Decimal("100000"), positions)

    monkeypatch.setattr("conductor.runtime.app.NautilusBridgeExecutionAdapter", fake_adapter)
    app = ConductorRuntimeApp.from_path(config, route_scope={"ibkr_main"})
    try:
        # Explicit initial migration manifest starts the mirror even though AUB is new relative to
        # the frozen virtual ledger. Subsequent cycles no longer need a manifest for sticky names.
        app.refresh_shadow_mirror(
            "ibkr_main",
            ownership={"ETSA": {"AUB": Decimal("-142")}, "RPSchteroids": {"EMB": Decimal("77")}},
            commit=True,
        )
        result = app.shadow_cycle("ibkr_main", trigger="test")
        assert [row["status"] for row in result["captures"]] == ["captured", "captured"]
        assert result["status"] == "planned"
        # Counterfactual planning must not rewrite the durable Conductor ownership ledger.
        assert app.ledger.strategy_positions("ETSA") == {"AEP": Decimal("-59")}
        assert app.ledger.implementation_strategy_positions("ETSA") == {"AUB": Decimal("-142")}
    finally:
        app.close()


def test_active_shadow_refuses_live_orders_enabled(tmp_path, monkeypatch) -> None:
    config = _shadow_config(tmp_path)

    def fake_adapter(**kwargs):
        return _ShadowAdapter(kwargs["route_id"], Decimal("100000"), {})

    monkeypatch.setattr("conductor.runtime.app.NautilusBridgeExecutionAdapter", fake_adapter)
    app = ConductorRuntimeApp.from_path(config, route_scope={"ibkr_main"})
    app.refresh_shadow_mirror(
        "ibkr_main", ownership={"ETSA": {}, "RPSchteroids": {}}, commit=True
    )
    app.close()

    text = config.read_text(encoding="utf-8")
    text = text.replace(
        '[routes.ibkr_main]\nadapter = "nautilus_ibkr"\naccount = "MAIN"\nbridge_db = "data/main.sqlite"\nlive_orders_enabled = false',
        '[routes.ibkr_main]\nadapter = "nautilus_ibkr"\naccount = "MAIN"\nbridge_db = "data/main.sqlite"\nlive_orders_enabled = true',
    )
    config.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="active external shadow authority"):
        ConductorRuntimeApp.from_path(config, route_scope={"ibkr_main"})
