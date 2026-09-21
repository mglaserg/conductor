from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from conductor.domain.models import ExecutionReport, InstrumentSpec, VirtualTarget, ZERO
from conductor.ledger import ConductorLedger
from conductor.runtime.models import StrategyAccountView

Owner = tuple[str, str]


@dataclass(frozen=True, slots=True)
class InternalCross:
    route_id: str
    instrument: str
    from_strategy: str
    from_book: str
    to_strategy: str
    to_book: str
    quantity: Decimal
    reference_unit_notional: Decimal


class VirtualAccountingEngine:
    """Economic subledger for strategy ownership and virtual cash.

    V0.4 shadow/paper settlement uses Conductor's current instrument mark as the
    transfer price. Live execution will replace the external-trade portion with
    actual fill/commission allocation while preserving the same ownership model.
    """

    def __init__(
        self,
        ledger: ConductorLedger,
        instruments: dict[str, InstrumentSpec],
    ) -> None:
        self.ledger = ledger
        self.instruments = instruments

    def account_view(self, strategy_id: str, *, book_id: str = "main") -> StrategyAccountView:
        account = self.ledger.strategy_account(strategy_id, book_id=book_id)
        if account is None:
            raise KeyError(f"strategy account not seeded: {strategy_id}/{book_id}")
        positions = self.ledger.strategy_positions(strategy_id, book_id=book_id)
        notionals: dict[str, Decimal] = {}
        for instrument, quantity in positions.items():
            try:
                spec = self.instruments[instrument]
            except KeyError as exc:
                raise KeyError(f"missing instrument mark for {instrument}") from exc
            notionals[instrument] = quantity * spec.unit_notional
        cash = Decimal(account["cash"])
        net = sum(notionals.values(), ZERO)
        gross = sum((abs(value) for value in notionals.values()), ZERO)
        return StrategyAccountView(
            strategy_id=strategy_id,
            book_id=book_id,
            allocated_capital=Decimal(account["allocated_capital"]),
            cash=cash,
            positions=positions,
            equity=cash + net,
            gross_exposure=gross,
            net_exposure=net,
        )

    def _current_positions(self) -> dict[tuple[str, str, str, str], Decimal]:
        return {
            (row["strategy_id"], row["book_id"], row["route_id"], row["instrument"]): Decimal(
                row["quantity"]
            )
            for row in self.ledger.virtual_positions()
        }

    @staticmethod
    def _pair_crosses(
        deltas: dict[tuple[str, str, str, str], Decimal],
        instruments: dict[str, InstrumentSpec],
    ) -> tuple[list[InternalCross], dict[tuple[str, str, str, str], Decimal]]:
        """Pair opposite strategy changes before anything reaches the market.

        Returns both the deterministic internal crosses and the per-owner remainder
        that must be settled against the external broker fill.
        """
        remaining = dict(deltas)
        by_market: dict[tuple[str, str], list[tuple[tuple[str, str, str, str], Decimal]]] = (
            defaultdict(list)
        )
        for key, delta in deltas.items():
            if delta != ZERO:
                by_market[(key[2], key[3])].append((key, delta))

        crosses: list[InternalCross] = []
        for (route_id, instrument), changes in sorted(by_market.items()):
            buyers = [[key, delta] for key, delta in changes if delta > ZERO]
            sellers = [[key, -delta] for key, delta in changes if delta < ZERO]
            buyers.sort(key=lambda item: (item[0][0], item[0][1]))
            sellers.sort(key=lambda item: (item[0][0], item[0][1]))
            bi = si = 0
            while bi < len(buyers) and si < len(sellers):
                buyer_key, buy_left = buyers[bi]
                seller_key, sell_left = sellers[si]
                quantity = min(buy_left, sell_left)
                crosses.append(
                    InternalCross(
                        route_id=route_id,
                        instrument=instrument,
                        from_strategy=seller_key[0],
                        from_book=seller_key[1],
                        to_strategy=buyer_key[0],
                        to_book=buyer_key[1],
                        quantity=quantity,
                        reference_unit_notional=instruments[instrument].unit_notional,
                    )
                )
                remaining[buyer_key] -= quantity
                remaining[seller_key] += quantity
                buyers[bi][1] -= quantity
                sellers[si][1] -= quantity
                if buyers[bi][1] == ZERO:
                    bi += 1
                if sellers[si][1] == ZERO:
                    si += 1

        return crosses, {key: value for key, value in remaining.items() if value != ZERO}

    def commit(
        self,
        desired: Iterable[VirtualTarget],
        *,
        run_id: str | None = None,
        execution_reports: Iterable[ExecutionReport] | None = None,
    ) -> None:
        desired_rows = list(desired)
        current = self._current_positions()
        wanted = {
            (t.strategy_id, t.book_id, t.route_id, t.instrument): t.target for t in desired_rows
        }
        keys = set(current) | set(wanted)
        deltas = {key: wanted.get(key, ZERO) - current.get(key, ZERO) for key in keys}

        # Refuse to invent initial virtual cash. Migration must seed every changing book.
        changing_owners = {(key[0], key[1]) for key, delta in deltas.items() if delta != ZERO}
        for strategy_id, book_id in changing_owners:
            if self.ledger.strategy_account(strategy_id, book_id=book_id) is None:
                raise KeyError(f"strategy account not seeded: {strategy_id}/{book_id}")

        cash_changes: dict[Owner, Decimal] = defaultdict(lambda: ZERO)
        crosses, external = self._pair_crosses(deltas, self.instruments)
        for cross in crosses:
            notional = cross.quantity * cross.reference_unit_notional
            cash_changes[(cross.from_strategy, cross.from_book)] += notional
            cash_changes[(cross.to_strategy, cross.to_book)] -= notional
            self.ledger.append_event(
                "accounting.internal_cross",
                {
                    "run_id": run_id,
                    "route_id": cross.route_id,
                    "instrument": cross.instrument,
                    "from_strategy": cross.from_strategy,
                    "from_book": cross.from_book,
                    "to_strategy": cross.to_strategy,
                    "to_book": cross.to_book,
                    "quantity": str(cross.quantity),
                    "reference_unit_notional": str(cross.reference_unit_notional),
                },
            )

        reports_by_market = {
            (report.route_id, report.instrument): report
            for report in (execution_reports or [])
        }
        external_by_market: dict[tuple[str, str], list[tuple[tuple[str, str, str, str], Decimal]]] = (
            defaultdict(list)
        )
        for key, delta in external.items():
            external_by_market[(key[2], key[3])].append((key, delta))

        for (route_id, instrument), owner_changes in sorted(external_by_market.items()):
            expected = sum((delta for _, delta in owner_changes), ZERO)
            report = reports_by_market.get((route_id, instrument))
            spec = self.instruments[instrument]
            if report is not None:
                if report.filled_quantity != expected:
                    raise RuntimeError(
                        f"execution report mismatch for {route_id}/{instrument}: "
                        f"expected {expected}, got {report.filled_quantity}"
                    )
                unit_notional = (
                    report.avg_price * spec.contract_multiplier
                    if report.avg_price is not None
                    else spec.unit_notional
                )
                commission = report.commission
                settlement_source = "broker_fill" if report.avg_price is not None else "reference_mark"
            else:
                unit_notional = spec.unit_notional
                commission = ZERO
                settlement_source = "reference_mark"

            total_abs = sum((abs(delta) for _, delta in owner_changes), ZERO)
            for key, delta in owner_changes:
                owner = (key[0], key[1])
                cash_changes[owner] -= delta * unit_notional
                commission_share = (commission * abs(delta) / total_abs) if total_abs else ZERO
                cash_changes[owner] -= commission_share
                self.ledger.append_event(
                    "accounting.external_settlement",
                    {
                        "run_id": run_id,
                        "route_id": route_id,
                        "instrument": instrument,
                        "strategy_id": key[0],
                        "book_id": key[1],
                        "quantity": str(delta),
                        "unit_notional": str(unit_notional),
                        "commission_allocated": str(commission_share),
                        "settlement_source": settlement_source,
                        "order_id": None if report is None else report.order_id,
                    },
                )

        for (strategy_id, book_id), change in sorted(cash_changes.items()):
            account = self.ledger.strategy_account(strategy_id, book_id=book_id)
            assert account is not None
            before = Decimal(account["cash"])
            after = before + change
            self.ledger.update_strategy_cash(strategy_id, after, book_id=book_id)
            self.ledger.append_event(
                "accounting.virtual_cash_changed",
                {
                    "run_id": run_id,
                    "strategy_id": strategy_id,
                    "book_id": book_id,
                    "cash_before": str(before),
                    "cash_change": str(change),
                    "cash_after": str(after),
                },
            )

        self.ledger.replace_virtual_positions(desired_rows)
