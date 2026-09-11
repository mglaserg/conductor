from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from conductor.domain.models import RiskDecision, VirtualTarget, ZERO, ONE


class SimplePortfolioRisk:
    """Deliberately boring V0.1 portfolio risk scaler.

    Targets are assumed to be NAV weights. It caps gross exposure and individual
    instrument aggregate exposure without changing relative strategy ownership.
    """

    def __init__(
        self,
        max_gross: Decimal = Decimal("1.50"),
        max_instrument_abs: Decimal = Decimal("0.20"),
    ) -> None:
        self.max_gross = max_gross
        self.max_instrument_abs = max_instrument_abs

    def scale(self, targets: Iterable[VirtualTarget]) -> tuple[list[VirtualTarget], RiskDecision]:
        items = list(targets)
        by_instrument: dict[str, Decimal] = {}
        for item in items:
            by_instrument[item.instrument] = by_instrument.get(item.instrument, ZERO) + item.target

        gross = sum((abs(v) for v in by_instrument.values()), ZERO)
        largest = max((abs(v) for v in by_instrument.values()), default=ZERO)

        gross_scale = ONE if gross <= self.max_gross or gross == ZERO else self.max_gross / gross
        instrument_scale = (
            ONE
            if largest <= self.max_instrument_abs or largest == ZERO
            else self.max_instrument_abs / largest
        )
        scale = min(ONE, gross_scale, instrument_scale)

        if scale == ONE:
            reason = "PASS"
        else:
            constraints = []
            if gross_scale < ONE:
                constraints.append("max_gross")
            if instrument_scale < ONE:
                constraints.append("max_instrument")
            reason = "SCALED:" + ",".join(constraints)

        scaled = [
            VirtualTarget(
                strategy_id=t.strategy_id,
                sleeve_id=t.sleeve_id,
                instrument=t.instrument,
                target=t.target * scale,
                exposure_type=t.exposure_type,
            )
            for t in items
        ]
        return scaled, RiskDecision(scale=scale, reason=reason)
