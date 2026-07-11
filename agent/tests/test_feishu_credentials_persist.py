"""Unit tests for persisting Feishu login credentials to the VT agent config."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import src.config.paths as config_paths
from src.channels.feishu import FeishuConfig, _persist_feishu_credentials


@pytest.fixture()
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the VT config discovery at an isolated runtime root."""
    root = tmp_path / ".vibe-trading"
    monkeypatch.setattr(config_paths, "get_runtime_root", lambda config_path=None: root)
    return root


def test_persist_writes_credentials_to_new_config(runtime_root: Path) -> None:
    cfg = FeishuConfig(app_id="cli_abc", app_secret="secret123", domain="lark", enabled=True)

    path = _persist_feishu_credentials(cfg)

    assert path == runtime_root / "agent.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["channels"]["feishu"] == {
        "app_id": "cli_abc",
        "app_secret": "secret123",
        "domain": "lark",
        "enabled": True,
    }
    # The reloaded section round-trips through the adapter's config model.
    reloaded = FeishuConfig.model_validate(data["channels"]["feishu"])
    assert reloaded.app_id == "cli_abc"
    assert reloaded.enabled is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode not enforced on Windows")
def test_persist_uses_owner_only_permissions(runtime_root: Path) -> None:
    cfg = FeishuConfig(app_id="cli_abc", app_secret="secret123", domain="feishu", enabled=True)

    path = _persist_feishu_credentials(cfg)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_persist_merges_without_clobbering_other_sections(runtime_root: Path) -> None:
    runtime_root.mkdir(parents=True)
    existing = {
        "mcp_servers": {"robinhood": {"type": "streamableHttp"}},
        "channels": {
            "send_progress": False,
            "telegram": {"enabled": True, "token": "keep-me"},
            "feishu": {"app_id": "old", "app_secret": "old", "domain": "feishu", "enabled": False},
        },
    }
    config_path = runtime_root / "agent.json"
    config_path.write_text(json.dumps(existing), encoding="utf-8")

    cfg = FeishuConfig(app_id="cli_new", app_secret="new_secret", domain="lark", enabled=True)
    _persist_feishu_credentials(cfg)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    # Untouched sections survive.
    assert data["mcp_servers"] == {"robinhood": {"type": "streamableHttp"}}
    assert data["channels"]["send_progress"] is False
    assert data["channels"]["telegram"] == {"enabled": True, "token": "keep-me"}
    # Feishu credentials are overwritten with the fresh values.
    assert data["channels"]["feishu"] == {
        "app_id": "cli_new",
        "app_secret": "new_secret",
        "domain": "lark",
        "enabled": True,
    }


def test_persist_recovers_from_corrupt_config(runtime_root: Path) -> None:
    runtime_root.mkdir(parents=True)
    config_path = runtime_root / "agent.json"
    config_path.write_text("{not valid json", encoding="utf-8")

    cfg = FeishuConfig(app_id="cli_abc", app_secret="secret123", domain="feishu", enabled=True)
    _persist_feishu_credentials(cfg)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["channels"]["feishu"]["app_id"] == "cli_abc"
