from decimal import Decimal

import pytest

from conductor.adapters.paper import PaperExecutionAdapter
from conductor.domain.models import (
    BrokerPosition,
    ExecutionReport,
    ExposureType,
    InstrumentSpec,
    RunState,
    SleeveAllocation,
    StrategyIntent,
    TradeDelta,
)
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import PortfolioBuilder
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine


def test_engine_commits_ownership_only_after_reconciliation_and_second_run_is_empty(
    tmp_path,
) -> None:
    nav = Decimal("100000")
    instruments = {"AAPL": InstrumentSpec("AAPL", Decimal("100"))}
    portfolio = PortfolioBuilder(
        {"equities": SleeveAllocation("equities", {"ETSA": Decimal("1")})},
        instruments,
        portfolio_nav=nav,
    )
    paper = PaperExecutionAdapter([BrokerPosition("AAPL", Decimal("50"))])
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    engine = ConductorEngine(
        portfolio=portfolio,
        reconciler=DesiredStateReconciler(),
        execution=paper,
        risk=PortfolioRiskEngine(nav),
        order_planner=OrderPlanner(portfolio_nav=nav, instruments=instruments),
        ledger=ledger,
    )
    intents = [
        StrategyIntent(
            "ETSA",
            {"AAPL": Decimal("0.10")},
            ExposureType.NAV_WEIGHT,
            sleeve_id="equities",
        )
    ]

    first = engine.run_cycle(intents)
    assert first.reconciled
    assert first.deltas[0].delta == Decimal("50")
    assert ledger.virtual_positions()[0]["quantity"] == "100"

    second = engine.run_cycle(intents)
    assert second.reconciled
    assert second.deltas == ()


class TerminalExecutionAdapter:
    def __init__(self, *, status: str, filled_quantity: Decimal) -> None:
        self.status = status
        self.filled_quantity = filled_quantity
        self.quantity = Decimal(50)

    def positions(self) -> list[BrokerPosition]:
        return [BrokerPosition("AAPL", self.quantity)]

    def submit_deltas(self, deltas: list[TradeDelta]) -> list[ExecutionReport]:
        delta = deltas[0]
        self.quantity += self.filled_quantity
        return [
            ExecutionReport(
                route_id=delta.route_id,
                instrument=delta.instrument,
                requested_quantity=delta.delta,
                filled_quantity=self.filled_quantity,
                avg_price=Decimal(100) if self.filled_quantity else None,
                status=self.status,
                order_id="terminal-order",
            )
        ]


@pytest.mark.parametrize(
    ("status", "filled_quantity"),
    [("rejected", Decimal(0)), ("canceled", Decimal(20))],
    ids=("rejected", "partial-then-canceled"),
)
def test_terminal_execution_failure_blocks_without_committing_virtual_ownership(
    tmp_path, status: str, filled_quantity: Decimal
) -> None:
    nav = Decimal(100_000)
    instruments = {"AAPL": InstrumentSpec("AAPL", Decimal(100))}
    ledger = ConductorLedger(tmp_path / "blocked.sqlite")
    ledger.seed_virtual_book(
        strategy_id="ETSA",
        book_id="main",
        sleeve_id="equities",
        route_id="default",
        positions={"AAPL": Decimal(50)},
    )
    execution = TerminalExecutionAdapter(status=status, filled_quantity=filled_quantity)
    engine = ConductorEngine(
        portfolio=PortfolioBuilder(instruments=instruments, portfolio_nav=nav),
        reconciler=DesiredStateReconciler(),
        execution=execution,
        risk=PortfolioRiskEngine(nav),
        order_planner=OrderPlanner(portfolio_nav=nav, instruments=instruments),
        ledger=ledger,
    )
    intent = StrategyIntent(
        "ETSA",
        {"AAPL": Decimal(100)},
        ExposureType.QUANTITY,
        sleeve_id="equities",
    )

    result = engine.run_cycle([intent])

    assert result.state is RunState.BLOCKED
    assert not result.reconciled
    assert ledger.strategy_positions("ETSA") == {"AAPL": Decimal(50)}
    completed = [event for event in ledger.events() if event["event_type"] == "run_completed"]
    assert '"state": "blocked"' in completed[-1]["payload_json"]
