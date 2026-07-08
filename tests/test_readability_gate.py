from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.readability import analyze_writer_output_readability
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import Project
from forwin.models.base import Base
from forwin.protocol.writer import WriterOutput


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "readability"


def _fixture(name: str) -> WriterOutput:
    return WriterOutput.model_validate_json((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def test_polluted_ch13_readability_signals_block_fatal_gate() -> None:
    output = _fixture("ch13_polluted.json")

    signals = analyze_writer_output_readability(
        project_id=output.project_id,
        chapter_number=output.chapter_number,
        writer_output=output,
        protagonist_names={"陆明"},
    )
    signal_types = {signal.signal_type for signal in signals}
    gate = evaluate_canon_admission(
        project_id=output.project_id,
        chapter_number=output.chapter_number,
        signals=signals,
        mode="fatal_only",
    )

    assert {
        "appellation_referent_conflict",
        "internal_key_leakage_v2",
        "protagonist_name_missing",
    }.issubset(signal_types)
    assert gate.commit_allowed is False
    assert gate.verdict == "fail"
    assert gate.blocking_issue_count >= 3


def test_title_mismatch_and_empty_summary_are_readability_blockers() -> None:
    title_output = _fixture("ch28_title_mismatch.json")
    summary_output = _fixture("ch30_empty_summary.json")

    title_signals = analyze_writer_output_readability(
        project_id=title_output.project_id,
        chapter_number=title_output.chapter_number,
        writer_output=title_output,
        protagonist_names={"陆明"},
    )
    summary_signals = analyze_writer_output_readability(
        project_id=summary_output.project_id,
        chapter_number=summary_output.chapter_number,
        writer_output=summary_output,
        protagonist_names={"陆明"},
    )

    assert [signal.signal_type for signal in title_signals] == ["chapter_title_mismatch"]
    assert [signal.signal_type for signal in summary_signals] == ["chapter_summary_empty"]
    assert all(signal.severity == "error" for signal in [*title_signals, *summary_signals])


def test_clean_chapter_has_no_readability_false_positives() -> None:
    output = _fixture("ch1_clean.json")

    signals = analyze_writer_output_readability(
        project_id=output.project_id,
        chapter_number=output.chapter_number,
        writer_output=output,
        protagonist_names={"陆明"},
    )

    assert signals == []


def test_quality_service_collects_readability_with_form_disabled() -> None:
    engine, session = _session()
    try:
        project = Project(title="可读性", premise="主角：陆明。", genre="pulp", setting_summary="旧港。")
        session.add(project)
        session.flush()
        output = _fixture("ch13_polluted.json").model_copy(update={"project_id": project.id})

        result = analyze_writer_output_quality(
            session=session,
            project_id=project.id,
            chapter_number=13,
            writer_output=output,
            persist=False,
            mode="off",
        )

        signal_types = {signal.signal_type for signal in result.signals}
        assert result.blocking is True
        assert "protagonist_name_missing" in signal_types
        assert result.deterministic_quality_report["blocking"] is True
    finally:
        session.close()
        engine.dispose()
