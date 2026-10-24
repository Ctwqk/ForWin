"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import logging


from forwin.protocol.context import (
    ChapterContextPack,
)
from forwin.chapter_titles import rebase_generic_numeric_chapter_title
from forwin.observability.context import OperationContext
from forwin.observability.ports import NullObservability
from .canon_quality_context import _build_canon_quality_context

logger = logging.getLogger(__name__)

_MAP_CONTEXT_NEIGHBOR_LIMIT = 8
_MAP_CONTEXT_REVIEW_GRAPH_NODE_LIMIT = 256
_MAP_CONTEXT_REVIEW_GRAPH_EDGE_LIMIT = 512


class ChapterContextAssembler:
    def __init__(self, *, providers: list | None = None, gates: list | None = None, observability=None) -> None:
        self.providers = providers or self._default_providers()
        self.gates = gates if gates is not None else self._default_gates()
        self.observability = observability or NullObservability()

    @property
    def provider_names(self) -> list[str]:
        return [str(getattr(provider, "name", provider.__class__.__name__)) for provider in self.providers]

    def assemble(
        self,
        repo,
        project_id: str,
        chapter_plan,
    ) -> ChapterContextPack:
        from forwin.context.request import ContextDraft, ContextRequest

        request = ContextRequest(
            project_id=project_id,
            chapter_plan=chapter_plan,
            repo=repo,
            session=getattr(repo, "session", None),
        )
        draft = ContextDraft(data={}, issues=[])
        base_context = OperationContext(
            project_id=project_id,
            chapter_number=int(getattr(chapter_plan, "chapter_number", 0) or 0),
            stage="chapter.assemble_context",
        )
        for provider in self.providers:
            provider_name = str(getattr(provider, "name", provider.__class__.__name__))
            with self.observability.span(
                base_context,
                f"context.provider.{provider_name}",
                span_kind="context",
                component="context",
                tags={"provider": provider_name},
            ) as span:
                before_issue_count = len(draft.issues)
                before_key_count = len(draft.data)
                provider.contribute(request, draft)
                span.metric("data_key_count", len(draft.data))
                span.metric("added_data_keys", max(0, len(draft.data) - before_key_count))
                span.metric("provider_issue_count", max(0, len(draft.issues) - before_issue_count))
        for gate in self.gates:
            gate_name = str(getattr(gate, "name", gate.__class__.__name__))
            with self.observability.span(
                base_context,
                f"context.gate.{gate_name}",
                span_kind="context",
                component="context",
                tags={"gate": gate_name},
            ) as span:
                issues = gate.validate(request, draft)
                draft.issues.extend(issues)
                span.metric("issue_count", len(issues))
        return self._build_pack(
            project_id=project_id,
            chapter_plan=chapter_plan,
            draft=draft,
            session=getattr(repo, "session", None),
        )

    def _build_pack(self, *, project_id: str, chapter_plan, draft, session=None) -> ChapterContextPack:
        from forwin.protocol.world_model import WorldContextPack

        data = draft.data
        project = data["project"]
        chapter_experience_plan = data.get("chapter_experience_plan")
        return ChapterContextPack(
            project_id=project_id,
            project_title=project.title,
            premise=project.premise,
            genre=project.genre,
            setting_summary=project.setting_summary,
            project_target_total_chapters=int(getattr(project, "target_total_chapters", 0) or 0),
            genesis_context_refs=data.get("genesis_refs", {}),
            genesis_world_overview=data.get("genesis_world_overview", ""),
            genesis_map_overview=data.get("genesis_map_overview", ""),
            genesis_story_engine_summary=data.get("genesis_story_engine_summary", ""),
            chapter_number=chapter_plan.chapter_number,
            chapter_plan_title=rebase_generic_numeric_chapter_title(
                chapter_plan.title,
                chapter_plan.chapter_number,
            ),
            chapter_plan_one_line=chapter_plan.one_line,
            chapter_goals=data.get("goals", []),
            chapter_plan_hook=str(getattr(chapter_plan, "hook", "") or ""),
            arc_phase_purpose=data.get("arc_phase_purpose", ""),
            world_view=data.get("world_view"),
            previous_chapter_summaries=data.get("summaries", []),
            active_entities=data.get("entities", []),
            active_relations=data.get("relations", []),
            canon_name_anchors=data.get("canon_name_anchors", []),
            active_threads=data.get("threads", []),
            timeline=data.get("timeline"),
            reader_feedback=data.get("reader_feedback"),
            audience_hints=data.get("audience_hints"),
            reader_promise=data.get("reader_promise"),
            arc_payoff_map=data.get("arc_payoff_map"),
            band_delight_schedule=data.get("band_schedule"),
            chapter_experience_plan=chapter_experience_plan,
            active_subworlds=data.get("active_subworlds", []),
            allowed_entities=data.get("allowed_entities", []),
            chapter_entry_targets=(
                list(chapter_experience_plan.chapter_entry_targets)
                if chapter_experience_plan is not None
                else []
            ),
            entity_admission_rule=(
                str(chapter_experience_plan.entity_admission_rule or "").strip()
                if chapter_experience_plan is not None
                else ""
            ),
            chapter_task_contract=data.get("chapter_task_contract", []),
            band_task_contract=data.get("band_task_contract", []),
            active_future_constraints=data.get("active_constraints", []),
            next_band_summary=data.get("next_band_summary"),
            world_context=data.get("world_context", WorldContextPack()),
            map_context=data.get("map_context", {}),
            active_personality_contexts=data.get("active_personality_contexts", []),
            personality_integrity_issues=data.get("personality_integrity_issues", []),
            canon_quality_context=_build_canon_quality_context(
                session=session,
                project_id=project_id,
                chapter_number=int(getattr(chapter_plan, "chapter_number", 0) or 0),
                target_total_chapters=int(getattr(project, "target_total_chapters", 0) or 0),
                chapter_title=str(getattr(chapter_plan, "title", "") or ""),
                chapter_summary=str(getattr(chapter_plan, "one_line", "") or ""),
            ),
        )

    @staticmethod
    def _default_providers() -> list:
        from forwin.context.providers import (
            BookStateContextProvider,
            ExperienceContextProvider,
            FeedbackContextProvider,
            GenesisContextProvider,
            MapContextProvider,
            PersonalityContextProvider,
            StateContextProvider,
            WorldViewContextProvider,
        )

        return [
            GenesisContextProvider(),
            StateContextProvider(),
            ExperienceContextProvider(),
            MapContextProvider(),
            BookStateContextProvider(),
            WorldViewContextProvider(),
            PersonalityContextProvider(),
            FeedbackContextProvider(),
        ]

    @staticmethod
    def _default_gates() -> list:
        from forwin.context.gates import ContextIntegrityGate, PersonalityIntegrityGate

        return [PersonalityIntegrityGate(), ContextIntegrityGate()]


def assemble_context(
    repo,  # StateRepository
    project_id: str,
    chapter_plan,  # ChapterPlan ORM object
) -> ChapterContextPack:
    """Build a ChapterContextPack for the writer through the provider chain."""
    return ChapterContextAssembler().assemble(repo, project_id, chapter_plan)


__all__ = [
    'ChapterContextAssembler',
    'assemble_context',
]
