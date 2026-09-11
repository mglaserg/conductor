from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from uuid import uuid4

from conductor.adapters.base import ExecutionAdapter
from conductor.domain.models import RunResult, RunState, StrategyIntent, TradeDelta
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.portfolio import IntentBook, PortfolioBuilder
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine


@dataclass(slots=True)
class ConductorEngine:
    portfolio: PortfolioBuilder
    reconciler: DesiredStateReconciler
    execution: ExecutionAdapter
    risk: PortfolioRiskEngine
    order_planner: OrderPlanner
    intent_book: IntentBook = field(default_factory=IntentBook)
    ledger: ConductorLedger | None = None

    def run_cycle(self, intents: Iterable[StrategyIntent]) -> RunResult:
        run_id = uuid4().hex
        resolved = self.intent_book.resolve(intents)
        virtual = self.portfolio.build_virtual_targets(resolved)
        virtual, risk_decision = self.risk.apply(virtual)
        metrics = self.portfolio.metrics(virtual)

        if self.ledger:
            self.ledger.replace_virtual_targets(virtual)
            self.ledger.append_event(
                "risk_decision",
                {
                    "run_id": run_id,
                    "scale": str(risk_decision.scale),
                    "reason": risk_decision.reason,
                    "gross_before": str(risk_decision.gross_before),
                    "gross_after": str(risk_decision.gross_after),
                },
            )

        aggregate = self.portfolio.aggregate(virtual)
        actual_before = self.execution.positions()
        raw_deltas = self.reconciler.reconcile(aggregate, actual_before)
        deltas = self.order_planner.plan(raw_deltas)

        if self.ledger:
            self.ledger.append_event(
                "reconciliation_planned",
                {
                    "run_id": run_id,
                    "deltas": [
                        {
                            "instrument": d.instrument,
                            "current": str(d.current),
                            "desired": str(d.desired),
                            "delta": str(d.delta),
                            "estimated_notional": str(d.estimated_notional),
                        }
                        for d in deltas
                    ],
                },
            )

        self.execution.submit_deltas(deltas)
        actual_after = self.execution.positions()
        reconciled = self.reconciler.is_reconciled(aggregate, actual_after)
        state = RunState.RECONCILED if reconciled else RunState.SUBMITTED

        # For synchronous paper/sandbox execution this commits immediately. For a
        # live async adapter, ownership remains uncommitted until a later cycle sees
        # venue state fully aligned with desired aggregate state.
        if reconciled and self.ledger:
            self.ledger.replace_virtual_positions(virtual)

        if self.ledger:
            self.ledger.record_run(
                run_id,
                state=state.value,
                reconciled=reconciled,
                details={
                    "risk": risk_decision.reason,
                    "gross_leverage": str(metrics.gross_leverage),
                    "net_leverage": str(metrics.net_leverage),
                    "trade_count": len(deltas),
                },
            )
            self.ledger.append_event(
                "run_completed",
                {
                    "run_id": run_id,
                    "state": state.value,
                    "reconciled": reconciled,
                },
            )

        return RunResult(
            run_id=run_id,
            state=state,
            deltas=tuple(deltas),
            risk=risk_decision,
            metrics=metrics,
            reconciled=reconciled,
        )

    def run_once(self, intents: Iterable[StrategyIntent]) -> list[TradeDelta]:
        """V0.1-compatible convenience API."""
        return list(self.run_cycle(intents).deltas)
