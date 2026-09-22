from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from uuid import uuid4

from conductor.domain.models import ZERO, BrokerPosition, ExecutionReport, TradeDelta
from conductor.ledger import ConductorLedger


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


class DurablePaperExecutionAdapter:
    """Offline synthetic broker whose positions and fills live in Conductor SQLite."""

    def __init__(
        self,
        *,
        ledger: ConductorLedger,
        route_id: str,
        price_provider: Callable[[str], Decimal],
        initial_positions: Sequence[BrokerPosition] | None = None,
    ) -> None:
        self.ledger = ledger
        self.route_id = route_id
        self.price_provider = price_provider
        aggregate: dict[str, Decimal] = {}
        for position in initial_positions or []:
            if position.route_id != route_id:
                continue
            aggregate[position.instrument] = (
                aggregate.get(position.instrument, ZERO) + position.quantity
            )
        self.ledger.initialize_paper_route(route_id, aggregate)

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(instrument, quantity, self.route_id)
            for instrument, quantity in self.ledger.paper_broker_positions(
                self.route_id
            ).items()
            if quantity != ZERO
        ]

    def submit_deltas(self, deltas: Sequence[TradeDelta]) -> list[ExecutionReport]:
        reports: list[ExecutionReport] = []
        for delta in deltas:
            if delta.route_id != self.route_id:
                raise ValueError(
                    f"paper route mismatch: expected {self.route_id}, got {delta.route_id}"
                )
            price = self.price_provider(delta.instrument)
            order_id = f"paper-{uuid4().hex}"
            self.ledger.apply_paper_fill(
                route_id=self.route_id,
                instrument=delta.instrument,
                quantity=delta.delta,
                price=price,
                order_id=order_id,
            )
            reports.append(
                ExecutionReport(
                    route_id=self.route_id,
                    instrument=delta.instrument,
                    requested_quantity=delta.delta,
                    filled_quantity=delta.delta,
                    avg_price=price,
                    status="filled",
                    order_id=order_id,
                )
            )
        return reports
