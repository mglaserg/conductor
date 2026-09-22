"""Harmless deterministic target producer for an offline-paper CLI smoke."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    output = Path(os.environ["CONDUCTOR_OUTPUT"])
    output.write_text(
        json.dumps(
            {"targets": {"EQ.US.PAPER": "0.10"}},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
