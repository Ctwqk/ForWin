# Canon Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone operator tool that replays the Chapter Review Form over already accepted chapters and safely backfills form-sourced canon rows.

**Architecture:** Add a new CLI, `scripts/canon_replay.py`, backed by focused helpers in `forwin/canon_quality/chapter_review_form/`. The replay service reconstructs a minimal `WriterOutput` from accepted drafts, calls the existing `analyze_writer_output_quality()` form pipeline, and layers range state, cost control, dry-run artifacts, and diff mode around that existing pipeline without changing writer generation.

**Tech Stack:** Python 3.12/3.13, argparse, Pydantic, SQLAlchemy ORM, pytest, existing ForWin `WriterOutput`, `CanonQualityRepository`, and Chapter Review Form contracts.

---

## Source Documents

- Functional authority: `docs/superpowers/specs/2026-05-18-canon-replay-design.md`
- Migration complement: `scripts/migrate_legacy_canon_to_form.py`
- Existing rough replay script for reference only: `scripts/replay_canon_quality_for_project.py`
- Live workflow authority: `AGENTS.md`

## Execution Rules

- Do not change chapter text, chapter plans, candidate drafts, or generation task state.
- Do not mark legacy rows superseded. That remains the responsibility of `scripts/migrate_legacy_canon_to_form.py`.
- Use TDD: write each failing test first, run it, then implement the minimal production code.
- Keep replay sequential. Do not parallelize chapters because Chapter Review Form prior-canon context depends on earlier replay output.
- Use one DB session per replayed chapter once range execution exists.
- Commit after each task with only the task's files staged.
- Before the final handoff, run:

```bash
python3 -m pytest tests/test_canon_replay_*.py -q
python3 -m compileall -q forwin scripts
git diff --check
```

## File Responsibility Map

- `scripts/canon_replay.py`: operator CLI, argument parsing, structured stdout/stderr, exit codes, config/session/bootstrap wiring.
- `forwin/canon_quality/chapter_review_form/replay.py`: accepted-draft lookup, `WriterOutput` reconstruction, single-chapter replay, range orchestration.
- `forwin/canon_quality/chapter_review_form/replay_state.py`: state file path resolution, schema, resume decisions, atomic JSON writes, state clearing.
- `forwin/canon_quality/chapter_review_form/cost_estimator.py`: per-chapter token/cost estimates, usage extraction from LLM attempt events, cap decisions.
- `forwin/canon_quality/chapter_review_form/replay_diff.py`: candidate/existing row normalization and add/remove/change diff computation.
- `docs/operations/canon_replay.md`: operator documentation and worked command examples.
- `tests/helpers/canon_replay.py`: shared replay test fixtures and fake LLM clients; no tests live in this helper module.
- `tests/test_canon_replay_reconstruction.py`: accepted draft lookup, writer output reconstruction, preflight checks.
- `tests/test_canon_replay_single_chapter.py`: one-chapter replay behavior, dry-run and persist behavior.
- `tests/test_canon_replay_resume.py`: range state, resume, force restart, force rerun, abort-on-error.
- `tests/test_canon_replay_cost.py`: estimate-only, cap enforcement, usage fallback.
- `tests/test_canon_replay_diff.py`: dry-run candidate rows and diff-mode row comparison.
- `tests/test_canon_replay_cli.py`: CLI argument conflicts, structured output, clear-state safety, documentation examples.

## Shared Test Fixtures

Create the helper module below in `tests/helpers/canon_replay.py` during Task 1. Replay tests must import fixtures from this helper instead of importing from other test modules.

```python
from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.models import ArcPlanVersion, CandidateDraftRecord, ChapterDraft, ChapterPlan, ChapterReview, Project


def seed_project_with_accepted_chapter(session, *, chapter_number: int = 1, body: str = "主倒计时还有59分钟。"):
    project = Project(title="Canon Replay Test", premise="测试", genre="悬疑", target_total_chapters=3)
    session.add(project)
    session.flush()
    arc = ArcPlanVersion(project_id=project.id, arc_synopsis="测试", status="active", chapter_start=1, chapter_end=3)
    session.add(arc)
    session.flush()
    plan, draft = seed_accepted_chapter(
        session,
        project=project,
        arc=arc,
        chapter_number=chapter_number,
        body=body,
    )
    return project, arc, plan, draft


def seed_accepted_chapter(session, *, project: Project, arc: ArcPlanVersion, chapter_number: int, body: str = "主倒计时还有59分钟。"):
    plan = ChapterPlan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        one_line="测试",
        status="accepted",
    )
    session.add(plan)
    session.flush()
    draft = ChapterDraft(
        id=f"draft-{chapter_number}",
        chapter_plan_id=plan.id,
        version=1,
        body_text=body,
        summary=f"第{chapter_number}章摘要",
        char_count=len(body),
        llm_raw_response="{}",
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(id=f"review-{chapter_number}", draft_id=draft.id, verdict="pass")
    session.add(review)
    session.flush()
    session.add(
        CandidateDraftRecord(
            project_id=project.id,
            chapter_plan_id=plan.id,
            chapter_number=chapter_number,
            candidate_draft_id=draft.id,
            review_id=review.id,
            status="canon_committed",
            canon_status="canon",
        )
    )
    session.flush()
    return plan, draft


class FakeCountdownClient:
    def __init__(self, project_id: str, chapter_number: int) -> None:
        self.project_id = project_id
        self.chapter_number = chapter_number
        self.llm_attempt_events = [
            {
                "status": "succeeded",
                "input_text": "Replay prompt: 主倒计时还有59分钟。",
                "output_text": "Replay answer: 主倒计时还有59分钟。",
                "duration_ms": 10,
            }
        ]

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        quote = "主倒计时还有59分钟。"
        return {
            "project_id": self.project_id,
            "chapter_number": self.chapter_number,
            "form_schema_version": FORM_SCHEMA_VERSION,
            "characters": [],
            "countdowns": [
                {
                    "key": "main",
                    "mentioned_in_chapter": True,
                    "status_in_this_chapter": {
                        "value": "active",
                        "evidence_quote": quote,
                        "subject_of_quote": "主倒计时",
                        "confidence": 0.95,
                    },
                    "new_value_minutes": 59,
                    "new_value_evidence": {
                        "value": "59",
                        "evidence_quote": quote,
                        "subject_of_quote": "主倒计时",
                        "confidence": 0.95,
                    },
                    "consistent_with_prior": {"value": "true", "confidence": 0.95},
                    "inconsistency_kind": "",
                }
            ],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "主倒计时继续。",
        }
```

## Task 1: Foundation, Profile Resolver, And Reconstruction

**Files:**
- Create: `scripts/canon_replay.py`
- Create: `forwin/canon_quality/chapter_review_form/replay.py`
- Create: `tests/helpers/canon_replay.py`
- Test: `tests/test_canon_replay_reconstruction.py`
- Test: `tests/test_canon_replay_cli.py`

- [ ] **Step 1: Write shared helpers and reconstruction tests**

Create `tests/helpers/canon_replay.py` using the code from the Shared Test Fixtures section, then create `tests/test_canon_replay_reconstruction.py` with these tests:

