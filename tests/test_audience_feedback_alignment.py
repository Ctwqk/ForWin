from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select, text

from forwin.director.arc_director import ArcDirector
from forwin.models import (
    ChapterPlan,
    CommentSignalCandidate,
    FeedbackActionRecord,
    Project,
    PublisherRawComment,
    ReaderScaleSnapshot,
    SignalWindowAggregate,
    new_id,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.orchestrator.feedback_aggregator import run_feedback_aggregation_pass
from forwin.orchestrator.phase24 import ArcEnvelopeManager, policy_for_total_chapters
from forwin.orchestrator.phase3 import PacingStrategist
from forwin.orchestrator.phase4 import CommentAnalyzer, classify_signal_level
from forwin.publishers import PublisherManager
from forwin.state.repo import StateRepository


class _FakeLLM:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prompts: list[list[dict[str, str]]] = []

    def chat(self, prompt, **_kwargs):  # noqa: ANN001
        self.prompts.append(prompt)
        return self.raw


class _RaisingLLM:
    def chat(self, prompt, **_kwargs):  # noqa: ANN001
        raise RuntimeError("LLM unavailable")


class _CapturingDirector:
    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    def draft_arc_structure(self, **kwargs):  # noqa: ANN003
        self.last_kwargs = kwargs
        return {
            "phase_layout": ["setup", "pressure", "turn", "payoff"],
            "key_beats": ["beat-1", "beat-2"],
            "thread_priorities": [{"name": "主线", "priority": 1, "reason": "测试"}],
            "hotspot_candidates": ["热点A"],
            "compression_candidates": ["压缩A"],
        }


class AudienceFeedbackAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "audience-feedback.db"
        self.engine = get_engine(str(self.db_path))
        init_db(self.engine)
        self.SessionFactory = get_session_factory(self.engine)
        self.session = self.SessionFactory()

    def tearDown(self) -> None:
        self.session.close()
        self.tmpdir.cleanup()

    def _create_project(self, *, title: str = "测试书") -> Project:
        project = Project(
            id=new_id(),
            title=title,
            premise="测试前提",
            genre="玄幻",
            setting_summary="测试设定",
        )
        self.session.add(project)
        self.session.commit()
        return project

    def _add_comment(
        self,
        *,
        work_name: str,
        body: str,
        remote_comment_id: str,
        author_id: str,
        author_name: str,
        chapter_title: str = "第一章",
        like_count: int = 0,
        reply_count: int = 0,
    ) -> PublisherRawComment:
        row = PublisherRawComment(
            id=new_id(),
            platform_id="fanqie",
            remote_comment_id=remote_comment_id,
            work_name=work_name,
            chapter_title=chapter_title,
            author_id=author_id,
            author_name=author_name,
            body_text=body,
            like_count=like_count,
            reply_count=reply_count,
            raw_payload_json="{}",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _add_signal(
        self,
        *,
        project_id: str,
        comment_id: str,
        signal_type: str,
        target_type: str,
        target_name: str,
        severity: int,
        chapter_number: int,
        signal_level: str = "noise",
    ) -> CommentSignalCandidate:
        row = CommentSignalCandidate(
            id=new_id(),
            project_id=project_id,
            source_comment_id=comment_id,
            signal_type=signal_type,
            target_type=target_type,
            target_name=target_name,
            severity=severity,
            confidence=0.8,
            evidence_span="摘录",
            signal_level=signal_level,
            chapter_number=chapter_number,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def test_init_db_exposes_audience_feedback_schema(self) -> None:
        with self.engine.begin() as conn:
            comment_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(publisher_raw_comments)"))
            }
            self.assertIn("like_count", comment_columns)
            self.assertIn("reply_count", comment_columns)

            table_names = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            }
            self.assertIn("comment_signal_candidates", table_names)
            self.assertIn("signal_window_aggregates", table_names)
            self.assertIn("reader_scale_snapshots", table_names)
            self.assertIn("feedback_action_records", table_names)

    def test_publisher_manager_ingest_comments_batch_persists_like_and_reply_counts(self) -> None:
        manager = PublisherManager(self.SessionFactory)
        payload = manager.ingest_comments_batch(
            client_id="client-1",
            platform="fanqie",
            comments=[
                {
                    "remote_comment_id": "comment-1",
                    "work_id": "book-1",
                    "work_name": "测试书",
                    "chapter_id": "chapter-1",
                    "chapter_title": "第一章",
                    "author_id": "user-1",
                    "author_name": "读者A",
                    "body": "催更",
                    "created_at": "2026-04-01T12:00:00Z",
                    "like_count": 9,
                    "reply_count": 2,
                    "raw_payload": {"body": "催更"},
                }
            ],
        )
        self.assertTrue(payload["ok"])

        with self.SessionFactory() as session:
            row = session.execute(select(PublisherRawComment)).scalar_one()
            self.assertEqual(row.like_count, 9)
            self.assertEqual(row.reply_count, 2)

    def test_comment_analyzer_stores_multiple_llm_signals(self) -> None:
        project = self._create_project()
        comment = self._add_comment(
            work_name=project.title,
            body="主角动机这里我有点看不懂，而且感觉节奏偏慢。",
            remote_comment_id="comment-llm-1",
            author_id="user-1",
            author_name="读者A",
        )
        self.session.commit()

        llm = _FakeLLM(
            json.dumps(
                {
                    "signals": [
                        {
                            "comment_index": 0,
                            "signal_type": "confusion",
                            "target_type": "character",
                            "target_name": "主角动机",
                            "severity": 2,
                            "confidence": 0.9,
                            "evidence_span": "主角动机这里我有点看不懂",
                        },
                        {
                            "comment_index": 0,
                            "signal_type": "pacing",
                            "target_type": "arc",
                            "target_name": "节奏",
                            "severity": 2,
                            "confidence": 0.8,
                            "evidence_span": "感觉节奏偏慢",
                        },
                    ]
                },
                ensure_ascii=False,
            )
        )
        analyzer = CommentAnalyzer(llm_client=llm)
        rows = analyzer.analyze_and_store(
            session=self.session,
            project_id=project.id,
            comments=[comment],
            chapter_number=2,
        )

        self.assertEqual(len(rows), 2)
        stored = self.session.execute(
            select(CommentSignalCandidate).order_by(CommentSignalCandidate.signal_type.asc())
        ).scalars().all()
        self.assertEqual([row.signal_type for row in stored], ["confusion", "pacing"])
        self.assertEqual({row.target_name for row in stored}, {"主角动机", "节奏"})

    def test_comment_analyzer_falls_back_to_keywords_when_llm_fails(self) -> None:
        project = self._create_project()
        comment = self._add_comment(
            work_name=project.title,
            body="为什么主角要这么做？这一段也太拖了。",
            remote_comment_id="comment-fallback-1",
            author_id="user-2",
            author_name="读者B",
        )
        self.session.commit()

        analyzer = CommentAnalyzer(llm_client=_RaisingLLM())
        rows = analyzer.analyze_and_store(
            session=self.session,
            project_id=project.id,
            comments=[comment],
            chapter_number=3,
        )

        self.assertGreaterEqual(len(rows), 2)
        signal_types = {row.signal_type for row in rows}
        self.assertIn("confusion", signal_types)
        self.assertIn("pacing", signal_types)

    def test_classify_signal_level_matches_strict_spec(self) -> None:
        self.assertEqual(
            classify_signal_level(unique_users=1, spans_chapters=1, severity=3, signal_type="risk"),
            "watchlist",
        )
        self.assertEqual(
            classify_signal_level(unique_users=1, spans_chapters=2, severity=2, signal_type="pacing"),
            "noise",
        )
        self.assertEqual(
            classify_signal_level(unique_users=2, spans_chapters=1, severity=2, signal_type="character_heat"),
            "noise",
        )
        self.assertEqual(
            classify_signal_level(unique_users=3, spans_chapters=2, severity=1, signal_type="confusion"),
            "confirmed",
        )
        self.assertEqual(
            classify_signal_level(unique_users=2, spans_chapters=2, severity=2, signal_type="pacing"),
            "candidate",
        )

    def test_state_repository_reader_feedback_prefers_structured_signals(self) -> None:
        project = self._create_project()
        comment_a = self._add_comment(
            work_name=project.title,
            body="太拖了",
            remote_comment_id="feedback-1",
            author_id="user-a",
            author_name="读者A",
        )
        comment_b = self._add_comment(
            work_name=project.title,
            body="还是有点拖",
            remote_comment_id="feedback-2",
            author_id="user-b",
            author_name="读者B",
        )
        comment_c = self._add_comment(
            work_name=project.title,
            body="节奏再快一点就好了",
            remote_comment_id="feedback-3",
            author_id="user-c",
            author_name="读者C",
        )
        self._add_signal(
            project_id=project.id,
            comment_id=comment_a.id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=1,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=comment_b.id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=2,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=comment_c.id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=2,
        )
        self.session.add(
            ReaderScaleSnapshot(
                id=new_id(),
                project_id=project.id,
                chapter_number=2,
                reader_estimate=240,
                estimation_method="comment_proxy",
                tier=1,
            )
        )
        self.session.commit()

        repo = StateRepository(self.session)
        feedback = repo.get_recent_reader_feedback(project.id, before_chapter=3)

        assert feedback is not None
        self.assertEqual(feedback.dominant_sentiment, "pacing:confirmed")
        self.assertIn("节奏:pacing:confirmed", feedback.highlighted_topics)
        self.assertEqual(len(feedback.confirmed_signals), 1)
        self.assertEqual(feedback.confirmed_signals[0].level, "confirmed")
        self.assertEqual(feedback.reader_tier, 1)
        self.assertIn("主导信号", feedback.feedback_summary)

    def test_feedback_aggregation_pass_records_only_confirmed_and_watchlist_actions(self) -> None:
        project = self._create_project()
        comments = [
            self._add_comment(
                work_name=project.title,
                body="节奏太慢",
                remote_comment_id="agg-1",
                author_id="user-1",
                author_name="读者A",
            ),
            self._add_comment(
                work_name=project.title,
                body="节奏有点拖",
                remote_comment_id="agg-2",
                author_id="user-2",
                author_name="读者B",
            ),
            self._add_comment(
                work_name=project.title,
                body="推进还是慢",
                remote_comment_id="agg-3",
                author_id="user-3",
                author_name="读者C",
            ),
            self._add_comment(
                work_name=project.title,
                body="这里逻辑有 bug",
                remote_comment_id="agg-4",
                author_id="user-4",
                author_name="读者D",
            ),
        ]
        self._add_signal(
            project_id=project.id,
            comment_id=comments[0].id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=1,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=comments[1].id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=2,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=comments[2].id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=2,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=comments[3].id,
            signal_type="risk",
            target_type="plot",
            target_name="整体逻辑",
            severity=3,
            chapter_number=3,
        )
        self.session.commit()

        result = run_feedback_aggregation_pass(
            self.session,
            project.id,
            3,
            cooldown_chapters=3,
            comment_to_reader_ratio=50,
        )
        actionable_levels = {row.signal_level for row in result.actionable}
        self.assertTrue(actionable_levels.issubset({"confirmed", "watchlist"}))
        self.assertTrue(result.hint_pack.pacing_hints)
        self.assertTrue(result.hint_pack.risk_flags)

        records = self.session.execute(select(FeedbackActionRecord)).scalars().all()
        self.assertEqual(len(records), 2)

        second = run_feedback_aggregation_pass(
            self.session,
            project.id,
            3,
            cooldown_chapters=3,
            comment_to_reader_ratio=50,
        )
        self.assertEqual(second.actionable, [])

    def test_pacing_strategist_uses_medium_window_audience_signal_only(self) -> None:
        project = self._create_project()
        strategist = PacingStrategist()

        self.session.add(
            SignalWindowAggregate(
                id=new_id(),
                project_id=project.id,
                signal_key="pacing:arc:节奏",
                signal_type="pacing",
                target_type="arc",
                target_name="节奏",
                window_type="short",
                window_chapter_start=3,
                window_chapter_end=5,
                hit_comment_count=3,
                unique_user_count=3,
                total_comment_count=3,
                reader_estimate=150,
                reader_tier=1,
                max_severity=2,
                avg_confidence=0.8,
                signal_level="confirmed",
            )
        )
        self.session.commit()

        no_medium = strategist.analyze(session=self.session, project_id=project.id, chapter_number=5)
        self.assertNotEqual(no_medium.verdict, "audience_pacing_concern")

        self.session.add(
            SignalWindowAggregate(
                id=new_id(),
                project_id=project.id,
                signal_key="pacing:arc:节奏",
                signal_type="pacing",
                target_type="arc",
                target_name="节奏",
                window_type="medium",
                window_chapter_start=1,
                window_chapter_end=5,
                hit_comment_count=4,
                unique_user_count=4,
                total_comment_count=4,
                reader_estimate=200,
                reader_tier=1,
                max_severity=2,
                avg_confidence=0.85,
                signal_level="confirmed",
            )
        )
        self.session.commit()

        with_medium = strategist.analyze(session=self.session, project_id=project.id, chapter_number=5)
        self.assertEqual(with_medium.verdict, "audience_pacing_concern")
        self.assertEqual(with_medium.risk_level, "medium")

    def test_arc_envelope_manager_passes_long_window_audience_trends_to_director(self) -> None:
        project = self._create_project()
        self.session.add(
            SignalWindowAggregate(
                id=new_id(),
                project_id=project.id,
                signal_key="confusion:character:主角动机",
                signal_type="confusion",
                target_type="character",
                target_name="主角动机",
                window_type="long",
                window_chapter_start=1,
                window_chapter_end=12,
                hit_comment_count=5,
                unique_user_count=4,
                total_comment_count=10,
                reader_estimate=500,
                reader_tier=2,
                max_severity=2,
                avg_confidence=0.8,
                signal_level="confirmed",
            )
        )
        self.session.commit()

        director = _CapturingDirector()
        manager = ArcEnvelopeManager(director=director)
        chapter_plans = [
            ChapterPlan(
                id=new_id(),
                project_id=project.id,
                arc_plan_id="arc-1",
                chapter_number=1,
                title="第一章",
                one_line="推进一",
                goals_json='["g1"]',
            ),
            ChapterPlan(
                id=new_id(),
                project_id=project.id,
                arc_plan_id="arc-1",
                chapter_number=2,
                title="第二章",
                one_line="推进二",
                goals_json='["g2"]',
            ),
        ]

        structure = manager._build_structure_draft(  # noqa: SLF001
            session=self.session,
            project=project,
            total_chapters=120,
            chapter_plans=chapter_plans,
            policy=policy_for_total_chapters(120),
            base_target_size=18,
        )

        self.assertEqual(structure.phase_layout, ["setup", "pressure", "turn", "payoff"])
        assert director.last_kwargs is not None
        self.assertEqual(director.last_kwargs["audience_trends"], ["主角动机:confusion:confirmed"])


if __name__ == "__main__":
    unittest.main()
