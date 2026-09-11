"""FutureScope structured intent example. V0.3 validates/stores; sizing is later."""
from datetime import datetime, timezone
from pathlib import Path

from conductor.protocol.sdk import FilesystemConductorClient

client = FilesystemConductorClient(Path("runtime") / "inbox")
client.submit_structure(
    strategy_id="FUTURESCOPE",
    book_id="gc_curve",
    revision=18,
    as_of=datetime.now(timezone.utc),
    structures=[
        {
            "structure_id": "gc-z26-g27",
            "legs": [
                ("FUT.COMEX.GC.202612", "1"),
                ("FUT.COMEX.GC.202702", "-1"),
            ],
            "target_risk_fraction": "0.25",
        }
    ],
)
