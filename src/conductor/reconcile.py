from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from conductor.domain.models import AggregateTarget, BrokerPosition, TradeDelta, ZERO


class DesiredStateReconciler:
    """Compute minimal deltas from actual broker state to desired aggregate state."""

    def __init__(self, tolerance: Decimal = Decimal("0")) -> None:
        self.tolerance = tolerance

    def reconcile(
        self,
        desired: Iterable[AggregateTarget],
        actual: Iterable[BrokerPosition],
    ) -> list[TradeDelta]:
        desired_map = {(target.route_id, target.instrument): target.target for target in desired}
        actual_map = {(position.route_id, position.instrument): position.quantity for position in actual}
        instruments = sorted(set(desired_map) | set(actual_map))
        deltas: list[TradeDelta] = []
        for route_id, instrument in instruments:
            current = actual_map.get((route_id, instrument), ZERO)
            wanted = desired_map.get((route_id, instrument), ZERO)
            delta = wanted - current
            if abs(delta) > self.tolerance:
                deltas.append(
                    TradeDelta(
                        instrument=instrument,
                        current=current,
                        desired=wanted,
                        delta=delta,
                        route_id=route_id,
                    )
                )
        return deltas

    def is_reconciled(
        self,
        desired: Iterable[AggregateTarget],
        actual: Iterable[BrokerPosition],
    ) -> bool:
        return not self.reconcile(desired, actual)
