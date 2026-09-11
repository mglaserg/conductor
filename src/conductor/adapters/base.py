from __future__ import annotations

from typing import Protocol, Sequence

from conductor.domain.models import BrokerPosition, TradeDelta


class ExecutionAdapter(Protocol):
    """Port implemented by Nautilus, paper, or future execution runtimes."""

    def positions(self) -> Sequence[BrokerPosition]: ...

    def submit_deltas(self, deltas: Sequence[TradeDelta]) -> None: ...
