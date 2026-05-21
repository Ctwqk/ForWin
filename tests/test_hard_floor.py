from __future__ import annotations

from types import SimpleNamespace

from forwin.checker.hard_floor import HardFloorResult
from forwin.checker.hard_floor import run_hard_floor
from forwin.config import Config
from forwin.governance import DecisionEventType
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.orchestrator_loop_core import project_chapters
from forwin.orchestrator_loop_core.common import RunResult
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.state_change import EventCandidate
from forwin.protocol.writer import WriterOutput


def writer(body: str, **updates) -> WriterOutput:
    data = {
        "chapter_number": 1,
        "title": "第一章",
        "body": body,
        "char_count": len(body),
        "end_of_chapter_summary": "本章发生了一件事。",
        "new_events": [EventCandidate(summary="角色A完成行动")],
    }
    data.update(updates)
    return WriterOutput(**data)


def context(**updates) -> ChapterContextPack:
    data = {
        "project_id": "project-1",
        "project_title": "测试项目",
        "premise": "测试前提",
        "genre": "玄幻",
        "setting_summary": "测试设定",
        "chapter_number": 1,
        "chapter_plan_title": "第一章",
        "chapter_plan_one_line": "角色A开始调查。",
        "chapter_goals": ["找到线索"],
        "must_not_reveal": [],
    }
    data.update(updates)
    return ChapterContextPack(**data)


def config() -> Config:
    return Config(min_chapter_chars=20, hard_floor_gate_enabled=True)


class FakeSession:
    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class FakeRepo:
    def __init__(self) -> None:
        self.project = SimpleNamespace(id="project-1", governance_json="{}")
        self.chapter_plan = SimpleNamespace(id="plan-1", chapter_number=1)

    def get_project(self, project_id: str):
        return self.project if project_id == "project-1" else None

    def get_chapter_plan(self, project_id: str, chapter_number: int):
        if project_id == "project-1" and chapter_number == 1:
            return self.chapter_plan
        return None

    def list_chapter_rewrite_attempts(self, project_id: str, chapter_number: int):
        return [SimpleNamespace(id="rewrite-1"), SimpleNamespace(id="rewrite-2")]


class FakeUpdater:
    def __init__(self) -> None:
        self.status_calls: list[tuple[str, int, str, dict[str, object]]] = []
        self.saved_events: list[dict[str, object]] = []

    def mark_chapter_status(self, project_id: str, chapter_number: int, status: str, **kwargs) -> None:
        self.status_calls.append((project_id, chapter_number, status, kwargs))

    def save_decision_event(self, event) -> None:
        self.saved_events.append(event.model_dump(mode="json"))


class FakeChapterLoop:
    def __init__(self, *, hard_floor_gate_enabled: bool = True) -> None:
        self.config = SimpleNamespace(
            hard_floor_gate_enabled=hard_floor_gate_enabled,
            operation_mode="checkpoint",
            review_interval_chapters=0,
        )
        self.retrieval_broker = SimpleNamespace(
            last_observability_summary={},
            build_chapter_context=lambda *args, **kwargs: context(),
        )
        self.decision_events: list[dict[str, object]] = []

    def _abort_requested(self) -> bool:
        return False

    def _pause_requested(self) -> bool:
        return False

    def _project_governance(self, project):
        return SimpleNamespace()

    def _strict_progression_block(self, **kwargs):
        return "", "", ""

    def _manual_boundary_checkpoint(self, *args, **kwargs):
        return None

    def _emit_progress(self, *args, **kwargs) -> None:
        return None

    def _audit_current_plan_before_write(self, **kwargs):
        return kwargs["context"]

    def _write_chapter_with_attention_fallback(self, **kwargs):
        return writer("角色A推开门，看见证据。他当场拿出证据，反派失去资格。门外忽然传来第二封密令？")

    def _review_and_maybe_rewrite(self, **kwargs):
        return kwargs["writer_output"], SimpleNamespace(verdict="pass", issues=[]), False

    def _review_issue_payloads(self, verdict):
        return [
            {
                "reviewer": "continuity",
                "rule_name": "existing_review_issue",
                "severity": "warning",
                "message": "existing issue",
            }
        ]

    def _review_canon_risk(self, verdict) -> str:
        return ""

    def _record_decision_event(self, **kwargs):
        self.decision_events.append(kwargs)

    def _paused_result(
        self,
        project_id: str,
        requested_chapters: int,
        *,
        completed_chapters,
        failed_chapters,
        paused_chapters,
        frozen_artifacts,
        current_chapter,
    ) -> RunResult:
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=list(completed_chapters),
            failed_chapters=list(failed_chapters),
            paused_chapters=list(paused_chapters),
            frozen_artifacts=list(frozen_artifacts),
            paused=True,
        )

    def _cancelled_result(
        self,
        project_id: str,
        requested_chapters: int,
        *,
        completed_chapters,
        failed_chapters,
        paused_chapters,
        frozen_artifacts,
        current_chapter,
    ) -> RunResult:
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=list(completed_chapters),
            failed_chapters=list(failed_chapters),
            paused_chapters=list(paused_chapters),
            frozen_artifacts=list(frozen_artifacts),
            cancelled=True,
        )


