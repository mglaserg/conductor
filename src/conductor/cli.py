from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from conductor.adapters.paper import PaperExecutionAdapter
from conductor.domain.models import BrokerPosition, InstrumentSpec, SleeveAllocation
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import IntentBook, PortfolioBuilder
from conductor.protocol.acceptance import InboxProcessor, SnapshotAcceptor
from conductor.protocol.profile import StrategyProfile
from conductor.protocol.sdk import FilesystemConductorClient
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _print_run(label: str, engine: ConductorEngine, intents) -> None:
    result = engine.run_cycle(intents)
    print(f"\n{label}")
    print("-" * len(label))
    if not result.deltas:
        print("  No broker trades required.")
    for delta in result.deltas:
        side = "BUY" if delta.delta > 0 else "SELL"
        print(
            f"  {delta.instrument:<14} current={delta.current:>6} "
            f"desired={delta.desired:>6}  {side:<4} {abs(delta.delta):>6} "
            f"~{_money(delta.estimated_notional)}"
        )
    print(f"  reconciled={result.reconciled}  gross={result.metrics.gross_leverage:.3f}x")


def main() -> None:
    """V0.3 protocol → desired state → paper portfolio reconciliation demo."""
    nav = Decimal("250000")
    now = datetime.now(timezone.utc)
    instruments = {
        "EQ.US.AAPL": InstrumentSpec("EQ.US.AAPL", Decimal("200"), venue="IBKR"),
        "EQ.US.MSFT": InstrumentSpec("EQ.US.MSFT", Decimal("500"), venue="IBKR"),
        "EQ.US.NVDA": InstrumentSpec("EQ.US.NVDA", Decimal("120"), venue="IBKR"),
    }
    allocations = {
        "equities": SleeveAllocation(
            sleeve_id="equities",
            portfolio_weight=Decimal("0.50"),
            strategy_weights={"ETSA": Decimal("0.85"), "RPSchteroids": Decimal("0.15")},
        )
    }
    profiles = {
        "ETSA": StrategyProfile(
            "ETSA", "equities", allowed_books=frozenset({"main"}), max_age=timedelta(hours=36)
        ),
        "RPSchteroids": StrategyProfile(
            "RPSchteroids",
            "equities",
            allowed_books=frozenset({"main"}),
            max_age=timedelta(days=8),
        ),
    }

    paper = PaperExecutionAdapter(
        [
            BrokerPosition("EQ.US.AAPL", Decimal("200")),
            BrokerPosition("EQ.US.MSFT", Decimal("-60")),
            BrokerPosition("EQ.US.NVDA", Decimal("40")),
        ]
    )
    portfolio = PortfolioBuilder(allocations, instruments, portfolio_nav=nav)

    with TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        ledger = ConductorLedger(root / "conductor.sqlite")
        acceptor = SnapshotAcceptor(ledger, profiles)
        processor = InboxProcessor(root / "transport", acceptor)
        client = FilesystemConductorClient(processor.inbox)
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

        print("CONDUCTOR V0.3 - TARGET SNAPSHOT PROTOCOL DEMO")
        print("=" * 59)
        print(f"Portfolio NAV       {_money(nav)}")
        print("Transport           atomic local JSON inbox")
        print("Execution           synchronous paper adapter")

        client.submit_linear(
            strategy_id="ETSA",
            book_id="main",
            revision=1,
            event_id="etsa-r1",
            as_of=now,
            targets={"EQ.US.AAPL": "0.40", "EQ.US.MSFT": "-0.30"},
        )
        client.submit_linear(
            strategy_id="RPSchteroids",
            book_id="main",
            revision=1,
            event_id="rps-r1",
            as_of=now,
            targets={"EQ.US.AAPL": "0.20", "EQ.US.NVDA": "0.30"},
        )
        decisions = processor.process_all(now=now)
        print("\nSNAPSHOT ACCEPTANCE")
        for d in decisions:
            print(f"  {d.strategy_id}/{d.book_id} r{d.revision}: {d.status} ({d.reason})")

        intents = acceptor.current_linear_intents(now=now)
        _print_run("INITIAL DESIRED STATE", engine, intents)

        # Complete snapshot replacement: MSFT is intentionally absent in ETSA r2,
        # therefore ETSA's desired MSFT ownership becomes zero.
        client.submit_linear(
            strategy_id="ETSA",
            book_id="main",
            revision=2,
            event_id="etsa-r2",
            as_of=now,
            targets={"EQ.US.AAPL": "0.20"},
        )
        processor.process_all(now=now)
        intents = acceptor.current_linear_intents(now=now)
        _print_run("ETSA REVISION 2 - MSFT ABSENT => ZERO", engine, intents)
        _print_run("IDEMPOTENCY CHECK", engine, intents)

        print("\nCURRENT DESIRED BOOKS")
        for book in ledger.desired_books():
            print(f"  {book['strategy_id']}/{book['book_id']} revision={book['revision']}")

        print("\nCOMMITTED VIRTUAL OWNERSHIP")
        for row in ledger.virtual_positions():
            print(
                f"  {row['strategy_id']:<16} {row['book_id']:<6} "
                f"{row['instrument']:<14} qty={row['quantity']:>6}"
            )
        print("\nNothing here touches a live account.")


if __name__ == "__main__":
    main()
