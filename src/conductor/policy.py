from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Callable, Iterable, Mapping

from conductor.domain.models import ONE, ZERO, StrategyIntent, VirtualTarget


class PolicyError(RuntimeError):
    """A strategy policy cannot be evaluated safely."""


class MissingVolatilityBehavior(StrEnum):
    BLOCK = "block"
    UNSCALED = "unscaled"


@dataclass(frozen=True, slots=True)
class StrategyPolicy:
    """Versioned implementation/risk policy applied after intent normalization.

    Strategy code owns the economic signal. This policy owns how much of that signal the
    portfolio control plane is willing to implement. Defaults are deliberately no-op/fail-closed.
    """

    strategy_id: str
    version: str = "1"
    source: str = "config"
    risk_budget: Decimal = ONE
    target_volatility: Decimal | None = None
    volatility_metadata_key: str = "annualized_volatility"
    min_vol_scale: Decimal = ZERO
    max_vol_scale: Decimal = ONE
    max_gross_leverage: Decimal | None = None
    max_position_nav: Decimal | None = None
    rebalance_band: Decimal = ZERO
    max_target_age_seconds: int | None = None
    on_missing_volatility: MissingVolatilityBehavior = MissingVolatilityBehavior.BLOCK

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty")
        if not self.version.strip():
            raise ValueError("policy version must be non-empty")
        if not self.source.strip():
            raise ValueError("policy source must be non-empty")
        if self.risk_budget <= ZERO:
            raise ValueError("risk_budget must be positive")
        if self.target_volatility is not None and self.target_volatility <= ZERO:
            raise ValueError("target_volatility must be positive")
        if not self.volatility_metadata_key.strip():
            raise ValueError("volatility_metadata_key must be non-empty")
        if self.min_vol_scale < ZERO:
            raise ValueError("min_vol_scale cannot be negative")
        if self.max_vol_scale <= ZERO or self.max_vol_scale < self.min_vol_scale:
            raise ValueError("max_vol_scale must be positive and >= min_vol_scale")
        if self.max_gross_leverage is not None and self.max_gross_leverage <= ZERO:
            raise ValueError("max_gross_leverage must be positive")
        if self.max_position_nav is not None and self.max_position_nav <= ZERO:
            raise ValueError("max_position_nav must be positive")
        if self.rebalance_band < ZERO or self.rebalance_band >= ONE:
            raise ValueError("rebalance_band must be between 0 (inclusive) and 1")
        if self.max_target_age_seconds is not None and self.max_target_age_seconds <= 0:
            raise ValueError("max_target_age_seconds must be positive")


@dataclass(frozen=True, slots=True)
class StrategyPolicyDecision:
    strategy_id: str
    book_id: str
    route_id: str
    policy_version: str
    policy_source: str
    gross_before: Decimal
    gross_after: Decimal
    largest_position_before: Decimal
    capital_base: Decimal
    observed_volatility: Decimal | None
    volatility_scale: Decimal
    constraint_scale: Decimal
    total_scale: Decimal
    reason: str


