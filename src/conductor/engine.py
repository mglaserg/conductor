from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from conductor.adapters.base import ExecutionAdapter
from conductor.domain.models import StrategyIntent, TradeDelta
from conductor.ledger import ConductorLedger
from conductor.portfolio import PortfolioBuilder
from conductor.reconcile import DesiredStateReconciler


@dataclass(slots=True)
class ConductorEngine:
    portfolio: PortfolioBuilder
    reconciler: DesiredStateReconciler
    execution: ExecutionAdapter
    ledger: ConductorLedger | None = None

    def run_once(self, intents: Iterable[StrategyIntent]) -> list[TradeDelta]:
        virtual = self.portfolio.build_virtual_targets(intents)
        if self.ledger:
            self.ledger.replace_virtual_targets(virtual)

        aggregate = self.portfolio.aggregate(virtual)
        deltas = self.reconciler.reconcile(aggregate, self.execution.positions())

        if self.ledger:
            self.ledger.append_event(
                "reconciliation_planned",
                {
                    "deltas": [
                        {
                            "instrument": d.instrument,
                            "current": str(d.current),
                            "desired": str(d.desired),
                            "delta": str(d.delta),
                        }
                        for d in deltas
                    ]
                },
            )

        self.execution.submit_deltas(deltas)
        return deltas
