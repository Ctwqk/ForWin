from __future__ import annotations

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
        draft=_output("韩青走进{{地点}}，看到工作人员记录。"),
        issue_kind="placeholder_leakage",
        signals=[],
        context_pack={},
    )

    assert result.status == "rewritten"
    assert result.writer_output is not None
    assert "{{地点}}" not in result.writer_output.body
    assert result.mode == "deterministic_placeholder"


def test_body_truncated_requests_continuation_mode() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("第一幕完整。\n\n第二幕刚开始，韩青"),
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
