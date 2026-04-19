from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select, text
from tests_support import capture_select_statements

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
from forwin.models.base import get_engine, get_session_factory, init_db, upgrade_db
from forwin.orchestrator.feedback_aggregator import (
    run_feedback_aggregation_pass,
    score_signal_aggregate_v1,
)
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
        project_id: str = "",
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
            project_id=project_id,
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

    def test_upgrade_db_backfills_reader_scale_meta_columns_for_existing_audience_schema(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS signal_window_aggregates"))
            conn.execute(text("DROP TABLE IF EXISTS reader_scale_snapshots"))
            conn.execute(
                text(
                    """
                    CREATE TABLE signal_window_aggregates (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        signal_key TEXT NOT NULL,
                        signal_type TEXT NOT NULL DEFAULT '',
                        target_type TEXT NOT NULL DEFAULT '',
                        target_name TEXT NOT NULL DEFAULT '',
                        window_type TEXT NOT NULL DEFAULT 'short',
                        window_chapter_start INTEGER NOT NULL DEFAULT 0,
                        window_chapter_end INTEGER NOT NULL DEFAULT 0,
                        hit_comment_count INTEGER NOT NULL DEFAULT 0,
                        unique_user_count INTEGER NOT NULL DEFAULT 0,
                        total_comment_count INTEGER NOT NULL DEFAULT 0,
                        reader_estimate INTEGER NOT NULL DEFAULT 0,
                        reader_tier INTEGER NOT NULL DEFAULT 0,
                        max_severity INTEGER NOT NULL DEFAULT 0,
                        avg_confidence FLOAT NOT NULL DEFAULT 0,
                        signal_level TEXT NOT NULL DEFAULT 'noise',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE reader_scale_snapshots (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        chapter_number INTEGER NOT NULL DEFAULT 0,
                        reader_estimate INTEGER NOT NULL DEFAULT 0,
                        tier INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version)
                    VALUES ('audience_feedback_v1')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    DELETE FROM schema_migrations
                    WHERE version = 'audience_feedback_scale_meta_v1'
                    """
                )
            )

        upgrade_db(self.engine)

        with self.engine.begin() as conn:
            aggregate_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(signal_window_aggregates)"))
            }
            self.assertIn("estimation_method", aggregate_columns)
            self.assertIn("scale_confidence", aggregate_columns)

            snapshot_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(reader_scale_snapshots)"))
            }
            self.assertIn("estimation_method", snapshot_columns)

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
        self.assertEqual(payload["message"], "评论批次已入库。")
        self.assertEqual(payload["inserted"], 1)
        self.assertEqual(payload["updated"], 0)

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

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted((row.signal_type, row.target_type, row.severity) for row in rows),
            [("confusion", "general", 1), ("pacing", "arc", 1)],
        )
        self.assertEqual({row.target_name for row in rows}, {""})

    def test_comment_analyzer_accepts_extended_signal_type_schema(self) -> None:
        project = self._create_project()
        comment = self._add_comment(
            work_name=project.title,
            body="我猜他们迟早会在一起，这条关系线肯定还要反转。",
            remote_comment_id="comment-llm-2",
            author_id="user-3",
            author_name="读者C",
        )
        self.session.commit()

        llm = _FakeLLM(
            json.dumps(
                {
                    "signals": [
                        {
                            "comment_index": 0,
                            "signal_type": "relationship_interest",
                            "target_type": "character",
                            "target_name": "两人关系线",
                            "severity": 2,
                            "confidence": 0.82,
                            "evidence_span": "迟早会在一起",
                        },
                        {
                            "comment_index": 0,
                            "signal_type": "prediction",
                            "target_type": "plot",
                            "target_name": "关系线反转",
                            "severity": 1,
                            "confidence": 0.77,
                            "evidence_span": "肯定还要反转",
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

        self.assertEqual({row.signal_type for row in rows}, {"relationship_interest", "prediction"})

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

    def test_feedback_aggregation_updates_candidate_levels_by_exact_signal_identity(self) -> None:
        project = self._create_project()

        arc_comment_a = self._add_comment(
            work_name=project.title,
            body="这一段节奏还是慢。",
            remote_comment_id="signal-arc-1",
            author_id="arc-user-1",
            author_name="读者A",
        )
        arc_comment_b = self._add_comment(
            work_name=project.title,
            body="同意，节奏偏慢。",
            remote_comment_id="signal-arc-2",
            author_id="arc-user-2",
            author_name="读者B",
        )
        plot_comment_a = self._add_comment(
            work_name=project.title,
            body="主线节奏得再快一点。",
            remote_comment_id="signal-plot-1",
            author_id="plot-user-1",
            author_name="读者C",
        )
        plot_comment_b = self._add_comment(
            work_name=project.title,
            body="主线推进太慢了。",
            remote_comment_id="signal-plot-2",
            author_id="plot-user-2",
            author_name="读者D",
        )
        plot_comment_c = self._add_comment(
            work_name=project.title,
            body="主线确实拖了。",
            remote_comment_id="signal-plot-3",
            author_id="plot-user-3",
            author_name="读者E",
        )

        self._add_signal(
            project_id=project.id,
            comment_id=arc_comment_a.id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=1,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=arc_comment_b.id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=2,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=plot_comment_a.id,
            signal_type="pacing",
            target_type="plot",
            target_name="节奏",
            severity=2,
            chapter_number=1,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=plot_comment_b.id,
            signal_type="pacing",
            target_type="plot",
            target_name="节奏",
            severity=2,
            chapter_number=2,
        )
        self._add_signal(
            project_id=project.id,
            comment_id=plot_comment_c.id,
            signal_type="pacing",
            target_type="plot",
            target_name="节奏",
            severity=2,
            chapter_number=3,
        )
        self.session.commit()

        run_feedback_aggregation_pass(
            self.session,
            project.id,
            3,
            cooldown_chapters=3,
            comment_to_reader_ratio=50,
        )

        rows = self.session.execute(
            select(CommentSignalCandidate).order_by(
                CommentSignalCandidate.target_type.asc(),
                CommentSignalCandidate.chapter_number.asc(),
            )
        ).scalars().all()
        levels_by_target_type: dict[str, set[str]] = {}
        for row in rows:
            levels_by_target_type.setdefault(row.target_type, set()).add(row.signal_level)

        self.assertEqual(levels_by_target_type["arc"], {"candidate"})
        self.assertEqual(levels_by_target_type["plot"], {"confirmed"})

    def test_state_repository_reader_feedback_prefers_structured_signals(self) -> None:
        project = self._create_project()
        self._add_comment(
            work_name=project.title,
            body="太拖了",
            remote_comment_id="feedback-1",
            author_id="user-a",
            author_name="读者A",
        )
        self._add_comment(
            work_name=project.title,
            body="还是有点拖",
            remote_comment_id="feedback-2",
            author_id="user-b",
            author_name="读者B",
        )
        self._add_comment(
            work_name=project.title,
            body="节奏再快一点就好了",
            remote_comment_id="feedback-3",
            author_id="user-c",
            author_name="读者C",
        )
        self.session.add(
            SignalWindowAggregate(
                id=new_id(),
                project_id=project.id,
                signal_key="pacing:arc:节奏",
                signal_type="pacing",
                target_type="arc",
                target_name="节奏",
                window_type="short",
                window_chapter_start=1,
                window_chapter_end=2,
                hit_comment_count=3,
                unique_user_count=3,
                total_comment_count=3,
                reader_estimate=240,
                reader_tier=1,
                max_severity=2,
                avg_confidence=0.82,
                signal_level="confirmed",
            )
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
        self.assertEqual(
            {
                (
                    row.signal_key,
                    row.signal_level,
                    row.max_severity,
                    row.unique_user_count,
                    row.target_name,
                    row.signal_type,
                )
                for row in result.actionable
            },
            {
                ("pacing:arc:节奏", "confirmed", 2, 3, "节奏", "pacing"),
                ("risk:plot:整体逻辑", "watchlist", 3, 1, "整体逻辑", "risk"),
            },
        )
        self.assertEqual(
            result.hint_pack.pacing_hints,
            ["读者反馈回报偏干(1-3章, 3人), 近1-5章缩短 reward gap 并提高兑现密度"],
        )
        self.assertEqual(
            result.hint_pack.risk_flags,
            ["[整体逻辑]存在受控不确定性风险(1-3章), 保持 managed ambiguity，别让信息失真失控"],
        )

        records = self.session.execute(
            select(FeedbackActionRecord).order_by(FeedbackActionRecord.signal_key.asc())
        ).scalars().all()
        self.assertEqual(
            [
                (
                    row.signal_key,
                    row.signal_type,
                    row.action_type,
                    row.triggered_at_chapter,
                    row.cooldown_until_chapter,
                    row.notes,
                )
                for row in records
            ],
            [
                (
                    "pacing:arc:节奏",
                    "pacing",
                    "boost_reward_density",
                    3,
                    6,
                    "读者反馈回报偏干(1-3章, 3人), 近1-5章缩短 reward gap 并提高兑现密度",
                ),
                (
                    "risk:plot:整体逻辑",
                    "risk",
                    "hold_managed_ambiguity",
                    3,
                    6,
                    "[整体逻辑]存在受控不确定性风险(1-3章), 保持 managed ambiguity，别让信息失真失控",
                ),
            ],
        )

        second = run_feedback_aggregation_pass(
            self.session,
            project.id,
            3,
            cooldown_chapters=3,
            comment_to_reader_ratio=50,
        )
        self.assertEqual(second.actionable, [])

    def test_estimate_reader_scale_uses_raw_comment_volume_not_signal_count(self) -> None:
        from forwin.orchestrator.feedback_aggregator import estimate_reader_scale

        project = self._create_project()
        for index in range(4):
            self._add_comment(
                project_id=project.id,
                work_name=project.title,
                body=f"第{index + 1}条评论",
                remote_comment_id=f"reader-scale-{index + 1}",
                author_id=f"user-{index + 1}",
                author_name=f"读者{index + 1}",
                chapter_title="第一章",
            )
        first_comment_id = self.session.execute(
            select(PublisherRawComment.id).order_by(PublisherRawComment.remote_comment_id.asc()).limit(1)
        ).scalar_one()
        self._add_signal(
            project_id=project.id,
            comment_id=first_comment_id,
            signal_type="pacing",
            target_type="arc",
            target_name="节奏",
            severity=2,
            chapter_number=1,
        )
        self.session.commit()

        snapshot = estimate_reader_scale(
            self.session,
            project.id,
            chapter_number=1,
            comment_to_reader_ratio=50,
        )

        self.assertEqual(snapshot.reader_estimate, 200)
        self.assertEqual(snapshot.tier, 1)

    def test_estimate_reader_scale_prefers_platform_metric(self) -> None:
        from forwin.orchestrator.feedback_aggregator import estimate_reader_scale

        project = self._create_project()
        self._add_comment(
            project_id=project.id,
            work_name=project.title,
            body="好看",
            remote_comment_id="platform-scale-1",
            author_id="user-platform",
            author_name="读者",
            chapter_title="第一章",
        )
        row = self.session.execute(select(PublisherRawComment)).scalar_one()
        row.raw_payload_json = json.dumps(
            {"book_stats": {"read_count": 1234, "favorite_count": 88}},
            ensure_ascii=False,
        )
        self.session.commit()

        snapshot = estimate_reader_scale(
            self.session,
            project.id,
            chapter_number=1,
            comment_to_reader_ratio=50,
        )

        self.assertEqual(snapshot.reader_estimate, 1234)
        self.assertEqual(snapshot.estimation_method, "platform_metric:fanqie:read_count")
        self.assertEqual(snapshot.tier, 2)

    def test_action_effectiveness_marks_score_drop_improved(self) -> None:
        from forwin.orchestrator.feedback_aggregator import derive_action_effectiveness

        project = self._create_project()
        self.session.add(
            FeedbackActionRecord(
                id=new_id(),
                project_id=project.id,
                signal_key="pacing:arc:节奏",
                signal_type="pacing",
                action_type="boost_reward_density",
                triggered_at_chapter=5,
                cooldown_until_chapter=8,
                notes="缩短 reward gap",
            )
        )
        self.session.add_all(
            [
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="pacing:arc:节奏",
                    signal_type="pacing",
                    target_type="arc",
                    target_name="节奏",
                    window_type="long",
                    window_chapter_start=1,
                    window_chapter_end=5,
                    hit_comment_count=6,
                    unique_user_count=5,
                    total_comment_count=8,
                    reader_estimate=400,
                    reader_tier=2,
                    max_severity=3,
                    avg_confidence=0.9,
                    signal_level="confirmed",
                ),
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="pacing:arc:节奏",
                    signal_type="pacing",
                    target_type="arc",
                    target_name="节奏",
                    window_type="long",
                    window_chapter_start=4,
                    window_chapter_end=8,
                    hit_comment_count=1,
                    unique_user_count=1,
                    total_comment_count=10,
                    reader_estimate=400,
                    reader_tier=2,
                    max_severity=1,
                    avg_confidence=0.6,
                    signal_level="candidate",
                ),
            ]
        )
        self.session.commit()

        outcomes = derive_action_effectiveness(self.session, project.id)

        self.assertEqual(outcomes[0]["outcome"], "improved")
        self.assertEqual(outcomes[0]["action_type"], "boost_reward_density")

    def test_action_effectiveness_batches_signal_aggregate_reads(self) -> None:
        from forwin.orchestrator.feedback_aggregator import derive_action_effectiveness

        project = self._create_project()
        self.session.add_all(
            [
                FeedbackActionRecord(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="pacing:arc:节奏",
                    signal_type="pacing",
                    action_type="boost_reward_density",
                    triggered_at_chapter=5,
                    cooldown_until_chapter=8,
                ),
                FeedbackActionRecord(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="hook:character:主角",
                    signal_type="hook",
                    action_type="strengthen_hook",
                    triggered_at_chapter=4,
                    cooldown_until_chapter=7,
                ),
            ]
        )
        self.session.add_all(
            [
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="pacing:arc:节奏",
                    signal_type="pacing",
                    target_type="arc",
                    target_name="节奏",
                    window_type="long",
                    window_chapter_start=1,
                    window_chapter_end=5,
                    hit_comment_count=6,
                    unique_user_count=5,
                    total_comment_count=8,
                    reader_estimate=400,
                    reader_tier=2,
                    max_severity=3,
                    avg_confidence=0.9,
                    signal_level="confirmed",
                ),
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="pacing:arc:节奏",
                    signal_type="pacing",
                    target_type="arc",
                    target_name="节奏",
                    window_type="long",
                    window_chapter_start=4,
                    window_chapter_end=8,
                    hit_comment_count=1,
                    unique_user_count=1,
                    total_comment_count=10,
                    reader_estimate=400,
                    reader_tier=2,
                    max_severity=1,
                    avg_confidence=0.6,
                    signal_level="candidate",
                ),
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="hook:character:主角",
                    signal_type="hook",
                    target_type="character",
                    target_name="主角",
                    window_type="long",
                    window_chapter_start=1,
                    window_chapter_end=4,
                    hit_comment_count=4,
                    unique_user_count=4,
                    total_comment_count=7,
                    reader_estimate=300,
                    reader_tier=2,
                    max_severity=2,
                    avg_confidence=0.8,
                    signal_level="watchlist",
                ),
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="hook:character:主角",
                    signal_type="hook",
                    target_type="character",
                    target_name="主角",
                    window_type="long",
                    window_chapter_start=3,
                    window_chapter_end=7,
                    hit_comment_count=2,
                    unique_user_count=2,
                    total_comment_count=8,
                    reader_estimate=300,
                    reader_tier=2,
                    max_severity=1,
                    avg_confidence=0.5,
                    signal_level="candidate",
                ),
            ]
        )
        self.session.commit()

        with capture_select_statements(self.engine) as select_statements:
            outcomes = derive_action_effectiveness(self.session, project.id, limit=8)

        aggregate_queries = [
            statement for statement in select_statements if " from signal_window_aggregates" in statement
        ]
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(aggregate_queries), 1)

    def test_feedback_cooldown_batches_latest_action_lookup(self) -> None:
        from forwin.orchestrator.feedback_aggregator import FeedbackCooldown

        project = self._create_project()
        self.session.add_all(
            [
                FeedbackActionRecord(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="pacing:arc:节奏",
                    signal_type="pacing",
                    action_type="boost_reward_density",
                    triggered_at_chapter=3,
                    cooldown_until_chapter=6,
                ),
                FeedbackActionRecord(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="hook:character:主角",
                    signal_type="hook",
                    action_type="strengthen_hook",
                    triggered_at_chapter=2,
                    cooldown_until_chapter=5,
                ),
            ]
        )
        self.session.commit()

        aggregates = [
            SignalWindowAggregate(
                id=new_id(),
                project_id=project.id,
                signal_key="pacing:arc:节奏",
                signal_level="confirmed",
            ),
            SignalWindowAggregate(
                id=new_id(),
                project_id=project.id,
                signal_key="hook:character:主角",
                signal_level="watchlist",
            ),
            SignalWindowAggregate(
                id=new_id(),
                project_id=project.id,
                signal_key="emotion:scene:压抑",
                signal_level="confirmed",
            ),
        ]

        with capture_select_statements(self.engine) as select_statements:
            result = FeedbackCooldown(cooldown_chapters=3).filter_actionable(
                self.session,
                project.id,
                chapter_number=6,
                aggregates=aggregates,
            )

        feedback_action_queries = [
            statement for statement in select_statements if " from feedback_action_records" in statement
        ]
        self.assertEqual(
            [agg.signal_key for agg in result],
            ["pacing:arc:节奏", "hook:character:主角", "emotion:scene:压抑"],
        )
        self.assertEqual(len(feedback_action_queries), 1)

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

    def test_score_v1_and_repo_trends_surface_rising_signal(self) -> None:
        project = self._create_project()
        older = SignalWindowAggregate(
            id=new_id(),
            project_id=project.id,
            signal_key="relationship_interest:character:两人关系线",
            signal_type="relationship_interest",
            target_type="character",
            target_name="两人关系线",
            window_type="long",
            window_chapter_start=1,
            window_chapter_end=8,
            hit_comment_count=2,
            unique_user_count=2,
            total_comment_count=12,
            reader_estimate=600,
            reader_tier=2,
            max_severity=1,
            avg_confidence=0.72,
            signal_level="candidate",
        )
        newer = SignalWindowAggregate(
            id=new_id(),
            project_id=project.id,
            signal_key="relationship_interest:character:两人关系线",
            signal_type="relationship_interest",
            target_type="character",
            target_name="两人关系线",
            window_type="long",
            window_chapter_start=5,
            window_chapter_end=12,
            hit_comment_count=5,
            unique_user_count=4,
            total_comment_count=10,
            reader_estimate=500,
            reader_tier=2,
            max_severity=2,
            avg_confidence=0.88,
            signal_level="confirmed",
        )
        self.session.add_all([older, newer])
        self.session.commit()

        self.assertGreater(score_signal_aggregate_v1(newer), score_signal_aggregate_v1(older))

        trends = StateRepository(self.session).get_audience_trends(
            project.id,
            before_chapter=13,
            window_type="long",
        )

        self.assertEqual(len(trends), 1)
        self.assertEqual(trends[0].signal_type, "relationship_interest")
        self.assertEqual(trends[0].current_level, "confirmed")
        self.assertEqual(trends[0].trend_type, "rising")
        self.assertGreater(trends[0].current_score, trends[0].previous_score)

    def test_arc_envelope_manager_calibrates_overlay_from_audience_signals(self) -> None:
        from forwin.orchestrator.phase24 import ArcStructureDraftData
        from forwin.protocol.experience import ArcPayoffMap, ReaderPromise

        project = self._create_project()
        self.session.add_all(
            [
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="pacing:arc:节奏",
                    signal_type="pacing",
                    target_type="arc",
                    target_name="节奏",
                    window_type="long",
                    window_chapter_start=1,
                    window_chapter_end=10,
                    hit_comment_count=5,
                    unique_user_count=4,
                    total_comment_count=10,
                    reader_estimate=400,
                    reader_tier=2,
                    max_severity=2,
                    avg_confidence=0.82,
                    signal_level="confirmed",
                ),
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="confusion:setting:规则边界",
                    signal_type="confusion",
                    target_type="setting",
                    target_name="规则边界",
                    window_type="long",
                    window_chapter_start=1,
                    window_chapter_end=10,
                    hit_comment_count=4,
                    unique_user_count=4,
                    total_comment_count=10,
                    reader_estimate=400,
                    reader_tier=2,
                    max_severity=2,
                    avg_confidence=0.81,
                    signal_level="confirmed",
                ),
                SignalWindowAggregate(
                    id=new_id(),
                    project_id=project.id,
                    signal_key="relationship_interest:character:两人关系线",
                    signal_type="relationship_interest",
                    target_type="character",
                    target_name="两人关系线",
                    window_type="long",
                    window_chapter_start=1,
                    window_chapter_end=10,
                    hit_comment_count=5,
                    unique_user_count=5,
                    total_comment_count=10,
                    reader_estimate=400,
                    reader_tier=2,
                    max_severity=2,
                    avg_confidence=0.87,
                    signal_level="confirmed",
                ),
            ]
        )
        self.session.commit()

        manager = ArcEnvelopeManager(director=_CapturingDirector())
        profile = manager._build_audience_calibration_profile(  # noqa: SLF001
            session=self.session,
            project_id=project.id,
        )
        self.assertTrue(profile.boost_reward_density)
        self.assertTrue(profile.clarify_rule_legibility)
        self.assertTrue(profile.protect_character_heat)

        chapter_plans = [
            ChapterPlan(
                id=new_id(),
                project_id=project.id,
                arc_plan_id="arc-1",
                chapter_number=1,
                title="第一章",
                one_line="开局承压",
                goals_json='["推进主线"]',
            ),
            ChapterPlan(
                id=new_id(),
                project_id=project.id,
                arc_plan_id="arc-1",
                chapter_number=2,
                title="第二章",
                one_line="压力扩大",
                goals_json='["确认代价"]',
            ),
            ChapterPlan(
                id=new_id(),
                project_id=project.id,
                arc_plan_id="arc-1",
                chapter_number=3,
                title="第三章",
                one_line="悬念抬升",
                goals_json='["抬高问题"]',
            ),
        ]
        structure = ArcStructureDraftData(
            phase_layout=["setup", "pressure", "payoff"],
            key_beats=["开局承压", "确认代价", "抬高问题"],
            thread_priorities=[],
            hotspot_candidates=[],
            compression_candidates=[],
            reader_promise=ReaderPromise(
                genre_promise="悬疑网文",
                pleasure_promise="稳定给出悬念和兑现",
                core_pleasures=["悬念", "翻盘"],
                ambiguity_mode="stable",
                world_legibility_target="规则必须看得懂",
            ),
            arc_payoff_map=ArcPayoffMap(ambiguity_constraints=["翻盘必须遵守代价"]),
        )
        schedule = manager._derive_band_delight_schedule(  # noqa: SLF001
            band_id="band:1:3",
            chapter_start=1,
            chapter_end=3,
            structure=structure,
            active_band=chapter_plans,
            calibration=profile,
        )
        plan = manager._derive_chapter_experience_plan(  # noqa: SLF001
            chapter_number=2,
            structure=structure,
            schedule=schedule,
            chapter_plan=chapter_plans[1],
            calibration=profile,
        )

        reward_categories = [item.category for item in schedule.scheduled_rewards]
        self.assertGreaterEqual(reward_categories.count("power"), 3)
        self.assertIn("emotion", reward_categories)
        self.assertTrue(any("规则" in item.question_resolve or "代价" in item.question_resolve for item in schedule.curiosity_beats))
        self.assertTrue(any("规则" in item or "代价" in item for item in plan.rule_anchors))
        self.assertIn("relationship", plan.minimum_progress_channels)


if __name__ == "__main__":
    unittest.main()
