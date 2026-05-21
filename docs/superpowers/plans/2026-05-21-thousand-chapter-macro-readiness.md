# Thousand-Chapter Macro Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the remaining thousand-chapter readiness scope by adding arc-level macro progression, project progression rules, BookState macro-status projection, arc-boundary audits, publisher feedback calibration, 1000-chapter dry-run metrics, and the final UI target-limit cleanup.

**Architecture:** Keep Arc as the only macro planning unit. Store arc progression as typed JSON on `ArcPlanVersion`, store project-level planning constraints in one rule table, derive protagonist macro status from existing accepted state and BookState-facing sources, then wire those contracts into trope selection, future-plan audit, and read-only pressure reporting. Reuse existing publisher feedback aggregates and avoid Saga, Volume, new ledger tables, external queues, or legacy compatibility paths.

**Tech Stack:** Python 3.12+, SQLAlchemy, Alembic migrations, Pydantic, pytest, existing ForWin BookState, planning, experience, publisher feedback, and pressure-reporting modules.

---

## File Structure

- Modify `forwin/models/project.py`: add `ArcPlanVersion.macro_progression_json`.
- Create `forwin/migrations/versions/0015_arc_macro_progression.py`: add the arc macro JSON column.
- Create `forwin/planning/macro_progression.py`: typed `ArcMacroProgression` model and load/dump helpers.
- Create `tests/test_arc_macro_progression.py`: model, migration-adjacent, and helper tests.
- Create `forwin/models/progression.py`: `ProjectProgressionRule` SQLAlchemy model.
- Modify `forwin/models/__init__.py`: export `ProjectProgressionRule`.
- Create `forwin/migrations/versions/0016_project_progression_rules.py`: create rule table and indexes.
- Create `forwin/planning/progression_rules.py`: repository and active-rule selectors.
- Create `tests/test_project_progression_rules.py`: repository and chapter-range tests.
- Modify `forwin/ui_assets/home/body.html`: change target chapter max from 200 to 5000.
- Create `tests/test_entry_contract_static.py`: static contract assertion across schema, MCP, JS, and markup.
- Create `forwin/book_state/macro_status.py`: `ProtagonistMacroStatus` projection helpers.
- Modify `forwin/book_state/query_interface.py`: expose `get_protagonist_macro_status()`.
- Create `tests/test_book_state_macro_status.py`: derived macro projection tests.
- Modify `forwin/experience/service.py`: extend canonical `AudienceCalibrationProfile` and publisher feedback mapping.
- Modify `forwin/planning/band_plan_service.py`: load progression rules and pass them into calibration.
- Modify `forwin/experience/band_scheduler.py`: filter or warn on blocked trope templates/categories.
- Create `tests/test_progression_rule_trope_filter.py`: rule-aware trope scheduler tests.
- Create `forwin/planning/future_plan_audit/macro_progression.py`: arc boundary audit mixin/helper.
- Modify `forwin/planning/future_plan_audit/auditor.py`: include macro-progression audit.
- Create `tests/test_future_plan_macro_progression_audit.py`: boundary pass/fail tests.
- Modify `scripts/pulp_pressure_test.py`: add macro dry-run metrics.
- Extend `tests/test_pulp_pressure_test.py`: seeded macro dry-run assertions.

## Task 1: Static Entry Contract Cleanup

**Files:**
- Modify: `forwin/ui_assets/home/body.html`
- Create: `tests/test_entry_contract_static.py`

- [ ] **Step 1: Write the failing static test**

Create `tests/test_entry_contract_static.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_target_total_chapter_limit_is_5000_in_user_entrypoints() -> None:
    project_schema = (ROOT / "forwin/api_schema/project.py").read_text()
    mcp_client = (ROOT / "forwin/mcp/client.py").read_text()
    home_js = (ROOT / "forwin/ui_assets/home/app_library.js").read_text()
    home_html = (ROOT / "forwin/ui_assets/home/body.html").read_text()

    assert "le=5000" in project_schema
    assert "target_total_chapters > 5000" in mcp_client
    assert "payload.target_total_chapters > 5000" in home_js
    assert 'id="book_form_target_total_chapters"' in home_html
    assert 'max="5000"' in home_html
    assert 'max="200"' not in home_html
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```bash
python3 -m pytest tests/test_entry_contract_static.py -q
```

Expected: FAIL because `forwin/ui_assets/home/body.html` still contains `max="200"`.

- [ ] **Step 3: Update the HTML input limit**

In `forwin/ui_assets/home/body.html`, change:

```html
<input id="book_form_target_total_chapters" type="number" min="1" max="200" value="@@DEFAULT_CHAPTERS@@">
```

to:

```html
<input id="book_form_target_total_chapters" type="number" min="1" max="5000" value="@@DEFAULT_CHAPTERS@@">
```

- [ ] **Step 4: Verify**

Run:

```bash
python3 -m pytest tests/test_entry_contract_static.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/ui_assets/home/body.html tests/test_entry_contract_static.py
git commit -m "fix: align chapter target UI limit"
```

## Task 2: Arc Macro Progression Contract

**Files:**
- Modify: `forwin/models/project.py`
- Create: `forwin/migrations/versions/0015_arc_macro_progression.py`
- Create: `forwin/planning/macro_progression.py`
- Create: `tests/test_arc_macro_progression.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arc_macro_progression.py`:

```python
from forwin.models.project import ArcPlanVersion
from forwin.planning.macro_progression import (
    ArcMacroProgression,
    dump_arc_macro_progression,
    load_arc_macro_progression,
)


