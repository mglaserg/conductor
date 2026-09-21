from decimal import Decimal

from conductor.accounting import VirtualAccountingEngine
from conductor.domain.models import ExposureType, InstrumentSpec, VirtualTarget
from conductor.ledger import ConductorLedger


def target(strategy, quantity):
    return VirtualTarget(
        strategy_id=strategy,
        book_id="main",
        sleeve_id="equities",
        route_id="windows_ibkr_equities",
        instrument="EQ.US.AAPL",
        target=Decimal(str(quantity)),
        notional=Decimal(str(quantity)) * Decimal("100"),
        source_exposure_type=ExposureType.QUANTITY,
    )


def test_virtual_cash_and_internal_cross_preserve_strategy_ownership(tmp_path) -> None:
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    instruments = {"EQ.US.AAPL": InstrumentSpec("EQ.US.AAPL", Decimal("100"))}
    ledger.seed_strategy_account(
        "TLAQ",
        route_id="windows_ibkr_equities",
        allocated_capital=Decimal("100000"),
        cash=Decimal("-12000"),
    )
    ledger.seed_strategy_account(
        "RPSchteroids",
        route_id="windows_ibkr_equities",
        allocated_capital=Decimal("30000"),
        cash=Decimal("5000"),
    )
    ledger.seed_virtual_book(
        strategy_id="TLAQ",
        book_id="main",
        sleeve_id="equities",
        route_id="windows_ibkr_equities",
        positions={"EQ.US.AAPL": Decimal("100")},
    )
    ledger.seed_virtual_book(
        strategy_id="RPSchteroids",
        book_id="main",
        sleeve_id="equities",
        route_id="windows_ibkr_equities",
        positions={"EQ.US.AAPL": Decimal("50")},
    )
    accounting = VirtualAccountingEngine(ledger, instruments)
    accounting.commit([target("TLAQ", 120), target("RPSchteroids", 35)], run_id="x")

    positions = {
        (r["strategy_id"], r["instrument"]): Decimal(r["quantity"])
        for r in ledger.virtual_positions()
    }
    assert positions[("TLAQ", "EQ.US.AAPL")] == 120
    assert positions[("RPSchteroids", "EQ.US.AAPL")] == 35
    # TLAQ buys 20 at $100; RPS sells 15 at $100.
    assert Decimal(ledger.strategy_account("TLAQ")["cash"]) == Decimal("-14000")
    assert Decimal(ledger.strategy_account("RPSchteroids")["cash"]) == Decimal("6500")
    assert any(e["event_type"] == "accounting.internal_cross" for e in ledger.events())


def test_external_fill_price_and_commission_are_allocated_to_strategy_cash(tmp_path) -> None:
    from conductor.domain.models import ExecutionReport

    ledger = ConductorLedger(tmp_path / "fills.sqlite")
    instruments = {"EQ.US.AAPL": InstrumentSpec("EQ.US.AAPL", Decimal("100"))}
    ledger.seed_strategy_account(
        "TLAQ",
        route_id="windows_ibkr_equities",
        allocated_capital=Decimal("100000"),
        cash=Decimal("10000"),
    )
    ledger.seed_virtual_book(
        strategy_id="TLAQ",
        book_id="main",
        sleeve_id="equities",
        route_id="windows_ibkr_equities",
        positions={"EQ.US.AAPL": Decimal("10")},
    )
    accounting = VirtualAccountingEngine(ledger, instruments)
    accounting.commit(
        [target("TLAQ", 12)],
        run_id="fill-run",
        execution_reports=[
            ExecutionReport(
                route_id="windows_ibkr_equities",
                instrument="EQ.US.AAPL",
                requested_quantity=Decimal("2"),
                filled_quantity=Decimal("2"),
                avg_price=Decimal("101"),
                commission=Decimal("1.25"),
                order_id="77",
            )
        ],
    )

    # +2 shares at the real $101 fill, plus $1.25 commission.
    assert Decimal(ledger.strategy_account("TLAQ")["cash"]) == Decimal("9796.75")
    events = ledger.events()
    settlement = next(e for e in events if e["event_type"] == "accounting.external_settlement")
    assert '"settlement_source": "broker_fill"' in settlement["payload_json"]