```python
from __future__ import annotations

import pytest

from forwin.canon_quality.chapter_review_form.replay import (
    ChapterDraftNotFound,
    find_missing_accepted_chapters,
    reconstruct_writer_output,
)
from forwin.models import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.base import get_engine, get_session_factory, init_db
from tests.helpers.canon_replay import seed_accepted_chapter, seed_project_with_accepted_chapter
from tests.postgres import postgres_test_url


def test_reconstruct_writer_output_uses_accepted_draft_body() -> None:
    engine = get_engine(postgres_test_url("canon-replay-reconstruct"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, draft = seed_project_with_accepted_chapter(
                session,
                chapter_number=2,
                body="主倒计时还有59分钟。",
            )
            session.commit()

        with session_factory() as session:
            output = reconstruct_writer_output(session=session, project_id=project.id, chapter_number=2)

        assert output.project_id == project.id
        assert output.chapter_number == 2
        assert output.title == "第2章"
        assert output.body == draft.body_text
        assert output.char_count == len(draft.body_text)
        assert output.end_of_chapter_summary == draft.summary
        assert output.prompt_revision_hash == "replay"
        assert output.generation_meta["source"] == "canon_replay"
    finally:
        engine.dispose()


def test_reconstruct_writer_output_raises_when_no_committed_draft_exists() -> None:
    engine = get_engine(postgres_test_url("canon-replay-missing"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            session.query(CandidateDraftRecord).delete()
            session.commit()

        with session_factory() as session:
            with pytest.raises(ChapterDraftNotFound) as exc:
                reconstruct_writer_output(session=session, project_id=project.id, chapter_number=1)

        assert "accepted draft not found" in str(exc.value)
    finally:
        engine.dispose()


def test_reconstruct_writer_output_uses_latest_committed_candidate_for_chapter() -> None:
    engine = get_engine(postgres_test_url("canon-replay-latest"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1, body="旧正文")
            newer = ChapterDraft(
                id="draft-newer",
                chapter_plan_id=plan.id,
                version=2,
                body_text="新正文，主倒计时还有58分钟。",
                summary="新摘要",
                char_count=15,
                llm_raw_response="{}",
            )
            session.add(newer)
            session.flush()
            review = ChapterReview(id="review-newer", draft_id=newer.id, verdict="pass")
            session.add(review)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=plan.id,
                    chapter_number=1,
                    candidate_draft_id=newer.id,
                    review_id=review.id,
                    status="canon_committed",
                    canon_status="canon",
                    version=2,
                )
            )
            session.commit()

        with session_factory() as session:
            output = reconstruct_writer_output(session=session, project_id=project.id, chapter_number=1)

        assert output.body == "新正文，主倒计时还有58分钟。"
        assert output.end_of_chapter_summary == "新摘要"
    finally:
        engine.dispose()


def test_find_missing_accepted_chapters_reports_range_holes() -> None:
    engine = get_engine(postgres_test_url("canon-replay-missing-range"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            seed_accepted_chapter(session, project=project, arc=arc, chapter_number=3)
            session.commit()

        with session_factory() as session:
            missing = find_missing_accepted_chapters(
                session=session,
                project_id=project.id,
                from_chapter=1,
                to_chapter=3,
            )

        assert missing == [2]
    finally:
        engine.dispose()
```

- [ ] **Step 2: Verify reconstruction tests fail**

Run:

```bash
python3 -m pytest tests/test_canon_replay_reconstruction.py -q
```

Expected: import failure for `forwin.canon_quality.chapter_review_form.replay`.

- [ ] **Step 3: Implement reconstruction helpers**

Create `forwin/canon_quality/chapter_review_form/replay.py` with these public objects:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import CandidateDraftRecord, ChapterDraft, ChapterPlan
from forwin.protocol.writer import WriterOutput


class ChapterDraftNotFound(RuntimeError):
    """Raised when canon replay cannot find an accepted draft for a chapter."""


@dataclass(frozen=True)
class AcceptedDraftRef:
    project_id: str
    chapter_number: int
    plan_id: str
    draft_id: str
    title: str
    body: str
    summary: str
    char_count: int


def load_accepted_draft_ref(*, session: Session, project_id: str, chapter_number: int) -> AcceptedDraftRef:
    row = session.execute(
        select(CandidateDraftRecord, ChapterDraft, ChapterPlan)
        .join(ChapterDraft, ChapterDraft.id == CandidateDraftRecord.candidate_draft_id)
        .join(ChapterPlan, ChapterPlan.id == CandidateDraftRecord.chapter_plan_id)
        .where(
            CandidateDraftRecord.project_id == project_id,
            CandidateDraftRecord.chapter_number == int(chapter_number),
            CandidateDraftRecord.status == "canon_committed",
            CandidateDraftRecord.canon_status == "canon",
        )
        .order_by(
            CandidateDraftRecord.version.desc(),
            CandidateDraftRecord.updated_at.desc(),
            ChapterDraft.version.desc(),
            ChapterDraft.created_at.desc(),
            CandidateDraftRecord.id.desc(),
        )
        .limit(1)
    ).first()
    if row is None:
        raise ChapterDraftNotFound(
            f"accepted draft not found for project={project_id} chapter={chapter_number}"
        )
    _candidate, draft, plan = row
    body = str(draft.body_text or "")
    if not body.strip():
        raise ChapterDraftNotFound(
            f"accepted draft body is empty for project={project_id} chapter={chapter_number}"
        )
    return AcceptedDraftRef(
        project_id=project_id,
        chapter_number=int(chapter_number),
        plan_id=str(plan.id or ""),
        draft_id=str(draft.id or ""),
        title=str(plan.title or f"第{chapter_number}章"),
        body=body,
        summary=str(draft.summary or ""),
        char_count=int(draft.char_count or len(body)),
    )


def reconstruct_writer_output(*, session: Session, project_id: str, chapter_number: int) -> WriterOutput:
    draft = load_accepted_draft_ref(session=session, project_id=project_id, chapter_number=chapter_number)
    return WriterOutput(
        project_id=draft.project_id,
        chapter_number=draft.chapter_number,
        title=draft.title,
        body=draft.body,
        char_count=draft.char_count,
        end_of_chapter_summary=draft.summary,
        prompt_revision_hash="replay",
        generation_meta={
            "source": "canon_replay",
            "draft_id": draft.draft_id,
            "chapter_plan_id": draft.plan_id,
        },
    )


def find_missing_accepted_chapters(
    *,
    session: Session,
    project_id: str,
    from_chapter: int,
    to_chapter: int,
) -> list[int]:
    missing: list[int] = []
    for chapter_number in range(int(from_chapter), int(to_chapter) + 1):
        try:
            load_accepted_draft_ref(session=session, project_id=project_id, chapter_number=chapter_number)
        except ChapterDraftNotFound:
            missing.append(chapter_number)
    return missing
```

- [ ] **Step 4: Verify reconstruction tests pass**

Run:

```bash
python3 -m pytest tests/test_canon_replay_reconstruction.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Write CLI skeleton tests**

Create `tests/test_canon_replay_cli.py` with:

