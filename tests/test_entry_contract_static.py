from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_target_total_chapter_limit_is_5000_in_user_entrypoints() -> None:
    project_schema = (ROOT / "forwin/api_schema/project.py").read_text()
    mcp_client = (ROOT / "forwin/mcp/client.py").read_text()
    home_js = (ROOT / "forwin/ui_assets/home/app_library.js").read_text()
    home_html = (ROOT / "forwin/ui_assets/home/body.html").read_text()

    assert "le=5000" in project_schema
    assert "target_total_chapters > 5000" in mcp_client
    assert "payload.target_total_chapters > 5000" in home_js
    target_input = re.search(
        r'<input[^>]+id="book_form_target_total_chapters"[^>]+>',
        home_html,
    )
    assert target_input is not None
    assert 'max="5000"' in target_input.group(0)
    assert 'max="200"' not in target_input.group(0)
