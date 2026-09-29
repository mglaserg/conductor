from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLOCKWORK_RESEARCH_SCHEMA = "clockwork.research.v1"
EDGELAB_VALIDATION_SCHEMA = "edgelab.validation.v1"
FACTORSTRIP_DECOMPOSITION_SCHEMA = "factorstrip.decomposition.v1"

_OOS_WINDOWS = {
    "oos",
    "out-of-sample",
    "out_of_sample",
    "holdout",
    "sealed-holdout",
    "sealed_holdout",
    "validation",
}


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"evidence field {key!r} must be a non-empty string")
    return value.strip()


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"evidence field {key!r} must be a boolean")
    return value


def _hypothesis_id(payload: dict[str, Any]) -> str:
    hypothesis = payload.get("hypothesis")
    if not isinstance(hypothesis, dict):
        raise ValueError("evidence field 'hypothesis' must be an object")
    value = hypothesis.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("evidence hypothesis.id must be a non-empty string")
    return value.strip()


def _expected_evidence_id(payload: dict[str, Any], *, producer: str, hypothesis_id: str) -> str:
    body = dict(payload)
    body.pop("evidence_id", None)
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f"{producer}:{hypothesis_id}:{digest}"


@dataclass(frozen=True, slots=True)
class PortableEvidence:
    """Validated, content-addressed evidence emitted by a specialist research system."""

    evidence_id: str
    schema_version: str
    producer: str
    artifact_type: str
    producer_version: str
    decision: str
    eligible_for_validation: bool
    eligible_for_promotion: bool
    location: str
    artifact_sha256: str
    payload: dict[str, Any]
    subject_strategy_id: str | None = None

    @property
    def promotion_eligible_validation(self) -> bool:
        return (
            self.artifact_type == "validation"
            and self.decision == "pass"
            and self.eligible_for_promotion
        )

    def summary(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "schema_version": self.schema_version,
            "producer": self.producer,
            "artifact_type": self.artifact_type,
            "producer_version": self.producer_version,
            "decision": self.decision,
            "eligible_for_validation": self.eligible_for_validation,
            "eligible_for_promotion": self.eligible_for_promotion,
            "location": self.location,
            "artifact_sha256": self.artifact_sha256,
        }