```python
from __future__ import annotations

import json

import pytest

from scripts import canon_replay


class FakeReplayClient:
    def __init__(self, profiles):
        self.profiles = profiles
        self.api_key = "primary-key"
        self.base_url = "https://primary.example/v1"
        self.model = "primary-model"
        self.profile_id = ""
        self.profile_name = ""
        self.fallback_profiles = list(profiles)

    def _request_profiles(self):
        return [
            {
                "id": "",
                "name": "default",
                "api_key": self.api_key,
                "base_url": self.base_url,
                "model": self.model,
            },
            *self.profiles,
        ]


def test_parse_args_defaults_to_dry_run() -> None:
    args = canon_replay.parse_args(["--project-id", "p1", "--from-chapter", "1"])

    assert args.project_id == "p1"
    assert args.from_chapter == 1
    assert args.to_chapter is None
    assert args.dry_run is True
    assert args.persist is False


def test_parse_args_rejects_dry_run_and_persist_together() -> None:
    with pytest.raises(SystemExit):
        canon_replay.parse_args(
            ["--project-id", "p1", "--from-chapter", "1", "--dry-run", "--persist"]
        )


def test_emit_json_line_prints_one_json_object(capsys) -> None:
    canon_replay.emit_json_line({"status": "ok", "chapter_number": 1})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "ok", "chapter_number": 1}
    assert captured.out.endswith("\n")


def test_build_llm_client_for_replay_selects_complete_profile() -> None:
    client = canon_replay.build_llm_client_for_replay(
        object(),
        requested_profile="env-deepseek",
        client_builder=lambda _config: FakeReplayClient(
            [
                {
                    "id": "env-deepseek",
                    "name": "DeepSeek",
                    "api_key": "key",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                }
            ]
        ),
    )

    assert client.profile_id == "env-deepseek"
    assert client.api_key == "key"
    assert client.base_url == "https://api.deepseek.com/v1"
    assert client.model == "deepseek-chat"
    assert client.fallback_profiles == []


@pytest.mark.parametrize(
    "profile",
    [
        {"id": "bad", "name": "bad", "api_key": "", "base_url": "https://api.example/v1", "model": "m"},
        {"id": "bad", "name": "bad", "api_key": "key", "base_url": "", "model": "m"},
        {"id": "bad", "name": "bad", "api_key": "key", "base_url": "https://api.example/v1", "model": ""},
    ],
)
def test_build_llm_client_for_replay_rejects_incomplete_profile(profile) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit, match="LLM profile not found or incomplete"):
        canon_replay.build_llm_client_for_replay(
            object(),
            requested_profile="bad",
            client_builder=lambda _config: FakeReplayClient([profile]),
        )


def test_build_llm_client_for_replay_rejects_unknown_profile() -> None:
    with pytest.raises(SystemExit, match="LLM profile not found or incomplete"):
        canon_replay.build_llm_client_for_replay(
            object(),
            requested_profile="missing",
            client_builder=lambda _config: FakeReplayClient([]),
        )
```

- [ ] **Step 6: Verify CLI tests fail**

Run:

```bash
python3 -m pytest tests/test_canon_replay_cli.py -q
```

Expected: import failure for `scripts.canon_replay`.

- [ ] **Step 7: Implement CLI skeleton**

Create `scripts/canon_replay.py` with parse-only behavior:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def build_llm_client_for_replay(config, requested_profile: str = "", client_builder=None):  # noqa: ANN001, ANN201
    if client_builder is None:
        from forwin.runtime.container import ServiceContainer

        client_builder = ServiceContainer._build_llm_client
    client = client_builder(config)
    if not requested_profile:
        return client
    profiles = getattr(client, "_request_profiles", lambda: [])()
    requested = requested_profile.strip().lower()
    selected = [
        profile for profile in profiles
        if requested in {
            str(profile.get("id", "")).strip().lower(),
            str(profile.get("name", "")).strip().lower(),
        }
        and str(profile.get("api_key", "")).strip()
        and str(profile.get("base_url", "")).strip()
        and str(profile.get("model", "")).strip()
    ]
    if not selected:
        raise SystemExit(f"LLM profile not found or incomplete: {requested_profile}")
    profile = selected[0]
    client.api_key = str(profile["api_key"]).strip()
    client.base_url = str(profile["base_url"]).strip().rstrip("/")
    client.model = str(profile["model"]).strip()
    client.profile_id = str(profile.get("id", "")).strip()
    client.profile_name = str(profile.get("name", "")).strip()
    client.fallback_profiles = []
    return client


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay Chapter Review Form canon over accepted chapters.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--from-chapter", type=int, required=True)
    parser.add_argument("--to-chapter", type=int, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="Run LLM and produce candidate rows without DB writes.")
    mode.add_argument("--persist", action="store_true", help="Write replayed form-sourced canon rows.")
    parser.add_argument("--llm-profile", default="", help="Config LLM profile id or name. Empty means current default routing.")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force-restart", action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--abort-on-error", action="store_true")
    parser.add_argument("--cost-cap-usd", type=float, default=None)
    parser.add_argument("--no-cost-cap", action="store_true")
    parser.add_argument("--diff-mode", action="store_true")
    parser.add_argument("--schema-version", default="")
    parser.add_argument("--clear-state", action="store_true")
    parser.add_argument("--confirm-clear", action="store_true")
    args = parser.parse_args(argv)
    if args.persist:
        args.dry_run = False
    return args