def test_arc_plan_version_has_macro_progression_json_default() -> None:
    arc = ArcPlanVersion(
        project_id="project-1",
        arc_synopsis="主角从村镇进入县城市场。",
    )

    assert arc.macro_progression_json == "{}"


def test_arc_macro_progression_normalizes_tiers_and_lists() -> None:
    progression = ArcMacroProgression.model_validate(
        {
            "status_promise": "公开赢下县城资格",
            "status_tier_from": "1",
            "status_tier_to": "2",
            "wealth_tier_from": None,
            "wealth_tier_to": 3,
            "enemy_tier_from": 1,
            "enemy_tier_to": "4",
            "market_space_from": "村镇",
            "market_space_to": "县城",
            "ladder_rung_target": "village_to_county",
            "required_boundary_evidence": ["县城资格到手", ""],
            "forbidden_repetition_patterns": ["重复退婚打脸", ""],
        }
    )

    assert progression.status_tier_from == 1
    assert progression.status_tier_to == 2
    assert progression.wealth_tier_from == 0
    assert progression.wealth_tier_to == 3
    assert progression.enemy_tier_to == 4
    assert progression.required_boundary_evidence == ["县城资格到手"]
    assert progression.forbidden_repetition_patterns == ["重复退婚打脸"]


def test_arc_macro_progression_load_dump_round_trip() -> None:
    arc = ArcPlanVersion(project_id="project-1", arc_synopsis="a")
    progression = ArcMacroProgression(
        status_promise="进入内门",
        status_tier_from=1,
        status_tier_to=2,
        market_space_from="外门",
        market_space_to="内门",
        ladder_rung_target="outer_to_inner_sect",
    )

    arc.macro_progression_json = dump_arc_macro_progression(progression)

    assert load_arc_macro_progression(arc).status_promise == "进入内门"
    assert load_arc_macro_progression(arc).status_tier_to == 2
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_arc_macro_progression.py -q
```

Expected: FAIL because the model column and helper module do not exist.

- [ ] **Step 3: Add the model column**

In `forwin/models/project.py`, add `Text` is already imported. Add to
`ArcPlanVersion` after `arc_synopsis`:

```python
    macro_progression_json: Mapped[str] = mapped_column(Text, default="{}")
