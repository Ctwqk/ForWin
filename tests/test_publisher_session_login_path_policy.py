from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_routine_automation_scripts_never_invoke_qr_delivery_paths() -> None:
    forbidden = (
        "/api/publishers/extension/login-qr",
        "/api/publishers/login-qr-one-shot",
        "start_publisher_login_qr_one_shot",
        "FORWIN_PUBLISHER_LOGIN_QR_ONE_SHOT_WEBHOOK_URL",
    )
    script_paths = (
        "scripts/check_production_publisher_baseline.py",
        "scripts/smoke_production_publisher_upload_chain.py",
        "scripts/supervise_forwin_interventions.py",
    )

    for script_path in script_paths:
        source = read_repo_file(script_path)
        for marker in forbidden:
            assert marker not in source, f"{script_path} must not invoke {marker}"


def test_operator_docs_make_session_restore_the_routine_login_path() -> None:
    docs = {
        "README.md": read_repo_file("README.md"),
        "docs/operations/forwin-production-processes.md": read_repo_file(
            "docs/operations/forwin-production-processes.md"
        ),
        "browser_extension/forwin-publisher/README.md": read_repo_file(
            "browser_extension/forwin-publisher/README.md"
        ),
    }

    for path, text in docs.items():
        assert "Routine production login continuity uses backend-synced browser sessions." in text, path
        assert "python scripts/check_production_publisher_baseline.py" in text, path
        assert "production publisher browser profile" in text, path


def test_routine_docs_do_not_show_discord_qr_handoff_commands() -> None:
    routine_docs = {
        "README.md": read_repo_file("README.md"),
        "browser_extension/forwin-publisher/README.md": read_repo_file(
            "browser_extension/forwin-publisher/README.md"
        ),
    }

    for path, text in routine_docs.items():
        assert "start_publisher_login_qr_one_shot.py" not in text, path
        assert "FORWIN_PUBLISHER_LOGIN_QR_ONE_SHOT_WEBHOOK_URL" not in text, path
        assert "login-success confirmation" not in text, path


def test_operations_doc_marks_one_shot_qr_as_emergency_manual_only() -> None:
    text = read_repo_file("docs/operations/forwin-production-processes.md")

    assert "Emergency-only legacy QR handoff" in text
    assert "not supported for routine production automation" in text
    assert "must not be scheduled" in text
    assert "must not be used by baseline, smoke, supervisor, deploy, or recurring jobs" in text
    assert "https://discord.com/api/webhooks/" not in text
