from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from statistics import fmean
from typing import Mapping, Sequence


class AllocationError(RuntimeError):
    pass


class InsufficientAllocationHistory(AllocationError):
    pass


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    method: str
    weights: Mapping[str, Decimal]
    diagnostics: Mapping[str, object]


class StaticAllocator:
    def allocate(self, configured: Mapping[str, Decimal]) -> AllocationDecision:
        if any(weight < 0 for weight in configured.values()):
            raise AllocationError("static allocator weights cannot be negative")
        total = sum(configured.values(), Decimal("0"))
        if total <= 0:
            raise AllocationError("static allocator requires positive configured weights")
        weights = {name: weight / total for name, weight in configured.items()}
        return AllocationDecision("static", weights, {"configured_total": str(total)})


class InverseVolAllocator:
    def __init__(
        self,
        *,
        ewma_lambda: Decimal = Decimal("0.94"),
        min_observations: int = 20,
    ) -> None:
        if not Decimal("0") < ewma_lambda < Decimal("1"):
            raise ValueError("ewma_lambda must be between 0 and 1")
        self.ewma_lambda = ewma_lambda
        self.min_observations = min_observations

    def _ewma_vol(self, values: Sequence[Decimal]) -> Decimal:
        if len(values) < self.min_observations:
            raise InsufficientAllocationHistory(
                f"need {self.min_observations} observations, got {len(values)}"
            )
        lam = float(self.ewma_lambda)
        returns = [float(x) for x in values]
        mean = fmean(returns)
        variance = 0.0
        weight = 1.0
        weight_sum = 0.0
        for value in reversed(returns):
            variance += weight * (value - mean) ** 2
            weight_sum += weight
            weight *= lam
        if weight_sum <= 0:
            raise AllocationError("invalid EWMA weight sum")
        vol = sqrt(variance / weight_sum)
        if vol <= 0:
            raise AllocationError("zero volatility cannot be inverse-vol weighted")
        return Decimal(str(vol))

    def allocate(
        self,
        returns: Mapping[str, Sequence[Decimal]],
        *,
        risk_budgets: Mapping[str, Decimal] | None = None,
    ) -> AllocationDecision:
        vols = {name: self._ewma_vol(series) for name, series in returns.items()}
        budgets = risk_budgets or {name: Decimal("1") for name in vols}
        raw = {name: budgets.get(name, Decimal("0")) / vol for name, vol in vols.items()}
        total = sum(raw.values(), Decimal("0"))
        if total <= 0:
            raise AllocationError("inverse-vol raw weights are non-positive")
        weights = {name: value / total for name, value in raw.items()}
        return AllocationDecision(
            "inverse_vol",
            weights,
            {
                "ewma_lambda": str(self.ewma_lambda),
                "volatility": {name: str(vol) for name, vol in vols.items()},
            },
        )


