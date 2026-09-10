from __future__ import annotations

import json

from forwin.context.request import ContextDraft, ContextRequest
from forwin.protocol.context import GenesisReferenceFact


def _reference_facts(world_bible, story_engine, *, prefix, chapter_plan):
    """Select whole source statements; never shorten a fact into a new claim."""
    facts: list[GenesisReferenceFact] = []

    def add(path, category, value, subject=""):
        if isinstance(value, str) and value.strip():
            facts.append(GenesisReferenceFact(
                source_path=f"{prefix}{path}", category=category, subject=subject, text=value,
            ))

    add("world_bible.history_slice", "world_history", world_bible.get("history_slice"))
    axioms = world_bible.get("axioms")
    if isinstance(axioms, list):
        for index, value in enumerate(axioms):
            add(f"world_bible.axioms.{index}", "world_rule", value)
    cast = story_engine.get("core_cast")
    if isinstance(cast, list):
        for index, item in enumerate(cast):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                add(f"story_engine.core_cast.{index}.secret", "character_secret", item.get("secret"), name)

    # Chapter-mentioned characters precede the rest of the cast under pressure.
    chapter_text = "\n".join(str(getattr(chapter_plan, key, "") or "")
                             for key in ("title", "one_line", "goals_json"))
    facts.sort(key=lambda fact: fact.category == "character_secret" and fact.subject not in chapter_text)
    selected: list[GenesisReferenceFact] = []
    remaining_chars = 12000
    for fact in facts:
        size = len(fact.model_dump_json())
        if size <= remaining_chars and len(selected) < 64:
            selected.append(fact)
            remaining_chars -= size
    return selected, len(facts) - len(selected)


class GenesisContextProvider:
    name = "genesis"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        genesis_refs: dict[str, str] = {}
        genesis_world_overview = ""
        genesis_story_engine_summary = ""
        genesis_map_atlas: dict = {}
        genesis_story_engine: dict = {}
        genesis_reference_facts: list[GenesisReferenceFact] = []
        genesis_reference_omitted_count = 0
        genesis_getter = getattr(request.repo, "get_active_genesis_revision", None)
        if callable(genesis_getter):
            genesis_revision = genesis_getter(request.project_id)
            if genesis_revision is not None:
                try:
                    genesis_pack = json.loads(getattr(genesis_revision, "pack_json", "{}") or "{}") or {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    genesis_pack = {}
                if isinstance(genesis_pack, dict):
                    world_root = genesis_pack.get("world") if isinstance(genesis_pack.get("world"), dict) else {}
                    source_prefix = "world." if world_root else ""
                    if not world_root:
                        world_root = {
                            "world_bible": genesis_pack.get("world_bible") if isinstance(genesis_pack.get("world_bible"), dict) else {},
                            "map_atlas": genesis_pack.get("map_atlas") if isinstance(genesis_pack.get("map_atlas"), dict) else {},
                            "story_engine": genesis_pack.get("story_engine") if isinstance(genesis_pack.get("story_engine"), dict) else {},
                        }
                    world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
                    genesis_map_atlas = world_root.get("map_atlas") if isinstance(world_root.get("map_atlas"), dict) else {}
                    story_engine = world_root.get("story_engine") if isinstance(world_root.get("story_engine"), dict) else {}
                    genesis_story_engine = story_engine
                    genesis_reference_facts, genesis_reference_omitted_count = _reference_facts(
                        world_bible, story_engine, prefix=source_prefix, chapter_plan=request.chapter_plan,
                    )
                    genesis_world_overview = str(world_bible.get("overview", "") or "")
                    long_arcs = story_engine.get("long_arcs") if isinstance(story_engine.get("long_arcs"), list) else []
                    genesis_story_engine_summary = "；".join(str(item).strip() for item in long_arcs if str(item).strip())
                    genesis_refs = {
                        "genesis_revision_id": str(getattr(genesis_revision, "id", "") or ""),
                        "genesis_revision_number": str(getattr(genesis_revision, "revision", "") or ""),
                    }
        draft.data.update(
            {
                "genesis_refs": genesis_refs,
                "genesis_world_overview": genesis_world_overview,
                "genesis_story_engine_summary": genesis_story_engine_summary,
                "genesis_map_atlas": genesis_map_atlas,
                "genesis_story_engine": genesis_story_engine,
                "genesis_reference_facts": genesis_reference_facts,
                "genesis_reference_omitted_count": genesis_reference_omitted_count,
            }
        )
