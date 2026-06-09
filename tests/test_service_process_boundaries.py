from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_service_process_roadmap_doc_defines_logical_write_ownership() -> None:
    doc = _read("docs/operations/forwin-service-process-roadmap.md")

    required_phrases = [
        "Logical Write Ownership",
        "logical write authority, not physical database ownership",
        "generation task enqueue/control",
        "generation task lease/progress",
        "BookState/canon writes",
        "review/governance results",
        "publisher upload/comment/cover jobs",
        "MCP operations",
        "knowledge index writes",
        "observability/artifacts",
    ]
    for phrase in required_phrases:
        assert phrase in doc


def test_production_process_doc_names_126_processes_and_150_data_layer() -> None:
    doc = _read("docs/operations/forwin-production-processes.md")

    required_phrases = [
        "126 App Processes",
        "150 Data Layer",
        "forwin-app-swarm",
        "forwin-generation-worker-swarm",
        "forwin-mcp-swarm",
        "forwin-publisher-worker-swarm",
        "forwin-publisher-browser-swarm",
        "queued/running generation tasks",
        "lease_owner",
        "lease_expires_at",
        "publisher browser heartbeat",
    ]
    for phrase in required_phrases:
        assert phrase in doc


def test_mcp_gateway_does_not_import_database_modules() -> None:
    forbidden = [
        "from forwin.models",
        "import forwin.models",
        "from forwin.models.base",
        "get_engine",
        "get_session_factory",
        "init_db",
        "from sqlalchemy",
        "import sqlalchemy",
    ]
    for path in sorted((ROOT / "forwin" / "mcp").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in source, f"{path.relative_to(ROOT)} imports {pattern}"


def test_generation_to_api_core_dependency_is_limited_to_auto_continue_allowlist() -> None:
    allowed = {
        Path("forwin/generation/worker.py"): {
            "from forwin.api_core.generation import _create_continue_generation_task",
        },
    }
    for path in sorted((ROOT / "forwin" / "generation").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        matches = {
            line.strip()
            for line in source.splitlines()
            if "forwin.api_core" in line
        }
        expected = allowed.get(path.relative_to(ROOT), set())
        assert matches == expected, f"{path.relative_to(ROOT)} has unexpected api_core dependency: {matches}"


def test_publisher_browser_entrypoint_scripts_do_not_open_database_sessions() -> None:
    browser_scripts = [
        "scripts/launch_linux_extension_browser.sh",
        "scripts/check_publisher_browser_heartbeat.py",
        "scripts/qualify_linux_extension_profile.py",
    ]
    forbidden = [
        "get_engine",
        "get_session_factory",
        "from forwin.models.base",
        "import forwin.models.base",
    ]
    for script in browser_scripts:
        source = _read(script)
        for pattern in forbidden:
            assert pattern not in source, f"{script} uses database helper {pattern}"
