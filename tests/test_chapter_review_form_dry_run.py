from __future__ import annotations

import json

import pytest

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CanonQualitySignalRow, CountdownLedgerRow
from forwin.protocol.writer import WriterOutput


class FakeCountdownConflictClient:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        quote = "主倒计时只剩十分钟。"
        return {
            "project_id": self.project_id,
            "chapter_number": 2,
            "form_schema_version": FORM_SCHEMA_VERSION,
            "characters": [],
            "countdowns": [
                {
                    "key": "main",
                    "mentioned_in_chapter": True,
                    "status_in_this_chapter": {
                        "value": "advanced",
                        "evidence_quote": quote,
                        "subject_of_quote": "主倒计时",
                        "confidence": 0.94,
                    },
                    "new_value_minutes": 10,
                    "new_value_evidence": {
                        "value": "10",
                        "evidence_quote": quote,
                        "subject_of_quote": "主倒计时",
                        "confidence": 0.94,
                    },
                    "consistent_with_prior": {
                        "value": "false",
                        "evidence_quote": quote,
                        "subject_of_quote": "主倒计时",
                        "confidence": 0.93,
                    },
                    "inconsistency_kind": "regression",
                }
            ],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "主倒计时发生冲突。",
        }


def test_dry_run_downgrades_blocking_signals_and_does_not_write_canon_rows(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("FORWIN_ARTIFACT_ROOT", str(tmp_path))
    engine = get_engine(postgres_test_url("chapter_review_form_dry_run"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    write_calls: list[str] = []

    def reject_character_writes(self, transitions):  # noqa: ANN001, ANN202
        write_calls.append(f"characters:{len(transitions)}")
        raise AssertionError("dry-run attempted character canon row write")

    def reject_countdown_writes(self, entries):  # noqa: ANN001, ANN202
        write_calls.append(f"countdowns:{len(entries)}")
        raise AssertionError("dry-run attempted countdown canon row write")

    monkeypatch.setattr(CanonQualityRepository, "save_character_transitions", reject_character_writes)
    monkeypatch.setattr(CanonQualityRepository, "save_countdown_entries", reject_countdown_writes)

    try:
        with session_factory() as session:
            project = Project(title="Dry Run", premise="测试", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            session.add(
                CountdownLedgerRow(
                    project_id=project.id,
                    countdown_key="main",
                    label="主倒计时",
                    chapter_number=1,
                    normalized_remaining_minutes=40,
                    raw_mention="主倒计时还有四十分钟。",
                    status="active",
                    evidence_refs_json=json.dumps(["主倒计时还有四十分钟。"], ensure_ascii=False),
                    payload_json=json.dumps({"source": "chapter_review_form"}, ensure_ascii=False),
                )
            )
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=2,
                title="第二章",
                body="主倒计时只剩十分钟。",
                end_of_chapter_summary="主倒计时发生冲突。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=2,
                writer_output=output,
                draft_id="draft-2",
                persist=True,
                mode="dry_run",
                llm_client=FakeCountdownConflictClient(project.id),
                return_raw_analyzer_results=True,
            )
            session.commit()

            artifact_path = tmp_path / "chapter_review_form" / project.id / "2.json"
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            persisted_signals = session.query(CanonQualitySignalRow).filter_by(project_id=project.id).all()

        assert write_calls == []
        assert persisted_signals == []
        assert result.mode == "dry_run"
        assert result.blocking is False
        assert result.signals
        assert {signal.severity for signal in result.signals} == {"warning"}
        assert result.raw_analyzer_results[0]["blocking"] is False
        assert result.raw_analyzer_results[0]["metadata"]["source_mode"] == "chapter_review_form_dry_run"
        assert artifact["mode"] == "dry_run"
        assert artifact["projection_summary"]["signals_by_severity"] == {"warning": 1}
        assert artifact["projection_summary"]["validated_count"] >= 1
        assert artifact["signals"][0]["severity"] == "warning"
    finally:
        engine.dispose()


def test_dry_run_llm_unavailable_warns_without_blocking(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("FORWIN_ARTIFACT_ROOT", str(tmp_path))
    engine = get_engine(postgres_test_url("chapter_review_form_dry_run_llm_unavailable"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="Dry Run Missing LLM", premise="测试", genre="悬疑", target_total_chapters=1)
            session.add(project)
            session.flush()
            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=1,
                    title="一",
                    body="正文",
                    end_of_chapter_summary="",
                ),
                mode="dry_run",
                persist=True,
                llm_client=None,
            )
            artifact_path = tmp_path / "chapter_review_form" / project.id / "1.json"
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    finally:
        engine.dispose()

    assert result.mode == "dry_run"
    assert result.blocking is False
    assert result.signals[0].signal_type == "form_llm_unavailable"
    assert result.signals[0].severity == "warning"
    assert artifact["projection_summary"]["signals_by_severity"] == {"warning": 1}
    assert artifact["projection_summary"]["validated_count"] == 0
    assert artifact["projection_summary"]["rejected_count"] == 0
