from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class WorldLineRow(Base):
    __tablename__ = "world_lines"
    __table_args__ = (
        Index("ix_world_lines_project_line", "project_id", "world_line_id"),
        Index("ix_world_lines_project_type", "project_id", "line_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    world_line_id: Mapped[str] = mapped_column(String, nullable=False)
    line_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="")
    participants_json: Mapped[str] = mapped_column(Text, default="[]")
    objective_state_summary: Mapped[str] = mapped_column(Text, default="")
    is_visible_onstage: Mapped[bool] = mapped_column(Boolean, default=False)
    planned_reveal_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    long_term_promise: Mapped[str] = mapped_column(Text, default="")
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class WorldDeltaRow(Base):
    __tablename__ = "world_deltas"
    __table_args__ = (
        Index("ix_world_deltas_project_chapter", "project_id", "narrative_chapter"),
        Index("ix_world_deltas_project_line", "project_id", "world_line_id"),
        Index("ix_world_deltas_project_kind", "project_id", "delta_kind"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    delta_id: Mapped[str] = mapped_column(String, nullable=False)
    world_line_id: Mapped[str] = mapped_column(String, nullable=False)
    delta_kind: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    objective_story_time: Mapped[str] = mapped_column(Text, default="")
    narrative_chapter: Mapped[int] = mapped_column(Integer, default=0)
    source_type: Mapped[str] = mapped_column(String, default="")
    source_actor_id: Mapped[str] = mapped_column(String, default="")
    source_mechanism: Mapped[str] = mapped_column(Text, default="")
    source_evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    affected_entities_json: Mapped[str] = mapped_column(Text, default="[]")
    affected_factions_json: Mapped[str] = mapped_column(Text, default="[]")
    affected_locations_json: Mapped[str] = mapped_column(Text, default="[]")
    affected_resources_json: Mapped[str] = mapped_column(Text, default="[]")
    affected_rules_json: Mapped[str] = mapped_column(Text, default="[]")
    observer_states_json: Mapped[str] = mapped_column(Text, default="{}")
    allowed_for_canon: Mapped[bool] = mapped_column(Boolean, default=True)
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class BeliefRow(Base):
    __tablename__ = "beliefs"
    __table_args__ = (
        Index("ix_beliefs_project_holder", "project_id", "holder_type", "holder_id"),
        Index("ix_beliefs_project_status", "project_id", "belief_status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    belief_id: Mapped[str] = mapped_column(String, nullable=False)
    holder_type: Mapped[str] = mapped_column(String, nullable=False)
    holder_id: Mapped[str] = mapped_column(String, nullable=False)
    proposition: Mapped[str] = mapped_column(Text, nullable=False)
    truth_relation: Mapped[str] = mapped_column(String, default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    belief_status: Mapped[str] = mapped_column(String, default="active")
    evidence_sources_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    created_at_story_time: Mapped[str] = mapped_column(Text, default="")
    contradicted_by_json: Mapped[str] = mapped_column(Text, default="[]")
    last_updated_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CognitionSnapshotRow(Base):
    __tablename__ = "cognition_snapshots"
    __table_args__ = (
        Index(
            "ix_cognition_snapshots_project_observer",
            "project_id",
            "observer_type",
            "observer_id",
            "as_of_chapter",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    cognition_state_id: Mapped[str] = mapped_column(String, default="")
    observer_type: Mapped[str] = mapped_column(String, nullable=False)
    observer_id: Mapped[str] = mapped_column(String, nullable=False)
    as_of_chapter: Mapped[int] = mapped_column(Integer, default=0)
    as_of_story_time: Mapped[str] = mapped_column(Text, default="")
    beliefs_json: Mapped[str] = mapped_column(Text, default="[]")
    known_delta_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    suspected_gap_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    visibility_by_delta_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class KnowledgeGapRow(Base):
    __tablename__ = "knowledge_gaps"
    __table_args__ = (
        Index("ix_knowledge_gaps_project_status", "project_id", "status"),
        Index("ix_knowledge_gaps_project_line", "project_id", "related_world_line_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    gap_id: Mapped[str] = mapped_column(String, nullable=False)
    objective_truth: Mapped[str] = mapped_column(Text, nullable=False)
    happened_at_story_time: Mapped[str] = mapped_column(Text, default="")
    related_world_line_id: Mapped[str] = mapped_column(String, default="")
    observer_states_json: Mapped[str] = mapped_column(Text, default="{}")
    narrative_function: Mapped[str] = mapped_column(Text, default="")
    planned_closure: Mapped[str] = mapped_column(Text, default="")
    maximum_safe_delay: Mapped[int] = mapped_column(Integer, default=0)
    fairness_requirements_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String, default="open")
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class RevealEventRow(Base):
    __tablename__ = "reveal_events"
    __table_args__ = (
        Index("ix_reveal_events_project_gap", "project_id", "related_gap_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    reveal_event_id: Mapped[str] = mapped_column(String, nullable=False)
    reveals_fact_id: Mapped[str] = mapped_column(String, default="")
    reveals_delta_id: Mapped[str] = mapped_column(String, default="")
    related_gap_id: Mapped[str] = mapped_column(String, default="")
    reveal_to_reader: Mapped[bool] = mapped_column(Boolean, default=False)
    reveal_to_characters_json: Mapped[str] = mapped_column(Text, default="[]")
    reveal_method: Mapped[str] = mapped_column(String, default="")
    from_state: Mapped[str] = mapped_column(String, default="unknown")
    to_state: Mapped[str] = mapped_column(String, default="unknown")
    emotional_effect: Mapped[str] = mapped_column(Text, default="")
    narrative_function: Mapped[str] = mapped_column(Text, default="")
    fairness_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class KnowledgeUpdateEventRow(Base):
    __tablename__ = "knowledge_update_events"
    __table_args__ = (
        Index("ix_knowledge_updates_project_gap", "project_id", "related_gap_id"),
        Index("ix_knowledge_updates_project_observer", "project_id", "observer_type", "observer_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    update_event_id: Mapped[str] = mapped_column(String, nullable=False)
    update_type: Mapped[str] = mapped_column(String, nullable=False)
    observer_type: Mapped[str] = mapped_column(String, nullable=False)
    observer_id: Mapped[str] = mapped_column(String, nullable=False)
    related_gap_id: Mapped[str] = mapped_column(String, default="")
    related_delta_id: Mapped[str] = mapped_column(String, default="")
    from_state: Mapped[str] = mapped_column(String, default="unknown")
    to_state: Mapped[str] = mapped_column(String, default="unknown")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    story_time: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ReaderExperienceDeltaRow(Base):
    __tablename__ = "reader_experience_deltas"
    __table_args__ = (
        Index("ix_reader_exp_project_chapter", "project_id", "chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    reader_experience_delta_id: Mapped[str] = mapped_column(String, nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reader_state_before: Mapped[str] = mapped_column(Text, default="")
    reader_state_after: Mapped[str] = mapped_column(Text, default="")
    cognition_transition: Mapped[str] = mapped_column(Text, default="")
    payoff_type: Mapped[str] = mapped_column(String, default="")
    reward_tags_json: Mapped[str] = mapped_column(Text, default="[]")
    emotional_effect: Mapped[str] = mapped_column(Text, default="")
    promise_debt_change: Mapped[int] = mapped_column(Integer, default=0)
    next_desire: Mapped[str] = mapped_column(Text, default="")
    fairness_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WorldModelSnapshotV4Row(Base):
    __tablename__ = "world_model_snapshots_v4"
    __table_args__ = (
        Index("ix_world_snapshots_v4_project_chapter", "project_id", "as_of_chapter"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    snapshot_id: Mapped[str] = mapped_column(String, nullable=False)
    as_of_chapter: Mapped[int] = mapped_column(Integer, default=0)
    as_of_story_time: Mapped[str] = mapped_column(Text, default="")
    active_world_line_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    open_gap_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    reader_cognition_state_json: Mapped[str] = mapped_column(Text, default="{}")
    character_cognition_states_json: Mapped[str] = mapped_column(Text, default="{}")
    objective_state_summary: Mapped[str] = mapped_column(Text, default="")
    source_delta_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WorldCompileRunV4Row(Base):
    __tablename__ = "world_compile_runs_v4"
    __table_args__ = (
        Index("ix_world_compile_runs_v4_project_chapter", "project_id", "chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    compiler_run_id: Mapped[str] = mapped_column(String, nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    review_verdict_id: Mapped[str] = mapped_column(String, default="")
    committed: Mapped[bool] = mapped_column(Boolean, default=False)
    forced_accept_reason: Mapped[str] = mapped_column(Text, default="")
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    retrieval_pack_json: Mapped[str] = mapped_column(Text, default="{}")
    projection_refresh_json: Mapped[str] = mapped_column(Text, default="{}")
    blocked_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ArcWorldContractRow(Base):
    __tablename__ = "arc_world_contracts"
    __table_args__ = (
        Index("ix_arc_world_contracts_project_arc", "project_id", "arc_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(String, default="")
    arc_number: Mapped[int] = mapped_column(Integer, default=0)
    contract_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class BandWorldContractRow(Base):
    __tablename__ = "band_world_contracts"
    __table_args__ = (
        Index("ix_band_world_contracts_project_arc_band", "project_id", "arc_id", "band_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    chapter_start: Mapped[int] = mapped_column(Integer, default=0)
    chapter_end: Mapped[int] = mapped_column(Integer, default=0)
    contract_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class ChapterWorldDeltaIntentRow(Base):
    __tablename__ = "chapter_world_delta_intents"
    __table_args__ = (
        Index("ix_chapter_world_intents_project_chapter", "project_id", "chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_plan_id: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class ScenarioRehearsalRunRow(Base):
    __tablename__ = "scenario_rehearsal_runs"
    __table_args__ = (
        Index("ix_scenario_rehearsal_project_arc_band", "project_id", "arc_id", "band_id"),
        Index("ix_scenario_rehearsal_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    rehearsal_scope: Mapped[str] = mapped_column(String, default="band")
    chapter_numbers_json: Mapped[str] = mapped_column(Text, default="[]")
    trigger_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    recommendation: Mapped[str] = mapped_column(String, default="pass")
    risk_count: Mapped[int] = mapped_column(Integer, default=0)
    blocker_count: Mapped[int] = mapped_column(Integer, default=0)
    required_patch_count: Mapped[int] = mapped_column(Integer, default=0)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ScenarioPlanPatchRow(Base):
    __tablename__ = "scenario_plan_patches"
    __table_args__ = (
        Index("ix_scenario_plan_patches_project_run", "project_id", "run_id"),
        Index("ix_scenario_plan_patches_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String, ForeignKey("scenario_rehearsal_runs.id"), default="")
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    patch_type: Mapped[str] = mapped_column(String, default="")
    target: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    patch_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="proposed")
    approval_reason: Mapped[str] = mapped_column(Text, default="")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
