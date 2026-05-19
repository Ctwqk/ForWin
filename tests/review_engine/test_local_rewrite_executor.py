from __future__ import annotations

from pathlib import Path

from forwin.protocol.writer import WriterOutput
from forwin.reviser.local_rewrite_executor import LocalRewriteExecutor


def _output(body: str) -> WriterOutput:
    return WriterOutput(
        project_id="project-1",
        chapter_number=4,
        title="第4章",
        body=body,
        char_count=len(body),
        end_of_chapter_summary="摘要",
    )


def test_placeholder_leakage_removes_common_placeholders() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("角色甲走进{{地点}}，看到工作人员记录。"),
        issue_kind="placeholder_leakage",
        signals=[],
        context_pack={
            "active_entities": [
                {"kind": "location", "name": "地点甲", "description": "当前场景地点"},
            ],
        },
    )

    assert result.status == "rewritten"
    assert result.writer_output is not None
    assert "{{地点}}" not in result.writer_output.body
    assert "地点甲" in result.writer_output.body
    assert result.mode == "deterministic_placeholder"


def test_placeholder_leakage_uses_context_character_and_location_anchors() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("{{角色}}进入{{地点}}，确认门禁记录。"),
        issue_kind="placeholder_leakage",
        signals=[],
        context_pack={
            "active_entities": [
                {"kind": "character", "name": "角色甲", "description": "当前可用角色"},
                {"kind": "location", "name": "地点甲", "description": "当前可用地点"},
            ],
        },
    )

    assert result.status == "rewritten"
    assert result.writer_output is not None
    assert result.writer_output.body == "角色甲进入地点甲，确认门禁记录。"


def test_placeholder_leakage_uses_map_location_anchor() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("{{角色}}抵达{{地点}}。"),
        issue_kind="placeholder_leakage",
        signals=[],
        context_pack={
            "active_entities": [
                {"kind": "character", "name": "角色乙", "description": "当前可用角色"},
            ],
            "map_context": {
                "active_locations": [
                    {"entity_name": "角色乙", "location_name": "地点乙"},
                ],
            },
        },
    )

    assert result.status == "rewritten"
    assert result.writer_output is not None
    assert result.writer_output.body == "角色乙抵达地点乙。"


def test_placeholder_leakage_without_context_escalates_to_writer() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("{{角色}}进入{{地点}}。"),
        issue_kind="placeholder_leakage",
        signals=[],
        context_pack={},
    )

    assert result.status == "needs_writer"
    assert result.writer_output is None
    assert result.mode == "missing_canon_placeholder_anchor"
    assert "canon" in result.instruction


def test_body_truncated_requests_continuation_mode() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("第一幕完整。\n\n第二幕刚开始，角色甲"),
        issue_kind="body_truncated",
        signals=[],
        context_pack={},
    )

    assert result.status == "needs_writer"
    assert result.mode == "continue_from_last_complete_scene"
    assert "last_complete_scene" in result.instruction


def test_unsupported_issue_returns_unsupported() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("正文"),
        issue_kind="identity_ambiguity",
        signals=[],
        context_pack={},
    )

    assert result.status == "unsupported"


def test_repair_loop_passes_current_context_to_local_executor() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "forwin"
        / "orchestrator_loop_core"
        / "repair_loop.py"
    ).read_text(encoding="utf-8")
    call_start = source.index("LocalRewriteExecutor().execute(")
    call_body = source[call_start : call_start + 400]

    assert "context_pack=current_context" in call_body
    assert "context_pack={}" not in call_body
