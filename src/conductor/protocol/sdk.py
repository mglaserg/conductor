from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from conductor.protocol.models import (
    LINEAR_DATASCHEMA,
    LINEAR_EVENT_TYPE,
    STRUCTURE_DATASCHEMA,
    STRUCTURE_EVENT_TYPE,
    LinearSnapshotData,
    LinearTarget,
    LinearTargetSnapshot,
    StructureLeg,
    StructureSnapshotData,
    StructureTarget,
    StructureTargetSnapshot,
    TargetSnapshot,
)

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def _event_filename(snapshot: TargetSnapshot) -> str:
    if _SAFE_FILENAME.fullmatch(snapshot.id):
        return f"{snapshot.id}.json"
    digest = hashlib.sha256(f"{snapshot.source}\0{snapshot.id}".encode()).hexdigest()
    return f"event-{digest}.json"


class FilesystemConductorClient:
    """Tiny producer SDK: atomically publish complete snapshots into a local inbox."""

    def __init__(self, inbox: str | Path) -> None:
        self.inbox = Path(inbox)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def submit(self, snapshot: TargetSnapshot) -> Path:
        final_path = self.inbox / _event_filename(snapshot)
        temp_path = self.inbox / f".{uuid4().hex}.tmp"
        payload = snapshot.model_dump_json(indent=2)
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            # Same-directory hard-link creation is atomic and refuses to overwrite
            # an existing event. NTFS and normal Linux filesystems support this.
            os.link(temp_path, final_path)
        except FileExistsError:
            existing = final_path.read_text(encoding="utf-8")
            temp_path.unlink(missing_ok=True)
            if existing == payload:
                return final_path
            raise ValueError(
                f"event file already exists with different payload: {final_path.name}"
            ) from None
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(
                "Conductor atomic inbox publishing requires filesystem hard-link support"
            ) from exc
        finally:
            temp_path.unlink(missing_ok=True)
        return final_path

    def submit_linear(
        self,
        *,
        strategy_id: str,
        book_id: str,
        revision: int,
        targets: dict[str, Decimal | str | float | int],
        as_of: datetime | None = None,
        valid_until: datetime | None = None,
        event_id: str | None = None,
        source: str | None = None,
    ) -> Path:
        as_of = as_of or datetime.now(timezone.utc)
        snapshot = LinearTargetSnapshot(
            id=event_id or uuid4().hex,
            source=source or f"urn:defacto:strategy:{strategy_id.lower()}",
            type=LINEAR_EVENT_TYPE,
            subject=f"{strategy_id}/{book_id}",
            time=datetime.now(timezone.utc),
            dataschema=LINEAR_DATASCHEMA,
            data=LinearSnapshotData(
                strategy_id=strategy_id,
                book_id=book_id,
                revision=revision,
                as_of=as_of,
                valid_until=valid_until,
                targets=tuple(
                    LinearTarget(instrument=instrument, weight=Decimal(str(weight)))
                    for instrument, weight in targets.items()
                ),
            ),
        )
        return self.submit(snapshot)

    def submit_structure(
        self,
        *,
        strategy_id: str,
        book_id: str,
        revision: int,
        structures: Iterable[dict],
        as_of: datetime | None = None,
        valid_until: datetime | None = None,
        event_id: str | None = None,
        source: str | None = None,
    ) -> Path:
        """Publish complete desired structures.

        Each structure dict contains ``structure_id``, ``target_risk_fraction`` and
        ``legs`` as ``[(instrument, ratio), ...]``. V0.3 validates and stores this
        protocol but deliberately does not resolve structure sizing into executable
        quantities yet.
        """
        as_of = as_of or datetime.now(timezone.utc)
        targets = []
        for item in structures:
            targets.append(
                StructureTarget(
                    structure_id=item["structure_id"],
                    target_risk_fraction=Decimal(str(item["target_risk_fraction"])),
                    legs=tuple(
                        StructureLeg(instrument=instrument, ratio=Decimal(str(ratio)))
                        for instrument, ratio in item["legs"]
                    ),
                )
            )
        snapshot = StructureTargetSnapshot(
            id=event_id or uuid4().hex,
            source=source or f"urn:defacto:strategy:{strategy_id.lower()}",
            type=STRUCTURE_EVENT_TYPE,
            subject=f"{strategy_id}/{book_id}",
            time=datetime.now(timezone.utc),
            dataschema=STRUCTURE_DATASCHEMA,
            data=StructureSnapshotData(
                strategy_id=strategy_id,
                book_id=book_id,
                revision=revision,
                as_of=as_of,
                valid_until=valid_until,
                targets=tuple(targets),
            ),
        )
        return self.submit(snapshot)


def load_snapshot(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
