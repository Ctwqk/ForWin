from __future__ import annotations

from forwin.extractor.world_v4 import WorldDeltaExtractor
from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import DeltaKind, KnowledgeUpdateType, VisibilityState
from forwin.protocol.writer import WriterOutput


def test_extractor_derives_hint_delta_and_reader_cognition_from_chapter_intent() -> None:
    writer_output = WriterOutput(
        project_id="project-1",
        chapter_number=23,
        title="乱码呼号",
        body="防线修复完成后，通讯台里只剩乱码。杂音深处，有人反复报出父亲旧部的呼号。",
        end_of_chapter_summary="主角修复防线，并收到异常通讯。",
    )
    intent = ChapterWorldDeltaIntent(
        intent_id="chapter_23_intent",
        project_id="project-1",
        chapter_number=23,
        visible_delta_intents=["殖民地防线修复"],
        hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
        must_not_reveal=["father_sieged"],
        expected_observer_state_changes={
            "reader": "hidden -> hinted",
            "protagonist": "unknown -> suspected",
        },
    )

    extracted = WorldDeltaExtractor().extract(
        writer_output,
        chapter_intent=intent,
    )

    assert [delta.delta_kind for delta in extracted.world_deltas] == [
        DeltaKind.VISIBLE,
        DeltaKind.HINT,
    ]
    assert extracted.world_deltas[1].summary == "乱码通讯；父亲旧部呼号"
    assert extracted.reveal_events[0].from_state == VisibilityState.HIDDEN
    assert extracted.reveal_events[0].to_state == VisibilityState.HINTED
    assert extracted.knowledge_update_events[0].update_type == KnowledgeUpdateType.HINT
    assert extracted.reader_experience_deltas[0].cognition_transition == "hidden -> hinted"
    assert extracted.source_refs == ["writer_output:body", "chapter_intent:chapter_23_intent"]


def test_extractor_preserves_writer_self_reported_v4_changes() -> None:
    self_reported = WriterOutput(
        project_id="project-1",
        chapter_number=23,
        title="乱码呼号",
        body="正文",
        end_of_chapter_summary="摘要",
        observer_visibility_updates={"reader": "hidden -> hinted"},
        must_preserve_facts=["殖民地防线修复"],
        must_not_reveal_violations=[],
    )

    extracted = WorldDeltaExtractor().extract(self_reported)

    assert extracted.project_id == "project-1"
    assert extracted.chapter_number == 23
    assert extracted.metadata["observer_visibility_updates"] == {
        "reader": "hidden -> hinted"
    }
    assert extracted.metadata["must_preserve_facts"] == ["殖民地防线修复"]
    assert extracted.metadata["must_not_reveal_violations"] == []
