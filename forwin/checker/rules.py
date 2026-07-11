"""Rule-based continuity checker for Phase 0.5."""

from __future__ import annotations
import json
import logging
from typing import TYPE_CHECKING, Any

from forwin.book_state.query import BookStateQuery

if TYPE_CHECKING:
    from forwin.state.repo import StateRepository

from forwin.protocol.writer import WriterOutput
from forwin.protocol.review import ReviewVerdict, ContinuityIssue
from forwin.review.issue_groups import issue_group_for_issue
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
BODY_TERMINAL_PUNCTUATION = set("。！？!?…")
BODY_TRAILING_CLOSERS = set("”’」』）)]》】")


class ContinuityChecker:
    """Checks chapter output for basic continuity issues."""

    def __init__(
        self,
        repo: StateRepository,
        min_chars: int = 2500,
        max_chars: int = 3200,
        *,
        book_state_query: BookStateQuery | Any | None = None,
    ):
        self.repo = repo
        session = getattr(repo, "session", None)
        self.book_state = (
            book_state_query
            if book_state_query is not None
            else BookStateQuery(session)
            if session is not None
            else None
        )
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
            issues.append(
                ContinuityIssue(
                    rule_name="char_count_low",
                    severity="warning",
                    description=f"章节正文仅{char_count}字，低于最低要求{self.min_chars}字",
                    reviewer="continuity",
                    issue_type="continuity",
                    target_scope="chapter",
                )
            )
        elif char_count > self.max_chars * 1.5:  # Allow some overflow but flag extreme
            issues.append(
                ContinuityIssue(
                    rule_name="char_count_high",
                    severity="warning",
                    description=f"章节正文{char_count}字，远超目标{self.max_chars}字",
                    reviewer="continuity",
                    issue_type="continuity",
                    target_scope="chapter",
                )
            )

        return issues

    def _check_canon_name_anchors(
        self, project_id: str, output: WriterOutput
    ) -> list[ContinuityIssue]:
        anchors = self._canon_name_anchors(
            project_id,
            as_of_chapter=max(int(output.chapter_number) - 1, 0),
        )
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
                    evidence_refs=[
                        f"body:{violation.evidence}",
                        f"reason={violation.reason}",
                    ],
                    suggested_fix=(
                        f"凡指代{violation.role_label}姓名时必须逐字沿用「{violation.canonical_name}」，"
                        f"删除或替换「{violation.observed_name}」等变体。"
                    ),
                )
            )
        return issues

    def _canon_name_anchors(
        self,
        project_id: str,
        *,
        as_of_chapter: int = 10**9,
    ):
        if self.book_state is None:
            return []
        try:
            threads = list(
                self.book_state.active_threads(
                    project_id,
                    as_of_chapter=as_of_chapter,
                )
                or []
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("canon name anchor check skipped: %s", exc)
            return []
        texts: list[str] = []
        for thread in threads:
            texts.append(str(getattr(thread, "description", "") or ""))
            texts.extend(
                str(beat or "") for beat in (getattr(thread, "recent_beats", []) or [])
            )
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
            return [
                ContinuityIssue(
                    rule_name="empty_body",
                    severity="error",
                    description="章节正文为空或过短（不足100字）",
                    reviewer="continuity",
                    issue_type="continuity",
                    target_scope="chapter",
                    evidence_refs=[f"body_chars={len(output.body.strip())}"],
                )
            ]
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

    def _check_dead_characters(
        self, project_id: str, output: WriterOutput
    ) -> list[ContinuityIssue]:
        """Check if dead characters are being used as active participants."""
        issues = []

        # Get entities that are dead/inactive
        if self.book_state is None:
            return issues
        entities = self.book_state.active_entities(
            project_id,
            as_of_chapter=max(int(output.chapter_number) - 1, 0),
        )
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
                        issues.append(
                            ContinuityIssue(
                                rule_name="dead_character_active",
                                severity="error",
                                description=f"已死亡角色「{name}」在事件中被标记为{role}",
                                entity_names=[name],
                                reviewer="continuity",
                                issue_type="continuity",
                                target_scope="scene",
                                evidence_refs=[
                                    f"event={event.summary[:60]}",
                                    f"entity={name}",
                                    f"role={role}",
                                ],
                            )
                        )

        return issues

    def _check_thread_status(
        self, project_id: str, output: WriterOutput
    ) -> list[ContinuityIssue]:
        """Check if beat candidates reference resolved/abandoned threads."""
        issues = []

        if self.book_state is None:
            return issues

        for beat in output.thread_beats:
            thread = self.book_state.thread_by_name(
                project_id,
                beat.thread_name,
                as_of_chapter=max(int(output.chapter_number) - 1, 0),
            )
            if thread and thread.status in ("resolved", "abandoned"):
                issues.append(
                    ContinuityIssue(
                        rule_name="thread_already_closed",
                        severity="warning",
                        description=f"情节线「{beat.thread_name}」已{thread.status}，但本章仍有相关推进",
                        entity_names=[],
                        reviewer="continuity",
                        issue_type="continuity",
                        target_scope="chapter",
                    )
                )

        return issues

    def _check_state_change_validity(
        self, output: WriterOutput
    ) -> list[ContinuityIssue]:
        """Basic validation of state changes."""
        issues = []

        for sc in output.state_changes:
            if not sc.entity_name.strip():
                issues.append(
                    ContinuityIssue(
                        rule_name="empty_entity_name",
                        severity="warning",
                        description="状态变更中存在空的实体名称",
                        reviewer="continuity",
                        issue_type="continuity",
                        target_scope="chapter",
                    )
                )
            if not sc.field.strip():
                issues.append(
                    ContinuityIssue(
                        rule_name="empty_field_name",
                        severity="warning",
                        description=f"实体「{sc.entity_name}」的状态变更中字段名为空",
                        entity_names=[sc.entity_name],
                        reviewer="continuity",
                        issue_type="continuity",
                        target_scope="chapter",
                    )
                )

        return issues

    def _check_event_completeness(self, output: WriterOutput) -> list[ContinuityIssue]:
        """Check if events have proper structure."""
        issues = []

        for event in output.new_events:
            if len(event.involved_entity_names) != len(event.roles):
                issues.append(
                    ContinuityIssue(
                        rule_name="event_role_mismatch",
                        severity="warning",
                        description=f"事件「{event.summary[:30]}」的参与者数量与角色数量不匹配",
                        reviewer="continuity",
                        issue_type="continuity",
                        target_scope="chapter",
                    )
                )

        return issues
