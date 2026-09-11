from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from conductor.adapters.paper import PaperExecutionAdapter
from conductor.domain.models import BrokerPosition, ExposureType, InstrumentSpec, SleeveAllocation, StrategyIntent
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import IntentBook, PortfolioBuilder
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def main() -> None:
    """Portfolio-level ETSA + RPSchteroids paper orchestration demonstration."""
    nav = Decimal("250000")
    instruments = {
        "AAPL": InstrumentSpec("AAPL", Decimal("200"), lot_size=Decimal("1"), venue="IBKR"),
        "MSFT": InstrumentSpec("MSFT", Decimal("500"), lot_size=Decimal("1"), venue="IBKR"),
        "NVDA": InstrumentSpec("NVDA", Decimal("120"), lot_size=Decimal("1"), venue="IBKR"),
    }
    allocations = {
        "equities": SleeveAllocation(
            sleeve_id="equities",
            portfolio_weight=Decimal("0.50"),
            strategy_weights={"ETSA": Decimal("0.85"), "RPSchteroids": Decimal("0.15")},
        )
    }

    intents = [
        StrategyIntent(
            strategy_id="ETSA",
            sleeve_id="equities",
            exposure_type=ExposureType.NAV_WEIGHT,
            targets={"AAPL": Decimal("0.40"), "MSFT": Decimal("-0.30")},
        ),
        StrategyIntent(
            strategy_id="RPSchteroids",
            sleeve_id="equities",
            exposure_type=ExposureType.NAV_WEIGHT,
            targets={"AAPL": Decimal("0.20"), "NVDA": Decimal("0.30")},
        ),
    ]

    paper = PaperExecutionAdapter(
        [
            BrokerPosition("AAPL", Decimal("200")),
            BrokerPosition("MSFT", Decimal("-60")),
            BrokerPosition("NVDA", Decimal("40")),
        ]
    )
    portfolio = PortfolioBuilder(allocations, instruments, portfolio_nav=nav)

    with TemporaryDirectory() as tempdir:
        ledger = ConductorLedger(Path(tempdir) / "conductor.sqlite")
        engine = ConductorEngine(
            portfolio=portfolio,
            reconciler=DesiredStateReconciler(),
            execution=paper,
            risk=PortfolioRiskEngine(
                portfolio_nav=nav,
                max_gross_leverage=Decimal("1.25"),
                max_instrument_nav=Decimal("0.25"),
            ),
            order_planner=OrderPlanner(
                portfolio_nav=nav,
                instruments=instruments,
                min_trade_nav_bps=Decimal("1"),
            ),
            intent_book=IntentBook(),
            ledger=ledger,
        )

        virtual = portfolio.build_virtual_targets(engine.intent_book.resolve(intents))
        budgets = [
            portfolio.capital_budget("equities", "ETSA"),
            portfolio.capital_budget("equities", "RPSchteroids"),
        ]

        print("CONDUCTOR V0.2 — PORTFOLIO OPERATING DEMO")
        print("=" * 57)
        print(f"Portfolio NAV       {_money(nav)}")
        print(f"Equities sleeve     {_money(nav * Decimal('0.50'))}  (50%)")
        for budget in budgets:
            print(f"  {budget.strategy_id:<16} {_money(budget.strategy_nav)}")

        print("\nVIRTUAL STRATEGY TARGETS")
        for target in virtual:
            print(
                f"  {target.strategy_id:<16} {target.instrument:<5} "
                f"qty={target.target:>6}  notional={_money(target.notional):>12}"
            )

        result = engine.run_cycle(intents)
        print("\nPORTFOLIO RISK")
        print(f"  decision           {result.risk.reason}")
        print(f"  gross leverage     {result.metrics.gross_leverage:.3f}x")
        print(f"  net leverage       {result.metrics.net_leverage:.3f}x")

        print("\nDESIRED vs ACTUAL — ORDERS")
        if not result.deltas:
            print("  No broker trades required.")
        for delta in result.deltas:
            side = "BUY" if delta.delta > 0 else "SELL"
            print(
                f"  {delta.instrument:<5} current={delta.current:>6} "
                f"desired={delta.desired:>6}  {side:<4} {abs(delta.delta):>6} "
                f"~{_money(delta.estimated_notional)}"
            )

        print("\nBROKER STATE AFTER PAPER FILLS")
        for position in paper.positions():
            print(f"  {position.instrument:<5} {position.quantity:>8}")

        print("\nVIRTUAL OWNERSHIP COMMITTED")
        for row in ledger.virtual_positions():
            print(
                f"  {row['strategy_id']:<16} {row['instrument']:<5} "
                f"qty={row['quantity']:>6}  notional={_money(Decimal(row['notional']))}"
            )

        second = engine.run_cycle(intents)
        print("\nIDEMPOTENCY CHECK")
        print(f"  second cycle trades: {len(second.deltas)}")
        print(f"  reconciled:          {second.reconciled}")
        print("\nNothing here touches a live account.")


if __name__ == "__main__":
    main()
