from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import (
    Entity,
    NPCIntentSnapshot,
    PlotThread,
    Project,
    PublisherRawComment,
    WorldSimulationTurn,
    new_id,
)
from forwin.orchestrator.thread_sampling import sample_active_threads
from forwin.state.query_helpers import load_latest_entity_states
from forwin.utils import parse_llm_json

logger = logging.getLogger(__name__)

_POSITIVE_COMMENT_KEYWORDS = ("喜欢", "精彩", "好看", "期待", "爽", "牛", "神")
_NEGATIVE_COMMENT_KEYWORDS = ("水", "拖", "崩", "失望", "弃", "烂", "短", "乱")
_QUESTION_COMMENT_KEYWORDS = ("为什么", "怎么", "是不是", "会不会", "求", "能不能")


@dataclass(slots=True)
class NPCIntentDraft:
    entity_id: str
    entity_name: str
    intent_kind: str
    objective: str
    tactic: str
    urgency: int
    notes: str


@dataclass(slots=True)
class WorldTurnDraft:
    pressure_level: str
    pressure_summary: str
    notable_shifts: list[str]


class NPCIntentGenerator:
    def __init__(self, *, llm_client=None, active_thread_limit: int = 20) -> None:
        self.llm_client = llm_client
        self.active_thread_limit = max(1, int(active_thread_limit))

    def generate(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        limit: int = 5,
    ) -> list[NPCIntentDraft]:
        project = session.get(Project, project_id)
        entities = session.execute(
            select(Entity)
            .where(
                Entity.project_id == project_id,
                Entity.kind == "character",
                Entity.is_active == True,  # noqa: E712
            )
            .order_by(Entity.importance.desc(), Entity.created_at_chapter.asc())
            .limit(limit)
        ).scalars().all()

        active_threads = sample_active_threads(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            limit=min(2, self.active_thread_limit),
            stale_window=2,
            recent_window=2,
        ).threads
        thread_focus = "、".join(thread.name for thread in active_threads) or "当前主线"
        feedback = _load_reader_feedback(session, project.title if project else "", limit=6)

        llm_intents = self._generate_with_llm(
            entities=entities,
            active_threads=active_threads,
            chapter_number=chapter_number,
            thread_focus=thread_focus,
            feedback_summary=str(feedback.get("summary") or ""),
        )
        if llm_intents is not None:
            return llm_intents

        intents: list[NPCIntentDraft] = []
        latest_states = load_latest_entity_states(session, [entity.id for entity in entities])
        for index, entity in enumerate(entities):
            latest_state = latest_states.get(entity.id)
            state = {}
            if latest_state is not None:
                try:
                    state = json.loads(latest_state.state_json or "{}") or {}
                except (json.JSONDecodeError, TypeError):
                    state = {}
            status = str(state.get("status", "normal") or "normal")
            location = str(state.get("location", "") or "")

            intent_kind = "pressure" if index == 0 else "pursue"
            urgency = max(1, min(5, 5 - index))
            objective = (
                f"围绕{thread_focus}采取下一步行动，争取在下一章改变局面。"
                if status in {"normal", "active", ""}
                else f"在{status}状态下寻找翻盘机会，仍然围绕{thread_focus}行动。"
            )
            tactic_parts = []
            if location:
                tactic_parts.append(f"优先利用{location}的地利")
            if index == 0:
                tactic_parts.append("主动制造信息差")
            else:
                tactic_parts.append("跟进主角留下的线索")
            if feedback.get("dominant_sentiment") == "negative":
                urgency = min(5, urgency + 1)
                tactic_parts.append("尽快回应读者对推进节奏的担忧")
            elif feedback.get("dominant_sentiment") == "curious":
                tactic_parts.append("优先回应读者最关心的悬念")
            notes = f"重要度{entity.importance}，第{chapter_number + 1}章前生效。"
            intents.append(
                NPCIntentDraft(
                    entity_id=entity.id,
                    entity_name=entity.name,
                    intent_kind=intent_kind,
                    objective=objective,
                    tactic="；".join(tactic_parts),
                    urgency=urgency,
                    notes=notes,
                )
            )
        return intents

    def _generate_with_llm(
        self,
        *,
        entities: list[Entity],
        active_threads: list[PlotThread],
        chapter_number: int,
        thread_focus: str,
        feedback_summary: str,
    ) -> list[NPCIntentDraft] | None:
        if self.llm_client is None or not entities:
            return None
        entity_payload = [
            {
                "entity_id": entity.id,
                "entity_name": entity.name,
                "importance": entity.importance,
            }
            for entity in entities
        ]
        prompt = [
            {
                "role": "system",
                "content": "你是网文角色调度器，只输出 JSON，不要解释。",
            },
            {
                "role": "user",
                "content": (
                    "请为下一章生成 NPC 意图，只输出 JSON。\n"
                    f"当前章节：第 {chapter_number} 章\n"
                    f"主线焦点：{thread_focus}\n"
                    f"活跃线程：{json.dumps([t.name for t in active_threads], ensure_ascii=False)}\n"
                    f"读者反馈摘要：{feedback_summary}\n"
                    f"候选角色：{json.dumps(entity_payload, ensure_ascii=False)}\n\n"
                    "返回格式："
                    '{"intents":[{"entity_name":"角色名","intent_kind":"pursue|pressure|evade|ally","objective":"一句中文目标","tactic":"一句中文策略","urgency":1,"notes":"补充说明"}]}'
                ),
            },
        ]
        try:
            raw = self.llm_client.chat(
                prompt,
                temperature=0.45,
                max_tokens=900,
                response_format={"type": "json_object"},
            )
        except TypeError as exc:
            if "response_format" not in str(exc):
                logger.warning("Phase4 NPC LLM call failed.", exc_info=True)
                return None
            try:
                raw = self.llm_client.chat(
                    prompt,
                    temperature=0.45,
                    max_tokens=900,
                )
            except Exception:
                logger.warning("Phase4 NPC LLM fallback call failed.", exc_info=True)
                return None
        except Exception:
            logger.warning("Phase4 NPC LLM call failed.", exc_info=True)
            return None
        try:
            payload = parse_llm_json(raw, error_prefix="NPC intent parser")
        except Exception:
            logger.warning("Phase4 NPC intent parse failed.", exc_info=True)
            return None
        entity_map = {entity.name: entity for entity in entities}
        intents: list[NPCIntentDraft] = []
        for row in payload.get("intents") or []:
            if not isinstance(row, dict):
                continue
            entity = entity_map.get(str(row.get("entity_name", "")).strip())
            if entity is None:
                continue
            try:
                intents.append(
                    NPCIntentDraft(
                        entity_id=entity.id,
                        entity_name=entity.name,
                        intent_kind=str(row.get("intent_kind") or "pursue"),
                        objective=str(row.get("objective") or "").strip(),
                        tactic=str(row.get("tactic") or "").strip(),
                        urgency=max(1, min(5, int(row.get("urgency") or 3))),
                        notes=str(row.get("notes") or "").strip(),
                    )
                )
            except Exception:
                continue
        return intents or None


