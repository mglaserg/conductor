from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from conductor.adapters.base import ExecutionAdapter
from conductor.domain.models import BrokerPosition, ExecutionReport, TradeDelta


class RoutedExecutionAdapter:
    """Multiplex route-aware targets to independent broker/account adapters."""

    def __init__(self, routes: Mapping[str, ExecutionAdapter]) -> None:
        self.routes = dict(routes)

    def warm_instruments(self, route_instruments: Mapping[str, Iterable[str]]) -> None:
        for route_id, instruments in route_instruments.items():
            try:
                adapter = self.routes[route_id]
            except KeyError as exc:
                raise KeyError(f"no execution adapter configured for route {route_id}") from exc
            warm = getattr(adapter, "warm_instruments", None)
            if callable(warm):
                warm(list(instruments))

    def positions(self) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        for route_id, adapter in sorted(self.routes.items()):
            for position in adapter.positions():
                if position.route_id != route_id:
                    position = BrokerPosition(position.instrument, position.quantity, route_id)
                out.append(position)
        return out

    def submit_deltas(self, deltas: Sequence[TradeDelta]) -> list[ExecutionReport]:
        reports: list[ExecutionReport] = []
        grouped: dict[str, list[TradeDelta]] = defaultdict(list)
        for delta in deltas:
            grouped[delta.route_id].append(delta)
        for route_id, items in grouped.items():
            try:
                adapter = self.routes[route_id]
            except KeyError as exc:
                raise KeyError(f"no execution adapter configured for route {route_id}") from exc
            route_reports = adapter.submit_deltas(items)
            if route_reports:
                reports.extend(route_reports)
        return reports
