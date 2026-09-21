from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from conductor.domain.models import BrokerPosition, ExecutionReport, TradeDelta, ZERO


class PaperExecutionAdapter:
    """Deterministic in-memory adapter for orchestration tests."""

    def __init__(self, positions: Sequence[BrokerPosition] | None = None) -> None:
        self._positions = {(p.route_id, p.instrument): p.quantity for p in positions or []}
        self.submissions: list[TradeDelta] = []

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(instrument=instrument, quantity=quantity, route_id=route_id)
            for (route_id, instrument), quantity in sorted(self._positions.items())
            if quantity != ZERO
        ]

    def submit_deltas(self, deltas: Sequence[TradeDelta]) -> list[ExecutionReport]:
        reports: list[ExecutionReport] = []
        for delta in deltas:
            key = (delta.route_id, delta.instrument)
            self._positions[key] = self._positions.get(key, ZERO) + delta.delta
            self.submissions.append(delta)
            reports.append(
                ExecutionReport(
                    route_id=delta.route_id,
                    instrument=delta.instrument,
                    requested_quantity=delta.delta,
                    filled_quantity=delta.delta,
                    status="filled",
                )
            )
        return reports
