from decimal import Decimal

from conductor.allocation import ERCAllocator, InverseVolAllocator, StaticAllocator


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
