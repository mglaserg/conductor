from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Callable, Iterable, Mapping

from conductor.domain.models import (
    AggregateTarget,
    CapitalBudget,
    ExposureType,
    InstrumentSpec,
    IntentStatus,
    ONE,
    PortfolioMetrics,
    SleeveAllocation,
    StrategyIntent,
    VirtualTarget,
    ZERO,
)


class StaleIntentError(RuntimeError):
    pass


class MissingInstrumentError(KeyError):
    pass


class IntentBook:
    """Resolve latest strategy revisions and enforce freshness before trading."""

    def __init__(self, max_age: timedelta | None = None) -> None:
        # V0.3 protocol users enforce per-strategy freshness in StrategyProfile.
        # max_age remains available for legacy/internal direct StrategyIntent callers.
        self.max_age = max_age

    def resolve(
        self,
        intents: Iterable[StrategyIntent],
        *,
        now: datetime | None = None,
    ) -> list[StrategyIntent]:
        now = now or datetime.now(timezone.utc)
        latest: dict[tuple[str, str], StrategyIntent] = {}
        for intent in intents:
            key = (intent.strategy_id, intent.book_id)
            incumbent = latest.get(key)
            if incumbent is None or (intent.revision, intent.as_of) > (
                incumbent.revision,
                incumbent.as_of,
            ):
                latest[key] = intent

        stale = [
            f"{i.strategy_id}/{i.book_id}@r{i.revision}"
            for i in latest.values()
            if self.max_age is not None and now - i.as_of > self.max_age
        ]
        if stale:
            raise StaleIntentError("stale strategy intent(s): " + ", ".join(sorted(stale)))

        return sorted(latest.values(), key=lambda i: (i.strategy_id, i.book_id))


