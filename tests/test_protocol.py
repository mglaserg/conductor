from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from conductor.ledger import ConductorLedger
from conductor.protocol.acceptance import SnapshotAcceptor
from conductor.protocol.models import (
    LINEAR_DATASCHEMA,
    LINEAR_EVENT_TYPE,
    LinearSnapshotData,
    LinearTarget,
    LinearTargetSnapshot,
)
from conductor.protocol.profile import StrategyProfile


def make_snapshot(
    *,
    event_id: str = "evt-1",
    revision: int = 1,
    targets: tuple[tuple[str, str], ...] = (("EQ.US.AAPL", "0.10"),),
    strategy_id: str = "ETSA",
    book_id: str = "main",
    now: datetime | None = None,
) -> LinearTargetSnapshot:
    now = now or datetime.now(timezone.utc)
    return LinearTargetSnapshot(
        id=event_id,
        source=f"urn:defacto:strategy:{strategy_id.lower()}",
        type=LINEAR_EVENT_TYPE,
        subject=f"{strategy_id}/{book_id}",
        time=now,
        dataschema=LINEAR_DATASCHEMA,
        data=LinearSnapshotData(
            strategy_id=strategy_id,
            book_id=book_id,
            revision=revision,
            as_of=now,
            targets=tuple(LinearTarget(instrument=i, weight=Decimal(w)) for i, w in targets),
        ),
    )


def test_protocol_forbids_unknown_fields() -> None:
    payload = make_snapshot().model_dump(mode="json")
    payload["data"]["mystery"] = 123
    with pytest.raises(ValidationError):
        LinearTargetSnapshot.model_validate(payload)


def test_acceptance_is_idempotent_and_detects_id_collision(tmp_path) -> None:
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    acceptor = SnapshotAcceptor(
        ledger,
        {"ETSA": StrategyProfile("ETSA", "equities", allowed_books=frozenset({"main"}))},
    )
    snapshot = make_snapshot()
    first = acceptor.accept(snapshot, now=snapshot.data.as_of)
    assert first.status == "accepted"

    duplicate = acceptor.accept(snapshot, now=snapshot.data.as_of)
    assert duplicate.status == "duplicate"
    assert duplicate.reason == "duplicate_event"

    collision = make_snapshot(event_id="evt-1", revision=2, targets=(("EQ.US.AAPL", "0.20"),))
    collision = collision.model_copy(
        update={
            "time": snapshot.time,
            "data": collision.data.model_copy(update={"as_of": snapshot.data.as_of}),
        }
    )
    rejected = acceptor.accept(collision, now=snapshot.data.as_of)
    assert rejected.status == "rejected"
    assert rejected.reason == "event_id_payload_collision"
    assert ledger.desired_books()[0]["revision"] == 1


