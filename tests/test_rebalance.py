from decimal import Decimal

from conductor.domain.models import ExposureType, InstrumentSpec, SleeveAllocation, VirtualTarget
from conductor.ledger import ConductorLedger
from conductor.rebalance import VirtualRebalanceBuffer


def _target(strategy: str, quantity: str) -> VirtualTarget:
    return VirtualTarget(
        strategy_id=strategy,
        book_id="main",
        sleeve_id="etsa_rps",
        route_id="ibkr",
        instrument="EQ.US.AAPL",
        target=Decimal(quantity),
        notional=Decimal(quantity) * Decimal("100"),
        source_exposure_type=ExposureType.QUANTITY,
    )


def _seed(ledger: ConductorLedger) -> None:
    for strategy, capital, shares in (("ETSA", "85000", "100"), ("RPSchteroids", "15000", "50")):
        ledger.seed_strategy_account(
            strategy,
            allocated_capital=Decimal(capital),
            cash=Decimal("0"),
            route_id="ibkr",
        )
        ledger.seed_virtual_book(
            strategy_id=strategy,
            book_id="main",
            sleeve_id="etsa_rps",
            route_id="ibkr",
            positions={"EQ.US.AAPL": Decimal(shares)},
        )


def test_sleeve_band_is_applied_before_cross_strategy_netting(tmp_path) -> None:
    ledger = ConductorLedger(tmp_path / "band.sqlite")
    _seed(ledger)
    buffer = VirtualRebalanceBuffer(
        ledger=ledger,
        allocations={
            "etsa_rps": SleeveAllocation(
                "etsa_rps",
                {"ETSA": Decimal("0.85"), "RPSchteroids": Decimal("0.15")},
                rebalance_band=Decimal("0.025"),
            )
        },
        instruments={"EQ.US.AAPL": InstrumentSpec("EQ.US.AAPL", Decimal("100"))},
    )

    # Aggregate sleeve changes from 150 -> 160 shares = $1,000 / $100k = 1%,
    # so both strategy ownership changes remain unimplemented.
    implemented, decisions = buffer.apply([_target("ETSA", "120"), _target("RPSchteroids", "40")])
    got = {row.strategy_id: row.target for row in implemented}
    assert got == {"ETSA": Decimal("100"), "RPSchteroids": Decimal("50")}
    assert decisions[0].suppressed is True


def test_sleeve_band_allows_full_strategy_targets_when_threshold_is_crossed(tmp_path) -> None:
    ledger = ConductorLedger(tmp_path / "band2.sqlite")
    _seed(ledger)
    buffer = VirtualRebalanceBuffer(
        ledger=ledger,
        allocations={
            "etsa_rps": SleeveAllocation(
                "etsa_rps",
                {"ETSA": Decimal("0.85"), "RPSchteroids": Decimal("0.15")},
                rebalance_band=Decimal("0.025"),
            )
        },
        instruments={"EQ.US.AAPL": InstrumentSpec("EQ.US.AAPL", Decimal("100"))},
    )

    # 40-share aggregate change = $4,000 / $100k = 4%, so implement it.
    implemented, decisions = buffer.apply([_target("ETSA", "130"), _target("RPSchteroids", "60")])
    got = {row.strategy_id: row.target for row in implemented}
    assert got == {"ETSA": Decimal("130"), "RPSchteroids": Decimal("60")}
    assert decisions[0].suppressed is False
