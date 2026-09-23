from __future__ import annotations

from decimal import Decimal
from typing import Callable, Iterable, Mapping

from conductor.domain.models import InstrumentSpec, TradeDelta, ZERO


class OrderPlanner:
    """Apply route-capital-aware trade buffers before deltas reach execution."""

    def __init__(
        self,
        *,
        portfolio_nav: Decimal | Mapping[str, Decimal],
        instruments: Mapping[str, InstrumentSpec],
        min_trade_nav_bps: Decimal = Decimal("1"),
        instrument_provider: Callable[[str], InstrumentSpec] | None = None,
        route_instrument_provider: Callable[[str, str], InstrumentSpec] | None = None,
    ) -> None:
        self.portfolio_nav = portfolio_nav
        self.instruments = instruments if isinstance(instruments, dict) else dict(instruments)
        self.min_trade_nav_bps = min_trade_nav_bps
        self.instrument_provider = instrument_provider
        self.route_instrument_provider = route_instrument_provider

    def _route_nav(self, route_id: str) -> Decimal:
        if isinstance(self.portfolio_nav, Mapping):
            try:
                return self.portfolio_nav[route_id]
            except KeyError as exc:
                raise KeyError(f"no portfolio NAV configured for route {route_id}") from exc
        return self.portfolio_nav

    def plan(self, deltas: Iterable[TradeDelta]) -> list[TradeDelta]:
        planned: list[TradeDelta] = []
        for delta in deltas:
            threshold = self._route_nav(delta.route_id) * self.min_trade_nav_bps / Decimal("10000")
            spec = self.instruments.get(delta.instrument)
            if spec is None and self.route_instrument_provider is not None:
                spec = self.route_instrument_provider(delta.route_id, delta.instrument)
                self.instruments[delta.instrument] = spec
            if spec is None and self.instrument_provider is not None:
                spec = self.instrument_provider(delta.instrument)
                self.instruments[delta.instrument] = spec
            if spec is None:
                raise KeyError(f"no instrument spec for {delta.route_id}/{delta.instrument}")
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
                    route_id=delta.route_id,
                )
            )
        return planned