def test_project_chapter_loop_hard_floor_failure_marks_failed_and_records_event(monkeypatch) -> None:
    hard_floor = HardFloorResult(
        passed=False,
        fail_reasons=["chapter_length", "no_garbage"],
        checks={"chapter_length": False, "no_garbage": False},
        metadata={"body_char_count": 10},
    )
    hard_floor_calls = []

    def fake_run_hard_floor(**kwargs):
        hard_floor_calls.append(kwargs)
        return hard_floor

    monkeypatch.setattr(project_chapters, "run_hard_floor", fake_run_hard_floor, raising=False)

    chapter_loop = FakeChapterLoop(hard_floor_gate_enabled=True)
    session = FakeSession()
    repo = FakeRepo()
    updater = FakeUpdater()

    result = WritingOrchestrator._run_project_chapters(
        chapter_loop,
        session=session,
        repo=repo,
        updater=updater,
        checker=SimpleNamespace(),
        project_id="project-1",
        chapter_numbers=[1],
        requested_chapters=1,
    )

    assert result.failed_chapters == [1]
    assert result.paused_chapters == []
    assert result.completed_chapters == []
    assert len(hard_floor_calls) == 1
    assert hard_floor_calls[0]["project_id"] == "project-1"
    assert hard_floor_calls[0]["chapter_number"] == 1
    assert hard_floor_calls[0]["repo"] is repo
    assert hard_floor_calls[0]["config"] is chapter_loop.config

    assert len(updater.status_calls) == 1
    project_id, chapter_number, status, status_kwargs = updater.status_calls[0]
    assert (project_id, chapter_number, status) == ("project-1", 1, "failed")
    assert status_kwargs["repair_attempt_count"] == 2
    assert status_kwargs["canon_risk_level"] == "high"
    assert status_kwargs["residual_review_issues"] == [
        {
            "reviewer": "continuity",
            "rule_name": "existing_review_issue",
            "severity": "warning",
            "message": "existing issue",
        },
        {
            "reviewer": "hard_floor",
            "rule_name": "chapter_length",
            "severity": "error",
            "message": "hard floor failed: chapter_length",
        },
        {
            "reviewer": "hard_floor",
            "rule_name": "no_garbage",
            "severity": "error",
            "message": "hard floor failed: no_garbage",
        },
    ]

    assert len(chapter_loop.decision_events) == 1
    event = chapter_loop.decision_events[0]
    assert event["event_family"] == "evaluation_verdict"
    assert event["event_type"] == DecisionEventType.HARD_GATE_HIT
    assert event["scope"] == "chapter"
    assert event["reason"] == "chapter_length; no_garbage"
    assert "hard floor failed" in event["summary"]
    assert event["payload"] == hard_floor.model_dump(mode="json")
    assert session.commit_count >= 1


def test_project_chapter_loop_stops_after_hard_floor_failure(monkeypatch) -> None:
    hard_floor = HardFloorResult(
        passed=False,
        fail_reasons=["chapter_length"],
        checks={"chapter_length": False},
        metadata={"body_char_count": 10},
    )
    monkeypatch.setattr(
        project_chapters,
        "run_hard_floor",
        lambda **kwargs: hard_floor,
        raising=False,
    )

    class TwoChapterRepo(FakeRepo):
        def get_chapter_plan(self, project_id: str, chapter_number: int):
            if project_id == "project-1" and chapter_number in {1, 2}:
                return SimpleNamespace(id=f"plan-{chapter_number}", chapter_number=chapter_number)
            return None

    chapter_loop = FakeChapterLoop(hard_floor_gate_enabled=True)
    session = FakeSession()
    repo = TwoChapterRepo()
    updater = FakeUpdater()

    result = WritingOrchestrator._run_project_chapters(
        chapter_loop,
        session=session,
        repo=repo,
        updater=updater,
        checker=SimpleNamespace(),
        project_id="project-1",
        chapter_numbers=[1, 2],
        requested_chapters=2,
    )

    assert result.failed_chapters == [1]
    assert result.completed_chapters == []
    assert [call[1] for call in updater.status_calls] == [1]


def test_project_chapter_loop_skips_hard_floor_when_disabled(monkeypatch) -> None:
    def fake_run_hard_floor(**kwargs):
        raise AssertionError("hard floor should not run when disabled")

    monkeypatch.setattr(project_chapters, "run_hard_floor", fake_run_hard_floor, raising=False)

    chapter_loop = FakeChapterLoop(hard_floor_gate_enabled=False)
    result = WritingOrchestrator._run_project_chapters(
        chapter_loop,
        session=FakeSession(),
        repo=FakeRepo(),
        updater=FakeUpdater(),
        checker=SimpleNamespace(),
        project_id="project-1",
        chapter_numbers=[1],
        requested_chapters=1,
    )

    assert result.status == "needs_review"
    assert result.paused_chapters == [1]


