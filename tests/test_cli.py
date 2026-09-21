from __future__ import annotations

import io
import sys

from conductor.cli import main


def test_demo_output_is_compatible_with_default_windows_cp1252_console(monkeypatch) -> None:
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", console)

    main()

    console.flush()
    output = raw.getvalue().decode("cp1252")
    assert "ETSA REVISION 2 - MSFT ABSENT => ZERO" in output
    assert "Nothing here touches a live account." in output
