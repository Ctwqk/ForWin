from __future__ import annotations

from pathlib import Path


DOC_ROOT = Path(__file__).resolve().parents[1] / "Design-docs"


def test_v45_docs_do_not_reintroduce_superseded_backend_status() -> None:
    docs = {
        name: (DOC_ROOT / name).read_text(encoding="utf-8")
        for name in [
            "V4.5_markstone.md",
            "V4.5.1_markstone.md",
            "V4_final_book_state_runtime.md",
            "map_scheme_c.md",
            "writing_flow_state_machine.md",
            "maintenance_log.md",
        ]
    }
    stale_phrases = [
        "旧 V4 compiler commit 后追加 BookState",
        "BookState gate 是追加阻断点",
        "未新增独立地图 FastAPI 路由",
        "地图系统本轮不新增独立 FastAPI 路由",
        "arc expansion 待做",
    ]

    offenders = [
        (name, phrase)
        for name, text in docs.items()
        for phrase in stale_phrases
        if phrase in text
    ]

    assert offenders == []


def test_writing_flow_state_machine_names_v45_book_state_map_nodes() -> None:
    text = (DOC_ROOT / "writing_flow_state_machine.md").read_text(encoding="utf-8")

    for node_name in [
        "BookStateReviewGate",
        "BookStateCompile",
        "LegacyProjection",
        "MapMovementReview",
    ]:
        assert node_name in text


def test_v451_markstone_classifies_residual_design_without_v46_scope_creep() -> None:
    v45 = (DOC_ROOT / "V4.5_markstone.md").read_text(encoding="utf-8")
    v451 = (DOC_ROOT / "V4.5.1_markstone.md").read_text(encoding="utf-8")

    assert "V4.5.1_markstone.md" in v45
    assert "V4.6+ 不纳入 V4.5.1" in v451
    assert "3. V4.5.1 继续追踪的残余设计" in v451
    assert "4. 逐文档审计结果" in v451
    assert "native GraphDelta extractor" in v451
    assert "不作为 V4.5.1 未完成项" in v451

    for doc_name in [
        "V2_9_2.md",
        "V2_9_3_skill_runtime.md",
        "V3_8.md",
        "V4_final_book_state_runtime.md",
        "map_scheme_c.md",
        "provisional_mechanism_check.md",
        "review-design.rtf",
        "review_fix_log_2026-04-15.md",
    ]:
        assert doc_name in v451
