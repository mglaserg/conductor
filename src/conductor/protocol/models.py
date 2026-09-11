from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.type_adapter import TypeAdapter

LINEAR_EVENT_TYPE = "com.defacto.conductor.linear-target-snapshot.v1"
STRUCTURE_EVENT_TYPE = "com.defacto.conductor.structure-target-snapshot.v1"
LINEAR_DATASCHEMA = "urn:defacto:conductor:schema:linear-target-snapshot:v1"
STRUCTURE_DATASCHEMA = "urn:defacto:conductor:schema:structure-target-snapshot:v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LinearTarget(StrictModel):
    instrument: str = Field(min_length=1)
    weight: Decimal

    @field_validator("weight")
    @classmethod
    def weight_must_be_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("weight must be finite")
        return value

    @field_validator("instrument")
    @classmethod
    def instrument_must_be_canonicalish(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("instrument must be a non-empty whitespace-free Conductor ID")
        return value


class StructureLeg(StrictModel):
    instrument: str = Field(min_length=1)
    ratio: Decimal

    @field_validator("instrument")
    @classmethod
    def instrument_must_be_canonicalish(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("instrument must be a non-empty whitespace-free Conductor ID")
        return value

    @field_validator("ratio")
    @classmethod
    def ratio_cannot_be_zero(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("structure leg ratio must be finite")
        if value == 0:
            raise ValueError("structure leg ratio cannot be zero")
        return value


class StructureTarget(StrictModel):
    structure_id: str = Field(min_length=1)
    legs: tuple[StructureLeg, ...] = Field(min_length=2)
    target_risk_fraction: Decimal = Field(gt=0)

    @field_validator("target_risk_fraction")
    @classmethod
    def risk_fraction_must_be_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("target_risk_fraction must be finite")
        return value

    @model_validator(mode="after")
    def instruments_must_be_unique(self) -> "StructureTarget":
        instruments = [leg.instrument for leg in self.legs]
        if len(instruments) != len(set(instruments)):
            raise ValueError("a structure cannot repeat the same instrument")
        return self


class SnapshotDataBase(StrictModel):
    strategy_id: str = Field(min_length=1)
    book_id: str = Field(default="main", min_length=1)
    revision: int = Field(ge=1)
    as_of: datetime
    valid_until: datetime | None = None

    @field_validator("as_of", "valid_until")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validity_window_must_be_ordered(self) -> "SnapshotDataBase":
        if self.valid_until is not None and self.valid_until < self.as_of:
            raise ValueError("valid_until cannot be before as_of")
        return self


class LinearSnapshotData(SnapshotDataBase):
    weight_basis: Literal["strategy_budget"] = "strategy_budget"
    targets: tuple[LinearTarget, ...]

    @model_validator(mode="after")
    def instruments_must_be_unique(self) -> "LinearSnapshotData":
        instruments = [target.instrument for target in self.targets]
        if len(instruments) != len(set(instruments)):
            raise ValueError("linear snapshot cannot repeat an instrument")
        return self


class StructureSnapshotData(SnapshotDataBase):
    sizing_basis: Literal["strategy_budget_risk"] = "strategy_budget_risk"
    targets: tuple[StructureTarget, ...]

    @model_validator(mode="after")
    def structures_must_be_unique(self) -> "StructureSnapshotData":
        structure_ids = [target.structure_id for target in self.targets]
        if len(structure_ids) != len(set(structure_ids)):
            raise ValueError("structure snapshot cannot repeat a structure_id")
        return self


class LinearTargetSnapshot(StrictModel):
    specversion: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    type: Literal[LINEAR_EVENT_TYPE] = LINEAR_EVENT_TYPE
    subject: str = Field(min_length=1)
    time: datetime
    dataschema: Literal[LINEAR_DATASCHEMA] = LINEAR_DATASCHEMA
    data: LinearSnapshotData

    @field_validator("time")
    @classmethod
    def time_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def subject_matches_book(self) -> "LinearTargetSnapshot":
        expected = f"{self.data.strategy_id}/{self.data.book_id}"
        if self.subject != expected:
            raise ValueError(f"subject must be {expected!r}")
        return self


class StructureTargetSnapshot(StrictModel):
    specversion: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    type: Literal[STRUCTURE_EVENT_TYPE] = STRUCTURE_EVENT_TYPE
    subject: str = Field(min_length=1)
    time: datetime
    dataschema: Literal[STRUCTURE_DATASCHEMA] = STRUCTURE_DATASCHEMA
    data: StructureSnapshotData

    @field_validator("time")
    @classmethod
    def time_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def subject_matches_book(self) -> "StructureTargetSnapshot":
        expected = f"{self.data.strategy_id}/{self.data.book_id}"
        if self.subject != expected:
            raise ValueError(f"subject must be {expected!r}")
        return self


TargetSnapshot = Annotated[
    LinearTargetSnapshot | StructureTargetSnapshot,
    Field(discriminator="type"),
]
TARGET_SNAPSHOT_ADAPTER = TypeAdapter(TargetSnapshot)
