from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from conductor.domain.models import ZERO

Owner = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ShadowOwnerProfile:
    strategy_id: str
    book_id: str
    sleeve_id: str


@dataclass(frozen=True, slots=True)
class ShadowAssignment:
    owner: Owner
    instrument: str
    quantity: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class ShadowInferenceResult:
    assignments: tuple[ShadowAssignment, ...]
    unresolved: tuple[dict[str, object], ...]
    warnings: tuple[dict[str, object], ...]

    @property
    def committable(self) -> bool:
        return not self.unresolved


def infer_shadow_ownership(
    *,
    broker_positions: Mapping[str, Decimal],
    profiles: Mapping[str, ShadowOwnerProfile],
    previous: Mapping[Owner, Mapping[str, Decimal]],
    intents: Mapping[str, Mapping[str, Decimal]],
    explicit: Mapping[str, Mapping[str, Decimal]] | None = None,
) -> ShadowInferenceResult:
    """Infer external-authority ownership without rewriting economic history.

    Rules are deliberately conservative:
    - an explicit manifest is authoritative but must sum exactly to the broker;
    - a single-strategy route owns the whole broker route;
    - on shared routes, a previously single-owned symbol stays with that owner (sticky ownership);
    - a brand-new broker symbol may be assigned only to exactly one current intent claimant;
    - an aggregate quantity change on a multiply-owned symbol is ambiguous and fails closed.
    """

    clean_broker = {k: Decimal(v) for k, v in broker_positions.items() if Decimal(v) != ZERO}
    owners_by_instrument: dict[str, list[tuple[Owner, Decimal]]] = defaultdict(list)
    for owner, positions in previous.items():
        for instrument, quantity in positions.items():
            quantity = Decimal(quantity)
            if quantity != ZERO:
                owners_by_instrument[instrument].append((owner, quantity))

    assignments: list[ShadowAssignment] = []
    unresolved: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []

    if explicit is not None:
        unknown = set(explicit) - set(profiles)
        if unknown:
            unresolved.append(
                {"reason": "unknown_strategy", "strategies": sorted(unknown)}
            )
        assigned_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for strategy_id, positions in sorted(explicit.items()):
            profile = profiles.get(strategy_id)
            if profile is None:
                continue
            for instrument, quantity in sorted(positions.items()):
                quantity = Decimal(quantity)
                if quantity == ZERO:
                    continue
                assignments.append(
                    ShadowAssignment(
                        (strategy_id, profile.book_id), instrument, quantity, "explicit_manifest"
                    )
                )
                assigned_totals[instrument] += quantity
        for instrument in sorted(set(clean_broker) | set(assigned_totals)):
            if clean_broker.get(instrument, ZERO) != assigned_totals.get(instrument, ZERO):
                unresolved.append(
                    {
                        "reason": "manifest_mismatch",
                        "instrument": instrument,
                        "broker_quantity": str(clean_broker.get(instrument, ZERO)),
                        "assigned_quantity": str(assigned_totals.get(instrument, ZERO)),
                    }
                )
        return ShadowInferenceResult(tuple(assignments), tuple(unresolved), tuple(warnings))

    if len(profiles) == 1:
        strategy_id, profile = next(iter(profiles.items()))
        assignments.extend(
            ShadowAssignment(
                (strategy_id, profile.book_id), instrument, quantity, "single_owner_route"
            )
            for instrument, quantity in sorted(clean_broker.items())
        )
        return ShadowInferenceResult(tuple(assignments), (), ())

    for instrument, broker_quantity in sorted(clean_broker.items()):
        prior = owners_by_instrument.get(instrument, [])
        prior_total = sum((quantity for _, quantity in prior), ZERO)
        claimants = sorted(
            strategy_id
            for strategy_id, targets in intents.items()
            if Decimal(targets.get(instrument, ZERO)) != ZERO
        )

        if len(prior) == 1:
            owner, _ = prior[0]
            other_claimants = [sid for sid in claimants if sid != owner[0]]
            if broker_quantity != prior_total and other_claimants:
                unresolved.append(
                    {
                        "reason": "sticky_owner_conflict",
                        "instrument": instrument,
                        "broker_quantity": str(broker_quantity),
                        "previous_quantity": str(prior_total),
                        "previous_owner": owner[0],
                        "other_claimants": other_claimants,
                    }
                )
                continue
            assignments.append(
                ShadowAssignment(owner, instrument, broker_quantity, "sticky_previous_owner")
            )
            continue

        if len(prior) > 1:
            if broker_quantity != prior_total:
                unresolved.append(
                    {
                        "reason": "multi_owner_quantity_changed",
                        "instrument": instrument,
                        "broker_quantity": str(broker_quantity),
                        "previous_quantity": str(prior_total),
                        "owners": [owner[0] for owner, _ in prior],
                    }
                )
                continue
            for owner, quantity in sorted(prior):
                assignments.append(
                    ShadowAssignment(owner, instrument, quantity, "sticky_multi_owner")
                )
            continue

        if len(claimants) == 1:
            strategy_id = claimants[0]
            profile = profiles[strategy_id]
            assignments.append(
                ShadowAssignment(
                    (strategy_id, profile.book_id),
                    instrument,
                    broker_quantity,
                    "unique_current_intent",
                )
            )
        else:
            unresolved.append(
                {
                    "reason": "unclaimed" if not claimants else "ambiguous_new_symbol",
                    "instrument": instrument,
                    "broker_quantity": str(broker_quantity),
                    "claimants": claimants,
                }
            )

    # A prior symbol that is now flat is intentionally absent. That is an observed external exit,
    # not a reassignment. Surface missing strategy intents only as diagnostics because sticky
    # ownership is sufficient for already-known positions.
    missing_intents = sorted(set(profiles) - set(intents))
    if missing_intents:
        warnings.append({"reason": "missing_runtime_intent", "strategies": missing_intents})

    assigned_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for assignment in assignments:
        assigned_totals[assignment.instrument] += assignment.quantity
    for instrument in sorted(set(clean_broker) | set(assigned_totals)):
        broker_quantity = clean_broker.get(instrument, ZERO)
        assigned_quantity = assigned_totals.get(instrument, ZERO)
        if broker_quantity != assigned_quantity and not any(
            row.get("instrument") == instrument for row in unresolved
        ):
            unresolved.append(
                {
                    "reason": "aggregate_mismatch",
                    "instrument": instrument,
                    "broker_quantity": str(broker_quantity),
                    "assigned_quantity": str(assigned_quantity),
                }
            )

    return ShadowInferenceResult(tuple(assignments), tuple(unresolved), tuple(warnings))
