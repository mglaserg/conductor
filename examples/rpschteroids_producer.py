"""RPSchteroids uses the same LinearTargetSnapshot contract as ETSA."""
from datetime import datetime, timezone
from pathlib import Path

from conductor.protocol.sdk import FilesystemConductorClient

client = FilesystemConductorClient(Path("runtime") / "inbox")
client.submit_linear(
    strategy_id="RPSchteroids",
    book_id="main",
    revision=17,
    as_of=datetime.now(timezone.utc),
    targets={
        "EQ.US.AAPL": "0.20",
        "EQ.US.NVDA": "0.30",
    },
)