def load_portable_evidence(path: str | Path) -> PortableEvidence:
    """Load and independently verify a supported specialist evidence artifact."""
    artifact = Path(path)
    try:
        raw = artifact.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read evidence artifact {artifact}: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"evidence artifact is not valid UTF-8 JSON: {artifact}") from exc
    if not isinstance(payload, dict):
        raise ValueError("evidence artifact root must be a JSON object")

    evidence_id = _required_string(payload, "evidence_id")
    schema_version = _required_string(payload, "schema_version")
    producer = _required_string(payload, "producer")
    artifact_type = _required_string(payload, "artifact_type")
    producer_version = _required_string(payload, "producer_version")
    decision = _required_string(payload, "decision")
    hypothesis_id = _hypothesis_id(payload)

    expected_id = _expected_evidence_id(
        payload,
        producer=producer,
        hypothesis_id=hypothesis_id,
    )
    if evidence_id != expected_id:
        raise ValueError(
            f"evidence_id content hash mismatch: expected {expected_id}, got {evidence_id}"
        )

    subject_strategy_id: str | None = None
    if schema_version == CLOCKWORK_RESEARCH_SCHEMA:
        if producer != "clockwork" or artifact_type != "research":
            raise ValueError(
                "clockwork.research.v1 must declare producer='clockwork' and "
                "artifact_type='research'"
            )
        eligible_for_validation = _required_bool(payload, "eligible_for_validation")
        eligible_for_promotion = _required_bool(payload, "eligible_for_promotion")
        if decision != "pass" or not eligible_for_validation or eligible_for_promotion:
            raise ValueError(
                "Clockwork research evidence must be pass, eligible for validation, and not "
                "eligible for promotion"
            )
        candidate = payload.get("candidate")
        calibration = payload.get("calibration")
        if not isinstance(candidate, dict) or not str(candidate.get("config_hash") or "").strip():
            raise ValueError("Clockwork research evidence requires candidate.config_hash")
        if not isinstance(calibration, dict) or calibration.get("decision") != "PASS_CALIBRATION":
            raise ValueError("Clockwork research evidence requires PASS_CALIBRATION")
    elif schema_version == EDGELAB_VALIDATION_SCHEMA:
        if producer != "edgelab" or artifact_type != "validation":
            raise ValueError(
                "edgelab.validation.v1 must declare producer='edgelab' and "
                "artifact_type='validation'"
            )
        eligible_for_validation = False
        eligible_for_promotion = _required_bool(payload, "eligible_for_promotion")
        if decision not in {"pass", "fail", "diagnostic"}:
            raise ValueError("EdgeLab validation decision must be pass, fail, or diagnostic")
        if eligible_for_promotion != (decision == "pass"):
            raise ValueError(
                "EdgeLab eligible_for_promotion must be true exactly when decision='pass'"
            )
        evaluation = payload.get("evaluation")
        if not isinstance(evaluation, dict):
            raise ValueError("EdgeLab validation evidence requires an evaluation object")
        window = str(evaluation.get("window") or "").strip().lower().replace(" ", "-")
        survives = evaluation.get("survives")
        if decision == "pass":
            if window not in _OOS_WINDOWS or survives is not True:
                raise ValueError(
                    "promotion-eligible EdgeLab evidence must be an explicit OOS/holdout result "
                    "with survives=true"
                )
            if not str(evaluation.get("trial_id") or "").strip():
                raise ValueError("promotion-eligible EdgeLab evidence requires evaluation.trial_id")
        subject_strategy_id = None
    elif schema_version == FACTORSTRIP_DECOMPOSITION_SCHEMA:
        if producer != "factorstrip" or artifact_type != "decomposition":
            raise ValueError(
                "factorstrip.decomposition.v1 must declare producer='factorstrip' and "
                "artifact_type='decomposition'"
            )
        eligible_for_validation = _required_bool(payload, "eligible_for_validation")
        eligible_for_promotion = _required_bool(payload, "eligible_for_promotion")
        if decision != "descriptive":
            raise ValueError("FactorStrip decomposition decision must be 'descriptive'")
        if eligible_for_validation or eligible_for_promotion:
            raise ValueError(
                "FactorStrip decomposition evidence must not be eligible for validation "
                "or promotion"
            )

        decomposition = payload.get("decomposition")
        if not isinstance(decomposition, dict):
            raise ValueError("FactorStrip decomposition evidence requires a decomposition object")
        subject_strategy_id = str(decomposition.get("strategy_id") or "").strip()
        if not subject_strategy_id:
            raise ValueError("FactorStrip decomposition requires decomposition.strategy_id")
        if subject_strategy_id.casefold() != hypothesis_id.casefold():
            raise ValueError(
                "FactorStrip decomposition.strategy_id must match hypothesis.id"
            )
        observations = decomposition.get("observations")
        if not isinstance(observations, int) or isinstance(observations, bool) or observations <= 0:
            raise ValueError("FactorStrip decomposition.observations must be a positive integer")
        factor_betas = decomposition.get("factor_betas")
        if not isinstance(factor_betas, dict) or not factor_betas:
            raise ValueError("FactorStrip decomposition.factor_betas must be a non-empty object")

        methodology = payload.get("methodology")
        if not isinstance(methodology, dict):
            raise ValueError("FactorStrip decomposition evidence requires a methodology object")
        if methodology.get("model") != "time_series_ols_with_intercept":
            raise ValueError(
                "FactorStrip decomposition methodology.model must be "
                "'time_series_ols_with_intercept'"
            )
        interpretation = methodology.get("interpretation")
        if not isinstance(interpretation, str) or not interpretation.strip():
            raise ValueError("FactorStrip decomposition methodology.interpretation is required")
    else:
        raise ValueError(f"unsupported evidence schema: {schema_version}")

    return PortableEvidence(
        evidence_id=evidence_id,
        schema_version=schema_version,
        producer=producer,
        artifact_type=artifact_type,
        producer_version=producer_version,
        decision=decision,
        eligible_for_validation=eligible_for_validation,
        eligible_for_promotion=eligible_for_promotion,
        location=str(artifact.resolve()),
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
        payload=payload,
        subject_strategy_id=subject_strategy_id,
    )