class WorldSimulator:
    def __init__(self, *, llm_client=None, active_thread_limit: int = 20) -> None:
        self.llm_client = llm_client
        self.active_thread_limit = max(1, int(active_thread_limit))

    def simulate(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> WorldTurnDraft:
        project = session.get(Project, project_id)
        sampled = sample_active_threads(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            limit=self.active_thread_limit,
            stale_window=2,
            recent_window=2,
        )
        active_threads = sampled.threads
        latest_beats = sampled.latest_beats
        stale_threads: list[str] = []
        for thread in active_threads:
            last_beat = latest_beats.get(thread.id)
            reference_chapter = (
                last_beat.chapter_number if last_beat is not None else thread.opened_at_chapter
            )
            if chapter_number - reference_chapter >= 2:
                stale_threads.append(thread.name)
        feedback = _load_reader_feedback(session, project.title if project else "", limit=8)

        llm_turn = self._simulate_with_llm(
            chapter_number=chapter_number,
            active_threads=active_threads,
            stale_threads=stale_threads,
            feedback_summary=str(feedback.get("summary") or ""),
        )
        if llm_turn is not None:
            return llm_turn

        pressure_level = "steady"
        shifts: list[str] = []
        if stale_threads:
            pressure_level = "rising" if len(stale_threads) == 1 else "critical"
            shifts.append(f"悬置线程：{'、'.join(stale_threads)}")
        if feedback.get("dominant_sentiment") == "negative":
            pressure_level = "critical" if stale_threads else "rising"
            shifts.append("读者对最近推进节奏表达了明显担忧")
        elif feedback.get("dominant_sentiment") == "curious":
            if pressure_level == "steady":
                pressure_level = "rising"
            shifts.append("读者对悬念的追问正在放大世界压迫感")
        elif feedback.get("dominant_sentiment") == "positive":
            shifts.append("读者期待正在推高后续兑现压力")
        if chapter_number >= 3:
            shifts.append("世界正在对主角行动产生连锁反应")
            if pressure_level == "steady":
                pressure_level = "rising"
        if not shifts:
            shifts.append("主要矛盾仍处于可控推进状态")

        pressure_summary = (
            f"第{chapter_number}章后，世界压力为 {pressure_level}。"
            f"重点变化：{'；'.join(shifts)}。"
        )
        return WorldTurnDraft(
            pressure_level=pressure_level,
            pressure_summary=pressure_summary,
            notable_shifts=shifts,
        )

    def _simulate_with_llm(
        self,
        *,
        chapter_number: int,
        active_threads: list[PlotThread],
        stale_threads: list[str],
        feedback_summary: str,
    ) -> WorldTurnDraft | None:
        if self.llm_client is None:
            return None
        prompt = [
            {
                "role": "system",
                "content": "你是网文世界模拟器，只输出 JSON，不要解释。",
            },
            {
                "role": "user",
                "content": (
                    "请判断当前章节之后的世界压力，并输出下一章前的重要连锁反应。\n"
                    f"当前章节：第 {chapter_number} 章\n"
                    f"活跃线程数：{len(active_threads)}\n"
                    f"悬置线程：{json.dumps(stale_threads, ensure_ascii=False)}\n\n"
                    f"读者反馈摘要：{feedback_summary}\n\n"
                    '返回格式：{"pressure_level":"steady|rising|critical","pressure_summary":"一句中文总结","notable_shifts":["变化1","变化2"]}'
                ),
            },
        ]
        try:
            raw = self.llm_client.chat(
                prompt,
                temperature=0.35,
                max_tokens=700,
                response_format={"type": "json_object"},
            )
        except TypeError as exc:
            if "response_format" not in str(exc):
                logger.warning("Phase4 world LLM call failed.", exc_info=True)
                return None
            try:
                raw = self.llm_client.chat(
                    prompt,
                    temperature=0.35,
                    max_tokens=700,
                )
            except Exception:
                logger.warning("Phase4 world LLM fallback call failed.", exc_info=True)
                return None
        except Exception:
            logger.warning("Phase4 world LLM call failed.", exc_info=True)
            return None
        try:
            payload = parse_llm_json(raw, error_prefix="World simulator parser")
        except Exception:
            logger.warning("Phase4 world parse failed.", exc_info=True)
            return None
        level = str(payload.get("pressure_level") or "steady").strip().lower()
        if level not in {"steady", "rising", "critical"}:
            level = "steady"
        notable = [
            str(item).strip()
            for item in (payload.get("notable_shifts") or [])
            if str(item).strip()
        ]
        return WorldTurnDraft(
            pressure_level=level,
            pressure_summary=str(payload.get("pressure_summary") or "").strip()
            or f"第{chapter_number}章后，世界压力为 {level}。",
            notable_shifts=notable or ["世界仍在对主角行动做出反应"],
        )


def _load_reader_feedback(
    session: Session,
    project_title: str,
    *,
    limit: int = 6,
) -> dict[str, object]:
    title = str(project_title or "").strip()
    if not title:
        return {"dominant_sentiment": "neutral", "summary": "", "highlights": []}
    rows = session.execute(
        select(PublisherRawComment)
        .where(PublisherRawComment.work_name == title)
        .order_by(PublisherRawComment.synced_at.desc(), PublisherRawComment.updated_at.desc())
        .limit(limit)
    ).scalars().all()
    if not rows:
        return {"dominant_sentiment": "neutral", "summary": "", "highlights": []}

    positive = 0
    negative = 0
    curious = 0
    for row in rows:
        text = str(row.body_text or "")
        if any(keyword in text for keyword in _POSITIVE_COMMENT_KEYWORDS):
            positive += 1
        if any(keyword in text for keyword in _NEGATIVE_COMMENT_KEYWORDS):
            negative += 1
        if any(keyword in text for keyword in _QUESTION_COMMENT_KEYWORDS):
            curious += 1
    if negative > max(positive, curious):
        dominant = "negative"
    elif positive > max(negative, curious):
        dominant = "positive"
    elif curious:
        dominant = "curious"
    else:
        dominant = "neutral"
    return {
        "dominant_sentiment": dominant,
        "summary": f"最近 {len(rows)} 条评论，读者情绪以 {dominant} 为主。",
        "highlights": [str(row.body_text or "")[:120] for row in rows[:3]],
    }


def save_npc_intents(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    intents: list[NPCIntentDraft],
) -> None:
    for item in intents:
        session.add(
            NPCIntentSnapshot(
                id=new_id(),
                project_id=project_id,
                chapter_number=chapter_number,
                entity_id=item.entity_id,
                entity_name=item.entity_name,
                intent_kind=item.intent_kind,
                objective=item.objective,
                tactic=item.tactic,
                urgency=item.urgency,
                notes=item.notes,
            )
        )
    session.flush()


def save_world_turn(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    turn: WorldTurnDraft,
) -> None:
    session.add(
        WorldSimulationTurn(
            id=new_id(),
            project_id=project_id,
            chapter_number=chapter_number,
            pressure_level=turn.pressure_level,
            pressure_summary=turn.pressure_summary,
            notable_shifts_json=json.dumps(turn.notable_shifts, ensure_ascii=False),
        )
    )
    session.flush()
