from __future__ import annotations

from forwin.extractor.world_v4 import WorldDeltaExtractor
from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import DeltaKind
from forwin.protocol.writer import WriterOutput


def _chapter_23_intent() -> ChapterWorldDeltaIntent:
    return ChapterWorldDeltaIntent(
        intent_id="chapter_23_intent",
        project_id="project-1",
        chapter_number=23,
        visible_delta_intents=["防线修复"],
        hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
        offscreen_delta_intents=["敌方切断第三通讯阵列"],
        must_not_reveal=["father_sieged"],
        expected_observer_state_changes={"reader": "hidden -> hinted"},
    )


def test_extractor_extracts_body_span_hint_and_offscreen_source() -> None:
    writer_output = WriterOutput(
        project_id="project-1",
        chapter_number=23,
        title="乱码呼号",
        body="防线修复后，通讯台传出乱码。父亲旧部的呼号一闪即逝。敌方切断第三通讯阵列。",
        char_count=50,
        end_of_chapter_summary="通讯异常升级。",
    )

    extracted = WorldDeltaExtractor().extract(
        writer_output,
        chapter_intent=_chapter_23_intent(),
    )

    assert [delta.delta_kind for delta in extracted.world_deltas] == [
        DeltaKind.VISIBLE,
        DeltaKind.HINT,
        DeltaKind.OFFSCREEN,
    ]
    assert extracted.world_deltas[1].source.source_type.value == "information_spread"
    assert extracted.world_deltas[2].source.source_type.value == "faction_action"
    assert extracted.world_deltas[1].source_refs[0].startswith("body_span:")
    assert "乱码" in extracted.world_deltas[1].summary
