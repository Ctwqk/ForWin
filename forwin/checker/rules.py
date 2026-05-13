"""Rule-based continuity checker for Phase 0.5."""
from __future__ import annotations
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forwin.state.repo import StateRepository

from forwin.protocol.writer import WriterOutput
from forwin.protocol.review import ReviewVerdict, ContinuityIssue
from forwin.governance import issue_group_for_issue
from forwin.canon_quality.placeholder import extract_expected_protagonist_names
from forwin.canon_names import extract_canon_name_anchors, find_canon_name_violations

logger = logging.getLogger(__name__)
DEAD_STATUS_KEYWORDS = {
    "dead",
    "deceased",
    "已死",
    "死亡",
    "死了",
    "身亡",
    "阵亡",
    "已阵亡",
}
GENERIC_CHARACTER_REFERENCES = {
    "路人",
    "守卫",
    "老板",
    "店小二",
    "师兄",
    "师姐",
    "弟子",
    "首席运营官",
    "运营负责人",
    "财务总监",
    "财务负责人",
    "法务负责人",
    "部门总监",
    "部门负责人",
    "集团高管",
    "同学",
    "众人",
    "人群",
    "旁人",
    "馆员",
    "管理员",
    "工作人员",
    "服务员",
    "追踪者",
    "不明追踪者",
    "无脸人",
    "手下",
    "下属",
    "部下",
    "同伙",
    "随从",
}
GENERIC_CHARACTER_ROLE_SUFFIXES = (
    "手下",
    "下属",
    "部下",
    "同伙",
    "随从",
    "技术员",
    "工程师",
    "程序员",
    "黑客",
    "线人",
    "中间人",
    "摊主",
    "追兵",
    "追踪者",
    "安保",
    "保镖",
    "警员",
    "警察",
    "巡检员",
    "员工",
    "主管",
)
POSSESSIVE_GENERIC_ROLE_SUFFIXES = (
    "手下",
    "下属",
    "部下",
    "同伙",
    "随从",
    "队员",
    "巡检员",
    "追兵",
    "追踪者",
    "守卫",
    "保镖",
    "安保",
    "员工",
)
NON_CHARACTER_NAME_KEYWORDS = (
    "集团",
    "公司",
    "机构",
    "报社",
    "系统",
    "账本",
    "记忆馆",
    "旧港",
    "火灾",
    "事故",
    "码头",
    "咖啡馆",
    "档案",
    "论坛",
    "市场",
    "大楼",
    "实验室",
    "实验区",
)
RELATIONAL_REFERENCE_SUFFIXES = (
    "母亲",
    "父亲",
    "妈妈",
    "爸爸",
    "姐姐",
    "妹妹",
    "哥哥",
    "弟弟",
    "的母亲",
    "的父亲",
    "的妈妈",
    "的爸爸",
    "的姐姐",
    "的妹妹",
    "的哥哥",
    "的弟弟",
)
ABSENCE_ONLY_CHANGE_KEYWORDS = (
    "不存在",
    "消失",
    "抹除",
    "删除",
    "讣告",
    "记录",
    "死亡",
    "已死",
    "遇难",
    "遇难者",
    "死者",
    "遗体",
    "死因",
    "死亡证明",
)
BODY_TERMINAL_PUNCTUATION = set("。！？!?…")
BODY_TRAILING_CLOSERS = set("”’」』）)]》】")