class PortfolioBuilder:
    """Translate independent strategy intent into virtual and aggregate positions."""

    def __init__(
        self,
        allocations: Mapping[str, SleeveAllocation] | None = None,
        instruments: Mapping[str, InstrumentSpec] | None = None,
        portfolio_nav: Decimal = Decimal("100000"),
        instrument_provider: Callable[[str], InstrumentSpec] | None = None,
        route_instrument_provider: Callable[[str, str], InstrumentSpec] | None = None,
        strategy_capital_provider: Callable[[str, str, str], Decimal] | None = None,
        route_navs: Mapping[str, Decimal] | None = None,
    ) -> None:
        if portfolio_nav <= ZERO:
            raise ValueError("portfolio_nav must be positive")
        self.allocations = dict(allocations or {})
        self.instruments = dict(instruments or {})
        self.portfolio_nav = portfolio_nav
        self.instrument_provider = instrument_provider
        self.route_instrument_provider = route_instrument_provider
        self.strategy_capital_provider = strategy_capital_provider
        self.route_navs = dict(route_navs or {})

    def capital_budget(
        self, sleeve_id: str, strategy_id: str, book_id: str = "main"
    ) -> CapitalBudget:
        allocation = self.allocations.get(sleeve_id)
        if allocation is None:
            sleeve_weight = ONE
            strategy_weight = ONE
            leverage = ONE
        else:
            sleeve_weight = allocation.portfolio_weight
            strategy_weight = allocation.strategy_weights.get(strategy_id, ZERO)
            leverage = allocation.leverage
        sleeve_nav = self.portfolio_nav * sleeve_weight
        if self.strategy_capital_provider is not None:
            strategy_nav = self.strategy_capital_provider(sleeve_id, strategy_id, book_id)
            if strategy_nav < ZERO:
                raise ValueError(
                    f"allocated capital cannot be negative for {strategy_id}/{book_id}"
                )
        else:
            strategy_nav = sleeve_nav * strategy_weight
        return CapitalBudget(
            sleeve_id=sleeve_id,
            strategy_id=strategy_id,
            sleeve_nav=sleeve_nav,
            strategy_nav=strategy_nav,
            exposure_budget=strategy_nav * leverage,
        )

    @staticmethod
    def _round_quantity(quantity: Decimal, lot_size: Decimal) -> Decimal:
        lots = (abs(quantity) / lot_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = lots * lot_size
        return rounded if quantity >= ZERO else -rounded

    def _spec(self, instrument: str, route_id: str = "default") -> InstrumentSpec:
        spec = self.instruments.get(instrument)
        if spec is not None:
            return spec
        if self.route_instrument_provider is not None:
            spec = self.route_instrument_provider(route_id, instrument)
            self.instruments[instrument] = spec
            return spec
        if self.instrument_provider is not None:
            spec = self.instrument_provider(instrument)
            self.instruments[instrument] = spec
            return spec
        raise MissingInstrumentError(f"no instrument spec for {route_id}/{instrument}")

    def build_virtual_targets(self, intents: Iterable[StrategyIntent]) -> list[VirtualTarget]:
        virtual: list[VirtualTarget] = []
        for intent in intents:
            budget = self.capital_budget(intent.sleeve_id, intent.strategy_id, intent.book_id)
            targets = {} if intent.status is IntentStatus.FLAT else intent.targets

            for instrument, source_target in targets.items():
                spec = self._spec(instrument, intent.route_id)
                if intent.exposure_type is ExposureType.NAV_WEIGHT:
                    notional = budget.exposure_budget * source_target
                    raw_quantity = notional / spec.unit_notional
                elif intent.exposure_type is ExposureType.NOTIONAL:
                    notional = source_target
                    raw_quantity = notional / spec.unit_notional
                elif intent.exposure_type is ExposureType.QUANTITY:
                    raw_quantity = source_target
                    notional = raw_quantity * spec.unit_notional
                else:  # defensive against future enum additions
                    raise ValueError(f"unsupported exposure type: {intent.exposure_type}")

                quantity = self._round_quantity(raw_quantity, spec.lot_size)
                notional = quantity * spec.unit_notional
                if quantity == ZERO:
                    continue
                virtual.append(
                    VirtualTarget(
                        strategy_id=intent.strategy_id,
                        sleeve_id=intent.sleeve_id,
                        instrument=instrument,
                        book_id=intent.book_id,
                        target=quantity,
                        notional=notional,
                        exposure_type=ExposureType.QUANTITY,
                        source_exposure_type=intent.exposure_type,
                        lot_size=spec.lot_size,
                        route_id=intent.route_id,
                    )
                )
        return virtual

    @staticmethod
    def aggregate(virtual_targets: Iterable[VirtualTarget]) -> list[AggregateTarget]:
        quantities: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        notionals: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for target in virtual_targets:
            key = (target.route_id, target.instrument)
            quantities[key] += target.target
            notionals[key] += target.notional

        return [
            AggregateTarget(
                instrument=instrument,
                target=quantities[(route_id, instrument)],
                notional=notionals[(route_id, instrument)],
                route_id=route_id,
            )
            for route_id, instrument in sorted(quantities)
            if quantities[(route_id, instrument)] != ZERO
        ]

    def metrics(
        self,
        virtual_targets: Iterable[VirtualTarget],
        *,
        route_ids: Iterable[str] | None = None,
    ) -> PortfolioMetrics:
        aggregate = self.aggregate(virtual_targets)
        gross = sum((abs(t.notional) for t in aggregate), ZERO)
        net = sum((t.notional for t in aggregate), ZERO)
        long_notional = sum((max(t.notional, ZERO) for t in aggregate), ZERO)
        short_notional = sum((abs(min(t.notional, ZERO)) for t in aggregate), ZERO)
        selected_routes = set(route_ids or ())
        if self.route_navs and selected_routes:
            try:
                nav = sum((self.route_navs[route] for route in selected_routes), ZERO)
            except KeyError as exc:
                raise KeyError(f"no portfolio NAV configured for route {exc.args[0]}") from exc
        else:
            nav = self.portfolio_nav
        return PortfolioMetrics(
            nav=nav,
            gross_notional=gross,
            net_notional=net,
            gross_leverage=gross / nav,
            net_leverage=net / nav,
            long_notional=long_notional,
            short_notional=short_notional,
        )

    def strategy_notionals(self, targets: Iterable[VirtualTarget]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for target in targets:
            out[target.strategy_id] += abs(target.notional)
        return dict(sorted(out.items()))

    def sleeve_notionals(self, targets: Iterable[VirtualTarget]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for target in targets:
            out[target.sleeve_id] += abs(target.notional)
        return dict(sorted(out.items()))