class StrategyPolicyEngine:
    """Apply per-strategy policy before route-level portfolio risk and broker netting."""

    def __init__(
        self,
        policies: Mapping[str, StrategyPolicy],
        *,
        capital_provider: Callable[[str, str, str], Decimal],
    ) -> None:
        self.policies = dict(policies)
        self.capital_provider = capital_provider

    @staticmethod
    def _round_quantity(quantity: Decimal, lot_size: Decimal) -> Decimal:
        lots = (abs(quantity) / lot_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = lots * lot_size
        return rounded if quantity >= ZERO else -rounded

    @staticmethod
    def _intent_key(intent: StrategyIntent) -> tuple[str, str, str]:
        return intent.strategy_id, intent.book_id, intent.route_id

    def apply(
        self,
        targets: Iterable[VirtualTarget],
        intents: Iterable[StrategyIntent],
        *,
        now: datetime | None = None,
    ) -> tuple[list[VirtualTarget], list[StrategyPolicyDecision]]:
        items = list(targets)
        intents_by_key = {self._intent_key(intent): intent for intent in intents}
        grouped: dict[tuple[str, str, str], list[VirtualTarget]] = {}
        for item in items:
            grouped.setdefault((item.strategy_id, item.book_id, item.route_id), []).append(item)

        evaluated_at = now or datetime.now(timezone.utc)
        adjusted: list[VirtualTarget] = []
        decisions: list[StrategyPolicyDecision] = []

        for key, group in grouped.items():
            strategy_id, book_id, route_id = key
            policy = self.policies.get(strategy_id)
            if policy is None:
                adjusted.extend(group)
                continue
            intent = intents_by_key.get(key)
            if intent is None:
                raise PolicyError(
                    f"missing normalized intent for policy evaluation: {strategy_id}/{book_id}"
                )
            capital = self.capital_provider(group[0].sleeve_id, strategy_id, book_id)
            if capital <= ZERO and (
                policy.max_gross_leverage is not None or policy.max_position_nav is not None
            ):
                raise PolicyError(
                    f"strategy {strategy_id}/{book_id} capital-based policy requires "
                    "positive allocated capital"
                )
            gross_before = sum((abs(item.notional) for item in group), ZERO)
            largest_before = max((abs(item.notional) for item in group), default=ZERO)

            # Synthetic companion intents mean exactly "do not change this virtual book." Applying
            # leverage, target-vol, or concentration scaling to them would let one strategy run
            # mutate another strategy that did not run. Preserve them byte-for-economic-byte.
            if intent.metadata.get("producer") == "conductor-hold-current":
                adjusted.extend(group)
                decisions.append(
                    StrategyPolicyDecision(
                        strategy_id=strategy_id,
                        book_id=book_id,
                        route_id=route_id,
                        policy_version=policy.version,
                        policy_source=policy.source,
                        gross_before=gross_before,
                        gross_after=gross_before,
                        largest_position_before=largest_before,
                        capital_base=capital,
                        observed_volatility=None,
                        volatility_scale=ONE,
                        constraint_scale=ONE,
                        total_scale=ONE,
                        reason="HOLD_CURRENT",
                    )
                )
                continue

            if policy.max_target_age_seconds is not None:
                age = max(0.0, (evaluated_at - intent.as_of).total_seconds())
                if age > policy.max_target_age_seconds:
                    raise PolicyError(
                        f"strategy {strategy_id}/{book_id} target is stale: "
                        f"{age:.1f}s > {policy.max_target_age_seconds}s"
                    )

            observed_vol: Decimal | None = None
            vol_scale = ONE
            reasons: list[str] = []
            if policy.target_volatility is not None:
                raw_vol = intent.metadata.get(policy.volatility_metadata_key)
                if raw_vol is None:
                    if policy.on_missing_volatility is MissingVolatilityBehavior.BLOCK:
                        raise PolicyError(
                            f"strategy {strategy_id}/{book_id} policy needs metadata "
                            f"{policy.volatility_metadata_key!r} for target-vol scaling"
                        )
                    reasons.append("volatility_missing_unscaled")
                else:
                    try:
                        observed_vol = Decimal(str(raw_vol))
                    except Exception as exc:  # noqa: BLE001 - metadata boundary
                        raise PolicyError(
                            f"strategy {strategy_id}/{book_id} has invalid volatility metadata"
                        ) from exc
                    if not observed_vol.is_finite() or observed_vol <= ZERO:
                        raise PolicyError(
                            f"strategy {strategy_id}/{book_id} volatility metadata must be positive"
                        )
                    requested_scale = policy.target_volatility / observed_vol
                    vol_scale = max(
                        policy.min_vol_scale,
                        min(policy.max_vol_scale, requested_scale),
                    )
                    if vol_scale != ONE:
                        reasons.append("target_volatility")

            constraint_scale = ONE
            gross_after_vol = gross_before * vol_scale
            largest_after_vol = largest_before * vol_scale
            if policy.max_gross_leverage is not None and gross_after_vol > ZERO:
                gross_cap = capital * policy.max_gross_leverage
                if gross_after_vol > gross_cap:
                    constraint_scale = min(constraint_scale, gross_cap / gross_after_vol)
                    reasons.append("max_gross_leverage")
            if policy.max_position_nav is not None and largest_after_vol > ZERO:
                position_cap = capital * policy.max_position_nav
                if largest_after_vol > position_cap:
                    constraint_scale = min(
                        constraint_scale, position_cap / largest_after_vol
                    )
                    reasons.append("max_position_nav")

            total_scale = vol_scale * constraint_scale
            for item in group:
                if total_scale == ONE:
                    adjusted.append(item)
                    continue
                quantity = self._round_quantity(item.target * total_scale, item.lot_size)
                if quantity == ZERO:
                    continue
                unit_notional = item.notional / item.target
                adjusted.append(
                    VirtualTarget(
                        strategy_id=item.strategy_id,
                        book_id=item.book_id,
                        sleeve_id=item.sleeve_id,
                        route_id=item.route_id,
                        instrument=item.instrument,
                        target=quantity,
                        notional=quantity * unit_notional,
                        exposure_type=item.exposure_type,
                        source_exposure_type=item.source_exposure_type,
                        lot_size=item.lot_size,
                    )
                )

            decisions.append(
                StrategyPolicyDecision(
                    strategy_id=strategy_id,
                    book_id=book_id,
                    route_id=route_id,
                    policy_version=policy.version,
                    policy_source=policy.source,
                    gross_before=gross_before,
                    gross_after=sum(
                        (abs(item.notional) for item in adjusted if (
                            item.strategy_id, item.book_id, item.route_id
                        ) == key),
                        ZERO,
                    ),
                    largest_position_before=largest_before,
                    capital_base=capital,
                    observed_volatility=observed_vol,
                    volatility_scale=vol_scale,
                    constraint_scale=constraint_scale,
                    total_scale=total_scale,
                    reason="PASS" if not reasons else "SCALED:" + ",".join(dict.fromkeys(reasons)),
                )
            )

        return adjusted, decisions
