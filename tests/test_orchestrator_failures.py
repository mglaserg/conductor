from __future__ import annotations

import json
import sys
from decimal import Decimal
from textwrap import dedent

import pytest

from conductor.accounting import VirtualAccountingEngine
from conductor.adapters.paper import PaperExecutionAdapter
from conductor.domain.models import (
    BrokerPosition,
    ExecutionReport,
    InstrumentSpec,
    RunState,
    TradeDelta,
)
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import PortfolioBuilder
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine
from conductor.runtime.models import NativeResultMode, StrategyRunnerProfile
from conductor.runtime.orchestrator import StrategyRunOrchestrator


class RejectingExecutionAdapter:
    def positions(self) -> list[BrokerPosition]:
        return [BrokerPosition("EQ.US.AAPL", Decimal(10), "windows_ibkr_equities")]

    def submit_deltas(self, deltas: list[TradeDelta]) -> list[ExecutionReport]:
        delta = deltas[0]
        return [
            ExecutionReport(
                route_id=delta.route_id,
                instrument=delta.instrument,
                requested_quantity=delta.delta,
                filled_quantity=Decimal(0),
                status="rejected",
                order_id="rejected-order",
            )
        ]


def _failure_runtime(tmp_path, source: str, *, timeout_seconds: int = 5):
    script = tmp_path / "strategy.py"
    script.write_text(source, encoding="utf-8")
    route = "windows_ibkr_equities"
    profile = StrategyRunnerProfile(
        strategy_id="ETSA",
        sleeve_id="equities",
        result_mode=NativeResultMode.TARGET_QUANTITIES,
        command=(sys.executable, str(script)),
        cwd=tmp_path,
        route_id=route,
        timeout_seconds=timeout_seconds,
    )
    instrument = "EQ.US.AAPL"
    instruments = {instrument: InstrumentSpec(instrument, Decimal(100))}
    nav = Decimal(100_000)
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    ledger.ensure_strategy(profile.strategy_id)
    ledger.seed_strategy_account(
        profile.strategy_id,
        route_id=route,
        allocated_capital=nav,
        cash=Decimal(90_000),
    )
    ledger.seed_virtual_book(
        strategy_id=profile.strategy_id,
        book_id=profile.book_id,
        sleeve_id=profile.sleeve_id,
        route_id=route,
        positions={instrument: Decimal(10)},
    )
    paper = PaperExecutionAdapter([BrokerPosition(instrument, Decimal(10), route)])
    accounting = VirtualAccountingEngine(ledger, instruments)
    engine = ConductorEngine(
        portfolio=PortfolioBuilder(instruments=instruments, portfolio_nav=nav),
        reconciler=DesiredStateReconciler(),
        execution=paper,
        risk=PortfolioRiskEngine(
            nav,
            max_gross_leverage=Decimal(10),
            max_instrument_nav=Decimal(10),
        ),
        order_planner=OrderPlanner(
            portfolio_nav=nav,
            instruments=instruments,
            min_trade_nav_bps=Decimal(0),
        ),
        ledger=ledger,
        accounting=accounting,
    )
    orchestrator = StrategyRunOrchestrator(
        ledger=ledger,
        accounting=accounting,
        engine=engine,
        run_root=tmp_path / "runs",
        profiles={profile.strategy_id: profile},
    )
    return orchestrator, profile, ledger, paper


@pytest.mark.parametrize(
    ("source", "timeout_seconds", "expected_status", "error_fragment"),
    [
        ("raise SystemExit(7)\n", 5, "failed", "exited with code 7"),
        ("import time\ntime.sleep(5)\n", 1, "timed_out", "exceeded 1s timeout"),
        (
            dedent(
                """
                import os
                from pathlib import Path

                Path(os.environ['CONDUCTOR_OUTPUT']).write_text('{bad', encoding='utf-8')
                """
            ).lstrip(),
            5,
            "failed",
            "normalization/portfolio cycle failed",
        ),
    ],
    ids=("nonzero-exit", "timeout", "malformed-output"),
)
def test_strategy_process_failures_preserve_committed_state_and_are_audited(
    tmp_path,
    source: str,
    timeout_seconds: int,
    expected_status: str,
    error_fragment: str,
) -> None:
    orchestrator, profile, ledger, paper = _failure_runtime(
        tmp_path, source, timeout_seconds=timeout_seconds
    )

    outcome = orchestrator.run(profile, trigger="test")

    assert outcome.status == expected_status
    assert outcome.error is not None and error_fragment in outcome.error
    assert ledger.runtime_intents() == []
    assert ledger.strategy_positions(profile.strategy_id) == {"EQ.US.AAPL": Decimal(10)}
    assert paper.submissions == []
    persisted_run = ledger.strategy_runs()[0]
    assert persisted_run["status"] == expected_status
    finished = [
        json.loads(event["payload_json"])
        for event in ledger.events()
        if event["event_type"] == "strategy.run_finished"
    ]
    assert finished[-1]["status"] == expected_status
    assert error_fragment in finished[-1]["error"]


def test_terminal_execution_rejection_propagates_as_blocked_run(tmp_path) -> None:
    source = dedent(
        """
        import json
        import os

        with open(os.environ['CONDUCTOR_OUTPUT'], 'w', encoding='utf-8') as output:
            json.dump({'targets': {'EQ.US.AAPL': 20}}, output)
        """
    ).lstrip()
    orchestrator, profile, ledger, _paper = _failure_runtime(tmp_path, source)
    orchestrator.engine.execution = RejectingExecutionAdapter()

    outcome = orchestrator.run(profile, trigger="test")

    assert outcome.status == "blocked"
    assert outcome.error == "portfolio cycle blocked by terminal execution outcome"
    assert outcome.portfolio_result is not None
    assert outcome.portfolio_result.state is RunState.BLOCKED
    assert ledger.strategy_positions(profile.strategy_id) == {"EQ.US.AAPL": Decimal(10)}
    assert ledger.strategy_runs()[0]["status"] == "blocked"
