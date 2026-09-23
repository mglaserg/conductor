from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Mapping
from uuid import uuid4

ZERO = Decimal("0")
ONE = Decimal("1")


class ExposureType(StrEnum):
    NAV_WEIGHT = "nav_weight"
    NOTIONAL = "notional"
    QUANTITY = "quantity"


class IntentStatus(StrEnum):
    ACTIVE = "active"
    FLAT = "flat"


class RunState(StrEnum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    RECONCILED = "reconciled"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class StrategyIntent:
    """Economic intent emitted by a strategy, before portfolio/risk decisions.

    NAV_WEIGHT targets are interpreted as weights *within the strategy's allocated
    capital budget*. Quantity and notional intents are already absolute economic
    requests and are not multiplied by allocation weights.
    """

    strategy_id: str
    targets: Mapping[str, Decimal]
    exposure_type: ExposureType = ExposureType.NAV_WEIGHT
    sleeve_id: str = "default"
    status: IntentStatus = IntentStatus.ACTIVE
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    revision: int = 1
    schema_version: str = "1.0"
    intent_id: str = field(default_factory=lambda: uuid4().hex)
    metadata: Mapping[str, str] = field(default_factory=dict)
    book_id: str = "main"
    route_id: str = "default"

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty")
        if not self.book_id.strip():
            raise ValueError("book_id must be non-empty")
        if not self.sleeve_id.strip():
            raise ValueError("sleeve_id must be non-empty")
        if not self.route_id.strip():
            raise ValueError("route_id must be non-empty")
        if self.revision < 1:
            raise ValueError("revision must be >= 1")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SleeveAllocation:
    """Hierarchical capital allocation for one sleeve.

    ``portfolio_weight`` allocates total portfolio NAV to the sleeve. Each
    strategy then receives its share of that sleeve via ``strategy_weights``.
    Unallocated weight stays as reserve cash/capacity.
    """

    sleeve_id: str
    strategy_weights: Mapping[str, Decimal]
    portfolio_weight: Decimal = ONE
    leverage: Decimal = ONE
    rebalance_band: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.sleeve_id.strip():
            raise ValueError("sleeve_id must be non-empty")
        if self.portfolio_weight < ZERO or self.portfolio_weight > ONE:
            raise ValueError("portfolio_weight must be between 0 and 1")
        if self.leverage < ZERO:
            raise ValueError("sleeve leverage cannot be negative")
        if self.rebalance_band < ZERO or self.rebalance_band >= ONE:
            raise ValueError("rebalance_band must be between 0 (inclusive) and 1")
        if any(weight < ZERO for weight in self.strategy_weights.values()):
            raise ValueError("strategy weights cannot be negative")
        total = sum(self.strategy_weights.values(), ZERO)
        if total > ONE + Decimal("0.00000001"):
            raise ValueError(f"strategy weights sum to {total}; must be <= 1")


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    instrument: str
    price: Decimal
    contract_multiplier: Decimal = ONE
    lot_size: Decimal = ONE
    asset_class: str = "equity"
    venue: str | None = None

    def __post_init__(self) -> None:
        if not self.instrument.strip():
            raise ValueError("instrument must be non-empty")
        if self.price <= ZERO:
            raise ValueError("price must be positive")
        if self.contract_multiplier <= ZERO:
            raise ValueError("contract_multiplier must be positive")
        if self.lot_size <= ZERO:
            raise ValueError("lot_size must be positive")

    @property
    def unit_notional(self) -> Decimal:
        return self.price * self.contract_multiplier


@dataclass(frozen=True, slots=True)
class CapitalBudget:
    sleeve_id: str
    strategy_id: str
    sleeve_nav: Decimal
    strategy_nav: Decimal
    exposure_budget: Decimal


@dataclass(frozen=True, slots=True)
class VirtualTarget:
    """Per-strategy desired tradable ownership after capital translation."""

    strategy_id: str
    sleeve_id: str
    instrument: str
    target: Decimal  # quantity
    notional: Decimal
    book_id: str = "main"
    exposure_type: ExposureType = ExposureType.QUANTITY
    source_exposure_type: ExposureType = ExposureType.NAV_WEIGHT
    lot_size: Decimal = ONE
    route_id: str = "default"


@dataclass(frozen=True, slots=True)
class AggregateTarget:
    instrument: str
    target: Decimal  # quantity
    notional: Decimal
    exposure_type: ExposureType = ExposureType.QUANTITY
    route_id: str = "default"


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    instrument: str
    quantity: Decimal
    route_id: str = "default"


@dataclass(frozen=True, slots=True)
class TradeDelta:
    instrument: str
    current: Decimal
    desired: Decimal
    delta: Decimal
    estimated_notional: Decimal = ZERO
    route_id: str = "default"


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    route_id: str
    instrument: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    avg_price: Decimal | None = None
    commission: Decimal = ZERO
    status: str = "filled"
    order_id: str | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    scale: Decimal
    reason: str
    gross_before: Decimal = ZERO
    gross_after: Decimal = ZERO
    largest_instrument_before: Decimal = ZERO
    net_before: Decimal = ZERO
    net_after: Decimal = ZERO
    route_scales: Mapping[str, Decimal] = field(default_factory=dict)
    route_reasons: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PortfolioMetrics:
    nav: Decimal
    gross_notional: Decimal
    net_notional: Decimal
    gross_leverage: Decimal
    net_leverage: Decimal
    long_notional: Decimal
    short_notional: Decimal


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    state: RunState
    deltas: tuple[TradeDelta, ...]
    risk: RiskDecision
    metrics: PortfolioMetrics
    reconciled: bool
