from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_DOWN
from typing import Iterable

from conductor.domain.models import ONE, RiskDecision, VirtualTarget, ZERO


class PortfolioRiskEngine:
    """Portfolio-level exposure governor applied after strategy capital allocation."""

    def __init__(
        self,
        portfolio_nav: Decimal,
        max_gross_leverage: Decimal = Decimal("1.50"),
        max_instrument_nav: Decimal = Decimal("0.20"),
    ) -> None:
        if portfolio_nav <= ZERO:
            raise ValueError("portfolio_nav must be positive")
        if max_gross_leverage <= ZERO:
            raise ValueError("max_gross_leverage must be positive")
        if max_instrument_nav <= ZERO:
            raise ValueError("max_instrument_nav must be positive")
        self.portfolio_nav = portfolio_nav
        self.max_gross_leverage = max_gross_leverage
        self.max_instrument_nav = max_instrument_nav

    @staticmethod
    def _round_quantity(quantity: Decimal, lot_size: Decimal) -> Decimal:
        lots = (abs(quantity) / lot_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = lots * lot_size
        return rounded if quantity >= ZERO else -rounded

    def apply(self, targets: Iterable[VirtualTarget]) -> tuple[list[VirtualTarget], RiskDecision]:
        items = list(targets)
        by_market: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for item in items:
            by_market[(item.route_id, item.instrument)] += item.notional

        gross = sum((abs(v) for v in by_market.values()), ZERO)
        largest = max((abs(v) for v in by_market.values()), default=ZERO)

        gross_cap = self.portfolio_nav * self.max_gross_leverage
        instrument_cap = self.portfolio_nav * self.max_instrument_nav
        gross_scale = ONE if gross <= gross_cap or gross == ZERO else gross_cap / gross
        instrument_scale = (
            ONE if largest <= instrument_cap or largest == ZERO else instrument_cap / largest
        )
        scale = min(ONE, gross_scale, instrument_scale)

        constraints: list[str] = []
        if gross_scale < ONE:
            constraints.append("max_gross")
        if instrument_scale < ONE:
            constraints.append("max_instrument")
        reason = "PASS" if not constraints else "SCALED:" + ",".join(constraints)

        if scale == ONE:
            return items, RiskDecision(
                scale=ONE,
                reason=reason,
                gross_before=gross,
                gross_after=gross,
                largest_instrument_before=largest,
            )

        scaled: list[VirtualTarget] = []
        for item in items:
            quantity = self._round_quantity(item.target * scale, item.lot_size)
            if quantity == ZERO:
                continue
            unit_notional = item.notional / item.target
            scaled.append(
                VirtualTarget(
                    strategy_id=item.strategy_id,
                    sleeve_id=item.sleeve_id,
                    instrument=item.instrument,
                    target=quantity,
                    notional=quantity * unit_notional,
                    exposure_type=item.exposure_type,
                    source_exposure_type=item.source_exposure_type,
                    lot_size=item.lot_size,
                    route_id=item.route_id,
                )
            )

        by_market_after: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for item in scaled:
            by_market_after[(item.route_id, item.instrument)] += item.notional
        gross_after = sum((abs(v) for v in by_market_after.values()), ZERO)

        return scaled, RiskDecision(
            scale=scale,
            reason=reason,
            gross_before=gross,
            gross_after=gross_after,
            largest_instrument_before=largest,
        )


# Compatibility alias for V0.1 callers.
SimplePortfolioRisk = PortfolioRiskEngine
