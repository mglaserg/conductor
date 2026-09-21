from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from conductor.domain.models import ExposureType
from conductor.runtime.models import NativeResultMode, StrategyAccountView, StrategyRunnerProfile
from conductor.runtime.native import adapt_native_result


def account(**positions):
    return StrategyAccountView(
        strategy_id="TLAQ",
        book_id="main",
        allocated_capital=Decimal("100000"),
        cash=Decimal("-10000"),
        positions={k: Decimal(str(v)) for k, v in positions.items()},
        equity=Decimal("100000"),
        gross_exposure=Decimal("110000"),
        net_exposure=Decimal("110000"),
    )


def profile(mode):
    return StrategyRunnerProfile(
        strategy_id="TLAQ",
        sleeve_id="equities",
        result_mode=mode,
        command=("python", "strategy.py"),
        cwd=Path("."),
        route_id="windows_ibkr_equities",
    )


def test_tlaq_deltas_become_absolute_targets() -> None:
    intent = adapt_native_result(
        {"deltas": {"EQ.US.AAPL": 20, "EQ.US.TLT": -10}},
        profile=profile(NativeResultMode.POSITION_DELTAS),
        account=account(**{"EQ.US.AAPL": 100, "EQ.US.TLT": 400}),
        revision=7,
        as_of=datetime.now(timezone.utc),
        run_id="run7",
    )
    assert intent.exposure_type is ExposureType.QUANTITY
    assert intent.targets == {
        "EQ.US.AAPL": Decimal("120"),
        "EQ.US.TLT": Decimal("390"),
    }
    assert intent.route_id == "windows_ibkr_equities"


def test_rps_absolute_quantities_are_not_diffed() -> None:
    p = StrategyRunnerProfile(
        strategy_id="RPSchteroids",
        sleeve_id="equities",
        result_mode=NativeResultMode.TARGET_QUANTITIES,
        command=("python", "strategy.py"),
        cwd=Path("."),
    )
    a = account(**{"EQ.US.AAPL": 100})
    intent = adapt_native_result(
        {"targets": {"EQ.US.AAPL": 35}},
        profile=p,
        account=a,
        revision=2,
        run_id="rps2",
    )
    assert intent.targets["EQ.US.AAPL"] == Decimal("35")
