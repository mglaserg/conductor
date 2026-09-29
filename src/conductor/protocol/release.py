from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class StrategyReleaseState(StrEnum):
    """Lifecycle state for a strategy release controlled by Conductor."""

    RESEARCH = "research"
    VALIDATED = "validated"
    SHADOW = "shadow"
    LIVE = "live"
    KILLED = "killed"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class StrategyRelease:
    """Immutable identity for a strategy version admitted by Conductor."""

    strategy_id: str
    version: str
    state: StrategyReleaseState = StrategyReleaseState.RESEARCH
    evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty")
        if not self.version.strip():
            raise ValueError("version must be non-empty")

    def can_shadow(self) -> bool:
        return self.state in {
            StrategyReleaseState.VALIDATED,
            StrategyReleaseState.SHADOW,
            StrategyReleaseState.LIVE,
        }

    def can_trade_live(self) -> bool:
        return self.state == StrategyReleaseState.LIVE
