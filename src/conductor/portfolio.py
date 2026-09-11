from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable, Mapping

from conductor.domain.models import (
    AggregateTarget,
    ExposureType,
    SleeveAllocation,
    StrategyIntent,
    VirtualTarget,
    ZERO,
)


class PortfolioBuilder:
    """Transforms independent strategy intents into virtual and aggregate targets."""

    def __init__(self, allocations: Mapping[str, SleeveAllocation] | None = None) -> None:
        self.allocations = dict(allocations or {})

    def build_virtual_targets(self, intents: Iterable[StrategyIntent]) -> list[VirtualTarget]:
        virtual: list[VirtualTarget] = []
        for intent in intents:
            allocation = self.allocations.get(intent.sleeve_id)
            if allocation is None:
                strategy_scale = Decimal("1")
            else:
                strategy_scale = allocation.strategy_weights.get(intent.strategy_id, ZERO)

            for instrument, target in intent.targets.items():
                virtual.append(
                    VirtualTarget(
                        strategy_id=intent.strategy_id,
                        sleeve_id=intent.sleeve_id,
                        instrument=instrument,
                        target=target * strategy_scale,
                        exposure_type=intent.exposure_type,
                    )
                )
        return virtual

    @staticmethod
    def aggregate(virtual_targets: Iterable[VirtualTarget]) -> list[AggregateTarget]:
        totals: dict[tuple[str, ExposureType], Decimal] = defaultdict(lambda: ZERO)
        for target in virtual_targets:
            totals[(target.instrument, target.exposure_type)] += target.target

        instruments_to_types: dict[str, set[ExposureType]] = defaultdict(set)
        for instrument, exposure_type in totals:
            instruments_to_types[instrument].add(exposure_type)
        mixed = {k: v for k, v in instruments_to_types.items() if len(v) > 1}
        if mixed:
            raise ValueError(f"cannot aggregate mixed exposure types for instruments: {mixed}")

        return [
            AggregateTarget(instrument=instrument, target=target, exposure_type=exposure_type)
            for (instrument, exposure_type), target in sorted(totals.items())
        ]
