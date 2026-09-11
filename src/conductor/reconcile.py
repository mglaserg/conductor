from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from conductor.domain.models import AggregateTarget, BrokerPosition, ExposureType, TradeDelta, ZERO


class DesiredStateReconciler:
    """Computes the minimal deltas from actual broker state to desired state."""

    def __init__(self, tolerance: Decimal = Decimal("0")) -> None:
        self.tolerance = tolerance

    def reconcile(
        self,
        desired: Iterable[AggregateTarget],
        actual: Iterable[BrokerPosition],
    ) -> list[TradeDelta]:
        desired_map: dict[str, Decimal] = {}
        for target in desired:
            if target.exposure_type is not ExposureType.QUANTITY:
                raise ValueError(
                    "execution reconciliation requires QUANTITY targets; translate weights/notional first"
                )
            desired_map[target.instrument] = target.target

        actual_map = {position.instrument: position.quantity for position in actual}
        instruments = sorted(set(desired_map) | set(actual_map))
        deltas: list[TradeDelta] = []
        for instrument in instruments:
            current = actual_map.get(instrument, ZERO)
            wanted = desired_map.get(instrument, ZERO)
            delta = wanted - current
            if abs(delta) > self.tolerance:
                deltas.append(
                    TradeDelta(
                        instrument=instrument,
                        current=current,
                        desired=wanted,
                        delta=delta,
                    )
                )
        return deltas