def test_revision_rules_and_complete_empty_snapshot(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    acceptor = SnapshotAcceptor(ledger, {"ETSA": StrategyProfile("ETSA", "equities")})

    assert acceptor.accept(make_snapshot(revision=5, event_id="five", now=now), now=now).accepted

    same = acceptor.accept(make_snapshot(revision=5, event_id="five-b", now=now), now=now)
    assert (same.status, same.reason) == ("rejected", "revision_conflict")

    lower = acceptor.accept(make_snapshot(revision=4, event_id="four", now=now), now=now)
    assert (lower.status, lower.reason) == ("rejected", "stale_revision")

    flat = acceptor.accept(
        make_snapshot(revision=7, event_id="seven", targets=(), now=now), now=now
    )
    assert flat.accepted
    intents = acceptor.current_linear_intents(now=now)
    assert intents[0].revision == 7
    assert intents[0].targets == {}


def test_profile_staleness_is_enforced_on_accept_and_execution(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    profile = StrategyProfile("ETSA", "equities", max_age=timedelta(hours=1))
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    acceptor = SnapshotAcceptor(ledger, {"ETSA": profile})

    stale = make_snapshot(event_id="stale", now=now - timedelta(hours=2))
    decision = acceptor.accept(stale, now=now)
    assert (decision.status, decision.reason) == ("rejected", "snapshot_stale")
    assert ledger.desired_books() == []

    fresh = make_snapshot(event_id="fresh", now=now)
    assert acceptor.accept(fresh, now=now).accepted
    with pytest.raises(RuntimeError, match="snapshot_stale"):
        acceptor.current_linear_intents(now=now + timedelta(hours=2))


def test_independent_books_replace_independently(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    profile = StrategyProfile("FUTURESCOPE", "futures", allowed_books=frozenset({"gc", "es"}))
    acceptor = SnapshotAcceptor(ledger, {"FUTURESCOPE": profile})

    gc = make_snapshot(strategy_id="FUTURESCOPE", book_id="gc", event_id="gc1", now=now)
    es = make_snapshot(strategy_id="FUTURESCOPE", book_id="es", event_id="es1", now=now)
    assert acceptor.accept(gc, now=now).accepted
    assert acceptor.accept(es, now=now).accepted

    gc2 = make_snapshot(
        strategy_id="FUTURESCOPE",
        book_id="gc",
        event_id="gc2",
        revision=2,
        targets=(),
        now=now,
    )
    assert acceptor.accept(gc2, now=now).accepted
    books = {(b["strategy_id"], b["book_id"]): b["revision"] for b in ledger.desired_books()}
    assert books == {("FUTURESCOPE", "es"): 1, ("FUTURESCOPE", "gc"): 2}


def test_v02_virtual_ledger_migrates_to_main_book(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE virtual_targets (
            strategy_id TEXT NOT NULL,
            sleeve_id TEXT NOT NULL,
            instrument TEXT NOT NULL,
            target TEXT NOT NULL,
            notional TEXT NOT NULL DEFAULT '0',
            exposure_type TEXT NOT NULL,
            source_exposure_type TEXT NOT NULL DEFAULT 'quantity',
            lot_size TEXT NOT NULL DEFAULT '1',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (strategy_id, instrument)
        );
        CREATE TABLE virtual_positions (
            strategy_id TEXT NOT NULL,
            sleeve_id TEXT NOT NULL,
            instrument TEXT NOT NULL,
            quantity TEXT NOT NULL,
            notional TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (strategy_id, instrument)
        );
        INSERT INTO virtual_targets VALUES
            ('ETSA','equities','EQ.US.AAPL','10','2000','quantity','nav_weight','1','x');
        INSERT INTO virtual_positions VALUES
            ('ETSA','equities','EQ.US.AAPL','10','2000','x');
        """
    )
    conn.commit()
    conn.close()

    ledger = ConductorLedger(path)
    assert ledger.virtual_targets()[0].book_id == "main"
    assert ledger.virtual_positions()[0]["book_id"] == "main"


def test_book_cannot_change_protocol_family(tmp_path) -> None:
    from conductor.protocol.models import (
        STRUCTURE_DATASCHEMA,
        STRUCTURE_EVENT_TYPE,
        StructureLeg,
        StructureSnapshotData,
        StructureTarget,
        StructureTargetSnapshot,
    )

    now = datetime.now(timezone.utc)
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    profile = StrategyProfile(
        "MIXED",
        "test",
        allowed_event_types=frozenset({LINEAR_EVENT_TYPE, STRUCTURE_EVENT_TYPE}),
    )
    acceptor = SnapshotAcceptor(ledger, {"MIXED": profile})
    linear = make_snapshot(strategy_id="MIXED", event_id="linear-1", now=now)
    assert acceptor.accept(linear, now=now).accepted

    structure = StructureTargetSnapshot(
        id="structure-2",
        source="urn:defacto:strategy:mixed",
        type=STRUCTURE_EVENT_TYPE,
        subject="MIXED/main",
        time=now,
        dataschema=STRUCTURE_DATASCHEMA,
        data=StructureSnapshotData(
            strategy_id="MIXED",
            book_id="main",
            revision=2,
            as_of=now,
            targets=(
                StructureTarget(
                    structure_id="pair",
                    legs=(
                        StructureLeg(instrument="EQ.US.AAPL", ratio=Decimal("1")),
                        StructureLeg(instrument="EQ.US.MSFT", ratio=Decimal("-1")),
                    ),
                    target_risk_fraction=Decimal("0.1"),
                ),
            ),
        ),
    )
    decision = acceptor.accept(structure, now=now)
    assert (decision.status, decision.reason) == ("rejected", "book_type_conflict")
