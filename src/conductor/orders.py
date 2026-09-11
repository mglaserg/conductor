from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping

from conductor.domain.models import InstrumentSpec, TradeDelta, ZERO


class OrderPlanner:
    """Apply portfolio-level trade buffers before deltas reach execution."""

    def __init__(
        self,
        *,
        portfolio_nav: Decimal,
        instruments: Mapping[str, InstrumentSpec],
        min_trade_nav_bps: Decimal = Decimal("1"),
    ) -> None:
        self.portfolio_nav = portfolio_nav
        self.instruments = dict(instruments)
        self.min_trade_nav_bps = min_trade_nav_bps

    def plan(self, deltas: Iterable[TradeDelta]) -> list[TradeDelta]:
        threshold = self.portfolio_nav * self.min_trade_nav_bps / Decimal("10000")
        planned: list[TradeDelta] = []
        for delta in deltas:
            spec = self.instruments[delta.instrument]
            estimated_notional = abs(delta.delta) * spec.unit_notional
            if estimated_notional < threshold or delta.delta == ZERO:
                continue
            planned.append(
                TradeDelta(
                    instrument=delta.instrument,
                    current=delta.current,
                    desired=delta.desired,
                    delta=delta.delta,
                    estimated_notional=estimated_notional,
                )
            )
        return planned
