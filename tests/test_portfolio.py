from decimal import Decimal

from conductor.domain.models import ExposureType, SleeveAllocation, StrategyIntent
from conductor.portfolio import PortfolioBuilder


def test_shared_account_targets_net_across_strategies() -> None:
    builder = PortfolioBuilder(
        {
            "equities": SleeveAllocation(
                "equities",
                {"ETSA": Decimal("0.85"), "RPSchteroids": Decimal("0.15")},
            )
        }
    )
    virtual = builder.build_virtual_targets(
        [
            StrategyIntent(
                "ETSA",
                {"AAPL": Decimal("100")},
                ExposureType.QUANTITY,
                "equities",
            ),
            StrategyIntent(
                "RPSchteroids",
                {"AAPL": Decimal("-20")},
                ExposureType.QUANTITY,
                "equities",
            ),
        ]
    )
    aggregate = builder.aggregate(virtual)

    assert len(virtual) == 2
    assert aggregate[0].instrument == "AAPL"
    assert aggregate[0].target == Decimal("82")
