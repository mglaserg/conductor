from decimal import Decimal

from conductor.allocation import (
    ERCAllocator,
    FallbackAllocator,
    InverseVolAllocator,
    StaticAllocator,
)


def test_static_allocator_normalizes() -> None:
    result = StaticAllocator().allocate({"ETSA": Decimal("85"), "RPS": Decimal("15")})
    assert result.weights == {"ETSA": Decimal("0.85"), "RPS": Decimal("0.15")}


def test_inverse_vol_gives_more_weight_to_lower_vol_strategy() -> None:
    low = [Decimal(str(x / 10000)) for x in range(-15, 15)]
    high = [x * Decimal("3") for x in low]
    result = InverseVolAllocator(min_observations=20).allocate({"LOW": low, "HIGH": high})
    assert result.weights["LOW"] > result.weights["HIGH"]


def test_erc_uses_correlation_and_returns_weights() -> None:
    a = [Decimal(str(((i % 7) - 3) / 1000)) for i in range(60)]
    b = [Decimal(str((((i * 3) % 11) - 5) / 1000)) for i in range(60)]
    result = ERCAllocator(min_observations=30).allocate({"A": a, "B": b})
    assert abs(sum(result.weights.values()) - Decimal("1")) < Decimal("0.000001")
    assert set(result.weights) == {"A", "B"}


def test_erc_can_use_sample_covariance() -> None:
    a = [Decimal(str(((i % 7) - 3) / 1000)) for i in range(60)]
    b = [Decimal(str((((i * 3) % 11) - 5) / 1000)) for i in range(60)]
    result = ERCAllocator(min_observations=30, covariance_estimator="sample").allocate(
        {"A": a, "B": b}
    )
    assert result.diagnostics["covariance"] == "sample"
    assert abs(sum(result.weights.values()) - Decimal("1")) < Decimal("0.000001")


def test_inverse_vol_lookback_uses_recent_window() -> None:
    old = [Decimal("0.001"), Decimal("-0.001")] * 30
    recent_low = [Decimal("0.0001"), Decimal("-0.0001")] * 10
    recent_high = [Decimal("0.01"), Decimal("-0.01")] * 10
    result = InverseVolAllocator(
        min_observations=20, lookback_observations=20
    ).allocate({"LOW": old + recent_low, "HIGH": old + recent_high})
    assert result.weights["LOW"] > result.weights["HIGH"]
    assert result.diagnostics["lookback_observations"] == 20


def test_fallback_allocator_passes_risk_budgets_to_inverse_vol() -> None:
    series = [Decimal(str(((i % 7) - 3) / 1000)) for i in range(40)]
    result = FallbackAllocator().allocate(
        "inverse_vol",
        configured={"A": Decimal("1"), "B": Decimal("1")},
        returns={"A": series, "B": series},
        risk_budgets={"A": Decimal("3"), "B": Decimal("1")},
    )
    assert result.weights["A"] > result.weights["B"]
