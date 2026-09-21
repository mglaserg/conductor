from __future__ import annotations

import json
import sys
from decimal import Decimal

from conductor.accounting import VirtualAccountingEngine
from conductor.adapters.paper import PaperExecutionAdapter
from conductor.domain.models import BrokerPosition, InstrumentSpec, SleeveAllocation
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import PortfolioBuilder
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine
from conductor.runtime.models import NativeResultMode, StrategyRunnerProfile
from conductor.runtime.orchestrator import StrategyRunOrchestrator


def test_orchestrator_runs_legacy_rps_then_tlaq_without_flattening_other_books(tmp_path) -> None:
    script = tmp_path / "strategy.py"
    script.write_text(
        """
import json, os
sid = os.environ['CONDUCTOR_STRATEGY_ID']
out = os.environ['CONDUCTOR_OUTPUT']
if sid == 'RPSchteroids':
    payload = {'targets': {'EQ.US.AAPL': 35}}
elif sid == 'TLAQ':
    payload = {'deltas': {'EQ.US.AAPL': 20, 'EQ.US.TLT': -10}}
else:
    payload = {'targets': {'EQ.US.AAPL': 0.02}}
with open(out, 'w', encoding='utf-8') as f:
    json.dump(payload, f)
""".strip()
        + "\n",
        encoding="utf-8",
    )

    route = "windows_ibkr_equities"
    profiles = {
        "ETSA": StrategyRunnerProfile(
            "ETSA",
            "equities",
            NativeResultMode.TARGET_WEIGHTS,
            (sys.executable, str(script)),
            tmp_path,
            route_id=route,
        ),
        "RPSchteroids": StrategyRunnerProfile(
            "RPSchteroids",
            "equities",
            NativeResultMode.TARGET_QUANTITIES,
            (sys.executable, str(script)),
            tmp_path,
            route_id=route,
        ),
        "TLAQ": StrategyRunnerProfile(
            "TLAQ",
            "equities",
            NativeResultMode.POSITION_DELTAS,
            (sys.executable, str(script)),
            tmp_path,
            route_id=route,
        ),
    }
    instruments = {
        "EQ.US.AAPL": InstrumentSpec("EQ.US.AAPL", Decimal("100")),
        "EQ.US.TLT": InstrumentSpec("EQ.US.TLT", Decimal("100")),
    }
    nav = Decimal("200000")
    portfolio = PortfolioBuilder(
        {
            "equities": SleeveAllocation(
                "equities",
                {"ETSA": Decimal("0.5"), "RPSchteroids": Decimal("0.25"), "TLAQ": Decimal("0.25")},
            )
        },
        instruments,
        portfolio_nav=nav,
    )
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    initial = {
        "ETSA": {"EQ.US.AAPL": Decimal("10")},
        "RPSchteroids": {"EQ.US.AAPL": Decimal("50")},
        "TLAQ": {"EQ.US.AAPL": Decimal("100"), "EQ.US.TLT": Decimal("400")},
    }
    cash = {"ETSA": Decimal("90000"), "RPSchteroids": Decimal("5000"), "TLAQ": Decimal("-12000")}
    capital = {"ETSA": Decimal("100000"), "RPSchteroids": Decimal("50000"), "TLAQ": Decimal("50000")}
    for sid, positions in initial.items():
        ledger.ensure_strategy(sid)
        ledger.seed_strategy_account(
            sid,
            route_id=route,
            allocated_capital=capital[sid],
            cash=cash[sid],
        )
        ledger.seed_virtual_book(
            strategy_id=sid,
            book_id="main",
            sleeve_id="equities",
            route_id=route,
            positions=positions,
        )

    paper = PaperExecutionAdapter(
        [
            BrokerPosition("EQ.US.AAPL", Decimal("160"), route),
            BrokerPosition("EQ.US.TLT", Decimal("400"), route),
        ]
    )
    accounting = VirtualAccountingEngine(ledger, instruments)
    engine = ConductorEngine(
        portfolio=portfolio,
        reconciler=DesiredStateReconciler(),
        execution=paper,
        risk=PortfolioRiskEngine(nav, max_gross_leverage=Decimal("10"), max_instrument_nav=Decimal("10")),
        order_planner=OrderPlanner(portfolio_nav=nav, instruments=instruments, min_trade_nav_bps=Decimal("0")),
        ledger=ledger,
        accounting=accounting,
    )
    orchestrator = StrategyRunOrchestrator(
        ledger=ledger,
        accounting=accounting,
        engine=engine,
        run_root=tmp_path / "runs",
        profiles=profiles,
    )

    rps = orchestrator.run(profiles["RPSchteroids"], trigger="test")
    assert rps.status == "succeeded"
    # ETSA and TLAQ were held at current ownership; only RPS 50 -> 35 traded externally.
    assert rps.portfolio_result is not None
    assert [(d.instrument, d.delta) for d in rps.portfolio_result.deltas] == [
        ("EQ.US.AAPL", Decimal("-15"))
    ]
    after_rps = {(x["strategy_id"], x["instrument"]): Decimal(x["quantity"]) for x in ledger.virtual_positions()}
    assert after_rps[("ETSA", "EQ.US.AAPL")] == 10
    assert after_rps[("TLAQ", "EQ.US.AAPL")] == 100

    tlaq = orchestrator.run(profiles["TLAQ"], trigger="test")
    assert tlaq.status == "succeeded"
    assert tlaq.portfolio_result is not None
    assert {(d.instrument, d.delta) for d in tlaq.portfolio_result.deltas} == {
        ("EQ.US.AAPL", Decimal("20")),
        ("EQ.US.TLT", Decimal("-10")),
    }
    state = json.loads((tmp_path / "runs" / "TLAQ" / tlaq.run_id / "account_state.json").read_text())
    assert state["cash"] == "-12000"
    assert state["positions"]["EQ.US.AAPL"] == "100"


