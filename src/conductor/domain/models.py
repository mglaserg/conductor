from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Mapping

ZERO = Decimal("0")
ONE = Decimal("1")


class ExposureType(StrEnum):
    NAV_WEIGHT = "nav_weight"
    NOTIONAL = "notional"
    QUANTITY = "quantity"


class IntentStatus(StrEnum):
    ACTIVE = "active"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class StrategyIntent:
    """Economic intent emitted by a strategy, before portfolio/risk decisions."""

    strategy_id: str
    targets: Mapping[str, Decimal]
    exposure_type: ExposureType = ExposureType.NAV_WEIGHT
    sleeve_id: str = "default"
    status: IntentStatus = IntentStatus.ACTIVE
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty")
        if not self.sleeve_id.strip():
            raise ValueError("sleeve_id must be non-empty")


@dataclass(frozen=True, slots=True)
class SleeveAllocation:
    sleeve_id: str
    strategy_weights: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        if any(weight < ZERO for weight in self.strategy_weights.values()):
            raise ValueError("strategy weights cannot be negative")
        total = sum(self.strategy_weights.values(), ZERO)
        if total > ONE + Decimal("0.00000001"):
            raise ValueError(f"strategy weights sum to {total}; must be <= 1")


@dataclass(frozen=True, slots=True)
class VirtualTarget:
    strategy_id: str
    sleeve_id: str
    instrument: str
    target: Decimal
    exposure_type: ExposureType


@dataclass(frozen=True, slots=True)
class AggregateTarget:
    instrument: str
    target: Decimal
    exposure_type: ExposureType


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    instrument: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class TradeDelta:
    instrument: str
    current: Decimal
    desired: Decimal
    delta: Decimal


@dataclass(frozen=True, slots=True)
class RiskDecision:
    scale: Decimal
    reason: str
