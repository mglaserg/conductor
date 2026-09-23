from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_DOWN
from typing import Iterable, Mapping

from conductor.domain.models import ONE, RiskDecision, VirtualTarget, ZERO


class PortfolioRiskEngine:
    """Exposure governor with independent limits for independently funded routes.

    ``portfolio_nav`` may be a single Decimal for the legacy one-pool runtime or a
    ``route_id -> NAV`` mapping for the canonical multi-account runtime. In multi-route mode
    each route is scaled independently; one account can never donate unused risk capacity to
    another account.
    """

    def __init__(
        self,
        portfolio_nav: Decimal | Mapping[str, Decimal],
        max_gross_leverage: Decimal | Mapping[str, Decimal] = Decimal("1.50"),
        max_net_exposure: Decimal | Mapping[str, Decimal] = Decimal("1.00"),
        max_instrument_nav: Decimal | Mapping[str, Decimal] = Decimal("0.20"),
    ) -> None:
        self.portfolio_nav = portfolio_nav
        self.max_gross_leverage = max_gross_leverage
        self.max_net_exposure = max_net_exposure
        self.max_instrument_nav = max_instrument_nav

        navs = portfolio_nav.values() if isinstance(portfolio_nav, Mapping) else [portfolio_nav]
        if any(value <= ZERO for value in navs):
            raise ValueError("portfolio_nav must be positive")
        for name, value in (
            ("max_gross_leverage", max_gross_leverage),
            ("max_net_exposure", max_net_exposure),
            ("max_instrument_nav", max_instrument_nav),
        ):
            values = value.values() if isinstance(value, Mapping) else [value]
            if any(item <= ZERO for item in values):
                raise ValueError(f"{name} must be positive")

    @staticmethod
    def _round_quantity(quantity: Decimal, lot_size: Decimal) -> Decimal:
        lots = (abs(quantity) / lot_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = lots * lot_size
        return rounded if quantity >= ZERO else -rounded

    @staticmethod
    def _route_value(value: Decimal | Mapping[str, Decimal], route_id: str, field: str) -> Decimal:
        if isinstance(value, Mapping):
            try:
                return value[route_id]
            except KeyError as exc:
                raise KeyError(f"no {field} configured for route {route_id}") from exc
        return value

    def apply(self, targets: Iterable[VirtualTarget]) -> tuple[list[VirtualTarget], RiskDecision]:
        items = list(targets)
        by_route_market: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: ZERO)
        )
        for item in items:
            by_route_market[item.route_id][item.instrument] += item.notional

        route_scales: dict[str, Decimal] = {}
        route_reasons: dict[str, str] = {}
        gross_before = ZERO
        net_before = ZERO
        largest_before = ZERO

        routes = sorted({item.route_id for item in items})
        for route_id in routes:
            values = by_route_market[route_id]
            gross = sum((abs(value) for value in values.values()), ZERO)
            net = sum(values.values(), ZERO)
            largest = max((abs(value) for value in values.values()), default=ZERO)
            gross_before += gross
            net_before += net
            largest_before = max(largest_before, largest)

            nav = self._route_value(self.portfolio_nav, route_id, "portfolio NAV")
            max_gross = self._route_value(
                self.max_gross_leverage, route_id, "max_gross_leverage"
            )
            max_net = self._route_value(self.max_net_exposure, route_id, "max_net_exposure")
            max_instrument = self._route_value(
                self.max_instrument_nav, route_id, "max_instrument_nav"
            )

            gross_cap = nav * max_gross
            net_cap = nav * max_net
            instrument_cap = nav * max_instrument
            gross_scale = ONE if gross <= gross_cap or gross == ZERO else gross_cap / gross
            net_scale = ONE if abs(net) <= net_cap or net == ZERO else net_cap / abs(net)
            instrument_scale = (
                ONE if largest <= instrument_cap or largest == ZERO else instrument_cap / largest
            )
            scale = min(ONE, gross_scale, net_scale, instrument_scale)
            route_scales[route_id] = scale

            constraints: list[str] = []
            if gross_scale < ONE:
                constraints.append("max_gross")
            if net_scale < ONE:
                constraints.append("max_net")
            if instrument_scale < ONE:
                constraints.append("max_instrument")
            route_reasons[route_id] = "PASS" if not constraints else "SCALED:" + ",".join(
                constraints
            )

        scaled: list[VirtualTarget] = []
        for item in items:
            scale = route_scales.get(item.route_id, ONE)
            if scale == ONE:
                scaled.append(item)
                continue
            quantity = self._round_quantity(item.target * scale, item.lot_size)
            if quantity == ZERO:
                continue
            unit_notional = item.notional / item.target
            scaled.append(
                VirtualTarget(
                    strategy_id=item.strategy_id,
                    book_id=item.book_id,
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

        after_market: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for item in scaled:
            after_market[(item.route_id, item.instrument)] += item.notional
        gross_after = sum((abs(value) for value in after_market.values()), ZERO)
        net_after = sum(after_market.values(), ZERO)

        scaled_routes = [
            f"{route_id}[{route_reasons[route_id].removeprefix('SCALED:')}]"
            for route_id in routes
            if route_scales[route_id] < ONE
        ]
        if not scaled_routes:
            reason = "PASS"
        elif not isinstance(self.portfolio_nav, Mapping) and len(routes) == 1:
            # Preserve the V0.1/V0.3 single-pool reason string for callers/tests.
            reason = route_reasons[routes[0]]
        else:
            reason = "SCALED:" + ";".join(scaled_routes)
        overall_scale = min(route_scales.values(), default=ONE)
        return scaled, RiskDecision(
            scale=overall_scale,
            reason=reason,
            gross_before=gross_before,
            gross_after=gross_after,
            largest_instrument_before=largest_before,
            net_before=net_before,
            net_after=net_after,
            route_scales=route_scales,
            route_reasons=route_reasons,
        )


# Compatibility alias for V0.1 callers.
SimplePortfolioRisk = PortfolioRiskEngine
