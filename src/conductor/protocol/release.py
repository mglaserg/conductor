from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum


class StrategyReleaseState(StrEnum):
    """Admission state for one immutable strategy release controlled by Conductor."""

    RESEARCH = "research"
    VALIDATED = "validated"
    SHADOW = "shadow"
    LIVE = "live"
    KILLED = "killed"
    REVIEW = "review"


_ALLOWED_TRANSITIONS: dict[StrategyReleaseState, frozenset[StrategyReleaseState]] = {
    StrategyReleaseState.RESEARCH: frozenset(
        {StrategyReleaseState.VALIDATED, StrategyReleaseState.KILLED}
    ),
    StrategyReleaseState.VALIDATED: frozenset(
        {
            StrategyReleaseState.SHADOW,
            StrategyReleaseState.REVIEW,
            StrategyReleaseState.KILLED,
        }
    ),
    StrategyReleaseState.SHADOW: frozenset(
        {
            StrategyReleaseState.LIVE,
            StrategyReleaseState.REVIEW,
            StrategyReleaseState.KILLED,
        }
    ),
    StrategyReleaseState.LIVE: frozenset(
        {StrategyReleaseState.REVIEW, StrategyReleaseState.KILLED}
    ),
    # A reviewed release may re-enter only through validation. Resuming shadow/live directly would
    # skip the evidence gate that REVIEW is meant to force.
    StrategyReleaseState.REVIEW: frozenset(
        {StrategyReleaseState.VALIDATED, StrategyReleaseState.KILLED}
    ),
    StrategyReleaseState.KILLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StrategyRelease:
    """Immutable identity plus Conductor-owned admission state for a strategy version."""

    strategy_id: str
    version: str
    state: StrategyReleaseState = StrategyReleaseState.RESEARCH
    evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty")
        if not self.version.strip():
            raise ValueError("version must be non-empty")

    def allowed_transitions(self) -> tuple[StrategyReleaseState, ...]:
        return tuple(sorted(_ALLOWED_TRANSITIONS[self.state], key=lambda value: value.value))

    def can_transition_to(self, target: StrategyReleaseState | str) -> bool:
        target_state = StrategyReleaseState(target)
        return target_state == self.state or target_state in _ALLOWED_TRANSITIONS[self.state]

    def transitioned(self, target: StrategyReleaseState | str) -> "StrategyRelease":
        target_state = StrategyReleaseState(target)
        if target_state == self.state:
            return self
        if target_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(
                f"illegal strategy release transition {self.state.value} -> {target_state.value}"
            )
        return replace(self, state=target_state)

    def can_shadow(self) -> bool:
        return self.state in {
            StrategyReleaseState.VALIDATED,
            StrategyReleaseState.SHADOW,
            StrategyReleaseState.LIVE,
        }

    def can_trade_live(self) -> bool:
        return self.state == StrategyReleaseState.LIVE

    @classmethod
    def from_metadata(cls, strategy_id: str, metadata: object) -> "StrategyRelease":
        """Create a release identity from attached strategy metadata.

        Metadata seeds a release only once. After registration, the ledger owns admission state;
        configuration cannot silently promote or demote an existing strategy version.
        """
        evidence = getattr(metadata, "evidence", ())
        evidence_ids = tuple(
            f"{item.producer}:{item.artifact_type}:{item.location}"
            for item in evidence
        )
        return cls(
            strategy_id=strategy_id,
            version=getattr(metadata, "version", "0.0.0"),
            state=StrategyReleaseState(getattr(metadata, "release_state", "research")),
            evidence_ids=evidence_ids,
        )
