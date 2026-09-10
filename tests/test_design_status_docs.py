from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_ROOT = ROOT / "Design-docs"
# The status page is navigation; the current architecture owns policy details.
ACTIVE_CURRENT_POLICY_DOCS = {
    "Design-docs/CURRENT_ARCHITECTURE.md": (
        "`CURRENT_ARCHITECTURE.md` | active-current"
    ),
}



def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_current_architecture_and_design_status_are_documented() -> None:
    current = _read(DOC_ROOT / "CURRENT_ARCHITECTURE.md")
    status = _read(DOC_ROOT / "DESIGN_STATUS.md")
    readme = _read(ROOT / "README.md")

    assert "唯一 canon source 是 `BookState DB Canon`" in current
    assert "地图 canon：`BookMap / Scheme C`" in current
    assert "`world_model_v4`：已删除的旧 compatibility projection" in current
    assert "`forwin.book_state.extraction`：BookState candidate extraction" in current
    assert "CURRENT_ARCHITECTURE.md" in readme
    assert "DESIGN_STATUS.md" in readme

    for expected in [
        "`CURRENT_ARCHITECTURE.md` | active-current",
        "`V4.5_markstone.md` | active-current",
        "`V4_final_book_state_runtime.md` | active-current",
        "`map_scheme_c.md` | active-current",
        "`V2_9_2.md` | baseline-with-overrides",
        "`provisional_mechanism_check.md` | legacy-compatibility",
        "https://github.com/Ctwqk/ForWin/blob/521228871a5752ebe8572c057caa9f4944bb0295/Design-docs/DESIGN_STATUS.md",
    ]:
        assert expected in status


def test_historical_superpowers_plans_are_not_current_architecture_sources() -> None:
    for rel_path in [
        "docs/superpowers/plans/2026-04-24-forwin-v4-world-model.md",
        "docs/superpowers/plans/2026-04-24-forwin-v4-1-runtime-hardening.md",
    ]:
        text = _read(ROOT / rel_path)
        assert "Status: historical implementation plan" in text
        assert "CURRENT_ARCHITECTURE.md" in text
        assert "DESIGN_STATUS.md" in text


def test_active_current_runtime_policy_docs_match_report_only_v2_contract() -> None:
    status = _read(DOC_ROOT / "DESIGN_STATUS.md")
    documents = {
        rel_path: _read(ROOT / rel_path)
        for rel_path in ACTIVE_CURRENT_POLICY_DOCS
    }

    for registry_marker in ACTIVE_CURRENT_POLICY_DOCS.values():
        assert registry_marker in status

    removed_fields = (
        "generation_audit_" + "interval",
        "generation_audit_" + "pauses",
    )
    offenders = [
        (rel_path, field)
        for rel_path, text in documents.items()
        for field in removed_fields
        if field in text
    ]
    assert offenders == []

    contract_markers = (
        "`RuntimePolicy.schema_version=2`",
        "`report-only`",
        "`cadence=6`",
        '`ChapterPlan.status="accepted"`',
        "`all profiles`",
        "`generation_audit_checkpoint_reached`",
        '`event_family="runtime_observation"`',
        "`no pause/delegation/block`",
    )
    missing_contract = [
        (rel_path, marker)
        for rel_path, text in documents.items()
        for marker in contract_markers
        if marker not in text
    ]
    assert missing_contract == []


def test_registered_provisional_preview_evidence_has_current_boundary_guidance() -> None:
    status = _read(DOC_ROOT / "DESIGN_STATUS.md")
    evidence = _read(DOC_ROOT / "provisional_mechanism_check.md")

    assert "`provisional_mechanism_check.md` | legacy-compatibility" in status
    for historical_marker in (
        "Status: Provisional Band Preview runtime removed.",
        "## Removed Runtime",
        "## Preserved Names",
        "`PlanningPolicy.provisional_preview`",
        "`ProvisionalPromotionRecord`",
    ):
        assert historical_marker in evidence

    expected_path = """Arc/ChapterPlan
-> Writer
-> immutable CandidateDraftRecord
-> Candidate Draft Review / Repair
-> CanonPreparationService
-> CanonAdmissionService.commit_plan
-> BookState"""
    assert expected_path in evidence
    assert (
        "Arc/ChapterPlan planning services own pre-writing plan construction."
        in evidence
    )

    stale_scenario_symbols = (
        "Scenario Rehearsal",
        "ScenarioRehearsal",
        "scenario_rehearsal",
        "scenario-rehearsal",
        "scenarioRehearsal",
    )
    assert all(symbol not in evidence for symbol in stale_scenario_symbols)


def test_design_guard_does_not_reintroduce_local_subworld_semantics() -> None:
    active_docs = [
        "CURRENT_ARCHITECTURE.md",
        "V4.5_markstone.md",
        "V4.5.1_markstone.md",
        "V4_final_book_state_runtime.md",
        "map_scheme_c.md",
        "writing_flow_state_machine.md",
    ]
    forbidden_phrases = [
        "SubWorld = 城市",
        "SubWorld=城市",
        "把城市建成 `SubWorld`",
        "城市/客栈/遗迹入口作为 SubWorld",
    ]
    offenders = [
        (name, phrase)
        for name in active_docs
        for phrase in forbidden_phrases
        if phrase in _read(DOC_ROOT / name)
    ]

    assert offenders == []
