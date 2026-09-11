from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from conductor.ledger import ConductorLedger
from conductor.protocol.acceptance import InboxProcessor, SnapshotAcceptor
from conductor.protocol.profile import StrategyProfile
from conductor.protocol.sdk import FilesystemConductorClient


def test_filesystem_sdk_and_inbox_processor(tmp_path) -> None:
    root = tmp_path / "transport"
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    acceptor = SnapshotAcceptor(ledger, {"ETSA": StrategyProfile("ETSA", "equities")})
    processor = InboxProcessor(root, acceptor)
    client = FilesystemConductorClient(processor.inbox)
    now = datetime.now(timezone.utc)

    submitted = client.submit_linear(
        strategy_id="ETSA",
        book_id="main",
        revision=1,
        targets={"EQ.US.AAPL": Decimal("0.08")},
        as_of=now,
        event_id="etsa-1",
    )
    assert submitted.exists()
    decisions = processor.process_all(now=now)
    assert decisions[0].accepted
    assert not submitted.exists()
    assert (processor.accepted / "etsa-1.json").exists()
    intents = acceptor.current_linear_intents(now=now)
    assert intents[0].targets == {"EQ.US.AAPL": Decimal("0.08")}


def test_structure_snapshot_is_validated_and_persisted_but_not_linearized(tmp_path) -> None:
    from conductor.protocol.models import STRUCTURE_EVENT_TYPE

    root = tmp_path / "transport"
    ledger = ConductorLedger(tmp_path / "state.sqlite")
    profile = StrategyProfile(
        "FUTURESCOPE",
        "futures",
        allowed_event_types=frozenset({STRUCTURE_EVENT_TYPE}),
        allowed_books=frozenset({"gc_curve"}),
    )
    acceptor = SnapshotAcceptor(ledger, {"FUTURESCOPE": profile})
    processor = InboxProcessor(root, acceptor)
    client = FilesystemConductorClient(processor.inbox)
    now = datetime.now(timezone.utc)

    client.submit_structure(
        strategy_id="FUTURESCOPE",
        book_id="gc_curve",
        revision=1,
        event_id="gc-1",
        as_of=now,
        structures=[
            {
                "structure_id": "gc-z26-g27",
                "legs": [("FUT.COMEX.GC.202612", "1"), ("FUT.COMEX.GC.202702", "-1")],
                "target_risk_fraction": "0.25",
            }
        ],
    )
    decisions = processor.process_all(now=now)
    assert decisions[0].accepted
    assert ledger.desired_books()[0]["event_type"] == STRUCTURE_EVENT_TYPE
    assert acceptor.current_linear_intents(now=now) == []


def test_sdk_never_overwrites_unprocessed_event_id(tmp_path) -> None:
    from conductor.protocol.models import (
        LINEAR_DATASCHEMA,
        LINEAR_EVENT_TYPE,
        LinearSnapshotData,
        LinearTarget,
        LinearTargetSnapshot,
    )

    client = FilesystemConductorClient(tmp_path / "inbox")
    now = datetime.now(timezone.utc)
    first = LinearTargetSnapshot(
        id="same-id",
        source="urn:defacto:strategy:etsa",
        type=LINEAR_EVENT_TYPE,
        subject="ETSA/main",
        time=now,
        dataschema=LINEAR_DATASCHEMA,
        data=LinearSnapshotData(
            strategy_id="ETSA",
            book_id="main",
            revision=1,
            as_of=now,
            targets=(LinearTarget(instrument="EQ.US.AAPL", weight=Decimal("0.1")),),
        ),
    )
    path = client.submit(first)
    assert client.submit(first) == path

    changed = first.model_copy(
        update={
            "data": first.data.model_copy(
                update={
                    "targets": (
                        LinearTarget(instrument="EQ.US.AAPL", weight=Decimal("0.2")),
                    )
                }
            )
        }
    )
    import pytest

    with pytest.raises(ValueError, match="different payload"):
        client.submit(changed)
    assert "0.1" in path.read_text(encoding="utf-8")


def test_unsafe_event_id_is_not_used_as_a_path(tmp_path) -> None:
    client = FilesystemConductorClient(tmp_path / "inbox")
    now = datetime.now(timezone.utc)
    path = client.submit_linear(
        strategy_id="ETSA",
        book_id="main",
        revision=1,
        targets={"EQ.US.AAPL": "0.1"},
        as_of=now,
        event_id="../../escape",
    )
    assert path.parent == tmp_path / "inbox"
    assert path.name.startswith("event-")
