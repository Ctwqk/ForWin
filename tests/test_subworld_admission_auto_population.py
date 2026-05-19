from __future__ import annotations

from types import SimpleNamespace

from forwin.planning.subworld_admission import (
    EntityKind,
    build_subworld_admission_from_rows,
    classify_admission_signal,
)


def _entity(name: str, kind: str, chapter: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"ent-{name}",
        name=name,
        kind=kind,
        created_at_chapter=chapter,
        is_active=True,
    )


def test_canon_person_missing_from_roster_is_auto_carried() -> None:
    admission = build_subworld_admission_from_rows(
        project_id="p1",
        chapter_number=18,
        roster_items=[],
        canon_entities=[_entity("角色A", "character")],
    )

    entry = admission.entries_by_name["角色A"]
    assert entry.kind == EntityKind.person
    assert entry.auto_carried is True

    signal = classify_admission_signal(admission, entity_name="角色A", entity_kind="character")
    assert signal.signal_kind == "subworld_admission_missing_canon_entity"
    assert signal.blocking is False


def test_canon_organization_and_location_keep_their_kinds() -> None:
    admission = build_subworld_admission_from_rows(
        project_id="p1",
        chapter_number=18,
        roster_items=[],
        canon_entities=[
            _entity("组织A", "faction"),
            _entity("地点A", "location"),
        ],
    )

    assert admission.entries_by_name["组织A"].kind == EntityKind.organization
    assert admission.entries_by_name["地点A"].kind == EntityKind.location


def test_unknown_entity_blocks_unless_code_pattern_matches() -> None:
    admission = build_subworld_admission_from_rows(
        project_id="p1",
        chapter_number=18,
        roster_items=[],
        canon_entities=[],
        code_patterns=[r"^PS-\d+$"],
    )

    code_signal = classify_admission_signal(admission, entity_name="PS-07", entity_kind="code")
    unknown_signal = classify_admission_signal(admission, entity_name="韩青", entity_kind="character")

    assert code_signal.signal_kind == ""
    assert code_signal.blocking is False
    assert unknown_signal.signal_kind == "subworld_admission_unauthorized_new_entity"
    assert unknown_signal.blocking is True
