from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable
from uuid import uuid4

from conductor.accounting import VirtualAccountingEngine
from conductor.adapters.base import ExecutionAdapter
from conductor.domain.models import RunResult, RunState, StrategyIntent, TradeDelta
from conductor.ledger import ConductorLedger
from conductor.orders import OrderPlanner
from conductor.policy import StrategyPolicyEngine
from conductor.portfolio_policy import PortfolioPolicyEngine
from conductor.portfolio import IntentBook, PortfolioBuilder
from conductor.rebalance import VirtualRebalanceBuffer
from conductor.reconcile import DesiredStateReconciler
from conductor.risk import PortfolioRiskEngine

TERMINAL_EXECUTION_FAILURES = frozenset({"rejected", "denied", "canceled", "expired", "failed"})


@dataclass(slots=True)
class ConductorEngine:
    portfolio: PortfolioBuilder
    reconciler: DesiredStateReconciler
    execution: ExecutionAdapter
    risk: PortfolioRiskEngine
    order_planner: OrderPlanner
    intent_book: IntentBook = field(default_factory=IntentBook)
    ledger: ConductorLedger | None = None
    accounting: VirtualAccountingEngine | None = None
    rebalance_buffer: VirtualRebalanceBuffer | None = None
    strategy_policy: StrategyPolicyEngine | None = None
    portfolio_policy: PortfolioPolicyEngine | None = None
    external_authority_routes: set[str] = field(default_factory=set)

    def run_cycle(
        self, intents: Iterable[StrategyIntent], *, run_id: str | None = None
    ) -> RunResult:
        run_id = run_id or uuid4().hex
        resolved = self.intent_book.resolve(intents)
        active_routes = {intent.route_id for intent in resolved}
        warm = getattr(self.execution, "warm_instruments", None)
        if callable(warm):
            route_instruments: dict[str, set[str]] = defaultdict(set)
            for intent in resolved:
                if intent.status.value == "flat":
                    continue
                route_instruments[intent.route_id].update(intent.targets)
            warm(route_instruments)
        raw_virtual = self.portfolio.build_virtual_targets(resolved)
        if self.portfolio_policy is not None:
            policy_result = self.portfolio_policy.apply(
                raw_virtual, resolved, route_ids=active_routes
            )
            policy_virtual = policy_result.policy_adjusted
            desired_virtual = policy_result.risk_adjusted
            implemented_virtual = policy_result.implemented
            policy_decisions = policy_result.strategy_decisions
            risk_decision = policy_result.risk_decision
            band_decisions = policy_result.rebalance_decisions
        else:
            if self.strategy_policy is not None:
                policy_virtual, policy_decisions = self.strategy_policy.apply(raw_virtual, resolved)
            else:
                policy_virtual, policy_decisions = raw_virtual, []
            desired_virtual, risk_decision = self.risk.apply(policy_virtual)
            if self.rebalance_buffer is not None:
                implemented_virtual, band_decisions = self.rebalance_buffer.apply(
                    desired_virtual, route_ids=active_routes
                )
            else:
                implemented_virtual, band_decisions = desired_virtual, []

        if self.ledger:
            # Virtual targets mean desired/policy/risk-adjusted economic state. Implemented
            # ownership can differ temporarily because of explicit rebalance bands.
            self.ledger.replace_virtual_targets(desired_virtual, route_ids=active_routes)
            for decision in policy_decisions:
                self.ledger.append_event(
                    "strategy.policy_decision",
                    {
                        "run_id": run_id,
                        "strategy_id": decision.strategy_id,
                        "book_id": decision.book_id,
                        "route_id": decision.route_id,
                        "policy_version": decision.policy_version,
                        "policy_source": decision.policy_source,
                        "gross_before": str(decision.gross_before),
                        "gross_after": str(decision.gross_after),
                        "largest_position_before": str(decision.largest_position_before),
                        "capital_base": str(decision.capital_base),
                        "observed_volatility": (
                            None
                            if decision.observed_volatility is None
                            else str(decision.observed_volatility)
                        ),
                        "volatility_scale": str(decision.volatility_scale),
                        "constraint_scale": str(decision.constraint_scale),
                        "total_scale": str(decision.total_scale),
                        "reason": decision.reason,
                    },
                )
            self.ledger.append_event(
                "risk_decision",
                {
                    "run_id": run_id,
                    "scale": str(risk_decision.scale),
                    "reason": risk_decision.reason,
                    "gross_before": str(risk_decision.gross_before),
                    "gross_after": str(risk_decision.gross_after),
                    "net_before": str(risk_decision.net_before),
                    "net_after": str(risk_decision.net_after),
                    "route_scales": {
                        key: str(value) for key, value in risk_decision.route_scales.items()
                    },
                    "route_reasons": dict(risk_decision.route_reasons),
                },
            )

        metrics = self.portfolio.metrics(implemented_virtual, route_ids=active_routes)
        if self.ledger and band_decisions:
            for decision in band_decisions:
                self.ledger.append_event(
                    "rebalance.band_decision",
                    {
                        "run_id": run_id,
                        "sleeve_id": decision.sleeve_id,
                        "route_id": decision.route_id,
                        "instrument": decision.instrument,
                        "band": str(decision.band),
                        "capital_base": str(decision.capital_base),
                        "current_quantity": str(decision.current_quantity),
                        "desired_quantity": str(decision.desired_quantity),
                        "delta_notional": str(decision.delta_notional),
                        "suppressed": decision.suppressed,
                        "scope": decision.scope,
                        "strategy_id": decision.strategy_id,
                    },
                )

        aggregate = self.portfolio.aggregate(implemented_virtual)
        actual_before = [
            position
            for position in self.execution.positions()
            if position.route_id in active_routes
        ]
        raw_deltas = self.reconciler.reconcile(aggregate, actual_before)
        deltas = self.order_planner.plan(raw_deltas)

        if self.ledger:
            self.ledger.append_event(
                "reconciliation_planned",
                {
                    "run_id": run_id,
                    "deltas": [
                        {
                            "route_id": d.route_id,
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

        if self.ledger:
            def target_payload(rows):
                return [
                    {
                        "strategy_id": row.strategy_id,
                        "book_id": row.book_id,
                        "route_id": row.route_id,
                        "instrument": row.instrument,
                        "quantity": str(row.target),
                        "notional": str(row.notional),
                    }
                    for row in rows
                ]

            self.ledger.append_event(
                "portfolio.decision_lineage",
                {
                    "run_id": run_id,
                    "raw_strategy_targets": target_payload(raw_virtual),
                    "policy_adjusted_targets": target_payload(policy_virtual),
                    "risk_adjusted_targets": target_payload(desired_virtual),
                    "implemented_targets": target_payload(implemented_virtual),
                    "aggregate_broker_targets": [
                        {
                            "route_id": row.route_id,
                            "instrument": row.instrument,
                            "quantity": str(row.target),
                            "notional": str(row.notional),
                        }
                        for row in aggregate
                    ],
                    "planned_deltas": [
                        {
                            "route_id": row.route_id,
                            "instrument": row.instrument,
                            "current": str(row.current),
                            "desired": str(row.desired),
                            "delta": str(row.delta),
                            "estimated_notional": str(row.estimated_notional),
                        }
                        for row in deltas
                    ],
                },
            )

        execution_reports = list(self.execution.submit_deltas(deltas) or [])
        if self.ledger and execution_reports:
            self.ledger.append_event(
                "execution.reports_received",
                {
                    "run_id": run_id,
                    "reports": [
                        {
                            "route_id": report.route_id,
                            "instrument": report.instrument,
                            "requested_quantity": str(report.requested_quantity),
                            "filled_quantity": str(report.filled_quantity),
                            "avg_price": (
                                None if report.avg_price is None else str(report.avg_price)
                            ),
                            "commission": str(report.commission),
                            "status": report.status,
                            "order_id": report.order_id,
                        }
                        for report in execution_reports
                    ],
                },
            )
        actual_after = [
            position
            for position in self.execution.positions()
            if position.route_id in active_routes
        ]
        reconciled = self.reconciler.is_reconciled(aggregate, actual_after)
        shadow_planned = any(report.status == "shadow" for report in execution_reports)
        terminal_failure = any(
            report.status.lower() in TERMINAL_EXECUTION_FAILURES
            for report in execution_reports
        )
        if reconciled:
            state = RunState.RECONCILED
        elif shadow_planned:
            state = RunState.PLANNED
        elif terminal_failure:
            # A rejected/canceled terminal order cannot converge without a new
            # operator or scheduler action. Distinguish it from an asynchronous
            # submission whose broker state may still catch up.
            state = RunState.BLOCKED
        else:
            state = RunState.SUBMITTED

        # For synchronous paper/sandbox execution this commits immediately. For a
        # live async adapter, ownership remains uncommitted until a later cycle sees
        # venue state fully aligned with desired aggregate state.
        external_shadow = bool(active_routes & self.external_authority_routes)
        if reconciled and self.ledger and not external_shadow:
            if self.accounting is not None:
                self.accounting.commit(
                    implemented_virtual,
                    run_id=run_id,
                    execution_reports=execution_reports,
                    route_ids=active_routes,
                )
            else:
                self.ledger.replace_virtual_positions(
                    implemented_virtual, route_ids=active_routes
                )
        elif reconciled and self.ledger and external_shadow:
            self.ledger.append_event(
                "shadow.reconciled_without_virtual_commit",
                {
                    "run_id": run_id,
                    "route_ids": sorted(active_routes & self.external_authority_routes),
                },
            )

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
