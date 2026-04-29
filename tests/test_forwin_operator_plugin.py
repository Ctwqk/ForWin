from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "forwin-operator"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_forwin_operator_plugin_manifest_is_filled() -> None:
    manifest = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

    assert manifest["name"] == "forwin-operator"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["interface"]["displayName"] == "ForWin Operator"
    assert manifest["interface"]["defaultPrompt"]
    assert "[TODO:" not in json.dumps(manifest)


def test_forwin_operator_plugin_declares_forwin_mcp_server() -> None:
    mcp = load_json(PLUGIN_ROOT / ".mcp.json")

    assert mcp["mcpServers"]["forwin"] == {
        "transport": "streamable_http",
        "url": "http://127.0.0.1:8896/mcp",
    }


def test_forwin_operator_plugin_marketplace_entry_is_local() -> None:
    marketplace = load_json(REPO_ROOT / ".agents" / "plugins" / "marketplace.json")

    assert marketplace["name"] == "forwin-local"
    entry = marketplace["plugins"][0]
    assert entry["name"] == "forwin-operator"
    assert entry["source"] == {"source": "local", "path": "./plugins/forwin-operator"}
    assert entry["policy"] == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}


def test_forwin_operator_plugin_skill_keeps_mcp_safety_rules() -> None:
    skill = (PLUGIN_ROOT / "skills" / "forwin-operator" / "SKILL.md").read_text(encoding="utf-8")

    assert "Use the `forwin` MCP server as the authoritative interface" in skill
    assert "Do not inspect SQLite directly" in skill
    assert "task_active_generation_check" in skill
