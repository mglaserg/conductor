"""Example final step in an ETSA job: publish desired weights, never broker orders."""
from datetime import datetime, timezone
from pathlib import Path

from conductor.protocol.sdk import FilesystemConductorClient

client = FilesystemConductorClient(Path("runtime") / "inbox")
client.submit_linear(
    strategy_id="ETSA",
    book_id="main",
    revision=42,
    as_of=datetime.now(timezone.utc),
    targets={
        "EQ.US.AAPL": "0.08",
        "EQ.US.MSFT": "-0.06",
        "EQ.US.NVDA": "0.04",
    },
)
