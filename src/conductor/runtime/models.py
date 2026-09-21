from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Mapping


class NativeResultMode(StrEnum):
    """Native output semantics produced by an existing strategy."""

    TARGET_WEIGHTS = "target_weights"
    TARGET_QUANTITIES = "target_quantities"
    POSITION_DELTAS = "position_deltas"


class StrategyLifecycle(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRING = "retiring"
    RETIRED = "retired"


class StrategyRunStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    REJECTED_CONCURRENT = "rejected_concurrent"


@dataclass(frozen=True, slots=True)
class StrategyAccountView:
    strategy_id: str
    book_id: str
    allocated_capital: Decimal
    cash: Decimal
    positions: Mapping[str, Decimal]
    equity: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class StrategyRunnerProfile:
    strategy_id: str
    sleeve_id: str
    result_mode: NativeResultMode
    command: tuple[str, ...]
    cwd: Path
    route_id: str = "default"
    book_id: str = "main"
    timeout_seconds: int = 600
    max_concurrent_runs: int = 1
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty")
        if not self.sleeve_id.strip():
            raise ValueError("sleeve_id must be non-empty")
        if not self.book_id.strip():
            raise ValueError("book_id must be non-empty")
        if not self.route_id.strip():
            raise ValueError("route_id must be non-empty")
        if not self.command:
            raise ValueError("command must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_concurrent_runs != 1:
            raise ValueError("V0.4 supports exactly one concurrent run per strategy/book")
