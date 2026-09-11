from __future__ import annotations

import json
from pathlib import Path

from conductor.protocol.models import LinearTargetSnapshot, StructureTargetSnapshot


def write_json_schemas(directory: str | Path) -> tuple[Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    linear = directory / "linear-target-snapshot-v1.json"
    structure = directory / "structure-target-snapshot-v1.json"
    linear.write_text(
        json.dumps(LinearTargetSnapshot.model_json_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    structure.write_text(
        json.dumps(StructureTargetSnapshot.model_json_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return linear, structure
