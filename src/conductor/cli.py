from __future__ import annotations

from decimal import Decimal
from tempfile import NamedTemporaryFile

from conductor.adapters.paper import PaperExecutionAdapter
from conductor.domain.models import (
    BrokerPosition,
    ExposureType,
    SleeveAllocation,
    StrategyIntent,
)
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.portfolio import PortfolioBuilder
from conductor.reconcile import DesiredStateReconciler


def main() -> None:
    """Tiny ETSA + RPSchteroids desired-state demonstration."""
    allocations = {
        "equities": SleeveAllocation(
            sleeve_id="equities",
            strategy_weights={
                "ETSA": Decimal("0.85"),
                "RPSchteroids": Decimal("0.15"),
            },
        )
    }
    intents = [
        StrategyIntent(
            strategy_id="ETSA",
            sleeve_id="equities",
            exposure_type=ExposureType.QUANTITY,
            targets={"AAPL": Decimal("120"), "MSFT": Decimal("-40")},
        ),
        StrategyIntent(
            strategy_id="RPSchteroids",
            sleeve_id="equities",
            exposure_type=ExposureType.QUANTITY,
            targets={"AAPL": Decimal("20"), "NVDA": Decimal("30")},
        ),
    ]

    paper = PaperExecutionAdapter(
        [
            BrokerPosition("AAPL", Decimal("100")),
            BrokerPosition("MSFT", Decimal("-30")),
        ]
    )

    with NamedTemporaryFile(suffix=".sqlite") as db:
        engine = ConductorEngine(
            portfolio=PortfolioBuilder(allocations),
            reconciler=DesiredStateReconciler(),
            execution=paper,
            ledger=ConductorLedger(db.name),
        )
        deltas = engine.run_once(intents)

    print("CONDUCTOR V0.1 — DESIRED STATE PLAN")
    for delta in deltas:
        side = "BUY" if delta.delta > 0 else "SELL"
        print(
            f"{delta.instrument:8s} current={delta.current:>8} "
            f"desired={delta.desired:>8}  {side:4s} {abs(delta.delta)}"
        )


if __name__ == "__main__":
    main()
