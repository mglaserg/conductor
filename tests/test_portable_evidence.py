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


def _factorstrip(
    path: Path,
    *,
    strategy_id: str = "VixSnap",
    decision: str = "descriptive",
    eligible_for_validation: bool = False,
    eligible_for_promotion: bool = False,
) -> Path:
    body = {
        "schema_version": "factorstrip.decomposition.v1",
        "producer": "factorstrip",
        "artifact_type": "decomposition",
        "producer_version": "0.5.0",
        "decision": decision,
        "eligible_for_validation": eligible_for_validation,
        "eligible_for_promotion": eligible_for_promotion,
        "hypothesis": {
            "id": strategy_id,
            "family": "factor_attribution",
            "name": strategy_id,
        },
        "decomposition": {
            "strategy_id": strategy_id,
            "observations": 252,
            "sample_start": "2025-01-02 00:00:00",
            "sample_end": "2025-12-31 00:00:00",
            "periods_per_year": 252,
            "factor_betas": {"MKT": 0.8, "VALUE": -0.2},
            "factor_contributions_annualized": {"MKT": 0.04, "VALUE": -0.01},
            "r2": 0.61,
            "time_series_intercept_per_period": 0.0001,
            "time_series_intercept_annualized": 0.0252,
            "strategy_mean_annualized": 0.08,
            "factor_spanned_mean_annualized": 0.0548,
            "strategy_vol_annualized": 0.14,
            "residual_vol_annualized": 0.09,
            "max_abs_residual_factor_corr": 0.0,
            "condition_number": 2.1,
            "warnings": [
                "descriptive_factor_attribution_not_validation",
                "time_series_intercept_is_not_cross_sectional_orthogonal_alpha",
            ],
        },
        "methodology": {
            "model": "time_series_ols_with_intercept",
            "purpose": "describe_factor_span_and_residual_return_component",
            "interpretation": (
                "Descriptive only. The time-series intercept is not equivalent to "
                "cross-sectional orthogonal alpha and is not a promotion decision."
            ),
        },
        "inputs": {"strategy_returns_sha256": "abc", "factor_returns_sha256": "def"},
    }
    return _write_artifact(path, body, producer="factorstrip", hypothesis_id=strategy_id)


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


def test_factorstrip_decomposition_is_verified_descriptive_and_never_admission_proof(
    tmp_path,
) -> None:
    evidence = load_portable_evidence(_factorstrip(tmp_path / "decomposition.json"))

    assert evidence.schema_version == "factorstrip.decomposition.v1"
    assert evidence.subject_strategy_id == "VixSnap"
    assert evidence.decision == "descriptive"
    assert evidence.eligible_for_validation is False
    assert evidence.eligible_for_promotion is False
    assert evidence.promotion_eligible_validation is False

    ledger = ConductorLedger(tmp_path / "conductor.sqlite")
    ledger.ensure_strategy_release(StrategyRelease("VixSnap", "1.0.0"))
    assert ledger.attach_strategy_release_evidence("VixSnap", "1.0.0", evidence) is True

    decompositions = ledger.strategy_release_factor_decompositions("VixSnap", "1.0.0")
    assert len(decompositions) == 1
    assert decompositions[0]["evidence_id"] == evidence.evidence_id
    assert decompositions[0]["decomposition"]["factor_betas"] == {
        "MKT": 0.8,
        "VALUE": -0.2,
    }
    assert decompositions[0]["methodology"]["model"] == "time_series_ols_with_intercept"

    with pytest.raises(ValueError, match="requires promotion-eligible portable validation"):
        ledger.transition_strategy_release(
            "VixSnap", "1.0.0", "validated", reason="decomposition is descriptive only"
        )


def test_factorstrip_decomposition_refuses_promotional_flags_and_wrong_release(tmp_path) -> None:
    promotional = _factorstrip(
        tmp_path / "promotional.json", eligible_for_promotion=True
    )
    with pytest.raises(ValueError, match="must not be eligible for validation or promotion"):
        load_portable_evidence(promotional)

    evidence = load_portable_evidence(
        _factorstrip(tmp_path / "vixsnap.json", strategy_id="VixSnap")
    )
    ledger = ConductorLedger(tmp_path / "conductor.sqlite")
    ledger.ensure_strategy_release(StrategyRelease("ETSA", "1.0.0"))
    with pytest.raises(ValueError, match="does not match strategy release"):
        ledger.attach_strategy_release_evidence("ETSA", "1.0.0", evidence)
