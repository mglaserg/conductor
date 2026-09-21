from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conductor.domain.models import ExposureType, InstrumentSpec, SleeveAllocation, StrategyIntent
from conductor.portfolio import IntentBook, PortfolioBuilder, StaleIntentError


def test_nav_weight_becomes_strategy_budgeted_quantity() -> None:
    portfolio = PortfolioBuilder(
        allocations={
            "equities": SleeveAllocation(
                "equities",
                {"ETSA": Decimal("0.85"), "RPS": Decimal("0.15")},
                portfolio_weight=Decimal("0.50"),
            )
        },
        instruments={"AAPL": InstrumentSpec("AAPL", Decimal("200"))},
        portfolio_nav=Decimal("250000"),
    )

    virtual = portfolio.build_virtual_targets(
        [
            StrategyIntent(
                "ETSA",
                {"AAPL": Decimal("0.40")},
                ExposureType.NAV_WEIGHT,
                sleeve_id="equities",
            ),
            StrategyIntent(
                "RPS",
                {"AAPL": Decimal("0.20")},
                ExposureType.NAV_WEIGHT,
                sleeve_id="equities",
            ),
        ]
    )

    assert [(t.strategy_id, t.target, t.notional) for t in virtual] == [
        ("ETSA", Decimal("212"), Decimal("42400")),
        ("RPS", Decimal("18"), Decimal("3600")),
    ]
    aggregate = portfolio.aggregate(virtual)
    assert aggregate[0].target == Decimal("230")
    assert aggregate[0].notional == Decimal("46000")


def test_quantity_intent_is_not_multiplied_by_capital_weight() -> None:
    portfolio = PortfolioBuilder(
        allocations={
            "equities": SleeveAllocation(
                "equities", {"ETSA": Decimal("0.10")}, portfolio_weight=Decimal("0.20")
            )
        },
        instruments={"AAPL": InstrumentSpec("AAPL", Decimal("200"))},
        portfolio_nav=Decimal("100000"),
    )
    target = portfolio.build_virtual_targets(
        [
            StrategyIntent(
                "ETSA",
                {"AAPL": Decimal("7")},
                ExposureType.QUANTITY,
                sleeve_id="equities",
            )
        ]
    )[0]
    assert target.target == Decimal("7")


def test_intent_book_uses_latest_revision_and_rejects_stale() -> None:
    now = datetime.now(timezone.utc)
    book = IntentBook(max_age=timedelta(hours=1))
    old_revision = StrategyIntent("ETSA", {"AAPL": Decimal("1")}, revision=1, as_of=now)
    new_revision = StrategyIntent("ETSA", {"AAPL": Decimal("2")}, revision=2, as_of=now)
    assert book.resolve([old_revision, new_revision], now=now)[0].revision == 2

    stale = StrategyIntent("RPS", {"AAPL": Decimal("1")}, as_of=now - timedelta(hours=2))
    with pytest.raises(StaleIntentError):
        book.resolve([stale], now=now)


def test_instrument_specs_can_be_resolved_lazily() -> None:
    calls = []

    def resolve(instrument):
        calls.append(instrument)
        return InstrumentSpec(instrument, Decimal("25"))

    builder = PortfolioBuilder(portfolio_nav=Decimal("100000"), instrument_provider=resolve)
    intent = StrategyIntent(
        "RPS",
        {"EQ.US.NEW": Decimal("12")},
        ExposureType.QUANTITY,
    )
    targets = builder.build_virtual_targets([intent])
    assert targets[0].target == Decimal("12")
    assert calls == ["EQ.US.NEW"]
    # Cached after first resolution.
    builder.build_virtual_targets([intent])
    assert calls == ["EQ.US.NEW"]
