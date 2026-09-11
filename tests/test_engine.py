from decimal import Decimal

from conductor.adapters.paper import PaperExecutionAdapter
from conductor.domain.models import (
    BrokerPosition,
    ExposureType,
    InstrumentSpec,
    SleeveAllocation,
    StrategyIntent,
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