def emit_json_line(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    emit_json_line(
        {
            "status": "parsed",
            "project_id": args.project_id,
            "from_chapter": args.from_chapter,
            "to_chapter": args.to_chapter,
            "mode": "persist" if args.persist else "dry_run",
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

When later tasks wire real execution into `main()`, call `build_llm_client_for_replay()` during preflight before any replay DB write. Profile errors must exit before any LLM call and before any canon rows can be written.

- [ ] **Step 8: Verify Task 1 tests pass**

Run:

```bash
python3 -m pytest tests/test_canon_replay_reconstruction.py tests/test_canon_replay_cli.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add scripts/canon_replay.py forwin/canon_quality/chapter_review_form/replay.py tests/helpers/canon_replay.py tests/test_canon_replay_reconstruction.py tests/test_canon_replay_cli.py
git commit -m "feat: add canon replay reconstruction foundation"
```

## Task 2: Single-Chapter Replay Engine

**Files:**
- Modify: `scripts/canon_replay.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- Test: `tests/test_canon_replay_single_chapter.py`

- [ ] **Step 1: Write single-chapter replay tests**

Create `tests/test_canon_replay_single_chapter.py`:

```python
from __future__ import annotations

import json

import pytest

from forwin.canon_quality.chapter_review_form.replay import ReplayLLMUnavailable, replay_single_chapter
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CountdownLedgerRow
from tests.helpers.canon_replay import FakeCountdownClient, seed_project_with_accepted_chapter
from tests.postgres import postgres_test_url


def test_replay_single_chapter_dry_run_returns_candidate_rows_without_writing() -> None:
    engine = get_engine(postgres_test_url("canon-replay-single-dry"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            session.commit()

        with session_factory() as session:
            result = replay_single_chapter(
                session=session,
                project_id=project.id,
                chapter_number=1,
                llm_client=FakeCountdownClient(project.id, 1),
                persist=False,
                mode="dry_run",
            )
            session.rollback()

        with session_factory() as session:
            rows = session.query(CountdownLedgerRow).filter_by(project_id=project.id).all()

        assert result.status == "success"
        assert result.chapter_number == 1
        assert result.mode == "dry_run"
        assert result.candidate_rows["countdowns"][0]["normalized_remaining_minutes"] == 59
        assert rows == []
    finally:
        engine.dispose()


def test_replay_single_chapter_persist_writes_form_sourced_rows() -> None:
    engine = get_engine(postgres_test_url("canon-replay-single-persist"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            result = replay_single_chapter(
                session=session,
                project_id=project.id,
                chapter_number=1,
                llm_client=FakeCountdownClient(project.id, 1),
                persist=True,
                mode="primary",
            )
            session.commit()

        with session_factory() as session:
            rows = session.query(CountdownLedgerRow).filter_by(project_id=project.id).all()

        assert result.status == "success"
        assert len(rows) == 1
        assert json.loads(rows[0].payload_json)["source"] == "chapter_review_form"
    finally:
        engine.dispose()


def test_replay_single_chapter_requires_llm_client_before_writes() -> None:
    engine = get_engine(postgres_test_url("canon-replay-single-no-llm"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            session.commit()

        with session_factory() as session:
            with pytest.raises(ReplayLLMUnavailable):
                replay_single_chapter(
                    session=session,
                    project_id=project.id,
                    chapter_number=1,
                    llm_client=None,
                    persist=True,
                    mode="primary",
                )
            session.rollback()

        with session_factory() as session:
            assert session.query(CountdownLedgerRow).filter_by(project_id=project.id).count() == 0
    finally:
        engine.dispose()
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
python3 -m pytest tests/test_canon_replay_single_chapter.py -q
```

Expected: import failure for `replay_single_chapter` and `ReplayLLMUnavailable`.

- [ ] **Step 3: Implement result models and single-chapter replay**

Add to `forwin/canon_quality/chapter_review_form/replay.py`:

```python
from pydantic import BaseModel, Field

from forwin.canon_quality.service import analyze_writer_output_quality


class ReplayLLMUnavailable(RuntimeError):
    """Raised before replay when no LLM client is configured."""


class ReplayTokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = True


class ReplayChapterResult(BaseModel):
    chapter_number: int
    mode: str
    status: str
    blocking: bool = False
    signal_counts_by_severity: dict[str, int] = Field(default_factory=dict)
    character_transitions_written: int = 0
    countdown_entries_written: int = 0
    validation_report_summary: dict[str, Any] = Field(default_factory=dict)
    candidate_rows: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    token_usage: ReplayTokenUsage = Field(default_factory=ReplayTokenUsage)
    error_message: str = ""


def replay_single_chapter(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    llm_client: object | None,
    persist: bool,
    mode: str,
) -> ReplayChapterResult:
    if llm_client is None:
        raise ReplayLLMUnavailable("No LLM client configured for canon replay.")
    accepted = load_accepted_draft_ref(session=session, project_id=project_id, chapter_number=chapter_number)
    writer_output = reconstruct_writer_output(session=session, project_id=project_id, chapter_number=chapter_number)
    resolved_mode = "dry_run" if str(mode).lower() == "dry_run" else "primary"
    result = analyze_writer_output_quality(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        writer_output=writer_output,
        draft_id=accepted.draft_id,
        persist=persist,
        mode=resolved_mode,
        llm_client=llm_client,
        return_raw_analyzer_results=True,
    )
    counts: dict[str, int] = {}
    for signal in result.signals:
        counts[signal.severity] = counts.get(signal.severity, 0) + 1
    report = dict(result.deterministic_quality_report or {})
    return ReplayChapterResult(
        chapter_number=int(chapter_number),
        mode=resolved_mode,
        status="success",
        blocking=bool(result.blocking),
        signal_counts_by_severity=counts,
        character_transitions_written=0 if not persist else len(_candidate_character_rows(result)),
        countdown_entries_written=0 if not persist else len(_candidate_countdown_rows(result)),
        validation_report_summary={
            "blocking": bool(report.get("blocking")),
            "review_issue_count": len(report.get("review_issues") or []),
        },
        candidate_rows={
            "signals": [signal.model_dump(mode="json") for signal in result.signals],
            "characters": _candidate_character_rows(result),
            "countdowns": _candidate_countdown_rows(result),
        },
    )


def _candidate_character_rows(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in result.raw_analyzer_results or []:
        for item in raw.get("character_transitions") or []:
            rows.append(dict(item))
    return rows


def _candidate_countdown_rows(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in result.raw_analyzer_results or []:
        for item in raw.get("countdown_entries") or []:
            rows.append(dict(item))
    if rows:
        return rows
    metrics = (result.deterministic_quality_report or {}).get("full_body_metrics") or {}
    return [dict(item) for item in metrics.get("countdown_mentions") or []]
```

If `raw_analyzer_results` do not currently include projected candidate rows, update `forwin/canon_quality/chapter_review_form/service.py` so `_raw_result(...)` includes `character_transitions` and `countdown_entries` as JSON-safe lists.

- [ ] **Step 4: Verify single-chapter tests pass**

Run:

```bash
python3 -m pytest tests/test_canon_replay_single_chapter.py tests/test_canon_quality_service.py tests/test_chapter_review_form_dry_run.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Wire CLI single-chapter execution**

Update `scripts/canon_replay.py` so `main()` resolves config, engine, session factory, LLM client via the Task 1 `build_llm_client_for_replay()` helper, and runs one chapter when `from_chapter == to_chapter` or `to_chapter` is omitted.

```python
with session_factory() as session:
    result = replay_single_chapter(
        session=session,
        project_id=args.project_id,
        chapter_number=args.from_chapter,
        llm_client=build_llm_client_for_replay(config, args.llm_profile),
        persist=args.persist,
        mode="primary" if args.persist else "dry_run",
    )
    if args.persist:
        session.commit()
    else:
        session.rollback()
emit_json_line(result.model_dump(mode="json"))
```

- [ ] **Step 6: Verify CLI help and direct parse**

Run:

```bash
python3 scripts/canon_replay.py --help
python3 -m pytest tests/test_canon_replay_cli.py -q
```

Expected: help includes `--llm-profile`, `--dry-run`, `--persist`; CLI tests pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add scripts/canon_replay.py forwin/canon_quality/chapter_review_form/replay.py forwin/canon_quality/chapter_review_form/service.py tests/test_canon_replay_single_chapter.py tests/test_canon_replay_cli.py
git commit -m "feat: replay single chapter through review form"
```

## Task 3: Range Iteration And Resumable State

**Files:**
- Create: `forwin/canon_quality/chapter_review_form/replay_state.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- Modify: `scripts/canon_replay.py`
- Test: `tests/test_canon_replay_resume.py`

- [ ] **Step 1: Write state and range tests**

Create `tests/test_canon_replay_resume.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from forwin.canon_quality.chapter_review_form.replay_state import ReplayRangeOptions, ReplayState, state_file_path, write_state_atomic
from forwin.canon_quality.chapter_review_form.replay import replay_chapter_range
from forwin.models.base import get_engine, get_session_factory, init_db
from tests.helpers.canon_replay import FakeCountdownClient, seed_accepted_chapter, seed_project_with_accepted_chapter
from tests.postgres import postgres_test_url


def test_state_file_path_is_range_scoped(tmp_path: Path) -> None:
    path = state_file_path(root=tmp_path, project_id="p1", from_chapter=1, to_chapter=3)

    assert path == tmp_path / "canon_replay" / "p1" / "1-3.state.json"


def test_write_state_atomic_creates_valid_json(tmp_path: Path) -> None:
    path = state_file_path(root=tmp_path, project_id="p1", from_chapter=1, to_chapter=2)
    state = ReplayState(project_id="p1", from_chapter=1, to_chapter=2)

    write_state_atomic(path, state)

    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "canon_replay.v1"
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_range_replay_resumes_after_completed_chapter(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("canon-replay-range-resume"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            seed_accepted_chapter(session, project=project, arc=arc, chapter_number=2)
            session.commit()

        state_path = state_file_path(root=tmp_path, project_id=project.id, from_chapter=1, to_chapter=2)
        write_state_atomic(
            state_path,
            ReplayState(project_id=project.id, from_chapter=1, to_chapter=2).mark_completed(1, {"status": "success"}),
        )

        results = replay_chapter_range(
            session_factory=session_factory,
            project_id=project.id,
            from_chapter=1,
            to_chapter=2,
            llm_client_factory=lambda chapter: FakeCountdownClient(project.id, chapter),
            state_root=tmp_path,
            options=ReplayRangeOptions(
                persist=False,
                mode="dry_run",
                resume=True,
                force_restart=False,
                force_rerun=False,
                abort_on_error=True,
            ),
        )

        assert [result.chapter_number for result in results] == [2]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["chapters"]["1"]["status"] == "completed"
        assert state["chapters"]["2"]["status"] == "completed"
    finally:
        engine.dispose()


def test_range_replay_refuses_existing_state_without_resume_or_force_restart(tmp_path: Path) -> None:
    path = state_file_path(root=tmp_path, project_id="p1", from_chapter=1, to_chapter=2)
    write_state_atomic(path, ReplayState(project_id="p1", from_chapter=1, to_chapter=2))

    with pytest.raises(RuntimeError, match="state file already exists"):
        ReplayState.prepare_existing_state(
            path=path,
            project_id="p1",
            from_chapter=1,
            to_chapter=2,
            resume=False,
            force_restart=False,
        )
```

- [ ] **Step 2: Verify range tests fail**

Run:

```bash
python3 -m pytest tests/test_canon_replay_resume.py -q
```

Expected: import failure for `replay_state`.

- [ ] **Step 3: Implement replay state**

Create `forwin/canon_quality/chapter_review_form/replay_state.py`:

```python
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ReplayState(BaseModel):
    schema_version: str = "canon_replay.v1"
    project_id: str
    from_chapter: int
    to_chapter: int
    started_at: str = Field(default_factory=_now)
    last_updated_at: str = Field(default_factory=_now)
    chapters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    totals: dict[str, int] = Field(default_factory=lambda: {"completed": 0, "errors": 0, "skipped": 0})
    summary: dict[str, Any] = Field(default_factory=dict)

    def mark_completed(self, chapter_number: int, result_summary: dict[str, Any]) -> "ReplayState":
        data = self.model_copy(deep=True)
        data.chapters[str(chapter_number)] = {
            "status": "completed",
            "result_summary": result_summary,
            "last_updated_at": _now(),
        }
        data._recount()
        return data

    def mark_error(self, chapter_number: int, error_message: str) -> "ReplayState":
        data = self.model_copy(deep=True)
        data.chapters[str(chapter_number)] = {
            "status": "error",
            "error_message": error_message,
            "last_updated_at": _now(),
        }
        data._recount()
        return data

    def mark_skipped(self, chapter_number: int, reason: str) -> "ReplayState":
        data = self.model_copy(deep=True)
        data.chapters[str(chapter_number)] = {
            "status": "skipped_due_to_cap" if reason == "cost_cap" else "skipped",
            "reason": reason,
            "last_updated_at": _now(),
        }
        data._recount()
        return data

    def should_skip_completed(self, chapter_number: int, *, force_rerun: bool) -> bool:
        return not force_rerun and self.chapters.get(str(chapter_number), {}).get("status") == "completed"

    def _recount(self) -> None:
        completed = sum(1 for item in self.chapters.values() if item.get("status") == "completed")
        errors = sum(1 for item in self.chapters.values() if item.get("status") == "error")
        skipped = sum(1 for item in self.chapters.values() if str(item.get("status", "")).startswith("skipped"))
        self.totals = {"completed": completed, "errors": errors, "skipped": skipped}
        self.last_updated_at = _now()

    @staticmethod
    def prepare_existing_state(
        *,
        path: Path,
        project_id: str,
        from_chapter: int,
        to_chapter: int,
        resume: bool,
        force_restart: bool,
    ) -> "ReplayState":
        if path.exists() and not resume and not force_restart:
            raise RuntimeError(f"state file already exists: {path}")
        if force_restart:
            return ReplayState(project_id=project_id, from_chapter=from_chapter, to_chapter=to_chapter)
        if resume:
            if not path.exists():
                raise RuntimeError(f"cannot resume missing state file: {path}")
            return ReplayState.model_validate_json(path.read_text(encoding="utf-8"))
        return ReplayState(project_id=project_id, from_chapter=from_chapter, to_chapter=to_chapter)


class ReplayRangeOptions(BaseModel):
    persist: bool = False
    mode: str = "dry_run"
    resume: bool = False
    force_restart: bool = False
    force_rerun: bool = False
    abort_on_error: bool = True
    cost_cap_usd: float | None = None
    no_cost_cap: bool = False


def state_file_path(*, root: Path, project_id: str, from_chapter: int, to_chapter: int) -> Path:
    return root / "canon_replay" / project_id / f"{from_chapter}-{to_chapter}.state.json"


def write_state_atomic(path: Path, state: ReplayState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)
```

- [ ] **Step 4: Implement range replay**

Add `replay_chapter_range()` to `replay.py`:

```python
from pathlib import Path

from .replay_state import ReplayRangeOptions, ReplayState, state_file_path, write_state_atomic


def replay_chapter_range(
    *,
    session_factory,
    project_id: str,
    from_chapter: int,
    to_chapter: int,
    llm_client_factory,
    state_root: Path,
    options: ReplayRangeOptions,
) -> list[ReplayChapterResult]:
    """Replay a range sequentially.

    `llm_client_factory` is intentionally chapter-aware: production may return
    the same client for every chapter, while tests can return chapter-specific
    fake responses without changing production behavior.
    """
    path = state_file_path(
        root=state_root,
        project_id=project_id,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
    )
    state = ReplayState.prepare_existing_state(
        path=path,
        project_id=project_id,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        resume=options.resume,
        force_restart=options.force_restart,
    )
    results: list[ReplayChapterResult] = []
    for chapter_number in range(int(from_chapter), int(to_chapter) + 1):
        if state.should_skip_completed(chapter_number, force_rerun=options.force_rerun):
            continue
        try:
            with session_factory() as session:
                result = replay_single_chapter(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    llm_client=llm_client_factory(chapter_number),
                    persist=options.persist,
                    mode=options.mode,
                )
                session.commit()
            results.append(result)
            state = state.mark_completed(chapter_number, result.model_dump(mode="json"))
            write_state_atomic(path, state)
        except Exception as exc:  # noqa: BLE001
            with session_factory() as session:
                session.rollback()
            state = state.mark_error(chapter_number, str(exc))
            write_state_atomic(path, state)
            if options.abort_on_error:
                break
    return results
```

- [ ] **Step 5: Verify resume tests pass**

Run:

```bash
python3 -m pytest tests/test_canon_replay_resume.py tests/test_canon_replay_single_chapter.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Wire range CLI**

Update `scripts/canon_replay.py` so:

```python
state_root = Path(config.artifact_root)
options = ReplayRangeOptions(
    persist=args.persist,
    mode="primary" if args.persist else "dry_run",
    resume=args.resume,
    force_restart=args.force_restart,
    force_rerun=args.force_rerun,
    abort_on_error=args.abort_on_error,
)
results = replay_chapter_range(
    session_factory=session_factory,
    project_id=args.project_id,
    from_chapter=args.from_chapter,
    to_chapter=resolved_to_chapter,
    llm_client_factory=lambda _chapter: build_llm_client_for_replay(config, args.llm_profile),
    state_root=state_root,
    options=options,
)
for result in results:
    emit_json_line(result.model_dump(mode="json"))
```

Keep `resolved_to_chapter` equal to `args.to_chapter or args.from_chapter` until Phase 6 adds latest accepted chapter lookup.

- [ ] **Step 7: Commit Task 3**

```bash
git add scripts/canon_replay.py forwin/canon_quality/chapter_review_form/replay.py forwin/canon_quality/chapter_review_form/replay_state.py tests/test_canon_replay_resume.py
git commit -m "feat: add resumable canon replay range state"
```

## Task 4: Cost Estimation And Cap Enforcement

**Files:**
- Create: `forwin/canon_quality/chapter_review_form/cost_estimator.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay_state.py`
- Modify: `scripts/canon_replay.py`
- Test: `tests/test_canon_replay_cost.py`

- [ ] **Step 1: Write cost tests**

Create `tests/test_canon_replay_cost.py`:

```python
from __future__ import annotations

from forwin.canon_quality.chapter_review_form.cost_estimator import (
    CostEstimate,
    estimate_tokens_for_text,
    should_abort_for_cost_cap,
    usage_from_llm_client,
)


class ClientWithAttempts:
    llm_attempt_events = [
        {
            "status": "succeeded",
            "input_text": "ASCII prompt with 主倒计时",
            "output_text": "JSON answer with 主倒计时",
        },
        {"status": "failed", "input_chars": 9999, "output_chars": 9999},
    ]


def test_estimate_tokens_for_chinese_text_uses_half_char_ratio() -> None:
    assert estimate_tokens_for_text("主倒计时还有五十九分钟。") >= 6


def test_usage_from_llm_client_uses_last_successful_attempt() -> None:
    usage = usage_from_llm_client(ClientWithAttempts())

    assert usage.input_tokens == estimate_tokens_for_text("ASCII prompt with 主倒计时")
    assert usage.output_tokens == estimate_tokens_for_text("JSON answer with 主倒计时")
    assert usage.estimated is True


def test_cost_cap_aborts_before_next_chapter_estimate_exceeds_cap() -> None:
    current = CostEstimate(total_input_tokens=100, total_output_tokens=100, total_usd=0.90, chapters={})
    next_chapter = CostEstimate(total_input_tokens=20, total_output_tokens=20, total_usd=0.20, chapters={})

    decision = should_abort_for_cost_cap(current_cost=current, next_chapter_estimate=next_chapter, cap_usd=1.00)

    assert decision.abort is True
    assert decision.reason == "cost_cap"
```

- [ ] **Step 2: Verify cost tests fail**

Run:

```bash
python3 -m pytest tests/test_canon_replay_cost.py -q
```

Expected: import failure for `cost_estimator`.

- [ ] **Step 3: Implement cost estimator**

Create `forwin/canon_quality/chapter_review_form/cost_estimator.py`:

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from .replay import ReplayTokenUsage


class CostEstimate(BaseModel):
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_usd: float = 0.0
    chapters: dict[str, dict[str, float | int]] = Field(default_factory=dict)


class CostCapDecision(BaseModel):
    abort: bool = False
    reason: str = ""
    projected_total_usd: float = 0.0


def estimate_tokens_for_text(text: str) -> int:
    chars = len(str(text or ""))
    non_ascii = sum(1 for char in str(text or "") if ord(char) > 127)
    ascii_chars = chars - non_ascii
    return max(1, int(non_ascii * 0.5 + ascii_chars * 0.25))


def estimate_chapter_cost(*, chapter_number: int, body: str, input_price_per_million: float = 0.0, output_price_per_million: float = 0.0) -> CostEstimate:
    input_tokens = estimate_tokens_for_text(body) + 3500
    output_tokens = 3000
    total_usd = (input_tokens / 1_000_000 * input_price_per_million) + (
        output_tokens / 1_000_000 * output_price_per_million
    )
    return CostEstimate(
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_usd=total_usd,
        chapters={str(chapter_number): {"input_tokens": input_tokens, "output_tokens": output_tokens, "usd": total_usd}},
    )


def usage_from_llm_client(llm_client: object) -> ReplayTokenUsage:
    attempts = list(getattr(llm_client, "llm_attempt_events", []) or [])
    successes = [item for item in attempts if str(item.get("status", "")).lower() == "succeeded"]
    if not successes:
        return ReplayTokenUsage(estimated=True)
    last = successes[-1]
    input_tokens = last.get("input_tokens")
    output_tokens = last.get("output_tokens")
    if input_tokens is None:
        input_text = str(last.get("input_text") or "")
        input_tokens = estimate_tokens_for_text(input_text) if input_text else int(float(last.get("input_chars") or 0) * 0.5)
    if output_tokens is None:
        output_text = str(last.get("output_text") or "")
        output_tokens = estimate_tokens_for_text(output_text) if output_text else int(float(last.get("output_chars") or 0) * 0.5)
    return ReplayTokenUsage(
        input_tokens=max(0, int(input_tokens or 0)),
        output_tokens=max(0, int(output_tokens or 0)),
        estimated=not bool(last.get("input_tokens") or last.get("output_tokens")),
    )


def should_abort_for_cost_cap(*, current_cost: CostEstimate, next_chapter_estimate: CostEstimate, cap_usd: float | None) -> CostCapDecision:
    if cap_usd is None:
        return CostCapDecision(abort=False, projected_total_usd=current_cost.total_usd + next_chapter_estimate.total_usd)
    projected = current_cost.total_usd + next_chapter_estimate.total_usd
    return CostCapDecision(
        abort=projected > float(cap_usd),
        reason="cost_cap" if projected > float(cap_usd) else "",
        projected_total_usd=projected,
    )
```

- [ ] **Step 4: Integrate usage and cap into range replay**

Use `ReplayRangeOptions.cost_cap_usd` and `ReplayRangeOptions.no_cost_cap`; do not add more boolean parameters to `replay_chapter_range()`. Before each chapter, reconstruct body once for estimate and call `should_abort_for_cost_cap()`. If it aborts, mark that chapter skipped with `state.mark_skipped(chapter_number, "cost_cap")`, write state, and raise `RuntimeError("cost cap reached before chapter X")`. After each replay, set `result.token_usage = usage_from_llm_client(llm_client)`.

- [ ] **Step 5: Add CLI estimate-only behavior**

In `scripts/canon_replay.py`, implement:

```python
if args.estimate_only:
    estimate = estimate_run(session_factory=session_factory, project_id=args.project_id, from_chapter=args.from_chapter, to_chapter=resolved_to_chapter)
    emit_json_line({"status": "estimate", **estimate.model_dump(mode="json")})
    return 0
if args.cost_cap_usd is None and not args.no_cost_cap:
    emit_json_line({"status": "error", "error": "missing_cost_cap", "message": "Pass --cost-cap-usd <N> or --no-cost-cap."})
    return 2
```

Add `estimate_run()` to `cost_estimator.py`; it loops through accepted draft bodies and returns a `CostEstimate`.

- [ ] **Step 6: Verify cost tests and existing replay tests pass**

Run:

```bash
python3 -m pytest tests/test_canon_replay_cost.py tests/test_canon_replay_resume.py tests/test_canon_replay_single_chapter.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add scripts/canon_replay.py forwin/canon_quality/chapter_review_form/replay.py forwin/canon_quality/chapter_review_form/replay_state.py forwin/canon_quality/chapter_review_form/cost_estimator.py tests/test_canon_replay_cost.py
git commit -m "feat: add canon replay cost controls"
```

## Task 5: Dry-Run Candidate Rows And Diff Mode

**Files:**
- Create: `forwin/canon_quality/chapter_review_form/replay_diff.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- Modify: `scripts/canon_replay.py`
- Test: `tests/test_canon_replay_diff.py`

- [ ] **Step 1: Write diff tests**

Create `tests/test_canon_replay_diff.py`:

```python
from __future__ import annotations

from forwin.canon_quality.chapter_review_form.replay_diff import compute_diff, normalize_countdown_row


def test_normalize_countdown_row_uses_downstream_fields_only() -> None:
    row = normalize_countdown_row(
        {
            "id": "volatile",
            "created_at": "ignored",
            "chapter_number": 3,
            "countdown_key": "main",
            "normalized_remaining_minutes": 59,
            "status": "active",
            "payload": {"evidence_quote": "主倒计时还有59分钟。"},
        }
    )

    assert row.key == "3:countdown:main"
    assert row.fields == {
        "normalized_remaining_minutes": 59,
        "status": "active",
        "evidence_quote": "主倒计时还有59分钟。",
    }


def test_compute_diff_classifies_add_remove_and_change() -> None:
    existing = [
        {"kind": "countdown", "chapter_number": 3, "subject": "main", "fields": {"status": "active", "normalized_remaining_minutes": 60}},
        {"kind": "character_state", "chapter_number": 3, "subject": "韩青", "fields": {"to_state": "alive"}},
    ]
    candidate = [
        {"kind": "countdown", "chapter_number": 3, "subject": "main", "fields": {"status": "active", "normalized_remaining_minutes": 59}},
        {"kind": "countdown", "chapter_number": 3, "subject": "branch", "fields": {"status": "active", "normalized_remaining_minutes": 10}},
    ]

    diff = compute_diff(existing_rows=existing, candidate_rows=candidate)

    assert {item.kind for item in diff} == {"change", "add", "remove"}
    changed = next(item for item in diff if item.kind == "change")
    assert changed.subject == "main"
    assert changed.before["normalized_remaining_minutes"] == 60
    assert changed.after["normalized_remaining_minutes"] == 59


def test_compute_diff_empty_when_logical_rows_match() -> None:
    row = {"kind": "countdown", "chapter_number": 3, "subject": "main", "fields": {"status": "active"}}

    assert compute_diff(existing_rows=[row], candidate_rows=[row]) == []
```

- [ ] **Step 2: Verify diff tests fail**

Run:

```bash
python3 -m pytest tests/test_canon_replay_diff.py -q
```

Expected: import failure for `replay_diff`.

- [ ] **Step 3: Implement diff helper**

Create `forwin/canon_quality/chapter_review_form/replay_diff.py`:

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LogicalReplayRow(BaseModel):
    kind: str
    chapter_number: int
    subject: str
    fields: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.chapter_number}:{self.kind}:{self.subject}"


class ReplayDiff(BaseModel):
    kind: str
    chapter_number: int
    row_kind: str
    subject: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


def normalize_countdown_row(row: dict[str, Any]) -> LogicalReplayRow:
    payload = dict(row.get("payload") or {})
    return LogicalReplayRow(
        kind="countdown",
        chapter_number=int(row.get("chapter_number") or 0),
        subject=str(row.get("countdown_key") or row.get("subject") or ""),
        fields={
            "normalized_remaining_minutes": row.get("normalized_remaining_minutes"),
            "status": row.get("status"),
            "evidence_quote": payload.get("evidence_quote", row.get("evidence_quote", "")),
        },
    )


def normalize_character_row(row: dict[str, Any]) -> LogicalReplayRow:
    payload = dict(row.get("payload") or {})
    return LogicalReplayRow(
        kind="character_state",
        chapter_number=int(row.get("chapter_number") or 0),
        subject=str(row.get("character_name") or row.get("subject") or ""),
        fields={
            "to_state": row.get("to_state"),
            "terminality": row.get("terminality"),
            "evidence_quote": payload.get("evidence_quote", row.get("evidence_quote", "")),
            "subject_of_quote": payload.get("subject_of_quote", row.get("subject_of_quote", "")),
        },
    )


def _coerce(row: dict[str, Any]) -> LogicalReplayRow:
    if "fields" in row:
        return LogicalReplayRow(
            kind=str(row["kind"]),
            chapter_number=int(row["chapter_number"]),
            subject=str(row["subject"]),
            fields=dict(row["fields"]),
        )
    if str(row.get("kind") or "") == "character_state" or "character_name" in row:
        return normalize_character_row(row)
    return normalize_countdown_row(row)


def compute_diff(*, existing_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]) -> list[ReplayDiff]:
    existing = {_coerce(row).key: _coerce(row) for row in existing_rows}
    candidate = {_coerce(row).key: _coerce(row) for row in candidate_rows}
    diffs: list[ReplayDiff] = []
    for key in sorted(set(existing) | set(candidate)):
        before = existing.get(key)
        after = candidate.get(key)
        row = before or after
        assert row is not None
        if before is None:
            diffs.append(ReplayDiff(kind="add", chapter_number=row.chapter_number, row_kind=row.kind, subject=row.subject, after=after.fields if after else None))
        elif after is None:
            diffs.append(ReplayDiff(kind="remove", chapter_number=row.chapter_number, row_kind=row.kind, subject=row.subject, before=before.fields))
        elif before.fields != after.fields:
            diffs.append(ReplayDiff(kind="change", chapter_number=row.chapter_number, row_kind=row.kind, subject=row.subject, before=before.fields, after=after.fields))
    return diffs
```

- [ ] **Step 4: Integrate diff mode**

Add a helper in `replay.py`:

```python
def replay_single_chapter_diff(*, session: Session, project_id: str, chapter_number: int, llm_client: object) -> list[dict[str, Any]]:
    result = replay_single_chapter(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        llm_client=llm_client,
        persist=False,
        mode="dry_run",
    )
    existing = load_existing_form_sourced_rows(session=session, project_id=project_id, chapter_number=chapter_number)
    candidate = result.candidate_rows.get("characters", []) + result.candidate_rows.get("countdowns", [])
    return [item.model_dump(mode="json") for item in compute_diff(existing_rows=existing, candidate_rows=candidate)]
```

Implement `load_existing_form_sourced_rows()` by querying `CharacterStateTransitionRow` and `CountdownLedgerRow` for the project/chapter and keeping only payloads where `payload.source == "chapter_review_form"`.

- [ ] **Step 5: Wire CLI diff mode**

When `args.diff_mode` is true, force `persist=False`, call `replay_single_chapter_diff()` or range equivalent, emit one JSON object per chapter:

```json
{"chapter_number": 7, "status": "diff_completed", "differences": []}
```

- [ ] **Step 6: Verify diff and dry-run tests**

Run:

```bash
python3 -m pytest tests/test_canon_replay_diff.py tests/test_canon_replay_single_chapter.py tests/test_chapter_review_form_dry_run.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 5**

```bash
git add scripts/canon_replay.py forwin/canon_quality/chapter_review_form/replay.py forwin/canon_quality/chapter_review_form/replay_diff.py tests/test_canon_replay_diff.py
git commit -m "feat: add canon replay dry-run diff mode"
```

## Task 6: Operational Polish, Preflight, And Documentation

**Files:**
- Modify: `scripts/canon_replay.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay_state.py`
- Create: `docs/operations/canon_replay.md`
- Test: `tests/test_canon_replay_cli.py`

- [ ] **Step 1: Add CLI safety tests**

Append to `tests/test_canon_replay_cli.py`:

```python
from pathlib import Path


def test_clear_state_requires_confirm_clear(tmp_path: Path) -> None:
    state_path = tmp_path / "canon_replay" / "p1" / "1-2.state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")

    result = canon_replay.clear_state_if_requested(
        clear_state=True,
        confirm_clear=False,
        state_path=state_path,
    )

    assert result["status"] == "error"
    assert state_path.exists()


def test_clear_state_deletes_when_confirmed(tmp_path: Path) -> None:
    state_path = tmp_path / "canon_replay" / "p1" / "1-2.state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")

    result = canon_replay.clear_state_if_requested(
        clear_state=True,
        confirm_clear=True,
        state_path=state_path,
    )

    assert result["status"] == "cleared"
    assert not state_path.exists()


def test_missing_cost_cap_returns_structured_error() -> None:
    result = canon_replay.validate_cost_cap_args(cost_cap_usd=None, no_cost_cap=False)

    assert result == {
        "status": "error",
        "error": "missing_cost_cap",
        "message": "Pass --cost-cap-usd <N> or --no-cost-cap.",
    }


def test_schema_version_warning_when_pinned_version_differs() -> None:
    warning = canon_replay.schema_version_warning(
        requested_schema_version="chapter_review_form.v2",
        current_schema_version="chapter_review_form.v1",
    )

    assert warning == {
        "status": "warning",
        "warning": "schema_version_mismatch",
        "requested_schema_version": "chapter_review_form.v2",
        "current_schema_version": "chapter_review_form.v1",
    }


def test_schema_version_warning_empty_when_unpinned_or_matching() -> None:
    assert canon_replay.schema_version_warning(
        requested_schema_version="",
        current_schema_version="chapter_review_form.v1",
    ) == {}
    assert canon_replay.schema_version_warning(
        requested_schema_version="chapter_review_form.v1",
        current_schema_version="chapter_review_form.v1",
    ) == {}
```

- [ ] **Step 2: Verify safety tests fail**

Run:

```bash
python3 -m pytest tests/test_canon_replay_cli.py -q
```

Expected: missing helper failures.

- [ ] **Step 3: Implement CLI safety helpers**

Add to `scripts/canon_replay.py`:

```python
from pathlib import Path


def clear_state_if_requested(*, clear_state: bool, confirm_clear: bool, state_path: Path) -> dict[str, str]:
    if not clear_state:
        return {"status": "not_requested"}
    if not confirm_clear:
        return {"status": "error", "error": "confirm_clear_required", "message": "Pass --confirm-clear to delete replay state."}
    if state_path.exists():
        state_path.unlink()
    return {"status": "cleared", "state_file": str(state_path)}


def validate_cost_cap_args(*, cost_cap_usd: float | None, no_cost_cap: bool) -> dict[str, str]:
    if cost_cap_usd is None and not no_cost_cap:
        return {
            "status": "error",
            "error": "missing_cost_cap",
            "message": "Pass --cost-cap-usd <N> or --no-cost-cap.",
        }
    return {"status": "ok"}


def schema_version_warning(*, requested_schema_version: str, current_schema_version: str) -> dict[str, str]:
    requested = str(requested_schema_version or "").strip()
    current = str(current_schema_version or "").strip()
    if not requested or requested == current:
        return {}
    return {
        "status": "warning",
        "warning": "schema_version_mismatch",
        "requested_schema_version": requested,
        "current_schema_version": current,
    }
```

- [ ] **Step 4: Add final summary and latest accepted chapter lookup**

In `replay.py`, add:

```python
def latest_accepted_chapter(*, session: Session, project_id: str) -> int:
    value = session.execute(
        select(CandidateDraftRecord.chapter_number)
        .where(
            CandidateDraftRecord.project_id == project_id,
            CandidateDraftRecord.status == "canon_committed",
            CandidateDraftRecord.canon_status == "canon",
        )
        .order_by(CandidateDraftRecord.chapter_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if value is None:
        raise ChapterDraftNotFound(f"no accepted chapters found for project={project_id}")
    return int(value)


def summarize_replay_results(results: list[ReplayChapterResult]) -> dict[str, object]:
    return {
        "chapters_completed": sum(1 for item in results if item.status == "success"),
        "chapters_failed": sum(1 for item in results if item.status == "error"),
        "total_input_tokens": sum(item.token_usage.input_tokens for item in results),
        "total_output_tokens": sum(item.token_usage.output_tokens for item in results),
        "blocking_chapters": [item.chapter_number for item in results if item.blocking],
    }
```

Use `latest_accepted_chapter()` in CLI when `--to-chapter` is omitted. Also call `schema_version_warning(requested_schema_version=args.schema_version, current_schema_version=FORM_SCHEMA_VERSION)` before any LLM call; emit the warning to stderr as one JSON line and include it in the final summary. This flag pins operator intent and observability only; it must not change `FORM_SCHEMA_VERSION` or mutate the form schema implementation.

- [ ] **Step 5: Write operator documentation**

Create `docs/operations/canon_replay.md` with this structure and commands:

```markdown
# Canon Replay Operator Guide

## When To Use

Use canon replay after legacy canon migration, schema-version upgrades, LLM re-validation, or targeted re-audit of an accepted chapter. Do not use it to regenerate chapter prose, plans, drafts, world-model projections, Obsidian exports, or generation tasks.

## Recommended Workflow

1. Estimate cost.
2. Dry-run a narrow range.
3. Run diff mode against existing form-sourced rows.
4. Persist only after reviewing the dry-run and diff output.

## Examples

### Post-Migration Backfill

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --estimate-only
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --dry-run --cost-cap-usd 5
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --diff-mode --cost-cap-usd 5
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --persist --cost-cap-usd 5
```

### Schema Version Upgrade

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 60 --schema-version chapter_review_form.v2 --diff-mode --cost-cap-usd 10
```

### LLM Upgrade Re-Validation

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 10 --to-chapter 12 --llm-profile env-deepseek --diff-mode --cost-cap-usd 2
```

### Targeted Re-Audit

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 7 --to-chapter 7 --dry-run --cost-cap-usd 1
```

## Resume

State files live under `data/artifacts/canon_replay/<project_id>/<from>-<to>.state.json`.

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --resume --persist --cost-cap-usd 5
```

## Troubleshooting

- `missing_accepted_draft`: regenerate or accept the chapter through the normal writer workflow first.
- `missing_cost_cap`: pass `--cost-cap-usd <N>` or `--no-cost-cap`.
- `state file already exists`: pass `--resume` to continue or `--force-restart` to start a new state.
- `cost_cap`: inspect the state file, raise the cap, then resume.
```

- [ ] **Step 6: Verify all replay tests pass**

Run:

```bash
python3 -m pytest tests/test_canon_replay_*.py -q
```

Expected: all canon replay tests pass.

- [ ] **Step 7: Commit Task 6**

```bash
git add scripts/canon_replay.py forwin/canon_quality/chapter_review_form/replay.py forwin/canon_quality/chapter_review_form/replay_state.py docs/operations/canon_replay.md tests/test_canon_replay_cli.py
git commit -m "docs: add canon replay operator workflow"
```

## Task 7: Final Verification And Manual Staging Run

**Files:**
- No production file changes expected.
- Test command outputs and manual staging notes go in the final implementation response.

- [ ] **Step 1: Run focused replay suite**

Run:

```bash
python3 -m pytest tests/test_canon_replay_*.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run related regression suite**

Run:

```bash
python3 -m pytest tests/test_canon_quality_service.py tests/test_chapter_review_form_dry_run.py tests/test_canon_projector.py tests/test_legacy_canon_supersede.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run compile and diff hygiene**

Run:

```bash
python3 -m compileall -q forwin scripts
git diff --check
```

Expected: both commands exit 0 with no output.

- [ ] **Step 4: Run a staging estimate-only smoke**

Use a small existing writing project with at least two accepted chapters. Replace `PROJECT_ID` with the selected project id:

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 2 --estimate-only
```

Expected: stdout contains one JSON object with `"status": "estimate"` and no LLM call is made.

- [ ] **Step 5: Run a staging dry-run smoke**

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 1 --dry-run --cost-cap-usd 1
```

Expected: stdout contains a chapter result with `"status": "success"`, state file is written, and no new canon rows are persisted.

- [ ] **Step 6: Run a staging diff smoke**

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 1 --diff-mode --cost-cap-usd 1 --force-rerun --resume
```

Expected: stdout contains `"status": "diff_completed"` with a `differences` array. Empty differences are acceptable.

- [ ] **Step 7: Run a one-chapter persist smoke only after dry-run and diff are reviewed**

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 1 --persist --cost-cap-usd 1 --force-rerun --resume
```

Expected: stdout contains `"status": "success"` and form-sourced canon rows exist for that chapter. Do not run broad persist before operator review.

- [ ] **Step 8: Final status check**

Run:

```bash
git status --short --branch
```

Expected: clean working tree on the current implementation branch.

## Plan Self-Review

- Spec coverage: Tasks 1-2 cover reconstruction and single-chapter replay; Task 3 covers range, resume, and per-chapter commit; Task 4 covers estimate-only and cost cap; Task 5 covers dry-run candidate rows and diff mode; Task 6 covers operator polish, explicit cost-cap safety, state clearing, schema flag, latest accepted range, and docs; Task 7 covers verification and staging smoke.
- Scope check: This plan keeps replay as a standalone CLI and does not add UI, API, generation queue integration, parallel processing, or legacy migration behavior.
- Review fixes applied: `prompt_revision_hash` follows the design value `"replay"`; shared fixtures live in `tests/helpers/canon_replay.py`; range options use `ReplayRangeOptions`; `--schema-version` has explicit tests and warning behavior; profile resolver failures are tested before replay writes; cost usage falls back through the shared token estimator when prompt/response text is available.
- Ambiguity resolved: final CLI requires `--cost-cap-usd` or `--no-cost-cap` before LLM calls, and `--llm-profile` resolves against Config primary plus environment fallback profiles.
