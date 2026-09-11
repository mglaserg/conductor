from __future__ import annotations

from decimal import Decimal

from conductor.domain.models import ExposureType, StrategyIntent
from conductor.protocol.models import LinearTargetSnapshot
from conductor.protocol.profile import StrategyProfile


def linear_snapshot_to_intent(
    snapshot: LinearTargetSnapshot,
    profile: StrategyProfile,
) -> StrategyIntent:
    if snapshot.data.strategy_id != profile.strategy_id:
        raise ValueError("snapshot strategy_id does not match profile")
    return StrategyIntent(
        strategy_id=snapshot.data.strategy_id,
        book_id=snapshot.data.book_id,
        targets={target.instrument: Decimal(target.weight) for target in snapshot.data.targets},
        exposure_type=ExposureType.NAV_WEIGHT,
        sleeve_id=profile.sleeve_id,
        as_of=snapshot.data.as_of,
        revision=snapshot.data.revision,
        schema_version="linear-target-snapshot.v1",
        intent_id=snapshot.id,
        metadata={"source": snapshot.source, "event_type": snapshot.type},
    )