class ERCAllocator:
    """Long-only equal-risk-contribution allocator using open-source SciPy/sklearn.

    Ledoit-Wolf shrinkage estimates covariance; SciPy SLSQP solves for weights whose
    component risk contributions are equal (or proportional to supplied budgets).
    """

    def __init__(self, *, min_observations: int = 30) -> None:
        self.min_observations = min_observations

    def allocate(
        self,
        returns: Mapping[str, Sequence[Decimal]],
        *,
        risk_budgets: Mapping[str, Decimal] | None = None,
    ) -> AllocationDecision:
        try:
            import numpy as np
            from scipy.optimize import minimize
            from sklearn.covariance import LedoitWolf
        except ImportError as exc:  # pragma: no cover - optional install path
            raise AllocationError(
                "ERC requires the 'allocation' extra (numpy, scipy, scikit-learn)"
            ) from exc

        names = sorted(returns)
        if not names:
            raise AllocationError("ERC requires at least one strategy")
        lengths = {len(returns[name]) for name in names}
        if len(lengths) != 1:
            raise AllocationError("ERC return histories must be aligned and equal length")
        observations = lengths.pop()
        if observations < self.min_observations:
            raise InsufficientAllocationHistory(
                f"need {self.min_observations} aligned observations, got {observations}"
            )
        matrix = np.array([[float(x) for x in returns[name]] for name in names], dtype=float).T
        covariance = LedoitWolf().fit(matrix).covariance_

        if risk_budgets is None:
            budgets = np.ones(len(names), dtype=float) / len(names)
        else:
            budgets = np.array([float(risk_budgets.get(name, Decimal("0"))) for name in names])
            if np.any(budgets < 0) or budgets.sum() <= 0:
                raise AllocationError("ERC risk budgets must be non-negative with positive sum")
            budgets = budgets / budgets.sum()

        def objective(weights):
            variance = float(weights @ covariance @ weights)
            if variance <= 0:
                return 1e9
            marginal = covariance @ weights
            contributions = weights * marginal
            shares = contributions / variance
            return float(np.square(shares - budgets).sum())

        initial = np.ones(len(names), dtype=float) / len(names)
        result = minimize(
            objective,
            initial,
            method="SLSQP",
            bounds=[(0.0, 1.0)] * len(names),
            constraints=[{"type": "eq", "fun": lambda w: float(w.sum() - 1.0)}],
            options={"maxiter": 1000, "ftol": 1e-12},
        )
        if not result.success:
            raise AllocationError(f"ERC optimization failed: {result.message}")
        weights_array = result.x / result.x.sum()
        variance = float(weights_array @ covariance @ weights_array)
        contributions = weights_array * (covariance @ weights_array)
        shares = contributions / variance
        weights = {
            name: Decimal(str(float(weights_array[index]))) for index, name in enumerate(names)
        }
        return AllocationDecision(
            "erc",
            weights,
            {
                "covariance": "ledoit_wolf",
                "risk_contributions": {
                    name: str(float(shares[index])) for index, name in enumerate(names)
                },
                "observations": observations,
            },
        )


class FallbackAllocator:
    """ERC -> inverse vol -> static, with every fallback explicit in diagnostics."""

    def __init__(
        self,
        *,
        static: StaticAllocator | None = None,
        inverse_vol: InverseVolAllocator | None = None,
        erc: ERCAllocator | None = None,
    ) -> None:
        self.static = static or StaticAllocator()
        self.inverse_vol = inverse_vol or InverseVolAllocator()
        self.erc = erc or ERCAllocator()

    def allocate(
        self,
        method: str,
        *,
        configured: Mapping[str, Decimal],
        returns: Mapping[str, Sequence[Decimal]] | None = None,
    ) -> AllocationDecision:
        method = method.lower()
        if method == "static":
            return self.static.allocate(configured)
        if method == "inverse_vol":
            try:
                if returns is None:
                    raise InsufficientAllocationHistory("no return history")
                return self.inverse_vol.allocate(returns)
            except AllocationError as exc:
                fallback = self.static.allocate(configured)
                return AllocationDecision(
                    fallback.method,
                    fallback.weights,
                    {**fallback.diagnostics, "fallback_from": "inverse_vol", "reason": str(exc)},
                )
        if method == "erc":
            try:
                if returns is None:
                    raise InsufficientAllocationHistory("no return history")
                return self.erc.allocate(returns)
            except AllocationError as erc_error:
                try:
                    if returns is None:
                        raise InsufficientAllocationHistory("no return history")
                    fallback = self.inverse_vol.allocate(returns)
                    return AllocationDecision(
                        fallback.method,
                        fallback.weights,
                        {
                            **fallback.diagnostics,
                            "fallback_from": "erc",
                            "reason": str(erc_error),
                        },
                    )
                except AllocationError as iv_error:
                    fallback = self.static.allocate(configured)
                    return AllocationDecision(
                        fallback.method,
                        fallback.weights,
                        {
                            **fallback.diagnostics,
                            "fallback_from": "erc->inverse_vol",
                            "reason": f"ERC: {erc_error}; inverse vol: {iv_error}",
                        },
                    )
        raise AllocationError(f"unknown allocator: {method}")