def test_retire_flattens_only_that_virtual_book_and_keeps_history(tmp_path) -> None:
    route = "windows_ibkr_equities"
    instruments = {"EQ.US.AAPL": InstrumentSpec("EQ.US.AAPL", Decimal("100"))}
    nav = Decimal("100000")
    portfolio = PortfolioBuilder(instruments=instruments, portfolio_nav=nav)
    ledger = ConductorLedger(tmp_path / "retire.sqlite")
    profile = StrategyRunnerProfile(
        "TLAQ",
        "equities",
        NativeResultMode.POSITION_DELTAS,
        (sys.executable, "-c", "pass"),
        tmp_path,
        route_id=route,
    )
    ledger.ensure_strategy("TLAQ")
    ledger.seed_strategy_account(
        "TLAQ",
        route_id=route,
        allocated_capital=Decimal("10000"),
        cash=Decimal("0"),
    )
    ledger.seed_virtual_book(
        strategy_id="TLAQ",
        book_id="main",
        sleeve_id="equities",
        route_id=route,
        positions={"EQ.US.AAPL": Decimal("10")},
    )
    paper = PaperExecutionAdapter([BrokerPosition("EQ.US.AAPL", Decimal("10"), route)])
    accounting = VirtualAccountingEngine(ledger, instruments)
    engine = ConductorEngine(
        portfolio=portfolio,
        reconciler=DesiredStateReconciler(),
        execution=paper,
        risk=PortfolioRiskEngine(nav, max_gross_leverage=Decimal("10"), max_instrument_nav=Decimal("10")),
        order_planner=OrderPlanner(portfolio_nav=nav, instruments=instruments, min_trade_nav_bps=Decimal("0")),
        ledger=ledger,
        accounting=accounting,
    )
    orchestrator = StrategyRunOrchestrator(
        ledger=ledger,
        accounting=accounting,
        engine=engine,
        run_root=tmp_path / "runs",
        profiles={"TLAQ": profile},
    )
    result = orchestrator.retire(profile)
    assert result.reconciled
    assert ledger.strategy_positions("TLAQ") == {}
    assert ledger.strategy_lifecycle("TLAQ") == "retired"
    assert any(e["event_type"] == "strategy.lifecycle_changed" for e in ledger.events())
