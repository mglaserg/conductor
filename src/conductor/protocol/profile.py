from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from conductor.protocol.models import LINEAR_EVENT_TYPE


@dataclass(frozen=True, slots=True)
class StrategyProfile:
    """Conductor-owned permissions and capital routing for a strategy producer."""

    strategy_id: str
    sleeve_id: str
    source: str | None = None
    allowed_event_types: frozenset[str] = field(
        default_factory=lambda: frozenset({LINEAR_EVENT_TYPE})
    )
    allowed_books: frozenset[str] | None = None
    max_age: timedelta = timedelta(hours=26)
    max_future_skew: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty")
        if not self.sleeve_id.strip():
            raise ValueError("sleeve_id must be non-empty")
        if self.max_age.total_seconds() <= 0:
            raise ValueError("max_age must be positive")

    @property
    def expected_source(self) -> str:
        return self.source or f"urn:defacto:strategy:{self.strategy_id.lower()}"
