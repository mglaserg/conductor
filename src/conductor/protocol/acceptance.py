from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from pydantic import ValidationError

from conductor.ledger import ConductorLedger, SnapshotDecision
from conductor.protocol.conversion import linear_snapshot_to_intent
from conductor.protocol.models import (
    LINEAR_EVENT_TYPE,
    TARGET_SNAPSHOT_ADAPTER,
    LinearTargetSnapshot,
    TargetSnapshot,
)
from conductor.protocol.profile import StrategyProfile


@dataclass(frozen=True, slots=True)
class ProfileValidation:
    ok: bool
    reason: str = ""


def canonical_snapshot_json(snapshot: TargetSnapshot) -> str:
    return json.dumps(
        snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(snapshot: TargetSnapshot) -> str:
    return hashlib.sha256(canonical_snapshot_json(snapshot).encode("utf-8")).hexdigest()


def validate_against_profile(
    snapshot: TargetSnapshot,
    profile: StrategyProfile,
    *,
    now: datetime | None = None,
) -> ProfileValidation:
    now = now or datetime.now(timezone.utc)
    data = snapshot.data
    if data.strategy_id != profile.strategy_id:
        return ProfileValidation(False, "strategy_profile_mismatch")
    if snapshot.source != profile.expected_source:
        return ProfileValidation(False, "source_not_authorized")
    if snapshot.type not in profile.allowed_event_types:
        return ProfileValidation(False, "event_type_not_authorized")
    if profile.allowed_books is not None and data.book_id not in profile.allowed_books:
        return ProfileValidation(False, "book_not_authorized")
    if data.as_of > now + profile.max_future_skew:
        return ProfileValidation(False, "as_of_too_far_in_future")

    profile_expiry = data.as_of + profile.max_age
    effective_expiry = min(profile_expiry, data.valid_until) if data.valid_until else profile_expiry
    if now > effective_expiry:
        return ProfileValidation(False, "snapshot_stale")
    return ProfileValidation(True, "accepted")


class SnapshotAcceptor:
    def __init__(
        self,
        ledger: ConductorLedger,
        profiles: Mapping[str, StrategyProfile],
    ) -> None:
        self.ledger = ledger
        self.profiles = dict(profiles)

    def accept(self, snapshot: TargetSnapshot, *, now: datetime | None = None) -> SnapshotDecision:
        profile = self.profiles.get(snapshot.data.strategy_id)
        if profile is None:
            validation = ProfileValidation(False, "unknown_strategy")
        else:
            validation = validate_against_profile(snapshot, profile, now=now)

        payload = canonical_snapshot_json(snapshot)
        return self.ledger.record_snapshot(
            source=snapshot.source,
            event_id=snapshot.id,
            payload_hash=payload_hash(snapshot),
            event_type=snapshot.type,
            strategy_id=snapshot.data.strategy_id,
            book_id=snapshot.data.book_id,
            revision=snapshot.data.revision,
            as_of=snapshot.data.as_of.isoformat(),
            valid_until=(
                snapshot.data.valid_until.isoformat() if snapshot.data.valid_until else None
            ),
            payload_json=payload,
            prevalidated=validation.ok,
            rejection_reason=validation.reason,
        )

    def current_linear_intents(self, *, now: datetime | None = None):
        intents = []
        for raw in self.ledger.desired_book_payloads(event_type=LINEAR_EVENT_TYPE):
            snapshot = TARGET_SNAPSHOT_ADAPTER.validate_json(raw)
            if not isinstance(snapshot, LinearTargetSnapshot):
                continue
            profile = self.profiles.get(snapshot.data.strategy_id)
            if profile is None:
                raise RuntimeError(f"missing profile for {snapshot.data.strategy_id}")
            validation = validate_against_profile(snapshot, profile, now=now)
            if not validation.ok:
                raise RuntimeError(
                    f"desired book {snapshot.data.strategy_id}/{snapshot.data.book_id} "
                    f"is not executable: {validation.reason}"
                )
            intents.append(linear_snapshot_to_intent(snapshot, profile))
        return intents


class InboxProcessor:
    """Consume local JSON files into durable Conductor desired state."""

    def __init__(self, root: str | Path, acceptor: SnapshotAcceptor) -> None:
        self.root = Path(root)
        self.acceptor = acceptor
        self.inbox = self.root / "inbox"
        self.accepted = self.root / "accepted"
        self.rejected = self.root / "rejected"
        self.duplicates = self.root / "duplicate"
        for directory in (self.inbox, self.accepted, self.rejected, self.duplicates):
            directory.mkdir(parents=True, exist_ok=True)

    def process_all(self, *, now: datetime | None = None) -> list[SnapshotDecision]:
        decisions: list[SnapshotDecision] = []
        for path in sorted(self.inbox.glob("*.json")):
            try:
                snapshot = TARGET_SNAPSHOT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                self.acceptor.ledger.append_event(
                    "snapshot_parse_rejected",
                    {"file": path.name, "error": str(exc)[:1000]},
                )
                self._move_with_result(path, self.rejected, "rejected", "schema_validation_failed")
                continue

            decision = self.acceptor.accept(snapshot, now=now)
            decisions.append(decision)
            destination = {
                "accepted": self.accepted,
                "duplicate": self.duplicates,
                "rejected": self.rejected,
            }[decision.status]
            self._move_with_result(path, destination, decision.status, decision.reason)
        return decisions

    @staticmethod
    def _move_with_result(path: Path, destination: Path, status: str, reason: str) -> None:
        target = destination / path.name
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            counter = 2
            while True:
                candidate = destination / f"{stem}.{counter}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                counter += 1
        path.replace(target)
        result_path = target.with_suffix(target.suffix + ".result.json")
        result_path.write_text(
            json.dumps({"status": status, "reason": reason}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
