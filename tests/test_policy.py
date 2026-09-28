from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conductor.domain.models import ExposureType, StrategyIntent, VirtualTarget
from conductor.policy import PolicyError, StrategyPolicy, StrategyPolicyEngine


def _target(quantity: str) -> VirtualTarget:
    qty = Decimal(quantity)
    return VirtualTarget(
        strategy_id="ETSA",
        book_id="main",
        sleeve_id="equities",
        route_id="ibkr_main",
        instrument="AAPL",
        target=qty,
        notional=qty * Decimal("100"),
        source_exposure_type=ExposureType.NAV_WEIGHT,
    )


def _engine(policy: StrategyPolicy) -> StrategyPolicyEngine:
    return StrategyPolicyEngine(
        {"ETSA": policy},
        capital_provider=lambda sleeve, strategy, book: Decimal("10000"),
    )


def test_strategy_policy_enforces_gross_leverage_before_portfolio_risk() -> None:
    policy = StrategyPolicy("ETSA", max_gross_leverage=Decimal("1"))
    intent = StrategyIntent(
        "ETSA",
        {"AAPL": Decimal("2")},
        sleeve_id="equities",
        route_id="ibkr_main",
    )

    adjusted, decisions = _engine(policy).apply([_target("200")], [intent])

    assert adjusted[0].target == Decimal("100")
    assert decisions[0].total_scale == Decimal("0.5")
    assert decisions[0].reason == "SCALED:max_gross_leverage"


def test_strategy_policy_target_volatility_uses_explicit_strategy_metadata() -> None:
    policy = StrategyPolicy(
        "ETSA",
        target_volatility=Decimal("0.10"),
        max_vol_scale=Decimal("2"),
    )
    intent = StrategyIntent(
        "ETSA",
        {"AAPL": Decimal("1")},
        sleeve_id="equities",
        route_id="ibkr_main",
        metadata={"annualized_volatility": "0.20"},
    )

    adjusted, decisions = _engine(policy).apply([_target("100")], [intent])

    assert adjusted[0].target == Decimal("50")
    assert decisions[0].volatility_scale == Decimal("0.5")
    assert decisions[0].observed_volatility == Decimal("0.20")


def test_target_volatility_fails_closed_when_required_metadata_is_missing() -> None:
    policy = StrategyPolicy("ETSA", target_volatility=Decimal("0.10"))
    intent = StrategyIntent(
        "ETSA",
        {"AAPL": Decimal("1")},
        sleeve_id="equities",
        route_id="ibkr_main",
    )

    with pytest.raises(PolicyError, match="needs metadata"):
        _engine(policy).apply([_target("100")], [intent])


def test_policy_freshness_is_independent_of_strategy_process_success() -> None:
    policy = StrategyPolicy("ETSA", max_target_age_seconds=60)
    intent = StrategyIntent(
        "ETSA",
        {"AAPL": Decimal("1")},
        sleeve_id="equities",
        route_id="ibkr_main",
        as_of=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    with pytest.raises(PolicyError, match="target is stale"):
        _engine(policy).apply([_target("100")], [intent])


def test_hold_current_companion_intent_is_never_rescaled_by_another_strategy_run() -> None:
    policy = StrategyPolicy(
        "ETSA",
        target_volatility=Decimal("0.10"),
        max_gross_leverage=Decimal("0.5"),
    )
    intent = StrategyIntent(
        "ETSA",
        {"AAPL": Decimal("200")},
        ExposureType.QUANTITY,
        sleeve_id="equities",
        route_id="ibkr_main",
        metadata={"producer": "conductor-hold-current"},
    )

    adjusted, decisions = _engine(policy).apply([_target("200")], [intent])

    assert adjusted[0].target == Decimal("200")
    assert decisions[0].reason == "HOLD_CURRENT"
