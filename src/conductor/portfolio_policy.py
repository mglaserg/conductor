from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from conductor.domain.models import RiskDecision, StrategyIntent, VirtualTarget
from conductor.policy import StrategyPolicyDecision, StrategyPolicyEngine
from conductor.rebalance import RebalanceBandDecision, VirtualRebalanceBuffer
from conductor.risk import PortfolioRiskEngine


@dataclass(frozen=True, slots=True)
class PortfolioPolicyResult:
    policy_adjusted: list[VirtualTarget]
    risk_adjusted: list[VirtualTarget]
    implemented: list[VirtualTarget]
    strategy_decisions: list[StrategyPolicyDecision]
    risk_decision: RiskDecision
    rebalance_decisions: list[RebalanceBandDecision]


class PortfolioPolicyEngine:
    """Compose strategy implementation policy and route-level portfolio policy.

    This is deliberately not an allocator or execution engine. Capital allocation is resolved by
    the runtime, then this stage governs the translated strategy targets before broker aggregation:
    strategy constraints -> route risk -> strategy/sleeve implementation deadbands.
    """

    def __init__(
        self,
        *,
        strategy_policy: StrategyPolicyEngine,
        risk: PortfolioRiskEngine,
        rebalance_buffer: VirtualRebalanceBuffer | None = None,
    ) -> None:
        self.strategy_policy = strategy_policy
        self.risk = risk
        self.rebalance_buffer = rebalance_buffer

    def apply(
        self,
        raw_targets: Iterable[VirtualTarget],
        intents: Iterable[StrategyIntent],
        *,
        route_ids: set[str] | None = None,
    ) -> PortfolioPolicyResult:
        raw = list(raw_targets)
        resolved_intents = list(intents)
        policy_adjusted, strategy_decisions = self.strategy_policy.apply(raw, resolved_intents)
        risk_adjusted, risk_decision = self.risk.apply(policy_adjusted)
        if self.rebalance_buffer is None:
            implemented, rebalance_decisions = risk_adjusted, []
        else:
            implemented, rebalance_decisions = self.rebalance_buffer.apply(
                risk_adjusted, route_ids=route_ids
            )
        return PortfolioPolicyResult(
            policy_adjusted=policy_adjusted,
            risk_adjusted=risk_adjusted,
            implemented=implemented,
            strategy_decisions=strategy_decisions,
            risk_decision=risk_decision,
            rebalance_decisions=rebalance_decisions,
        )
