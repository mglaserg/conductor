from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from conductor.domain.models import BrokerPosition, TradeDelta, ZERO


class PaperExecutionAdapter:
    """Deterministic in-memory adapter for orchestration tests."""

    def __init__(self, positions: Sequence[BrokerPosition] | None = None) -> None:
        self._positions = {p.instrument: p.quantity for p in positions or []}
        self.submissions: list[TradeDelta] = []

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(instrument=k, quantity=v)
            for k, v in sorted(self._positions.items())
            if v != ZERO
        ]

    def submit_deltas(self, deltas: Sequence[TradeDelta]) -> None:
        for delta in deltas:
            self._positions[delta.instrument] = self._positions.get(delta.instrument, ZERO) + delta.delta
            self.submissions.append(delta)
