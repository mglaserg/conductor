from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

from conductor.command import main as command_main
from conductor.config import load_runtime_config
from conductor.runtime.app import ConductorRuntimeApp


def _write_paper_fixture(tmp_path: Path) -> Path:
    strategy = tmp_path / "strategy.py"
    strategy.write_text(
        """
import json, os
with open(os.environ["CONDUCTOR_OUTPUT"], "w", encoding="utf-8") as handle:
    json.dump({"targets": {"EQ.US.AAPL": "0.50", "EQ.US.MSFT": "0.25"}}, handle)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "conductor.toml"
    command = json.dumps([sys.executable, str(strategy)])
    cwd = json.dumps(str(tmp_path))
    config.write_text(
        f"""
[node]
id = "portable-node"
state_db = "data/conductor.sqlite"
run_root = "data/runs"
portfolio_nav = 100000

[portfolio]
allocator = "static"

[portfolio.static.weights]
ETSA = 0.40
RPSchteroids = 0.15
TLAQ = 0.45

[paper]
default_price = 100

[paper_prices]
"EQ.US.AAPL" = 200

[risk]
max_gross_leverage = 2
max_instrument_nav = 1

[execution]
min_trade_nav_bps = 0

[routes.shared]
adapter = "nautilus_ibkr"
bridge_db = "data/live-bridge.sqlite"
live_orders_enabled = true

[strategies.ETSA]
result_mode = "target_weights"
route_id = "shared"
cwd = {cwd}
command = {command}

[strategies.RPSchteroids]
result_mode = "target_weights"
route_id = "shared"
cwd = {cwd}
command = {command}

[strategies.TLAQ]
result_mode = "target_weights"
route_id = "shared"
cwd = {cwd}
command = {command}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def test_paper_config_uses_separate_database_static_allocation_and_casefold_lookup(
    tmp_path, monkeypatch
) -> None:
    config_path = _write_paper_fixture(tmp_path)
    config = load_runtime_config(config_path)
    assert config.paper_state_db == tmp_path / "data" / "conductor.paper.sqlite"

    def refuse_live_adapter(*args, **kwargs):
        raise AssertionError("paper mode constructed the live execution adapter")

    monkeypatch.setattr(
        "conductor.runtime.app.NautilusBridgeExecutionAdapter", refuse_live_adapter
    )
    app = ConductorRuntimeApp(config, paper=True)
    try:
        assert app.resolve_strategy_id("ETSA") == "ETSA"
        assert app.resolve_strategy_id("etsa") == "ETSA"
        assert app.resolve_strategy_id("Etsa") == "ETSA"
        accounts = {
            row["strategy_id"]: (row["allocated_capital"], row["cash"])
            for row in app.ledger.strategy_accounts()
        }
        assert accounts == {
            "ETSA": ("40000.0", "40000.0"),
            "RPSchteroids": ("15000.00", "15000.00"),
            "TLAQ": ("45000.00", "45000.00"),
        }
        assert app.status()["runtime_mode"] == "paper"
    finally:
        app.close()

    assert config.paper_state_db.exists()
    assert not config.state_db.exists()
    assert not (tmp_path / "data" / "live-bridge.sqlite").exists()


def test_offline_paper_run_is_funded_priced_durable_audited_and_idempotent(
    tmp_path, monkeypatch
) -> None:
    config_path = _write_paper_fixture(tmp_path)

    def refuse_live_adapter(*args, **kwargs):
        raise AssertionError("paper mode constructed the live execution adapter")

    monkeypatch.setattr(
        "conductor.runtime.app.NautilusBridgeExecutionAdapter", refuse_live_adapter
    )
    first_app = ConductorRuntimeApp.from_path(config_path, paper=True)
    first = first_app.run_strategy("etsa", trigger="test")
    assert first.status == "succeeded"
    assert first.portfolio_result is not None
    assert {(item.instrument, item.delta) for item in first.portfolio_result.deltas} == {
        ("EQ.US.AAPL", Decimal(100)),
        ("EQ.US.MSFT", Decimal(100)),
    }
    input_state = json.loads(
        (
            first_app.run_root
            / "ETSA"
            / first.run_id
            / "account_state.json"
        ).read_text(encoding="utf-8")
    )
    assert input_state["allocated_capital"] == "40000.0"
    assert input_state["cash"] == "40000.0"
    assert Decimal(first_app.ledger.strategy_account("ETSA")["cash"]) == Decimal(10000)
    assert first_app.ledger.paper_broker_positions("shared") == {
        "EQ.US.AAPL": Decimal(100),
        "EQ.US.MSFT": Decimal(100),
    }
    event_types = {event["event_type"] for event in first_app.ledger.events()}
    assert {
        "paper.allocation_decision",
        "runtime.target_resolved",
        "reconciliation_planned",
        "paper.fill",
        "execution.reports_received",
        "run_completed",
    } <= event_types
    first_app.close()

    second_app = ConductorRuntimeApp.from_path(config_path, paper=True)
    try:
        second = second_app.run_strategy("ETSA", trigger="test")
        assert second.status == "succeeded"
        assert second.portfolio_result is not None
        assert second.portfolio_result.deltas == ()
        assert second_app.ledger.paper_broker_positions("shared") == {
            "EQ.US.AAPL": Decimal(100),
            "EQ.US.MSFT": Decimal(100),
        }
    finally:
        second_app.close()


def test_cli_run_and_status_accept_paper_flag(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_paper_fixture(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["conductor", "run", "Etsa", "--paper", "--config", str(config_path)],
    )
    command_main()
    run_output = json.loads(capsys.readouterr().out)
    assert run_output["strategy_id"] == "ETSA"
    assert run_output["status"] == "succeeded"

    monkeypatch.setattr(
        sys,
        "argv",
        ["conductor", "status", "--paper", "--config", str(config_path)],
    )
    command_main()
    status_output = json.loads(capsys.readouterr().out)
    assert status_output["runtime_mode"] == "paper"
    assert status_output["state_db"].endswith("conductor.paper.sqlite")
