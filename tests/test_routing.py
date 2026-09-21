from decimal import Decimal

from conductor.domain.models import BrokerPosition, ExposureType, StrategyIntent
from conductor.portfolio import PortfolioBuilder
from conductor.reconcile import DesiredStateReconciler


def test_same_instrument_on_different_routes_does_not_net() -> None:
    from conductor.domain.models import InstrumentSpec

    instruments = {"BTC": InstrumentSpec("BTC", Decimal("100"), asset_class="crypto")}
    portfolio = PortfolioBuilder(instruments=instruments)
    intents = [
        StrategyIntent(
            "A",
            {"BTC": Decimal("1")},
            ExposureType.QUANTITY,
            route_id="hl_a",
        ),
        StrategyIntent(
            "B",
            {"BTC": Decimal("-1")},
            ExposureType.QUANTITY,
            route_id="hl_b",
        ),
    ]
    aggregate = portfolio.aggregate(portfolio.build_virtual_targets(intents))
    assert len(aggregate) == 2
    deltas = DesiredStateReconciler().reconcile(
        aggregate,
        [
            BrokerPosition("BTC", Decimal("0"), route_id="hl_a"),
            BrokerPosition("BTC", Decimal("0"), route_id="hl_b"),
        ],
    )
    assert {d.route_id for d in deltas} == {"hl_a", "hl_b"}
