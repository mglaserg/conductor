from decimal import Decimal

from conductor.domain.models import ExposureType, VirtualTarget
from conductor.risk import PortfolioRiskEngine


def test_risk_scales_to_single_instrument_cap() -> None:
    risk = PortfolioRiskEngine(
        portfolio_nav=Decimal("100000"),
        max_gross_leverage=Decimal("2"),
        max_instrument_nav=Decimal("0.20"),
    )
    targets = [
        VirtualTarget(
            strategy_id="ETSA",
            sleeve_id="equities",
            instrument="AAPL",
            target=Decimal("500"),
            notional=Decimal("50000"),
            source_exposure_type=ExposureType.NAV_WEIGHT,
        )
    ]
    scaled, decision = risk.apply(targets)
    assert decision.reason == "SCALED:max_instrument"
    assert decision.scale == Decimal("0.4")
    assert scaled[0].target == Decimal("200")
    assert scaled[0].notional == Decimal("20000.0")