```

- [ ] **Step 4: Add the migration**

Create `forwin/migrations/versions/0015_arc_macro_progression.py`:

```python
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0015_arc_macro_progression"
down_revision = "0014_project_target_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "arc_plan_versions",
        sa.Column("macro_progression_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("arc_plan_versions", "macro_progression_json")
```

- [ ] **Step 5: Add typed helpers**

Create `forwin/planning/macro_progression.py`:

```python
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ArcMacroProgression(BaseModel):
    status_promise: str = ""
    status_tier_from: int = 0
    status_tier_to: int = 0
    wealth_tier_from: int = 0
    wealth_tier_to: int = 0
    enemy_tier_from: int = 0
    enemy_tier_to: int = 0
    market_space_from: str = ""
    market_space_to: str = ""
    ladder_rung_target: str = ""
    required_boundary_evidence: list[str] = Field(default_factory=list)
    forbidden_repetition_patterns: list[str] = Field(default_factory=list)

    @field_validator(
        "status_promise",
        "market_space_from",
        "market_space_to",
        "ladder_rung_target",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator(
        "status_tier_from",
        "status_tier_to",
        "wealth_tier_from",
        "wealth_tier_to",
        "enemy_tier_from",
        "enemy_tier_to",
        mode="before",
    )
    @classmethod
    def _clean_tier(cls, value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @field_validator("required_boundary_evidence", "forbidden_repetition_patterns", mode="before")
    @classmethod
    def _clean_text_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]


def load_arc_macro_progression(arc: object) -> ArcMacroProgression:
    raw = getattr(arc, "macro_progression_json", "{}") or "{}"
    try:
        payload: Any = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ArcMacroProgression.model_validate(payload)


def dump_arc_macro_progression(progression: ArcMacroProgression) -> str:
    return json.dumps(progression.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


__all__ = [
    "ArcMacroProgression",
    "dump_arc_macro_progression",
    "load_arc_macro_progression",
]
```

- [ ] **Step 6: Verify**

Run:

```bash
python3 -m pytest tests/test_arc_macro_progression.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add forwin/models/project.py forwin/migrations/versions/0015_arc_macro_progression.py forwin/planning/macro_progression.py tests/test_arc_macro_progression.py
git commit -m "feat: add arc macro progression contract"
```

## Task 3: Project Progression Rules

**Files:**
- Create: `forwin/models/progression.py`
- Modify: `forwin/models/__init__.py`
- Create: `forwin/migrations/versions/0016_project_progression_rules.py`
- Create: `forwin/planning/progression_rules.py`
- Create: `tests/test_project_progression_rules.py`

- [ ] **Step 1: Write the failing repository tests**

Create `tests/test_project_progression_rules.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.models import Base, ProjectProgressionRule
from forwin.planning.progression_rules import (
    ProgressionRuleRepository,
    active_progression_rules_for_chapter,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_repository_lists_active_rules_for_chapter_range() -> None:
    session = _session()
    session.add_all(
        [
            ProjectProgressionRule(
                project_id="p1",
                rule_type="repetition_ban",
                chapter_start=1,
                chapter_end=50,
                severity="blocking",
                payload_json='{"blocked_categories":["cheap_insult"]}',
                active=True,
            ),
            ProjectProgressionRule(
                project_id="p1",
                rule_type="wealth_ceiling",
                chapter_start=60,
                chapter_end=80,
                severity="warning",
                payload_json='{"max_tier":3}',
                active=True,
            ),
            ProjectProgressionRule(
                project_id="p1",
                rule_type="trope_filter",
                chapter_start=1,
                chapter_end=50,
                severity="blocking",
                payload_json='{"blocked_template_ids":["face_slap_001"]}',
                active=False,
            ),
        ]
    )
    session.commit()

    rules = active_progression_rules_for_chapter(session, project_id="p1", chapter_number=25)

    assert [rule.rule_type for rule in rules] == ["repetition_ban"]
    assert rules[0].payload["blocked_categories"] == ["cheap_insult"]


def test_repository_create_rule_normalizes_payload() -> None:
    session = _session()
    repo = ProgressionRuleRepository(session)

    rule = repo.create_rule(
        project_id="p1",
        rule_type="trope_filter",
        chapter_start=10,
        chapter_end=20,
        severity="blocking",
        payload={"blocked_template_ids": ["t1"]},
    )
    session.commit()

    loaded = active_progression_rules_for_chapter(session, project_id="p1", chapter_number=15)
    assert loaded[0].id == rule.id
    assert loaded[0].payload == {"blocked_template_ids": ["t1"]}
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_project_progression_rules.py -q
```

Expected: FAIL because the model and repository do not exist.

- [ ] **Step 3: Add the SQLAlchemy model**

Create `forwin/models/progression.py`:

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class ProjectProgressionRule(Base):
    __tablename__ = "project_progression_rules"
    __table_args__ = (
        Index("ix_project_progression_rules_project_range", "project_id", "chapter_start", "chapter_end"),
        Index("ix_project_progression_rules_project_type", "project_id", "rule_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    rule_type: Mapped[str] = mapped_column(String, nullable=False)
    chapter_start: Mapped[int] = mapped_column(Integer, default=1)
    chapter_end: Mapped[int] = mapped_column(Integer, default=0)
    severity: Mapped[str] = mapped_column(String, default="warning")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
```

Export it from `forwin/models/__init__.py` by importing it and adding
`"ProjectProgressionRule"` to `__all__`.

- [ ] **Step 4: Add the migration**

Create `forwin/migrations/versions/0016_project_progression_rules.py`:

```python
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0016_project_progression_rules"
down_revision = "0015_arc_macro_progression"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_progression_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("rule_type", sa.String(), nullable=False),
        sa.Column("chapter_start", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("chapter_end", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("severity", sa.String(), nullable=False, server_default="warning"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_project_progression_rules_project_range",
        "project_progression_rules",
        ["project_id", "chapter_start", "chapter_end"],
    )
    op.create_index(
        "ix_project_progression_rules_project_type",
        "project_progression_rules",
        ["project_id", "rule_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_progression_rules_project_type", table_name="project_progression_rules")
    op.drop_index("ix_project_progression_rules_project_range", table_name="project_progression_rules")
    op.drop_table("project_progression_rules")
```

- [ ] **Step 5: Add repository and typed wrappers**

Create `forwin/planning/progression_rules.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import ProjectProgressionRule, new_id


@dataclass(slots=True)
class ActiveProgressionRule:
    id: str
    rule_type: str
    severity: str
    chapter_start: int
    chapter_end: int
    payload: dict[str, Any]


class ProgressionRuleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_rule(
        self,
        *,
        project_id: str,
        rule_type: str,
        chapter_start: int,
        chapter_end: int,
        severity: str,
        payload: dict[str, Any],
    ) -> ProjectProgressionRule:
        row = ProjectProgressionRule(
            id=new_id(),
            project_id=project_id,
            rule_type=rule_type,
            chapter_start=max(1, int(chapter_start or 1)),
            chapter_end=max(0, int(chapter_end or 0)),
            severity=severity if severity in {"warning", "blocking"} else "warning",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            active=True,
        )
        self.session.add(row)
        self.session.flush()
        return row


def active_progression_rules_for_chapter(
    session: Session,
    *,
    project_id: str,
    chapter_number: int,
) -> list[ActiveProgressionRule]:
    chapter = int(chapter_number or 0)
    rows = session.execute(
        select(ProjectProgressionRule)
        .where(
            ProjectProgressionRule.project_id == project_id,
            ProjectProgressionRule.active.is_(True),
            ProjectProgressionRule.chapter_start <= chapter,
            (ProjectProgressionRule.chapter_end == 0) | (ProjectProgressionRule.chapter_end >= chapter),
        )
        .order_by(ProjectProgressionRule.chapter_start.asc(), ProjectProgressionRule.created_at.asc())
    ).scalars().all()
    return [_to_active_rule(row) for row in rows]


def _to_active_rule(row: ProjectProgressionRule) -> ActiveProgressionRule:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ActiveProgressionRule(
        id=str(row.id or ""),
        rule_type=str(row.rule_type or ""),
        severity=str(row.severity or "warning"),
        chapter_start=int(row.chapter_start or 0),
        chapter_end=int(row.chapter_end or 0),
        payload=payload,
    )
```

- [ ] **Step 6: Verify**

Run:

```bash
python3 -m pytest tests/test_project_progression_rules.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add forwin/models/progression.py forwin/models/__init__.py forwin/migrations/versions/0016_project_progression_rules.py forwin/planning/progression_rules.py tests/test_project_progression_rules.py
git commit -m "feat: add project progression rules"
```

## Task 4: BookState Macro Status Projection

**Files:**
- Create: `forwin/book_state/macro_status.py`
- Modify: `forwin/book_state/query_interface.py`
- Create: `tests/test_book_state_macro_status.py`

- [ ] **Step 1: Write the failing projection tests**

Create `tests/test_book_state_macro_status.py`:

```python
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state.query_interface import SqlBookStateQueryInterface
from forwin.models import Base, ChapterPlan, Project, ArcPlanVersion


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_macro_status_projection_derives_from_accepted_chapter_experience() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(ArcPlanVersion(id="a1", project_id="p1", arc_synopsis="a", chapter_start=1, chapter_end=10))
    session.add(
        ChapterPlan(
            project_id="p1",
            arc_plan_id="a1",
            chapter_number=8,
            status="accepted",
            experience_plan_json=json.dumps(
                {
                    "macro_status": {
                        "status_tier": 2,
                        "wealth_tier": 3,
                        "enemy_tier": 2,
                        "market_space": "县城",
                    }
                },
                ensure_ascii=False,
            ),
        )
    )
    session.commit()

    status = SqlBookStateQueryInterface(session).get_protagonist_macro_status(
        project_id="p1",
        as_of_chapter=8,
    )

    assert status.status_tier == 2
    assert status.wealth_tier == 3
    assert status.enemy_tier == 2
    assert status.market_space == "县城"
    assert status.evidence_refs == ["chapter_plan:8"]
    assert status.source == "book_state_macro_projection"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_book_state_macro_status.py -q
```

Expected: FAIL because the query method does not exist.

- [ ] **Step 3: Add macro status projection helper**

Create `forwin/book_state/macro_status.py`:

```python
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import ChapterPlan


class ProtagonistMacroStatus(BaseModel):
    project_id: str
    as_of_chapter: int
    status_tier: int = 0
    wealth_tier: int = 0
    enemy_tier: int = 0
    market_space: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    source: str = "book_state_macro_projection"

    @field_validator("status_tier", "wealth_tier", "enemy_tier", mode="before")
    @classmethod
    def _clean_tier(cls, value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0


def derive_protagonist_macro_status(
    session: Session,
    *,
    project_id: str,
    as_of_chapter: int,
) -> ProtagonistMacroStatus:
    rows = session.execute(
        select(ChapterPlan)
        .where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.status == "accepted",
            ChapterPlan.chapter_number <= int(as_of_chapter or 0),
        )
        .order_by(ChapterPlan.chapter_number.asc())
    ).scalars().all()
    status = ProtagonistMacroStatus(project_id=project_id, as_of_chapter=int(as_of_chapter or 0))
    for row in rows:
        payload = _loads(row.experience_plan_json, {})
        macro = payload.get("macro_status") if isinstance(payload, dict) else None
        if not isinstance(macro, dict):
            continue
        update: dict[str, Any] = {}
        for key in ("status_tier", "wealth_tier", "enemy_tier", "market_space"):
            if key in macro and macro[key] not in (None, ""):
                update[key] = macro[key]
        if update:
            refs = list(status.evidence_refs)
            refs.append(f"chapter_plan:{int(row.chapter_number or 0)}")
            update["evidence_refs"] = refs[-8:]
            status = status.model_copy(update=update)
    return status


def _loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
```

- [ ] **Step 4: Expose the query-interface method**

In `forwin/book_state/query_interface.py`, import:

```python
from forwin.book_state.macro_status import ProtagonistMacroStatus, derive_protagonist_macro_status
```

Add to `BookStateQueryInterface`:

```python
    def get_protagonist_macro_status(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> ProtagonistMacroStatus: ...
```

Add to `SqlBookStateQueryInterface`:

```python
    def get_protagonist_macro_status(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> ProtagonistMacroStatus:
        return derive_protagonist_macro_status(
            self.session,
            project_id=project_id,
            as_of_chapter=int(as_of_chapter or 0),
        )
```

Add `"ProtagonistMacroStatus"` to `__all__`.

- [ ] **Step 5: Verify**

Run:

```bash
python3 -m pytest tests/test_book_state_macro_status.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/book_state/macro_status.py forwin/book_state/query_interface.py tests/test_book_state_macro_status.py
git commit -m "feat: expose protagonist macro status projection"
```

## Task 5: Rule-Aware Trope Selection And Feedback Calibration

**Files:**
- Modify: `forwin/experience/service.py`
- Modify: `forwin/planning/band_plan_service.py`
- Modify: `forwin/experience/band_scheduler.py`
- Test: `tests/test_progression_rule_trope_filter.py`
- Extend: existing audience feedback tests if a local fixture already covers `build_audience_calibration_profile()`

- [ ] **Step 1: Write failing scheduler tests**

Create `tests/test_progression_rule_trope_filter.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.service import AudienceCalibrationProfile
from forwin.models import Base
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_scheduler_filters_progression_rule_blocked_template_ids() -> None:
    scheduler = BandExperienceScheduler()
    calibration = AudienceCalibrationProfile(
        progression_blocked_template_ids=["face_slap_001"],
        progression_blocked_categories=[],
    )

    schedule = scheduler.derive_band_delight_schedule(
        band_id="b1",
        chapter_start=1,
        chapter_end=4,
        structure=None,
        arc_experience=(ReaderPromise(), ArcPayoffMap()),
        active_band=None,
        calibration=calibration,
        cost_ceiling=3,
    )

    selected = {item.template_id for item in schedule.scheduled_rewards}
    assert "face_slap_001" not in selected


def test_feedback_calibration_sets_visible_payoff_for_pacing_signal() -> None:
    from forwin.models import SignalWindowAggregate
    from forwin.experience.service import ExperiencePlanningService

    session = _session()
    session.add(
        SignalWindowAggregate(
            project_id="p1",
            signal_key="pacing:slow_setup",
            signal_type="pacing",
            target_name="整体",
            window_type="long",
            window_chapter_start=1,
            window_chapter_end=20,
            hit_comment_count=5,
            unique_user_count=4,
            total_comment_count=8,
            reader_estimate=200,
            max_severity=3,
            avg_confidence=0.8,
            signal_level="confirmed",
        )
    )
    session.commit()

    profile = ExperiencePlanningService().build_audience_calibration_profile(
        session=session,
        project_id="p1",
    )

    assert profile.boost_reward_density is True
    assert profile.favor_visible_payoff is True
    assert profile.reduce_setup_ratio is True
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_progression_rule_trope_filter.py -q
```

Expected: FAIL because the new calibration fields do not exist and the scheduler does not consume progression blocks.

- [ ] **Step 3: Extend canonical calibration profile**

In `forwin/experience/service.py`, extend `AudienceCalibrationProfile`:

```python
    favor_visible_payoff: bool = False
    reduce_setup_ratio: bool = False
    boost_status_payoff: bool = False
    avoid_trope_categories: list[str] | None = None
    progression_blocked_template_ids: list[str] | None = None
    progression_blocked_categories: list[str] | None = None
```

Inside `build_audience_calibration_profile()`, when a strong pacing signal is
present, set:

```python
profile.boost_reward_density = True
profile.favor_visible_payoff = True
profile.reduce_setup_ratio = True
```

When a strong status or scale signal is present, set:

```python
profile.boost_status_payoff = True
```

When a strong risk/confusion signal has a target category, append that normalized
category to `profile.avoid_trope_categories`.

- [ ] **Step 4: Load active progression rules in band planning**

In `forwin/planning/band_plan_service.py`, after `calibration` is built and
before calling the scheduler, load active rules for `window.chapter_start`:

```python
from forwin.planning.progression_rules import active_progression_rules_for_chapter

rules = active_progression_rules_for_chapter(
    session,
    project_id=request.project_id,
    chapter_number=window.chapter_start,
)
blocked_template_ids: list[str] = []
blocked_categories: list[str] = []
for rule in rules:
    if rule.rule_type in {"trope_filter", "repetition_ban"}:
        blocked_template_ids.extend(str(item) for item in rule.payload.get("blocked_template_ids", []))
        blocked_categories.extend(str(item) for item in rule.payload.get("blocked_categories", []))
calibration.progression_blocked_template_ids = [item for item in blocked_template_ids if item]
calibration.progression_blocked_categories = [item for item in blocked_categories if item]
```

- [ ] **Step 5: Filter scheduler candidates**

In `forwin/experience/band_scheduler.py`, before final template selection,
derive blocked sets:

```python
blocked_template_ids = set(getattr(calibration, "progression_blocked_template_ids", []) or [])
blocked_categories = set(getattr(calibration, "progression_blocked_categories", []) or [])
avoid_categories = set(getattr(calibration, "avoid_trope_categories", []) or [])
blocked_categories.update(avoid_categories)
```

Exclude a candidate when its `template_id` is in `blocked_template_ids` or its
category is in `blocked_categories`. If all candidates are excluded, use the
existing candidate order and add a schedule metadata warning if the schedule
model supports metadata; if it does not, keep fallback selection deterministic
and cover the blocked-primary case in the test by asserting the preferred
blocked template is avoided when alternatives exist.

- [ ] **Step 6: Verify**

Run:

```bash
python3 -m pytest tests/test_progression_rule_trope_filter.py tests/test_trope_cooldown.py tests/test_audience_feedback_alignment.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add forwin/experience/service.py forwin/planning/band_plan_service.py forwin/experience/band_scheduler.py tests/test_progression_rule_trope_filter.py
git commit -m "feat: apply progression rules to experience planning"
```

## Task 6: Arc Boundary Macro Audit

**Files:**
- Create: `forwin/planning/future_plan_audit/macro_progression.py`
- Modify: `forwin/planning/future_plan_audit/auditor.py`
- Modify: `forwin/planning/future_plan_audit/__init__.py` if exports are needed
- Test: `tests/test_future_plan_macro_progression_audit.py`

- [ ] **Step 1: Write failing audit tests**

Create `tests/test_future_plan_macro_progression_audit.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state.macro_status import ProtagonistMacroStatus
from forwin.models import Base, Project, ArcPlanVersion
from forwin.planning.future_plan_audit.macro_progression import audit_arc_macro_boundary
from forwin.planning.macro_progression import ArcMacroProgression, dump_arc_macro_progression


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_arc_macro_boundary_passes_when_status_reaches_target() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p"))
    arc = ArcPlanVersion(
        id="a1",
        project_id="p1",
        arc_synopsis="a",
        chapter_start=1,
        chapter_end=10,
        macro_progression_json=dump_arc_macro_progression(
            ArcMacroProgression(status_tier_to=2, wealth_tier_to=1, market_space_to="县城")
        ),
    )

    issues = audit_arc_macro_boundary(
        arc=arc,
        current_chapter=10,
        status=ProtagonistMacroStatus(
            project_id="p1",
            as_of_chapter=10,
            status_tier=2,
            wealth_tier=1,
            market_space="县城",
        ),
    )

    assert issues == []


def test_arc_macro_boundary_blocks_unmet_status_target() -> None:
    arc = ArcPlanVersion(
        id="a1",
        project_id="p1",
        arc_synopsis="a",
        chapter_start=1,
        chapter_end=10,
        macro_progression_json=dump_arc_macro_progression(
            ArcMacroProgression(status_tier_to=3, wealth_tier_to=2, market_space_to="县城")
        ),
    )

    issues = audit_arc_macro_boundary(
        arc=arc,
        current_chapter=10,
        status=ProtagonistMacroStatus(
            project_id="p1",
            as_of_chapter=10,
            status_tier=1,
            wealth_tier=2,
            market_space="县城",
        ),
    )

    assert len(issues) == 1
    assert issues[0].issue_type == "arc_macro_progression_not_met"
    assert issues[0].blocking is True
    assert "status_tier" in issues[0].metadata["missing_targets"]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_future_plan_macro_progression_audit.py -q
```

Expected: FAIL because the audit helper does not exist.

- [ ] **Step 3: Add the audit helper**

Create `forwin/planning/future_plan_audit/macro_progression.py`:

```python
from __future__ import annotations

from forwin.book_state.macro_status import ProtagonistMacroStatus
from forwin.models.project import ArcPlanVersion
from forwin.planning.future_plan_audit.models import FuturePlanAuditIssue
from forwin.planning.macro_progression import load_arc_macro_progression


def audit_arc_macro_boundary(
    *,
    arc: ArcPlanVersion,
    current_chapter: int,
    status: ProtagonistMacroStatus,
) -> list[FuturePlanAuditIssue]:
    if int(current_chapter or 0) < int(arc.chapter_end or 0):
        return []
    progression = load_arc_macro_progression(arc)
    missing: list[str] = []
    if progression.status_tier_to and status.status_tier < progression.status_tier_to:
        missing.append("status_tier")
    if progression.wealth_tier_to and status.wealth_tier < progression.wealth_tier_to:
        missing.append("wealth_tier")
    if progression.enemy_tier_to and status.enemy_tier < progression.enemy_tier_to:
        missing.append("enemy_tier")
    if progression.market_space_to and progression.market_space_to != status.market_space:
        missing.append("market_space")
    if not missing:
        return []
    return [
        FuturePlanAuditIssue(
            issue_type="arc_macro_progression_not_met",
            severity="error",
            target_chapter=int(arc.chapter_end or current_chapter or 0),
            target_plan_id="",
            description=f"Arc {int(arc.arc_number or 0)} ended without required macro progression: {', '.join(missing)}.",
            evidence_refs=list(status.evidence_refs),
            patch_type="macro_progression_boundary",
            blocking=True,
            metadata={
                "arc_id": str(arc.id or ""),
                "arc_number": int(arc.arc_number or 0),
                "missing_targets": missing,
                "planned": progression.model_dump(mode="json"),
                "actual": status.model_dump(mode="json"),
            },
        )
    ]
```

- [ ] **Step 4: Wire the helper into `FuturePlanApplyMixin.audit_and_apply()`**

In `forwin/planning/future_plan_audit/apply.py`, import:

```python
from sqlalchemy import select
from forwin.book_state.query_interface import SqlBookStateQueryInterface
from forwin.models.project import ArcPlanVersion
from .macro_progression import audit_arc_macro_boundary
```

After `result = self.audit_plans(...)` and before `plans_by_id = ...`, load arcs
whose boundary has just been reached and append issues to `result`:

```python
macro_status = SqlBookStateQueryInterface(session).get_protagonist_macro_status(
    project_id=project_id,
    as_of_chapter=int(current_chapter or 0),
)
boundary_arcs = session.execute(
    select(ArcPlanVersion).where(
        ArcPlanVersion.project_id == project_id,
        ArcPlanVersion.chapter_end == int(current_chapter or 0),
    )
).scalars().all()
macro_issues = [
    issue
    for arc in boundary_arcs
    for issue in audit_arc_macro_boundary(
        arc=arc,
        current_chapter=int(current_chapter or 0),
        status=macro_status,
    )
]
if macro_issues:
    issues = [*result.issues, *macro_issues]
    blocking_reasons = [
        *result.blocking_reasons,
        *[
            f"{issue.issue_type}:{issue.metadata.get('arc_id', '')}"
            for issue in macro_issues
            if issue.blocking
        ],
    ]
    result = result.model_copy(
        update={
            "issues": issues,
            "status": "fail",
            "blocking_reasons": blocking_reasons,
        }
    )
```

- [ ] **Step 5: Verify pure helper**

Run:

```bash
python3 -m pytest tests/test_future_plan_macro_progression_audit.py -q
```

Expected: PASS for pure helper coverage.

- [ ] **Step 6: Add integration coverage**

Add a second test that calls the orchestrator-side future audit wrapper where
`session` is already available. Assert that a failing arc boundary adds
`"arc_macro_progression_not_met"` to `blocking_reasons`.

- [ ] **Step 7: Verify integration**

Run:

```bash
python3 -m pytest tests/test_future_plan_macro_progression_audit.py tests/test_phase05_regressions.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add forwin/planning/future_plan_audit/macro_progression.py forwin/planning/future_plan_audit/auditor.py tests/test_future_plan_macro_progression_audit.py
git commit -m "feat: audit arc macro progression boundaries"
```

## Task 7: 1000-Chapter Dry-Run Pressure Metrics

**Files:**
- Modify: `scripts/pulp_pressure_test.py`
- Modify: `tests/test_pulp_pressure_test.py`

- [ ] **Step 1: Add failing pressure metric assertions**

Extend `tests/test_pulp_pressure_test.py` seeded data with:

```python
summary = json.loads((report_dir / "summary.json").read_text())

assert "task_resume_success_rate" in summary
assert "arc_macro_boundary_failure_rate" in summary
assert "progression_rule_violation_rate" in summary
assert "macro_status_evidence_gap_rate" in summary
```

Seed decision events with payloads:

```python
{
    "event_type": "future_plan_audit_completed",
    "payload": {
        "issues": [
            {"issue_type": "arc_macro_progression_not_met", "blocking": True}
        ]
    },
}
```

and progression rule events:

```python
{
    "event_type": "progression_rule_evaluated",
    "payload": {"violated": True}
}
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_pulp_pressure_test.py -q
```

Expected: FAIL because the summary fields are missing.

- [ ] **Step 3: Add dry-run summary fields**

In `scripts/pulp_pressure_test.py`, extend the summary dict with:

```python
"task_resume_success_rate": _task_resume_success_rate(tasks),
"arc_macro_boundary_failure_rate": _event_rate(events, issue_type="arc_macro_progression_not_met"),
"progression_rule_violation_rate": _event_rate(events, event_type="progression_rule_evaluated", payload_key="violated"),
"macro_status_evidence_gap_rate": _event_rate(events, issue_type="macro_status_evidence_gap"),
```

Add helpers that return `None` when no denominator exists and a float in
`0.0..1.0` when matching telemetry exists.

- [ ] **Step 4: Keep report read-only**

Audit `scripts/pulp_pressure_test.py` for writes outside the output directory.
The script may read DB rows and write `metrics.csv`, `summary.json`, and
`README.md`; it must not create projects, start tasks, continue generation, or
modify rows.

- [ ] **Step 5: Verify**

Run:

```bash
python3 -m pytest tests/test_pulp_pressure_test.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/pulp_pressure_test.py tests/test_pulp_pressure_test.py
git commit -m "feat: report macro dry-run readiness metrics"
```

## Task 8: Final Verification

**Files:**
- No new source files unless prior tasks reveal missing exports.

- [ ] **Step 1: Run focused tests**

Run:

```bash
python3 -m pytest \
  tests/test_entry_contract_static.py \
  tests/test_arc_macro_progression.py \
  tests/test_project_progression_rules.py \
  tests/test_book_state_macro_status.py \
  tests/test_progression_rule_trope_filter.py \
  tests/test_future_plan_macro_progression_audit.py \
  tests/test_pulp_pressure_test.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run adjacent regression tests**

Run:

```bash
python3 -m pytest \
  tests/test_trope_cooldown.py \
  tests/test_retrieval_typed_budget.py \
  tests/test_generation_task_lease.py \
  tests/test_generation_task_persistence.py \
  tests/test_hard_floor.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Compile**

Run:

```bash
python3 -m compileall -q forwin scripts
```

Expected: exit code 0.

- [ ] **Step 4: Check whitespace**

Run:

```bash
git diff --check
```

Expected: exit code 0.

- [ ] **Step 5: Check legacy inventory**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
```

Expected: exit code 0.

- [ ] **Step 6: Commit final verification notes if docs changed**

If only source/test changes exist from previous tasks, skip this commit. If this
plan or a design doc was updated during execution, commit those docs:

```bash
git add docs/superpowers/specs/2026-05-21-thousand-chapter-macro-readiness-design.md docs/superpowers/plans/2026-05-21-thousand-chapter-macro-readiness.md
git commit -m "docs: plan thousand chapter macro readiness"
```

## Self-Review

- Spec coverage: Tasks cover UI contract cleanup, arc macro progression,
  project progression rules, BookState macro status, rule-aware trope
  selection, publisher feedback calibration, arc-boundary audit, and dry-run
  pressure fields.
- Placeholder scan: The plan contains no open placeholders and no missing
  feature names.
- Type consistency: `ArcMacroProgression`, `ProjectProgressionRule`,
  `ActiveProgressionRule`, and `ProtagonistMacroStatus` are introduced before
  dependent tasks consume them.
