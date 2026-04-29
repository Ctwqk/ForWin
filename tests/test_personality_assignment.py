from __future__ import annotations

from pathlib import Path

from forwin.personality.assignment import PersonalityLoadoutAssigner
from forwin.personality.library import CharacterPersonalityLibrary
from forwin.personality.models import (
    CharacterPersonalityPolicy,
    PersonalityAssignmentRequest,
)


def _write_skill(root: Path, relative: str, *, skill_type: str, description: str) -> None:
    path = root / relative / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"name: {path.parent.name}",
                "version: 1.0.0",
                f"description: {description}",
                "forwin_scope: character_personality",
                "category: character_personality_skill",
                f"skill_type: {skill_type}",
                "mode: instruction_only",
                "---",
                f"# Skill: {path.parent.name}",
                "",
                "prompt_compression:",
                "  decision_bias:",
                f"    - {description}",
            ]
        ),
        encoding="utf-8",
    )


def _write_catalog(root: Path) -> None:
    catalog = root / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "assignment_rules.yaml").write_text(
        """
version: character_personality_assignment.test
skills:
  trait-loyal-protector:
    skill_type: trait
    eligible_slots: [dominant, secondary]
    default_weight:
      dominant: 0.72
      secondary: 0.55
    signals:
      personality_tags: [protector]
      role_hint: [护卫, protector]
      narrative_role: [supporting_ally]
      description_keywords: [保护, 承诺]
  trait-ambitious-climber:
    skill_type: trait
    eligible_slots: [dominant, secondary]
    default_weight:
      dominant: 0.70
      secondary: 0.52
    signals:
      role_hint: [对手, rival]
      description_keywords: [野心, 上位]
  trait-suspicious-survivor:
    skill_type: trait
    eligible_slots: [dominant, secondary]
    default_weight:
      dominant: 0.74
      secondary: 0.56
    signals:
      role_hint: [幸存者, survivor]
      description_keywords: [背叛, 追杀]
  trait-quiet-observer:
    skill_type: trait
    eligible_slots: [dominant, secondary]
    default_weight:
      dominant: 0.45
      secondary: 0.35
    signals:
      description_keywords: [观察]
  mask-cold-professional:
    skill_type: social_mask
    eligible_slots: [social_mask]
    default_weight:
      social_mask: 0.58
    default_active_when: [public_scene, professional_context]
    signals:
      personality_tags: [professional]
      public_identity: [执事, officer]
      description_keywords: [冷静, 克制, 职业]
  stress-paranoid-controller:
    skill_type: stress_mode
    eligible_slots: [stress_modes]
    default_weight:
      stress_modes: 0.50
    default_trigger: [betrayal_suspected, information_control_lost]
    signals:
      description_keywords: [背叛, 监视, 信息失控]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (catalog / "fallback_policy.yaml").write_text(
        """
version: character_personality_fallback.test
fallbacks:
  named_supporting_character:
    dominant:
      skill: trait-quiet-observer
      weight: 0.45
    status: valid_needs_review
  background_named_character:
    dominant:
      skill: trait-quiet-observer
      weight: 0.35
    status: fallback_used
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (catalog / "compatibility_matrix.yaml").write_text(
        """
version: character_personality_compatibility.test
rules:
  - left: trait-loyal-protector
    right: trait-ambitious-climber
    relation: tension
    allowed: true
    preferred_slots:
      left: dominant
      right: secondary
    note: 忠诚保护与上位欲可以形成张力，但需要复核写作表达。
  - left: trait-suspicious-survivor
    right: trait-loyal-protector
    relation: conflict
    allowed: false
    note: 测试用硬冲突。
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _library_root(tmp_path: Path) -> Path:
    for name, skill_type, description in (
        ("trait-loyal-protector", "trait", "以保护和承诺组织行动。"),
        ("trait-ambitious-climber", "trait", "优先判断权力上升空间。"),
        ("trait-suspicious-survivor", "trait", "先判断背叛风险和退路。"),
        ("trait-quiet-observer", "trait", "低强度观察环境后再行动。"),
        ("mask-cold-professional", "social_mask", "公开场合保持冷静职业。"),
        ("stress-paranoid-controller", "stress_mode", "压力下控制信息。"),
        ("rel-rival-respect", "relationship_pattern", "竞争中承认对方实力。"),
        ("rel-mentor-protector", "relationship_pattern", "通过扶持和传承建立连接。"),
    ):
        folder = {
            "trait": "skills/traits",
            "social_mask": "skills/social_masks",
            "stress_mode": "skills/stress_modes",
            "relationship_pattern": "skills/relationship_patterns",
        }[skill_type]
        _write_skill(tmp_path, f"{folder}/{name}", skill_type=skill_type, description=description)
    _write_catalog(tmp_path)
    return tmp_path


def test_auto_assignment_selects_protector_and_professional_mask(tmp_path: Path) -> None:
    root = _library_root(tmp_path)
    result = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).assign(
        PersonalityAssignmentRequest(
            project_id="proj",
            character_id="char_shen",
            character_name="沈临川",
            source="subworld_core_named_character",
            description="外门执事，冷静克制，负责保护主角。他对承诺极重。",
            role_hint="protector",
            narrative_role="supporting_ally",
            public_identity="外门执事",
            personality_tags=["protector", "professional"],
            policy=CharacterPersonalityPolicy(),
        )
    )

    assert result.loadout.dominant is not None
    assert result.loadout.dominant.skill == "trait-loyal-protector"
    assert result.loadout.dominant.weight == 0.72
    assert result.loadout.social_mask[0].skill == "mask-cold-professional"
    assert result.loadout.social_mask[0].active_when == ["public_scene", "professional_context"]
    assert result.report.assignment_mode == "auto_rule"
    assert result.report.status == "valid"
    assert result.report.confidence >= 0.8
    assert "role_hint:protector" in result.report.reason_tags


def test_existing_manual_loadout_is_preserved(tmp_path: Path) -> None:
    root = _library_root(tmp_path)
    existing = {
        "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.8},
        "secondary": [],
        "social_mask": [],
        "stress_modes": [],
        "relationship_patterns": [],
        "overrides": {},
    }

    result = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).assign(
        PersonalityAssignmentRequest(
            project_id="proj",
            character_id="char_manual",
            character_name="手动角色",
            source="world_studio_manual",
            description="护卫，冷静负责。",
            existing_loadout=existing,
            existing_assignment={"manual_override": True},
            policy=CharacterPersonalityPolicy(),
        )
    )

    assert result.loadout.dominant is not None
    assert result.loadout.dominant.skill == "trait-suspicious-survivor"
    assert result.report.assignment_mode == "preserve_existing"
    assert result.report.manual_override is True
    assert result.report.preserved_existing_loadout is True
    assert result.report.status == "preserved_manual"


def test_low_signal_named_character_gets_reviewable_fallback(tmp_path: Path) -> None:
    root = _library_root(tmp_path)

    result = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).assign(
        PersonalityAssignmentRequest(
            project_id="proj",
            character_id="char_low",
            character_name="赵平",
            source="api_manual",
            description="临时出现的镇民。",
            policy=CharacterPersonalityPolicy(),
        )
    )

    assert result.loadout.dominant is not None
    assert result.loadout.dominant.skill == "trait-quiet-observer"
    assert result.report.assignment_mode == "fallback_minimal"
    assert result.report.status in {"fallback_used", "valid_needs_review"}
    assert result.report.confidence < 0.4


def test_validation_rejects_unknown_skill_and_stress_without_trigger(tmp_path: Path) -> None:
    root = _library_root(tmp_path)
    report = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).validate(
        {
            "dominant": {"skill": "trait-missing", "weight": 0.75},
            "secondary": [],
            "social_mask": [],
            "stress_modes": [{"skill": "stress-paranoid-controller", "weight": 0.5}],
            "relationship_patterns": [],
            "overrides": {},
        }
    )

    assert not report.ok
    assert "unknown_skill:trait-missing" in report.errors
    assert "stress_mode_without_trigger:stress-paranoid-controller" in report.errors


def test_stress_mode_from_catalog_gets_default_trigger(tmp_path: Path) -> None:
    root = _library_root(tmp_path)
    result = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).assign(
        PersonalityAssignmentRequest(
            project_id="proj",
            character_id="char_stress",
            character_name="疑心者",
            source="api_manual",
            description="他长期遭遇背叛和监视，害怕信息失控。",
            role_hint="survivor",
            policy=CharacterPersonalityPolicy(),
        )
    )

    assert result.loadout.stress_modes
    assert result.loadout.stress_modes[0].skill == "stress-paranoid-controller"
    assert result.loadout.stress_modes[0].trigger == ["betrayal_suspected", "information_control_lost"]


def test_secondary_trait_and_compatibility_warning_are_reported(tmp_path: Path) -> None:
    root = _library_root(tmp_path)

    result = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).assign(
        PersonalityAssignmentRequest(
            project_id="proj",
            character_id="char_tension",
            character_name="张弛",
            source="api_manual",
            description="他保护同伴，也有强烈野心，想要上位。",
            role_hint="protector",
            personality_tags=["protector"],
            policy=CharacterPersonalityPolicy(),
        )
    )

    assert result.loadout.dominant is not None
    assert result.loadout.dominant.skill == "trait-loyal-protector"
    assert [item.skill for item in result.loadout.secondary] == ["trait-ambitious-climber"]
    assert any(item.skill == "trait-ambitious-climber" and item.slot == "secondary" for item in result.report.selected_skills)
    assert any("compatibility_tension" in warning for warning in result.report.warnings)


def test_hard_conflict_candidate_is_rejected_from_secondary(tmp_path: Path) -> None:
    root = _library_root(tmp_path)

    result = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).assign(
        PersonalityAssignmentRequest(
            project_id="proj",
            character_id="char_conflict",
            character_name="冷护卫",
            source="api_manual",
            description="他保护同伴，但曾被背叛和追杀。",
            role_hint="protector",
            personality_tags=["protector"],
            policy=CharacterPersonalityPolicy(),
        )
    )

    assert result.loadout.dominant is not None
    assert result.loadout.dominant.skill == "trait-loyal-protector"
    assert "trait-suspicious-survivor" not in [item.skill for item in result.loadout.secondary]
    assert any(item.skill == "trait-suspicious-survivor" and item.reason == "compatibility_conflict" for item in result.report.rejected_skills)


def test_cast_diversity_adjustment_changes_tied_dominant_choice(tmp_path: Path) -> None:
    root = _library_root(tmp_path)
    crowded_cast = [
        {
            "dominant": {"skill": "trait-loyal-protector", "weight": 0.72},
            "secondary": [],
            "social_mask": [],
            "stress_modes": [],
            "relationship_patterns": [],
            "overrides": {},
        }
        for _ in range(3)
    ]

    result = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).assign(
        PersonalityAssignmentRequest(
            project_id="proj",
            character_id="char_diverse",
            character_name="齐衡",
            source="api_manual",
            description="他一边保护同伴，一边盘算上位。",
            existing_cast_loadouts=crowded_cast,
            policy=CharacterPersonalityPolicy(),
        )
    )

    assert result.loadout.dominant is not None
    assert result.loadout.dominant.skill == "trait-ambitious-climber"
    protector_candidate = next(item for item in result.report.candidate_skills if item.skill == "trait-loyal-protector")
    assert any(tag.startswith("cast_diversity:") for tag in protector_candidate.reason_tags)


def test_assignment_tie_break_is_seeded_and_repeatable(tmp_path: Path) -> None:
    root = _library_root(tmp_path)
    request = PersonalityAssignmentRequest(
        project_id="proj",
        character_id="char_seeded",
        character_name="平衡者",
        source="api_manual",
        description="他保护同伴，也盘算上位。",
        policy=CharacterPersonalityPolicy(),
    )
    assigner = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root))

    first = assigner.assign(request)
    second = assigner.assign(request)

    assert first.loadout.dominant is not None
    assert second.loadout.dominant is not None
    assert first.loadout.dominant.skill == second.loadout.dominant.skill
    assert [item.skill for item in first.report.candidate_skills] == [item.skill for item in second.report.candidate_skills]


def test_explain_uses_report_lookup_callback(tmp_path: Path) -> None:
    root = _library_root(tmp_path)
    report = PersonalityLoadoutAssigner(CharacterPersonalityLibrary(root)).assign(
        PersonalityAssignmentRequest(
            project_id="proj",
            character_id="char_report",
            character_name="报告角色",
            source="api_manual",
            description="保护同伴。",
            policy=CharacterPersonalityPolicy(),
        )
    ).report

    assigner = PersonalityLoadoutAssigner(
        CharacterPersonalityLibrary(root),
        report_lookup=lambda assignment_id: report if assignment_id == report.assignment_id else None,
    )

    assert assigner.explain(report.assignment_id).assignment_id == report.assignment_id
