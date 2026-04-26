from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import md5
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.director.arc_director import ArcDirector
from forwin.models import (
    ArcPlanVersion,
    ChapterPlan,
    Entity,
    Project,
    SubWorld,
    SubWorldRosterItem,
    new_id,
)
from forwin.orchestrator.goals import load_goals_json
from forwin.map.protocol import RegionNode
from forwin.map.repository import MapRepository
from forwin.protocol import (
    ChapterEntryTarget,
    SubWorldPlanDelta,
    SubWorldPlanItem,
    SubWorldSummary,
)
from forwin.state.updater import StateUpdater


_NAME_SURNAMES = (
    "沈", "顾", "林", "陆", "苏", "许", "周", "谢", "秦", "江", "宋", "裴", "陈", "白",
)
_NAME_GIVEN = (
    "临川", "知遥", "明序", "清和", "宴秋", "昭宁", "星野", "景川", "怀瑾", "时雨", "砚书", "听澜",
)
_GENERIC_CHARACTER_TOKENS = {
    "路人", "守卫", "老板", "店小二", "师兄", "师姐", "弟子", "同学", "众人", "人群", "伙计", "旁人",
}


def _load_json(raw: str, default):
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _clean_token(text: str) -> str:
    return "".join(ch for ch in str(text or "").strip() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


@dataclass(slots=True)
class BandActivationPlan:
    active_subworld_ids: list[str]
    chapter_entry_targets: list[ChapterEntryTarget]


class SubWorldManager:
    def __init__(self, *, director: ArcDirector | None = None) -> None:
        self.director = director

    def ensure_registry(self, session: Session, project_id: str) -> str:
        global_core = session.execute(
            select(SubWorld)
            .where(
                SubWorld.project_id == project_id,
                SubWorld.scope == "global_core",
            )
            .order_by(SubWorld.created_at.asc(), SubWorld.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if global_core is None:
            global_core = SubWorld(
                id=new_id(),
                project_id=project_id,
                origin_arc_id=None,
                parent_subworld_id=None,
                name="global_core",
                purpose="项目级核心常驻角色池",
                scope="global_core",
                status="active",
                introduced_at_chapter=0,
                retired_at_chapter=None,
                metadata_json="{}",
            )
            session.add(global_core)
            session.flush()

        rostered = {
            str(row[0])
            for row in session.execute(
                select(SubWorldRosterItem.entity_id)
                .where(
                    SubWorldRosterItem.project_id == project_id,
                    SubWorldRosterItem.entity_id.is_not(None),
                )
            ).all()
            if str(row[0] or "").strip()
        }
        active_characters = session.execute(
            select(Entity)
            .where(
                Entity.project_id == project_id,
                Entity.kind == "character",
                Entity.is_active == True,  # noqa: E712
            )
        ).scalars().all()
        for entity in active_characters:
            if entity.id in rostered:
                continue
            session.add(
                SubWorldRosterItem(
                    id=new_id(),
                    project_id=project_id,
                    subworld_id=global_core.id,
                    entity_id=entity.id,
                    entity_kind="character",
                    display_name=entity.name,
                    slot_key="",
                    role_hint="",
                    description=entity.description,
                    is_core=True,
                    status="seeded_named",
                    activation_chapter=0,
                    metadata_json="{}",
                )
            )
        session.flush()
        return global_core.id

    def summarize_registry(self, session: Session, project_id: str) -> list[SubWorldSummary]:
        rows = session.execute(
            select(SubWorld)
            .where(SubWorld.project_id == project_id)
            .order_by(SubWorld.scope.asc(), SubWorld.created_at.asc())
        ).scalars().all()
        result: list[SubWorldSummary] = []
        for row in rows:
            roster_items = session.execute(
                select(SubWorldRosterItem)
                .where(SubWorldRosterItem.subworld_id == row.id)
                .order_by(SubWorldRosterItem.is_core.desc(), SubWorldRosterItem.created_at.asc())
            ).scalars().all()
            result.append(
                SubWorldSummary(
                    id=row.id,
                    name=row.name,
                    purpose=row.purpose,
                    scope=row.scope,
                    status=row.status,
                    active_in_current_band=False,
                    core_cast=[
                        item.display_name
                        for item in roster_items
                        if item.is_core and str(item.display_name or "").strip()
                    ],
                    planned_slot_count=sum(1 for item in roster_items if item.status == "planned_slot"),
                )
            )
        return result

    def ensure_initial_registry_for_active_arc(
        self,
        *,
        session: Session,
        project_id: str,
    ) -> None:
        self.ensure_registry(session, project_id)
        subworlds = session.execute(
            select(SubWorld)
            .where(SubWorld.project_id == project_id)
            .order_by(SubWorld.created_at.asc())
        ).scalars().all()
        roster_count = session.execute(
            select(SubWorldRosterItem)
            .where(SubWorldRosterItem.project_id == project_id)
        ).scalars().all()
        if len(subworlds) > 1 or roster_count:
            return

        if self.director is None:
            return
        project = session.get(Project, project_id)
        active_arc = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
            .order_by(ArcPlanVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        chapter_plans = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.status.in_(("planned", "failed", "accepted", "needs_review")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        if project is None or active_arc is None or not chapter_plans:
            return

        chapter_seed = [
            {
                "chapter_number": plan.chapter_number,
                "title": plan.title,
                "one_line": plan.one_line,
                "goals": load_goals_json(plan.goals_json),
            }
            for plan in chapter_plans[:4]
        ]
        delta = self.director.plan_subworld_delta(
            premise=project.premise,
            genre=project.genre,
            arc_synopsis=active_arc.arc_synopsis,
            chapter_seed=chapter_seed,
            existing_subworlds=self.summarize_registry(session, project_id),
            focus_threads=[],
        )
        updater = StateUpdater(session)
        self.apply_arc_delta(
            session=session,
            updater=updater,
            project_id=project_id,
            arc_id=active_arc.id,
            delta=SubWorldPlanDelta.model_validate(delta),
            chapter_number=0,
            entity_map={},
        )

    def apply_initial_arc_plan(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        arc_id: str,
        arc_plan: dict,
        entity_map: dict[str, str],
    ) -> None:
        self.ensure_registry(session, project_id)
        delta = SubWorldPlanDelta.model_validate(
            arc_plan.get("subworld_delta")
            or self._fallback_initial_delta(arc_plan)
        )
        self.apply_arc_delta(
            session=session,
            updater=updater,
            project_id=project_id,
            arc_id=arc_id,
            delta=delta,
            chapter_number=0,
            entity_map=entity_map,
        )

    def apply_arc_delta(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        arc_id: str,
        delta: SubWorldPlanDelta,
        chapter_number: int,
        entity_map: dict[str, str] | None = None,
    ) -> SubWorldPlanDelta:
        entity_map = dict(entity_map or {})
        global_core_id = self.ensure_registry(session, project_id)
        roster_lookup = self._roster_lookup(session, project_id)
        existing_names = self._entity_name_map(session, project_id)
        actual_active_ids: list[str] = []

        for subworld_id in delta.reuse_subworld_ids:
            if subworld_id == global_core_id or session.get(SubWorld, subworld_id) is not None:
                actual_active_ids.append(subworld_id)

        for subworld_id in delta.retire_subworld_ids:
            row = session.get(SubWorld, subworld_id)
            if row is None or row.scope == "global_core":
                continue
            row.status = "retired"
            row.retired_at_chapter = max(0, int(chapter_number or 0))
            session.add(row)

        resolved_new_items: list[SubWorldPlanItem] = []
        for item in delta.new_subworlds:
            target_row = None
            if item.scope == "global_core":
                target_row = session.get(SubWorld, global_core_id)
            elif item.subworld_id:
                target_row = session.get(SubWorld, item.subworld_id)
            if target_row is None:
                metadata = {
                    "chapter_window_hint": item.chapter_window_hint,
                    "region_drafts": [
                        seed.model_dump(mode="json") if hasattr(seed, "model_dump") else seed
                        for seed in (item.region_seeds or [])
                    ],
                    "region_source": "runtime_generated" if item.region_seeds else "",
                    "region_promotion_state": "draft" if item.region_seeds else "",
                }
                target_row = SubWorld(
                    id=item.subworld_id or new_id(),
                    project_id=project_id,
                    origin_arc_id=arc_id,
                    parent_subworld_id=item.parent_subworld_id or None,
                    name=item.name,
                    purpose=item.purpose,
                    scope=item.scope,
                    status="active",
                    introduced_at_chapter=max(0, int(chapter_number or 0)),
                    retired_at_chapter=None,
                    metadata_json=json.dumps(metadata, ensure_ascii=False),
                )
                session.add(target_row)
                session.flush()
                if item.region_seeds:
                    self._persist_region_seeds(
                        session=session,
                        project_id=project_id,
                        subworld_id=target_row.id,
                        seeds=item.region_seeds,
                    )
            else:
                if item.purpose and not str(target_row.purpose or "").strip():
                    target_row.purpose = item.purpose
                meta = _load_json(target_row.metadata_json, {})
                if item.chapter_window_hint:
                    meta["chapter_window_hint"] = item.chapter_window_hint
                if item.region_seeds:
                    meta["region_drafts"] = [
                        seed.model_dump(mode="json") if hasattr(seed, "model_dump") else seed
                        for seed in (item.region_seeds or [])
                    ]
                    meta["region_source"] = "runtime_generated"
                    meta["region_promotion_state"] = "draft"
                target_row.metadata_json = json.dumps(meta, ensure_ascii=False)
                target_row.status = "active"
                session.add(target_row)
                if item.region_seeds:
                    self._persist_region_seeds(
                        session=session,
                        project_id=project_id,
                        subworld_id=target_row.id,
                        seeds=item.region_seeds,
                    )

            for seed in item.core_named_characters:
                entity = None
                entity_id = entity_map.get(seed.name) or existing_names.get(seed.name)
                if entity_id:
                    entity = session.get(Entity, entity_id)
                if entity is None:
                    entity = updater.create_entity(
                        project_id=project_id,
                        kind="character",
                        name=seed.name,
                        description=seed.description,
                        aliases=seed.aliases,
                        importance=max(1, int(seed.importance or 5)),
                        chapter=max(0, int(chapter_number or 0)),
                    )
                    if seed.initial_state:
                        updater.create_entity_state(entity.id, max(0, int(chapter_number or 0)), seed.initial_state)
                    entity_map[entity.name] = entity.id
                    existing_names[entity.name] = entity.id
                self._ensure_roster_item(
                    session=session,
                    project_id=project_id,
                    subworld_id=target_row.id,
                    roster_lookup=roster_lookup,
                    entity_id=entity.id,
                    display_name=seed.name,
                    slot_key="",
                    role_hint=seed.role_hint,
                    description=seed.description,
                    is_core=True,
                    status="seeded_named",
                    activation_chapter=max(0, int(chapter_number or 0)),
                )

            for slot in item.planned_slots:
                self._ensure_roster_item(
                    session=session,
                    project_id=project_id,
                    subworld_id=target_row.id,
                    roster_lookup=roster_lookup,
                    entity_id="",
                    display_name="",
                    slot_key=slot.slot_key,
                    role_hint=slot.role_hint,
                    description=slot.description,
                    is_core=False,
                    status="planned_slot",
                    activation_chapter=0,
                )
            resolved_new_items.append(item.model_copy(update={"subworld_id": target_row.id}))
            if target_row.id not in actual_active_ids and target_row.scope == "global_core":
                actual_active_ids.append(target_row.id)

        normalized_initial_ids: list[str] = []
        for subworld_id in delta.initial_active_subworld_ids:
            if subworld_id == global_core_id or session.get(SubWorld, subworld_id) is not None:
                normalized_initial_ids.append(subworld_id)
        if global_core_id not in normalized_initial_ids:
            normalized_initial_ids.insert(0, global_core_id)
        session.flush()
        return SubWorldPlanDelta(
            reuse_subworld_ids=list(dict.fromkeys(delta.reuse_subworld_ids)),
            retire_subworld_ids=[
                subworld_id
                for subworld_id in delta.retire_subworld_ids
                if subworld_id != global_core_id
            ],
            new_subworlds=resolved_new_items,
            initial_active_subworld_ids=list(dict.fromkeys(normalized_initial_ids)),
        )

    def _persist_region_seeds(
        self,
        *,
        session: Session,
        project_id: str,
        subworld_id: str,
        seeds: Iterable,
    ) -> None:
        repo = MapRepository(session)
        for seed in seeds:
            payload = seed.model_dump(mode="json") if hasattr(seed, "model_dump") else dict(seed)
            name = str(payload.get("name", "") or "").strip()
            if not name:
                continue
            region_id = "region_" + md5(f"{project_id}:{subworld_id}:{name}".encode("utf-8")).hexdigest()[:16]
            terrain_raw = payload.get("terrain", [])
            terrain = (
                ",".join(str(item) for item in terrain_raw if str(item).strip())
                if isinstance(terrain_raw, list)
                else str(terrain_raw or "")
            )
            culture_raw = payload.get("culture_traits", [])
            culture_tag = (
                ",".join(str(item) for item in culture_raw if str(item).strip())
                if isinstance(culture_raw, list)
                else str(culture_raw or "")
            )
            repo.upsert_region(
                RegionNode(
                    id=region_id,
                    project_id=project_id,
                    subworld_id=subworld_id,
                    region_type=str(payload.get("kind", "") or "local_region"),
                    name=name,
                    description=str(payload.get("summary", "") or ""),
                    terrain=terrain,
                    culture_tag=culture_tag,
                    metadata={
                        **payload,
                        "legacy_source": "SubWorldPlanItem.region_seeds",
                    },
                )
            )

    def plan_band_activation(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        chapter_start: int,
        chapter_end: int,
        active_band: Iterable[ChapterPlan],
    ) -> BandActivationPlan:
        global_core_id = self.ensure_registry(session, project_id)
        subworlds = session.execute(
            select(SubWorld)
            .where(
                SubWorld.project_id == project_id,
                SubWorld.status == "active",
            )
            .order_by(SubWorld.scope.asc(), SubWorld.created_at.asc())
        ).scalars().all()
        band_plans = list(active_band)
        flattened_band_text = " ".join(
            part
            for plan in band_plans
            for part in [plan.title, plan.one_line, *load_goals_json(plan.goals_json)]
            if str(part or "").strip()
        )

        active_ids = [global_core_id]
        arc_local_candidates: list[tuple[int, SubWorld]] = []
        for row in subworlds:
            if row.scope == "global_core":
                continue
            score = self._score_subworld_for_band(
                row=row,
                band_text=flattened_band_text,
                chapter_start=chapter_start,
                chapter_end=chapter_end,
                session=session,
            )
            if score > 0:
                arc_local_candidates.append((score, row))
        arc_local_candidates.sort(key=lambda item: (-item[0], item[1].created_at, item[1].id))
        active_ids.extend(row.id for _, row in arc_local_candidates[:2])

        for subworld_id in active_ids:
            roster_items = session.execute(
                select(SubWorldRosterItem)
                .where(SubWorldRosterItem.subworld_id == subworld_id)
                .order_by(SubWorldRosterItem.is_core.desc(), SubWorldRosterItem.created_at.asc())
            ).scalars().all()
            for item in roster_items:
                if item.entity_kind != "character" or not item.is_core:
                    continue
                updater.materialize_roster_item(
                    roster_item_id=item.id,
                    chapter=chapter_start,
                )

        chapter_numbers = list(range(chapter_start, chapter_end + 1))
        entry_targets: list[ChapterEntryTarget] = []
        used_chapters: set[int] = set()
        for subworld_id in active_ids:
            if subworld_id == global_core_id:
                continue
            if len(entry_targets) >= 2:
                break
            planned_slots = session.execute(
                select(SubWorldRosterItem)
                .where(
                    SubWorldRosterItem.subworld_id == subworld_id,
                    SubWorldRosterItem.status == "planned_slot",
                    SubWorldRosterItem.entity_kind == "character",
                )
                .order_by(SubWorldRosterItem.created_at.asc(), SubWorldRosterItem.id.asc())
            ).scalars().all()
            for slot in planned_slots:
                chapter_hint = next(
                    (number for number in chapter_numbers if number not in used_chapters),
                    0,
                )
                if chapter_hint <= 0:
                    break
                entity = updater.materialize_roster_item(
                    roster_item_id=slot.id,
                    chapter=chapter_hint,
                )
                entry_targets.append(
                    ChapterEntryTarget(
                        chapter_hint=chapter_hint,
                        entity_name=entity.name,
                        subworld_id=subworld_id,
                        role_hint=slot.role_hint,
                    )
                )
                used_chapters.add(chapter_hint)
                if len(entry_targets) >= 2:
                    break

        return BandActivationPlan(
            active_subworld_ids=list(dict.fromkeys(active_ids)),
            chapter_entry_targets=entry_targets,
        )

    def _score_subworld_for_band(
        self,
        *,
        row: SubWorld,
        band_text: str,
        chapter_start: int,
        chapter_end: int,
        session: Session,
    ) -> int:
        score = 0
        meta = _load_json(row.metadata_json, {})
        hint = str(meta.get("chapter_window_hint", "")).strip()
        if self._window_overlaps(hint, chapter_start=chapter_start, chapter_end=chapter_end):
            score += 3
        if str(row.purpose or "").strip() and str(row.purpose).strip() in band_text:
            score += 2
        if str(row.name or "").strip() and str(row.name).strip() in band_text:
            score += 2
        role_hints = session.execute(
            select(SubWorldRosterItem.role_hint)
            .where(SubWorldRosterItem.subworld_id == row.id)
        ).all()
        for role_hint, in role_hints:
            token = str(role_hint or "").strip()
            if token and token in band_text:
                score += 1
                break
        if score <= 0 and not hint:
            score = 1
        return score

    def _window_overlaps(self, hint: str, *, chapter_start: int, chapter_end: int) -> bool:
        text = str(hint or "").strip().lower()
        if not text:
            return False
        digits = [int(part) for part in text.replace("~", "-").split("-") if part.isdigit()]
        if len(digits) >= 2:
            start, end = digits[0], digits[1]
            return not (end < chapter_start or start > chapter_end)
        if any(token in text for token in ("opening", "early", "前期", "开篇", "起始")):
            return chapter_start <= 3
        if any(token in text for token in ("mid", "midpoint", "中段", "中期")):
            return chapter_start >= 2
        if any(token in text for token in ("late", "后期", "末段")):
            return chapter_end >= 3
        return False

    def _roster_lookup(self, session: Session, project_id: str) -> dict[tuple[str, str, str], str]:
        items = session.execute(
            select(SubWorldRosterItem)
            .where(SubWorldRosterItem.project_id == project_id)
        ).scalars().all()
        mapping: dict[tuple[str, str, str], str] = {}
        for item in items:
            entity_key = str(item.entity_id or "").strip()
            if entity_key:
                mapping[(item.subworld_id, "entity", entity_key)] = item.id
            slot_key = str(item.slot_key or "").strip()
            if slot_key:
                mapping[(item.subworld_id, "slot", slot_key)] = item.id
        return mapping

    def _entity_name_map(self, session: Session, project_id: str) -> dict[str, str]:
        entities = session.execute(
            select(Entity)
            .where(Entity.project_id == project_id)
        ).scalars().all()
        return {
            entity.name: entity.id
            for entity in entities
            if str(entity.name or "").strip()
        }

    def _ensure_roster_item(
        self,
        *,
        session: Session,
        project_id: str,
        subworld_id: str,
        roster_lookup: dict[tuple[str, str, str], str],
        entity_id: str,
        display_name: str,
        slot_key: str,
        role_hint: str,
        description: str,
        is_core: bool,
        status: str,
        activation_chapter: int,
    ) -> None:
        key = (
            (subworld_id, "entity", entity_id)
            if str(entity_id or "").strip()
            else (subworld_id, "slot", str(slot_key or "").strip())
        )
        existing_id = roster_lookup.get(key)
        if existing_id:
            row = session.get(SubWorldRosterItem, existing_id)
            if row is not None:
                if entity_id and not str(row.entity_id or "").strip():
                    row.entity_id = entity_id
                if display_name and not str(row.display_name or "").strip():
                    row.display_name = display_name
                if role_hint and not str(row.role_hint or "").strip():
                    row.role_hint = role_hint
                if description and not str(row.description or "").strip():
                    row.description = description
                row.is_core = row.is_core or is_core
                row.status = status if row.status == "planned_slot" else row.status
                if activation_chapter and not row.activation_chapter:
                    row.activation_chapter = activation_chapter
                session.add(row)
            return
        row = SubWorldRosterItem(
            id=new_id(),
            project_id=project_id,
            subworld_id=subworld_id,
            entity_id=entity_id or None,
            entity_kind="character",
            display_name=display_name,
            slot_key=slot_key,
            role_hint=role_hint,
            description=description,
            is_core=is_core,
            status=status,
            activation_chapter=activation_chapter,
            metadata_json="{}",
        )
        session.add(row)
        session.flush()
        if entity_id:
            roster_lookup[(subworld_id, "entity", entity_id)] = row.id
        elif slot_key:
            roster_lookup[(subworld_id, "slot", slot_key)] = row.id

    def fallback_slot_name(self, *, project_id: str, subworld_id: str, slot_key: str, role_hint: str) -> str:
        payload = f"{project_id}:{subworld_id}:{slot_key}:{role_hint}".encode("utf-8")
        digest = md5(payload).hexdigest()
        surname = _NAME_SURNAMES[int(digest[:2], 16) % len(_NAME_SURNAMES)]
        given = _NAME_GIVEN[int(digest[2:4], 16) % len(_NAME_GIVEN)]
        return f"{surname}{given}"

    def _fallback_initial_delta(self, arc_plan: dict) -> SubWorldPlanDelta:
        return SubWorldPlanDelta(
            reuse_subworld_ids=[],
            retire_subworld_ids=[],
            # Older/custom ArcDirector outputs may not provide subworld planning.
            # In that case, keep bootstrap conservative and avoid inventing
            # canonical people or slots that were never present in the arc plan.
            new_subworlds=[],
            initial_active_subworld_ids=[],
        )

    @staticmethod
    def looks_like_named_character(name: str) -> bool:
        text = str(name or "").strip()
        if not text:
            return False
        if text in _GENERIC_CHARACTER_TOKENS:
            return False
        return len(text) <= 12
