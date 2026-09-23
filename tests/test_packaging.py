from __future__ import annotations

import tomllib
from pathlib import Path


def test_nautilus_is_a_required_runtime_dependency() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]

    dependencies = project["dependencies"]
    assert any(dep.startswith("nautilus_trader==") for dep in dependencies)
    assert any(dep.startswith("nautilus_ibapi==") for dep in dependencies)

    optional = project.get("optional-dependencies", {})
    assert "nautilus" not in optional