class ContinuityChecker:
    """Checks chapter output for basic continuity issues."""

    def __init__(self, repo: StateRepository, min_chars: int = 2500, max_chars: int = 3200):
        self.repo = repo
        self.min_chars = min_chars
        self.max_chars = max_chars

    def check(self, project_id: str, writer_output: WriterOutput) -> ReviewVerdict:
        """Run all continuity checks and return a verdict."""
        issues: list[ContinuityIssue] = []

        # Run all checks
        issues.extend(self._check_char_count(writer_output))
        issues.extend(self._check_empty_body(writer_output))
        issues.extend(self._check_body_completion(writer_output))
        issues.extend(self._check_canon_name_anchors(project_id, writer_output))
        issues.extend(self._check_dead_characters(project_id, writer_output))
        issues.extend(self._check_thread_status(project_id, writer_output))
        issues.extend(self._check_state_change_validity(writer_output))
        issues.extend(self._check_event_completeness(writer_output))
        issues.extend(self._check_subworld_admission(project_id, writer_output))

        # Determine verdict
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]

        if errors:
            verdict = "fail"
        elif warnings:
            verdict = "warn"
        else:
            verdict = "pass"

        return ReviewVerdict(
            verdict=verdict,
            issues=issues,
            recommended_action="rewrite" if verdict == "fail" else "continue",
            review_summary=f"continuity issues={len(issues)}",
        )

    def _check_char_count(self, output: WriterOutput) -> list[ContinuityIssue]:
        """Check if chapter body is within acceptable length."""
        issues = []
        char_count = len(output.body)

        if char_count < self.min_chars:
            issues.append(ContinuityIssue(
                rule_name="char_count_low",
                severity="warning",
                description=f"章节正文仅{char_count}字，低于最低要求{self.min_chars}字",
                reviewer="continuity",
                issue_type="continuity",
                target_scope="chapter",
            ))
        elif char_count > self.max_chars * 1.5:  # Allow some overflow but flag extreme
            issues.append(ContinuityIssue(
                rule_name="char_count_high",
                severity="warning",
                description=f"章节正文{char_count}字，远超目标{self.max_chars}字",
                reviewer="continuity",
                issue_type="continuity",
                target_scope="chapter",
            ))

        return issues

    def _check_canon_name_anchors(self, project_id: str, output: WriterOutput) -> list[ContinuityIssue]:
        anchors = self._canon_name_anchors(project_id)
        if not anchors:
            return []
        violations = find_canon_name_violations(
            self._canon_name_scan_text(output),
            anchors,
        )
        issues: list[ContinuityIssue] = []
        for violation in violations:
            issues.append(
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description=(
                        f"{violation.role_label}的 canon 姓名是「{violation.canonical_name}」，"
                        f"本章写成了「{violation.observed_name}」。"
                    ),
                    entity_names=[violation.observed_name, violation.canonical_name],
                    reviewer="continuity",
                    issue_type="continuity",
                    target_scope="chapter",
                    issue_group=issue_group_for_issue(
                        issue_type="continuity",
                        rule_name="canon_name_drift",
                    ),
                    evidence_refs=[f"body:{violation.evidence}", f"reason={violation.reason}"],
                    suggested_fix=(
                        f"凡指代{violation.role_label}姓名时必须逐字沿用「{violation.canonical_name}」，"
                        f"删除或替换「{violation.observed_name}」等变体。"
                    ),
                )
            )
        return issues

    def _canon_name_anchors(self, project_id: str):
        get_active_threads = getattr(self.repo, "get_active_threads", None)
        if not callable(get_active_threads):
            return []
        try:
            threads = list(get_active_threads(project_id) or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("canon name anchor check skipped: %s", exc)
            return []
        texts: list[str] = []
        for thread in threads:
            texts.append(str(getattr(thread, "description", "") or ""))
            texts.extend(str(beat or "") for beat in (getattr(thread, "recent_beats", []) or []))
        return extract_canon_name_anchors(texts)

    @staticmethod
    def _canon_name_scan_text(output: WriterOutput) -> str:
        payload = output.model_dump(mode="json", exclude={"generation_meta"})
        return "\n".join(
            [
                str(output.body or ""),
                str(output.end_of_chapter_summary or ""),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ]
        )

    def _check_empty_body(self, output: WriterOutput) -> list[ContinuityIssue]:
        """Check if body is empty or trivially short."""
        if len(output.body.strip()) < 100:
            return [ContinuityIssue(
                rule_name="empty_body",
                severity="error",
                description="章节正文为空或过短（不足100字）",
                reviewer="continuity",
                issue_type="continuity",
                target_scope="chapter",
                evidence_refs=[f"body_chars={len(output.body.strip())}"],
            )]
        return []

    def _check_body_completion(self, output: WriterOutput) -> list[ContinuityIssue]:
        """Detect drafts that appear to end in the middle of a sentence."""
        body = output.body.strip()
        if len(body) < 100:
            return []
        normalized_tail = body
        while normalized_tail and normalized_tail[-1] in BODY_TRAILING_CLOSERS:
            normalized_tail = normalized_tail[:-1].rstrip()
        if normalized_tail and normalized_tail[-1] in BODY_TERMINAL_PUNCTUATION:
            return []
        if self._looks_like_repeated_placeholder_body(body):
            return []
        tail = body[-40:]
        return [
            ContinuityIssue(
                rule_name="body_incomplete_ending",
                severity="error",
                description="章节正文结尾缺少完整句末标点，疑似在句中被截断。",
                reviewer="continuity",
                issue_type="continuity",
                target_scope="chapter",
                issue_group=issue_group_for_issue(
                    issue_type="continuity",
                    rule_name="body_incomplete_ending",
                ),
                evidence_refs=[f"ending={tail}"],
                suggested_fix="补完整本章最后一句或重写收束段，确保正文以完整句子结束。",
            )
        ]

    @staticmethod
    def _looks_like_repeated_placeholder_body(body: str) -> bool:
        text = str(body or "").strip()
        if len(text) < 200:
            return False
        if any(ch in text for ch in BODY_TERMINAL_PUNCTUATION):
            return False
        max_unit_len = 12
        for unit_len in range(1, min(max_unit_len, len(text)) + 1):
            unit = text[:unit_len]
            if not unit.strip():
                continue
            repeats, remainder = divmod(len(text), unit_len)
            if repeats < 20:
                continue
            if unit * repeats + unit[:remainder] == text:
                return True
        return False

    def _check_dead_characters(self, project_id: str, output: WriterOutput) -> list[ContinuityIssue]:
        """Check if dead characters are being used as active participants."""
        issues = []

        # Get entities that are dead/inactive
        entities = self.repo.get_active_entities(project_id)
        dead_names = set()
        for e in entities:
            state = e.current_state
            if isinstance(state, dict):
                status = state.get("status", "")
                normalized_status = str(status).strip()
                if (
                    normalized_status in DEAD_STATUS_KEYWORDS
                    or normalized_status.lower() in DEAD_STATUS_KEYWORDS
                ):
                    dead_names.add(e.name)
                    dead_names.update(e.aliases)

        if not dead_names:
            return issues

        # Check if dead characters appear in new events as active participants
        for event in output.new_events:
            for i, name in enumerate(event.involved_entity_names):
                if name in dead_names:
                    role = event.roles[i] if i < len(event.roles) else "unknown"
                    if role in ("protagonist", "antagonist"):
                        issues.append(ContinuityIssue(
                            rule_name="dead_character_active",
                            severity="error",
                            description=f"已死亡角色「{name}」在事件中被标记为{role}",
                            entity_names=[name],
                            reviewer="continuity",
                            issue_type="continuity",
                            target_scope="scene",
                            evidence_refs=[f"event={event.summary[:60]}", f"entity={name}", f"role={role}"],
                        ))

        return issues

    def _check_thread_status(self, project_id: str, output: WriterOutput) -> list[ContinuityIssue]:
        """Check if beat candidates reference resolved/abandoned threads."""
        issues = []

        for beat in output.thread_beats:
            thread = self.repo.get_thread_by_name(project_id, beat.thread_name)
            if thread and thread.status in ("resolved", "abandoned"):
                issues.append(ContinuityIssue(
                    rule_name="thread_already_closed",
                    severity="warning",
                    description=f"情节线「{beat.thread_name}」已{thread.status}，但本章仍有相关推进",
                    entity_names=[],
                    reviewer="continuity",
                    issue_type="continuity",
                    target_scope="chapter",
                ))

        return issues

    def _check_state_change_validity(self, output: WriterOutput) -> list[ContinuityIssue]:
        """Basic validation of state changes."""
        issues = []

        for sc in output.state_changes:
            if not sc.entity_name.strip():
                issues.append(ContinuityIssue(
                    rule_name="empty_entity_name",
                    severity="warning",
                    description="状态变更中存在空的实体名称",
                    reviewer="continuity",
                    issue_type="continuity",
                    target_scope="chapter",
                ))
            if not sc.field.strip():
                issues.append(ContinuityIssue(
                    rule_name="empty_field_name",
                    severity="warning",
                    description=f"实体「{sc.entity_name}」的状态变更中字段名为空",
                    entity_names=[sc.entity_name],
                    reviewer="continuity",
                    issue_type="continuity",
                    target_scope="chapter",
                ))

        return issues

    def _check_event_completeness(self, output: WriterOutput) -> list[ContinuityIssue]:
        """Check if events have proper structure."""
        issues = []

        for event in output.new_events:
            if len(event.involved_entity_names) != len(event.roles):
                issues.append(ContinuityIssue(
                    rule_name="event_role_mismatch",
                    severity="warning",
                    description=f"事件「{event.summary[:30]}」的参与者数量与角色数量不匹配",
                    reviewer="continuity",
                    issue_type="continuity",
                    target_scope="chapter",
                ))

        return issues

    def _check_subworld_admission(self, project_id: str, output: WriterOutput) -> list[ContinuityIssue]:
        allowed_names = {
            self._normalize_character_reference(name)
            for name in self.repo.get_allowed_entity_names(project_id, output.chapter_number)
        }
        allowed_names.update(
            self._normalize_character_reference(anchor.canonical_name)
            for anchor in self._canon_name_anchors(project_id)
        )
        allowed_names.update(self._known_character_names(project_id))
        allowed_names.update(self._project_protagonist_names(project_id))
        if not allowed_names:
            return []
        candidate_names: set[str] = set()
        maybe_event_names: set[str] = set()

        for mention in getattr(output, "entity_mentions", []):
            if (
                getattr(mention, "entity_kind", "") == "character"
                and bool(getattr(mention, "is_named", False))
                and bool(getattr(mention, "is_on_stage", True))
            ):
                name = self._candidate_character_name(getattr(mention, "entity_name", ""))
                if name:
                    candidate_names.add(name)

        for change in output.state_changes:
            if (
                change.entity_kind == "character"
                and not self._is_absence_only_state_change(change)
            ):
                name = self._candidate_character_name(change.entity_name)
                if name:
                    candidate_names.add(name)

        for event in output.new_events:
            for name in event.involved_entity_names:
                candidate = self._candidate_character_name(name)
                if candidate:
                    maybe_event_names.add(candidate)

        for scene in output.scene_outputs:
            for name in scene.involved_entities:
                candidate = self._candidate_character_name(name)
                if candidate:
                    candidate_names.add(candidate)

        if maybe_event_names:
            resolved = self.repo.get_entities_by_names(project_id, sorted(maybe_event_names))
            for name in maybe_event_names:
                entity = resolved.get(name)
                if entity is not None and entity.kind == "character":
                    candidate_names.add(name)

        issues: list[ContinuityIssue] = []
        for name in sorted(candidate_names):
            if name in allowed_names:
                continue
            issues.append(
                ContinuityIssue(
                    rule_name="sub_world_unknown_named_entity",
                    severity="error",
                    description=f"命名角色「{name}」未在当前 chapter 的 subworld 准入名单中。",
                    entity_names=[name],
                    reviewer="continuity",
                    issue_type="subworld_admission",
                    target_scope="chapter",
                    issue_group=issue_group_for_issue(issue_type="director_imbalance", rule_name="sub_world_unknown_named_entity"),
                    evidence_refs=[f"chapter={output.chapter_number}", f"entity={name}"],
                    suggested_fix="改用允许名单中的角色，或改写为无名泛称角色。",
                )
            )
        return issues

    def _project_protagonist_names(self, project_id: str) -> set[str]:
        get_project = getattr(self.repo, "get_project", None)
        if not callable(get_project):
            return set()
        try:
            project = get_project(project_id)
        except Exception:  # noqa: BLE001
            return set()
        return {
            self._normalize_character_reference(name)
            for name in extract_expected_protagonist_names(
                str(getattr(project, "premise", "") or ""),
                str(getattr(project, "setting_summary", "") or ""),
            )
            if str(name or "").strip()
        }

    def _known_character_names(self, project_id: str) -> set[str]:
        get_active_entities = getattr(self.repo, "get_active_entities", None)
        if not callable(get_active_entities):
            return set()
        try:
            entities = get_active_entities(project_id)
        except Exception:  # noqa: BLE001
            return set()
        names: set[str] = set()
        for entity in entities or []:
            if str(getattr(entity, "kind", "") or "") != "character":
                continue
            raw_names = [getattr(entity, "name", "") or "", *(getattr(entity, "aliases", []) or [])]
            for raw_name in raw_names:
                name = self._normalize_character_reference(str(raw_name or ""))
                if name:
                    names.add(name)
        return names

    @staticmethod
    def _looks_like_named_character(name: str) -> bool:
        text = ContinuityChecker._normalize_character_reference(name)
        if not text or ContinuityChecker._looks_like_generic_character_reference(text):
            return False
        if ContinuityChecker._looks_like_non_character_reference(text):
            return False
        return len(text) <= 12

    @staticmethod
    def _candidate_character_name(name: str) -> str:
        text = ContinuityChecker._normalize_character_reference(name)
        return text if ContinuityChecker._looks_like_named_character(text) else ""

    @staticmethod
    def _looks_like_generic_character_reference(name: str) -> bool:
        text = str(name or "").strip()
        if not text:
            return False
        if text in GENERIC_CHARACTER_REFERENCES:
            return True
        if "的" in text:
            _prefix, suffix = text.rsplit("的", 1)
            if suffix and any(suffix.endswith(role) for role in POSSESSIVE_GENERIC_ROLE_SUFFIXES):
                return True
        return len(text) <= 8 and any(text.endswith(suffix) for suffix in GENERIC_CHARACTER_ROLE_SUFFIXES)

    @staticmethod
    def _normalize_character_reference(name: str) -> str:
        text = str(name or "").strip()
        for opener, closer in (("（", "）"), ("(", ")")):
            if opener not in text or not text.endswith(closer):
                continue
            prefix, suffix = text.rsplit(opener, 1)
            suffix = suffix[: -len(closer)].strip()
            prefix = prefix.strip()
            if suffix in {"提及", "无名", "记录", "旁白", "幕后", "间接"} and prefix:
                text = prefix
            elif prefix and ContinuityChecker._looks_like_generic_character_reference(prefix):
                text = prefix
        return text

    @staticmethod
    def _looks_like_non_character_reference(name: str) -> bool:
        text = str(name or "").strip()
        if any(text.endswith(suffix) for suffix in RELATIONAL_REFERENCE_SUFFIXES):
            return True
        return any(keyword in text for keyword in NON_CHARACTER_NAME_KEYWORDS)

    @staticmethod
    def _is_absence_only_state_change(change) -> bool:  # noqa: ANN001
        if str(getattr(change, "field", "") or "").strip().lower() not in {
            "existence",
            "status",
            "availability",
        }:
            return False
        text = " ".join(
            str(getattr(change, attr, "") or "")
            for attr in ("old_value", "new_value", "reason")
        )
        return any(keyword in text for keyword in ABSENCE_ONLY_CHANGE_KEYWORDS)
