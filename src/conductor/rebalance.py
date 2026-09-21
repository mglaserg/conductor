from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from conductor.domain.models import ExposureType, InstrumentSpec, SleeveAllocation, VirtualTarget, ZERO
from conductor.ledger import ConductorLedger


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
    ) -> None:
        self.ledger = ledger
        self.allocations = dict(allocations)
        self.instruments = instruments

    def _sleeve_capital(self, sleeve_id: str) -> Decimal:
        configured = self.allocations.get(sleeve_id)
        if configured is None:
            return ZERO
        strategy_ids = set(configured.strategy_weights)
        return sum(
            (
                Decimal(row["allocated_capital"])
                for row in self.ledger.strategy_accounts()
                if row["strategy_id"] in strategy_ids
            ),
            ZERO,
        )

    def apply(self, desired: list[VirtualTarget]) -> tuple[list[VirtualTarget], list[RebalanceBandDecision]]:
        desired_by_key = {
            (row.strategy_id, row.book_id, row.sleeve_id, row.route_id, row.instrument): row
            for row in desired
        }
        current_rows = self.ledger.virtual_positions()
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

        groups: dict[tuple[str, str, str], set[tuple[str, str, str, str, str]]] = defaultdict(set)
        for key in set(desired_by_key) | set(current_by_key):
            groups[(key[2], key[3], key[4])].add(key)

        implemented: list[VirtualTarget] = []
        decisions: list[RebalanceBandDecision] = []
        for (sleeve_id, route_id, instrument), keys in sorted(groups.items()):
            allocation = self.allocations.get(sleeve_id)
            band = ZERO if allocation is None else allocation.rebalance_band
            current_qty = sum((current_by_key.get(key, ZERO) for key in keys), ZERO)
            desired_qty = sum(
                (desired_by_key[key].target if key in desired_by_key else ZERO for key in keys), ZERO
            )
            spec = self.instruments[instrument]
            delta_notional = (desired_qty - current_qty) * spec.unit_notional
            capital_base = self._sleeve_capital(sleeve_id) if band > ZERO else ZERO
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
                implemented.extend(desired_by_key[key] for key in sorted(keys) if key in desired_by_key)
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
