"""Rule-based continuity checker for Phase 0.5."""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forwin.state.repo import StateRepository

from forwin.protocol.writer import WriterOutput
from forwin.protocol.review import ReviewVerdict, ContinuityIssue
from forwin.governance import issue_group_for_issue

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
    "同学",
    "众人",
    "人群",
    "旁人",
}


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
        allowed_names = self.repo.get_allowed_entity_names(project_id, output.chapter_number)
        if not allowed_names:
            return []
        candidate_names: set[str] = set()
        maybe_event_names: set[str] = set()

        for mention in getattr(output, "entity_mentions", []):
            if (
                getattr(mention, "entity_kind", "") == "character"
                and bool(getattr(mention, "is_named", False))
                and self._looks_like_named_character(getattr(mention, "entity_name", ""))
            ):
                candidate_names.add(str(getattr(mention, "entity_name", "")).strip())

        for change in output.state_changes:
            if change.entity_kind == "character" and self._looks_like_named_character(change.entity_name):
                candidate_names.add(change.entity_name.strip())

        for event in output.new_events:
            for name in event.involved_entity_names:
                if self._looks_like_named_character(name):
                    maybe_event_names.add(str(name).strip())

        for scene in output.scene_outputs:
            for name in scene.involved_entities:
                if self._looks_like_named_character(name):
                    candidate_names.add(str(name).strip())

        if maybe_event_names:
            resolved = self.repo.get_entities_by_names(project_id, sorted(maybe_event_names))
            for name in maybe_event_names:
                entity = resolved.get(name)
                if entity is None or entity.kind == "character":
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

    @staticmethod
    def _looks_like_named_character(name: str) -> bool:
        text = str(name or "").strip()
        if not text or text in GENERIC_CHARACTER_REFERENCES:
            return False
        return len(text) <= 12
