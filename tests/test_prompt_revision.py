from __future__ import annotations

import json
from difflib import unified_diff
from pathlib import Path
from typing import Any

from forwin.protocol.context import ChapterContextPack
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.prompt_budget import prompt_message_chars, prompt_revision_hash
from forwin.writer.prompt_core import (
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
    build_single_chapter_draft_prompt,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "prompt_regression" / "cases.json"
SNAPSHOT_DIR = ROOT / "tests" / "fixtures" / "prompt_regression" / "snapshots"

EXPECTED_PROMPT_REVISIONS = {
    "single_minimal_opening": "1d7572918f7cbbb8",
    "single_countdown_generic_profile": "1c2edd3b4583cd19",
    "single_closed_countdown": "142b8ac32d850135",
    "single_character_state": "01a9eed48f9542fb",
    "single_pre_audit_suppression": "49227d88da75ec42",
    "single_future_plan_audit": "2c1767d1cbdd91dd",
    "preview_arc_rehearsal": "d2a7ea5ffa3d2dd1",
    "scene_breakdown_basic": "a54e50e38ad6e7e0",
    "single_final_chapter": "a2f16b5ad83df991",
    "single_obligation_visible": "aebcb21e7fada7e3",
}


class _FakeLLM:
    profile_id = "fake"
    profile_name = "fake"
    model = "fake-model"
    base_url = ""

    def __init__(self) -> None:
        self.last_messages: list[dict[str, Any]] = []
        self.last_call_result = None

    def chat(self, messages: list[dict[str, Any]], **_: Any) -> str:
        self.last_messages = messages
        body = "陈星推开舱门，确认异常信号仍在面板上闪烁。" * 20
        return (
            "<<FORWIN_TITLE>>\n"
            "测试章\n"
            "<<FORWIN_BODY>>\n"
            f"{body}\n"
            "<<FORWIN_SUMMARY>>\n"
            "陈星确认异常信号仍未消失。"
        )

    def drain_model_fallback_events(self) -> list[dict[str, Any]]:
        return []

    def drain_llm_attempt_events(self) -> list[dict[str, Any]]:
        return []


def _build_messages(case: dict[str, Any]) -> list[dict[str, Any]]:
    context = ChapterContextPack(**case["context"])
    builder = str(case["builder"])
    if builder == "single":
        return build_single_chapter_draft_prompt(context)
    if builder == "preview":
        return build_preview_chapter_prompt(context)
    if builder == "scene_breakdown":
        return build_scene_breakdown_prompt(context)
    raise AssertionError(f"Unsupported prompt regression builder: {builder}")


def _snapshot_text(messages: list[dict[str, Any]]) -> str:
    return "\n\n--- MESSAGE ---\n".join(
        f"role={message.get('role', '')}\n{message.get('content', '')}"
        for message in messages
    )


def test_writer_output_records_prompt_revision_hash() -> None:
    llm = _FakeLLM()
    context = ChapterContextPack(
        project_id="prompt-revision",
        project_title="星门档案",
        premise="主角：陈星，空间站事故调查员。",
        genre="科幻悬疑",
        setting_summary="近未来轨道城和失联空间站。",
        chapter_number=1,
        chapter_plan_title="失联信号",
        chapter_plan_one_line="陈星收到异常信号。",
        chapter_goals=["确认异常信号"],
    )
    writer = ChapterWriter(llm, min_chapter_chars=20, target_chapter_chars=80, max_chapter_chars=120)

    output = writer.write_preview_chapter(context)

    expected = prompt_revision_hash(llm.last_messages)
    assert output.prompt_revision_hash == expected
    assert output.generation_meta["prompt_revision_hash"] == expected


def test_prompt_regression_fixtures_have_stable_revision_hashes() -> None:
    """Update workflow: regenerate snapshots only after reviewing the prompt diff intentionally."""
    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert len(cases) == 10
    observed: dict[str, str] = {}
    for case in cases:
        messages = _build_messages(case)
        case_id = str(case["case_id"])
        snapshot_path = SNAPSHOT_DIR / f"{case_id}.prompt.txt"
        expected_snapshot = snapshot_path.read_text(encoding="utf-8")
        actual_snapshot = _snapshot_text(messages)
        if actual_snapshot != expected_snapshot:
            diff = "\n".join(
                unified_diff(
                    expected_snapshot.splitlines(),
                    actual_snapshot.splitlines(),
                    fromfile=str(snapshot_path),
                    tofile=f"assembled:{case_id}",
                    lineterm="",
                )
            )
            raise AssertionError(f"Prompt snapshot changed for {case_id}:\n{diff}")
        observed[case_id] = prompt_revision_hash(messages)
        assert prompt_message_chars(messages) > 500

    assert observed == EXPECTED_PROMPT_REVISIONS
