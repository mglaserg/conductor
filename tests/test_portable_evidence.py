from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conductor.ledger import ConductorLedger
from conductor.protocol.evidence import load_portable_evidence
from conductor.protocol.release import StrategyRelease


def _write_artifact(path: Path, body: dict, *, producer: str, hypothesis_id: str) -> Path:
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    evidence_id = f"{producer}:{hypothesis_id}:{hashlib.sha256(canonical).hexdigest()[:24]}"
    path.write_text(
        json.dumps({"evidence_id": evidence_id, **body}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _clockwork(path: Path) -> Path:
    body = {
        "schema_version": "clockwork.research.v1",
        "producer": "clockwork",
        "artifact_type": "research",
        "producer_version": "0.6.0",
        "decision": "pass",
        "eligible_for_validation": True,
        "eligible_for_promotion": False,
        "hypothesis": {"id": "vixsnap", "family": "dislocation_stress"},
        "candidate": {"config_hash": "abc123"},
        "calibration": {"decision": "PASS_CALIBRATION"},
    }
    return _write_artifact(path, body, producer="clockwork", hypothesis_id="vixsnap")


def _edgelab(path: Path, *, trial_id: str = "trial-1") -> Path:
    body = {
        "schema_version": "edgelab.validation.v1",
        "producer": "edgelab",
        "artifact_type": "validation",
        "producer_version": "0.3.0",
        "decision": "pass",
        "eligible_for_promotion": True,
        "hypothesis": {"id": "vixsnap", "family": "dislocation_stress"},
        "evaluation": {"trial_id": trial_id, "window": "sealed-holdout", "survives": True},
    }
    return _write_artifact(path, body, producer="edgelab", hypothesis_id="vixsnap")


def test_clockwork_research_artifact_is_verified_and_not_promotion_evidence(tmp_path) -> None:
    evidence = load_portable_evidence(_clockwork(tmp_path / "research.json"))

    assert evidence.schema_version == "clockwork.research.v1"
    assert evidence.eligible_for_validation is True
    assert evidence.eligible_for_promotion is False
    assert evidence.promotion_eligible_validation is False
    assert evidence.artifact_sha256


def test_portable_evidence_rejects_content_tampering(tmp_path) -> None:
    path = _edgelab(tmp_path / "validation.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evaluation"]["survives"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="evidence_id content hash mismatch"):
        load_portable_evidence(path)


def test_release_evidence_is_append_only_idempotent_and_gates_validation(tmp_path) -> None:
    ledger = ConductorLedger(tmp_path / "conductor.sqlite")
    ledger.ensure_strategy_release(StrategyRelease("VixSnap", "1.0.0"))

    research = load_portable_evidence(_clockwork(tmp_path / "research.json"))
    assert ledger.attach_strategy_release_evidence("VixSnap", "1.0.0", research) is True
    assert ledger.attach_strategy_release_evidence("VixSnap", "1.0.0", research) is False

    release = ledger.strategy_release("VixSnap", "1.0.0")
    assert release is not None
    assert release.evidence_ids == (research.evidence_id,)
    with pytest.raises(ValueError, match="requires promotion-eligible portable validation"):
        ledger.transition_strategy_release(
            "VixSnap", "1.0.0", "validated", reason="research alone is insufficient"
        )

    validation = load_portable_evidence(_edgelab(tmp_path / "validation.json"))
    assert ledger.attach_strategy_release_evidence("VixSnap", "1.0.0", validation) is True
    validated = ledger.transition_strategy_release(
        "VixSnap", "1.0.0", "validated", reason="sealed holdout passed"
    )
    assert validated.state.value == "validated"
    assert validated.evidence_ids == (research.evidence_id, validation.evidence_id)

    records = ledger.strategy_release_evidence("VixSnap", "1.0.0")
    assert [row["artifact_type"] for row in records] == ["research", "validation"]
    assert records[-1]["eligible_for_promotion"] is True
    events = [
        row for row in ledger.events() if row["event_type"] == "strategy.release_evidence_ingested"
    ]
    assert len(events) == 2