def test_project_chapter_loop_defers_memory_upsert_failure_in_pulp() -> None:
    class FailingMemoryIndex:
        def upsert_chapter(self, **kwargs) -> None:
            raise TimeoutError("qdrant timeout")

    class AcceptedLoop(FakeChapterLoop):
        def __init__(self) -> None:
            super().__init__(hard_floor_gate_enabled=False)
            self.config = SimpleNamespace(
                hard_floor_gate_enabled=False,
                operation_mode="blackbox",
                review_interval_chapters=0,
                quality_profile="pulp",
            )
            self.retrieval_broker = SimpleNamespace(
                last_observability_summary={},
                build_chapter_context=lambda *args, **kwargs: context(),
                memory_index=FailingMemoryIndex(),
            )

        def _project_governance(self, project):
            return SimpleNamespace(auto_band_checkpoint=False)

        def _apply_canon_candidate(self, **kwargs):
            return None

        def _run_phase3_pass(self, **kwargs) -> None:
            return None

        def _audit_future_plans_after_acceptance(self, **kwargs):
            return None

        def _compile_world_model_after_acceptance(self, **kwargs) -> bool:
            return True

        def _record_generation_audit_checkpoint_if_due(self, **kwargs) -> bool:
            return False

    session = FakeSession()
    updater = FakeUpdater()
    result = WritingOrchestrator._run_project_chapters(
        AcceptedLoop(),
        session=session,
        repo=FakeRepo(),
        updater=updater,
        checker=SimpleNamespace(),
        project_id="project-1",
        chapter_numbers=[1],
        requested_chapters=1,
    )

    assert result.completed_chapters == [1]
    assert result.failed_chapters == []
    assert updater.status_calls[-1][2] == "accepted"
    assert updater.saved_events[-1]["event_type"] == "deferred_maintenance_recorded"
    assert updater.saved_events[-1]["payload"]["task_type"] == "memory_index_upsert"


def test_short_chapter_fails() -> None:
    result = run_hard_floor(
        writer_output=writer("太短"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "chapter_length" in result.fail_reasons
    assert result.checks["chapter_length"] is False


def test_inconsistent_char_count_fails_even_when_body_is_long_enough() -> None:
    body = "角色A推开门，看见证据。他当场拿出证据，反派失去资格。门外忽然传来第二封密令？"
    result = run_hard_floor(
        writer_output=writer(body, char_count=0),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "char_count_consistent" in result.fail_reasons
    assert result.checks["char_count_consistent"] is False
    assert result.metadata["body_char_count"] == len(body)
    assert result.metadata["writer_char_count"] == 0
    assert result.metadata["min_chapter_chars"] == 20


def test_model_artifact_fails() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见证据。assistant: 模型分析。章末问题出现。"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "no_garbage" in result.fail_reasons
    assert result.checks["no_garbage"] is False


def test_full_width_assistant_artifact_fails_no_garbage() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见证据。assistant：模型分析。门外忽然传来密令？"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "no_garbage" in result.fail_reasons
    assert result.checks["no_garbage"] is False


def test_chinese_dramatic_punctuation_is_not_garbage() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A怔住……——……——……——忽然听见门外传来第二封密令？"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is True
    assert result.fail_reasons == []
    assert result.checks["no_garbage"] is True


def test_must_not_reveal_fails_on_direct_match() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A终于发现父亲被围的真相，众人沉默片刻后继续行动。"),
        context_pack=context(must_not_reveal=["父亲被围"]),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "must_not_reveal" in result.fail_reasons
    assert result.checks["must_not_reveal"] is False
    assert result.metadata["must_not_reveal_hits"] == ["父亲被围"]


def test_missing_event_fails() -> None:
    result = run_hard_floor(
        writer_output=writer(
            "角色A推开门，看见证据。章末新的脚步声靠近。",
            new_events=[],
            state_changes=[],
            thread_beats=[],
        ),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "at_least_one_event" in result.fail_reasons
    assert result.checks["at_least_one_event"] is False


def test_ending_hook_is_warning_only() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见证据。他把证据交给同伴，众人决定继续调查。"),
        context_pack=context(),
        repo=SimpleNamespace(get_chapter_experience_plan=lambda *args: None),
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is True
    assert "ending_hook" in result.warning_reasons
    assert "ending_hook" not in result.fail_reasons
    assert result.checks["ending_hook"] is False


def test_clean_chapter_passes() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见证据。他当场拿出证据，反派失去资格。门外忽然传来第二封密令？"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is True
    assert result.fail_reasons == []
    assert result.warning_reasons == []
    assert result.metadata["project_id"] == "project-1"
    assert result.metadata["chapter_number"] == 1


def test_pulp_visible_payoff_missing_is_warning_only() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A走在路上，想起很多前情。忽然门外传来第二封密令？"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=Config(
            min_chapter_chars=20,
            hard_floor_gate_enabled=True,
            quality_profile="pulp",
        ),
    )

    assert result.passed is True
    assert "pulp_visible_payoff" in result.warning_reasons
    assert result.checks["pulp_visible_payoff"] is False
    assert result.metadata["pulp_beat"]["visible_payoff_present"] is False
