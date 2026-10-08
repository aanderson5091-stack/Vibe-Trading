import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _isolated_jev_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.jev-loop, so a PAUSED left on the real
    dashboard can never change a test result (and tests never touch it)."""
    from jevloop import loop, serve

    home = tmp_path / "jev-home"
    home.mkdir()
    monkeypatch.setattr(loop, "LOG_DIR", home)
    monkeypatch.setattr(loop, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(loop, "LATEST_FILE", home / "latest.json")
    monkeypatch.setattr(serve, "LOG_DIR", home)
    return home
