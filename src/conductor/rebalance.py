from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from conductor.domain.models import (
    ZERO,
    ExposureType,
    InstrumentSpec,
    SleeveAllocation,
    VirtualTarget,
)
from conductor.ledger import ConductorLedger
from conductor.policy import StrategyPolicy


@dataclass(frozen=True, slots=True)
class RebalanceBandDecision:
    sleeve_id: str
    route_id: str
    instrument: str
    band: Decimal
    capital_base: Decimal
    current_quantity: Decimal
    desired_quantity: Decimal
    delta_notional: Decimal
    suppressed: bool
    scope: str = "sleeve"
    strategy_id: str | None = None


class VirtualRebalanceBuffer:
    """Apply sleeve implementation bands before different strategies are netted.

    This preserves the existing ETSA/RPS behavior even when TLAQ owns the same
    physical ticker. A suppressed sleeve/instrument keeps its current *implemented*
    virtual ownership; the desired target remains separately auditable.
    """

    def __init__(
        self,
        *,
        ledger: ConductorLedger,
        allocations: Mapping[str, SleeveAllocation],
        instruments: dict[str, InstrumentSpec],
        strategy_policies: Mapping[str, StrategyPolicy] | None = None,
    ) -> None:
        self.ledger = ledger
        self.allocations = dict(allocations)
        self.instruments = instruments
        self.strategy_policies = dict(strategy_policies or {})

    def _sleeve_capital(self, sleeve_id: str, route_id: str) -> Decimal:
        configured = self.allocations.get(sleeve_id)
        if configured is None:
            return ZERO
        strategy_ids = set(configured.strategy_weights)
        return sum(
            (
                Decimal(row["allocated_capital"])
                for row in self.ledger.strategy_accounts()
                if row["strategy_id"] in strategy_ids and row["route_id"] == route_id
            ),
            ZERO,
        )

    def apply(
        self,
        desired: list[VirtualTarget],
        *,
        route_ids: set[str] | None = None,
    ) -> tuple[list[VirtualTarget], list[RebalanceBandDecision]]:
        desired_by_key = {
            (row.strategy_id, row.book_id, row.sleeve_id, row.route_id, row.instrument): row
            for row in desired
        }
        current_rows = self.ledger.implementation_positions(route_ids=route_ids)
        current_by_key = {
            (
                row["strategy_id"],
                row["book_id"],
                row["sleeve_id"],
                row["route_id"],
                row["instrument"],
            ): Decimal(row["quantity"])
            for row in current_rows
        }

        decisions: list[RebalanceBandDecision] = []
        strategy_adjusted = dict(desired_by_key)
        # Strategy deadbands are evaluated against that strategy's own allocated capital before
        # sleeve aggregation or cross-strategy netting. This prevents one strategy's large move
        # from dragging another strategy's microscopic rebalance through the broker.
        for key in sorted(set(desired_by_key) | set(current_by_key)):
            strategy_id, book_id, sleeve_id, route_id, instrument = key
            policy = self.strategy_policies.get(strategy_id)
            band = ZERO if policy is None else policy.rebalance_band
            if band <= ZERO:
                continue
            account = self.ledger.strategy_account(strategy_id, book_id=book_id)
            capital_base = ZERO if account is None else Decimal(account["allocated_capital"])
            if capital_base <= ZERO:
                raise ValueError(
                    f"rebalance band configured for {strategy_id}/{book_id} "
                    "but allocated capital is zero"
                )
            current_qty = current_by_key.get(key, ZERO)
            desired_row = desired_by_key.get(key)
            desired_qty = ZERO if desired_row is None else desired_row.target
            spec = self.instruments[instrument]
            delta_notional = (desired_qty - current_qty) * spec.unit_notional
            suppressed = abs(delta_notional) / capital_base < band
            decisions.append(
                RebalanceBandDecision(
                    sleeve_id=sleeve_id,
                    route_id=route_id,
                    instrument=instrument,
                    band=band,
                    capital_base=capital_base,
                    current_quantity=current_qty,
                    desired_quantity=desired_qty,
                    delta_notional=delta_notional,
                    suppressed=suppressed,
                    scope="strategy",
                    strategy_id=strategy_id,
                )
            )
            if not suppressed:
                continue
            if current_qty == ZERO:
                strategy_adjusted.pop(key, None)
                continue
            strategy_adjusted[key] = VirtualTarget(
                strategy_id=strategy_id,
                book_id=book_id,
                sleeve_id=sleeve_id,
                route_id=route_id,
                instrument=instrument,
                target=current_qty,
                notional=current_qty * spec.unit_notional,
                exposure_type=ExposureType.QUANTITY,
                source_exposure_type=ExposureType.QUANTITY,
                lot_size=spec.lot_size,
            )

        groups: dict[tuple[str, str, str], set[tuple[str, str, str, str, str]]] = defaultdict(set)
        for key in set(strategy_adjusted) | set(current_by_key):
            groups[(key[2], key[3], key[4])].add(key)

        implemented: list[VirtualTarget] = []
        for (sleeve_id, route_id, instrument), keys in sorted(groups.items()):
            allocation = self.allocations.get(sleeve_id)
            band = ZERO if allocation is None else allocation.rebalance_band
            current_qty = sum((current_by_key.get(key, ZERO) for key in keys), ZERO)
            desired_qty = sum(
                (
                    strategy_adjusted[key].target
                    if key in strategy_adjusted
                    else ZERO
                    for key in keys
                ),
                ZERO,
            )
            spec = self.instruments[instrument]
            delta_notional = (desired_qty - current_qty) * spec.unit_notional
            capital_base = self._sleeve_capital(sleeve_id, route_id) if band > ZERO else ZERO
            if band > ZERO and capital_base <= ZERO:
                raise ValueError(
                    f"rebalance band configured for {sleeve_id} but allocated capital is zero"
                )
            suppressed = band > ZERO and abs(delta_notional) / capital_base < band
            decisions.append(
                RebalanceBandDecision(
                    sleeve_id=sleeve_id,
                    route_id=route_id,
                    instrument=instrument,
                    band=band,
                    capital_base=capital_base,
                    current_quantity=current_qty,
                    desired_quantity=desired_qty,
                    delta_notional=delta_notional,
                    suppressed=suppressed,
                )
            )

            if not suppressed:
                implemented.extend(
                    strategy_adjusted[key]
                    for key in sorted(keys)
                    if key in strategy_adjusted
                )
                continue

            # Keep the sleeve/instrument exactly at its currently implemented ownership.
            for key in sorted(keys):
                quantity = current_by_key.get(key, ZERO)
                if quantity == ZERO:
                    continue
                implemented.append(
                    VirtualTarget(
                        strategy_id=key[0],
                        book_id=key[1],
                        sleeve_id=key[2],
                        route_id=key[3],
                        instrument=key[4],
                        target=quantity,
                        notional=quantity * spec.unit_notional,
                        exposure_type=ExposureType.QUANTITY,
                        source_exposure_type=ExposureType.QUANTITY,
                        lot_size=spec.lot_size,
                    )
                )

        return implemented, decisions
