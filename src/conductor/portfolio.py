from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Iterable, Mapping

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
    ) -> None:
        if portfolio_nav <= ZERO:
            raise ValueError("portfolio_nav must be positive")
        self.allocations = dict(allocations or {})
        self.instruments = dict(instruments or {})
        self.portfolio_nav = portfolio_nav

    def capital_budget(self, sleeve_id: str, strategy_id: str) -> CapitalBudget:
        allocation = self.allocations.get(sleeve_id)
        if allocation is None:
            sleeve_weight = ONE
            strategy_weight = ONE
        else:
            sleeve_weight = allocation.portfolio_weight
            strategy_weight = allocation.strategy_weights.get(strategy_id, ZERO)
        sleeve_nav = self.portfolio_nav * sleeve_weight
        return CapitalBudget(
            sleeve_id=sleeve_id,
            strategy_id=strategy_id,
            sleeve_nav=sleeve_nav,
            strategy_nav=sleeve_nav * strategy_weight,
        )

    @staticmethod
    def _round_quantity(quantity: Decimal, lot_size: Decimal) -> Decimal:
        lots = (abs(quantity) / lot_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = lots * lot_size
        return rounded if quantity >= ZERO else -rounded

    def _spec(self, instrument: str) -> InstrumentSpec:
        try:
            return self.instruments[instrument]
        except KeyError as exc:
            raise MissingInstrumentError(f"no instrument spec for {instrument}") from exc

    def build_virtual_targets(self, intents: Iterable[StrategyIntent]) -> list[VirtualTarget]:
        virtual: list[VirtualTarget] = []
        for intent in intents:
            budget = self.capital_budget(intent.sleeve_id, intent.strategy_id)
            targets = {} if intent.status is IntentStatus.FLAT else intent.targets

            for instrument, source_target in targets.items():
                spec = self._spec(instrument)
                if intent.exposure_type is ExposureType.NAV_WEIGHT:
                    notional = budget.strategy_nav * source_target
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
                    )
                )
        return virtual

    @staticmethod
    def aggregate(virtual_targets: Iterable[VirtualTarget]) -> list[AggregateTarget]:
        quantities: dict[str, Decimal] = defaultdict(lambda: ZERO)
        notionals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for target in virtual_targets:
            quantities[target.instrument] += target.target
            notionals[target.instrument] += target.notional

        return [
            AggregateTarget(
                instrument=instrument,
                target=quantities[instrument],
                notional=notionals[instrument],
            )
            for instrument in sorted(quantities)
            if quantities[instrument] != ZERO
        ]

    def metrics(self, virtual_targets: Iterable[VirtualTarget]) -> PortfolioMetrics:
        aggregate = self.aggregate(virtual_targets)
        gross = sum((abs(t.notional) for t in aggregate), ZERO)
        net = sum((t.notional for t in aggregate), ZERO)
        long_notional = sum((max(t.notional, ZERO) for t in aggregate), ZERO)
        short_notional = sum((abs(min(t.notional, ZERO)) for t in aggregate), ZERO)
        return PortfolioMetrics(
            nav=self.portfolio_nav,
            gross_notional=gross,
            net_notional=net,
            gross_leverage=gross / self.portfolio_nav,
            net_leverage=net / self.portfolio_nav,
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
