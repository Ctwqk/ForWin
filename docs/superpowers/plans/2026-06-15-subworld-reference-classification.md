# Subworld Reference Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the narrow subworld admission fix with generalized reference classification for role references, technical identifiers, numbered plot entities, role-prefixed person names, and compound identity forms.

**Architecture:** Create `forwin/checker/reference_classifier.py` as the single classifier for chapter entity-reference candidates. Keep `ContinuityChecker` static helper methods as compatibility delegates, and make band-plan entry-target inference use the same normalization/filtering boundary for character targets.

**Tech Stack:** Python, pytest, ForWin MCP, Docker Swarm deploy sync.

---

## File Structure

- Create `forwin/checker/reference_classifier.py`: constants and pure functions for reference normalization and candidate classification.
- Modify `forwin/checker/rules.py`: remove duplicated constants/helpers from the checker and delegate static helper methods to the new classifier.
- Modify `forwin/planning/band_plan_service.py`: import classifier normalization and reject non-character references from inferred chapter entry targets.
- Modify `tests/test_subworld_control.py`: add generalized checker regressions for the four production blocker classes plus variants.
- Modify `tests/test_band_plan_service.py`: add planner regressions proving natural names are admitted and technical IDs are not admitted.

## Task 1: Checker Regression Tests

**Files:**
- Modify: `tests/test_subworld_control.py`
- Test: `tests/test_subworld_control.py`

- [x] **Step 1: Add failing tests**

Add these tests near `test_subworld_admission_ignores_generic_afterimage_roles`:

```python
    def test_subworld_admission_generalizes_non_cast_reference_filtering(self) -> None:
        class FakeRepo:
            def get_active_entities(self, _project_id: str) -> list[object]:
                return []

            def get_thread_by_name(self, _project_id: str, _name: str) -> object | None:
                return None

            def get_allowed_entity_names(self, _project_id: str, _chapter_number: int) -> set[str]:
                return {"沈岚", "陈潮白", "许晏", "馆员"}

            def get_entities_by_names(self, _project_id: str, _names: list[str]) -> dict[str, object]:
                return {}

        checker = ContinuityChecker(FakeRepo())
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=68,
                title="第40份密钥：灯塔管理员",
                body=(
                    "沈岚收到老环线调度员留下的L-7坐标。"
                    "馆员陈潮白记录QT-7741与L7-09同时失联。"
                    "第004号分割体和第40份密钥都指向VT-7-19-γ。"
                    "许晏/馆员的双重身份被写入XU-CH-1997-0847。"
                    "灰鸦仍未获准进入本章。"
                )
                * 30,
                end_of_chapter_summary="沈岚确认坐标与双重身份证据。",
                entity_mentions=[
                    EntityMention(entity_name="沈岚", entity_kind="character", is_named=True),
                    EntityMention(entity_name="老环线调度员", entity_kind="character", is_named=True),
                    EntityMention(entity_name="系统巡检员", entity_kind="character", is_named=True),
                    EntityMention(entity_name="馆员陈潮白", entity_kind="character", is_named=True),
                    EntityMention(entity_name="003号分割体", entity_kind="character", is_named=True),
                    EntityMention(entity_name="第004号分割体", entity_kind="character", is_named=True),
                    EntityMention(entity_name="第40份密钥", entity_kind="character", is_named=True),
                    EntityMention(entity_name="L-7", entity_kind="character", is_named=True),
                    EntityMention(entity_name="L7-09", entity_kind="character", is_named=True),
                    EntityMention(entity_name="QT-7741", entity_kind="character", is_named=True),
                    EntityMention(entity_name="VT-7-19-γ", entity_kind="character", is_named=True),
                    EntityMention(entity_name="E-7749", entity_kind="character", is_named=True),
                    EntityMention(entity_name="XU-CH-1997-0847", entity_kind="character", is_named=True),
                    EntityMention(entity_name="许晏/馆员", entity_kind="character", is_named=True),
                    EntityMention(entity_name="许晏与馆员", entity_kind="character", is_named=True),
                    EntityMention(entity_name="许晏（馆员人格）", entity_kind="character", is_named=True),
                    EntityMention(entity_name="灰鸦", entity_kind="character", is_named=True),
                ],
            ),
        )

        unknown = [
            issue.entity_names[0]
            for issue in verdict.issues
            if issue.rule_name == "sub_world_unknown_named_entity"
        ]
        self.assertEqual(unknown, ["灰鸦"])
        self.assertEqual(ContinuityChecker._candidate_character_name("馆员陈潮白"), "陈潮白")

    def test_reference_classifier_direct_shapes_are_not_overfit_to_exact_strings(self) -> None:
        non_candidates = [
            "老环线调度员",
            "系统巡检员",
            "第七区溺水者残影",
            "003号分割体",
            "第004号分割体",
            "第40份密钥",
            "L-7",
            "L7-09",
            "QT-7741",
            "VT-7-19-γ",
            "E-7749",
            "XU-CH-1997-0847",
            "许晏/馆员",
            "许晏与馆员",
            "许晏（馆员人格）",
        ]
        for name in non_candidates:
            with self.subTest(name=name):
                self.assertEqual(ContinuityChecker._candidate_character_name(name), "")
                self.assertFalse(ContinuityChecker._looks_like_named_character(name))

        self.assertEqual(ContinuityChecker._candidate_character_name("馆员陈潮白"), "陈潮白")
        self.assertEqual(ContinuityChecker._candidate_character_name("灰鸦"), "灰鸦")
```

- [x] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_subworld_control.py::SubWorldControlTests::test_subworld_admission_generalizes_non_cast_reference_filtering tests/test_subworld_control.py::SubWorldControlTests::test_reference_classifier_direct_shapes_are_not_overfit_to_exact_strings -v
```

Expected: fail because the current checker still treats `老环线调度员`, `003号分割体`, `L-7`, `QT-7741`, and `许晏/馆员` as candidate names.

## Task 2: Planner Regression Tests

**Files:**
- Modify: `tests/test_band_plan_service.py`
- Test: `tests/test_band_plan_service.py`

- [x] **Step 1: Add failing planner test**

Add this test after `test_band_plan_service_admits_named_entry_target_from_chapter_goal`:

```python
def test_band_plan_service_uses_reference_classifier_for_entry_targets() -> None:
    engine = get_engine(postgres_test_url("band-plan-service-reference-classifier"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project("Band", "前提", "科幻悬疑")
        arc = updater.create_arc_plan(project.id, "当前弧", arc_number=1)
        chapters = [
            updater.create_chapter_plan(
                project.id,
                arc.id,
                number,
                f"第{number}章",
                "推进主线",
                ["推进主线"],
            )
            for number in range(1, 4)
        ]
        chapters.append(
            updater.create_chapter_plan(
                project.id,
                arc.id,
                4,
                "海底旧站的分割体",
                "与003号分割体接触，查明馆员陈潮白存在双重死亡记录，定位L-7与QT-7741",
                ["与003号分割体接触", "馆员陈潮白存在双重死亡记录", "定位L-7与QT-7741"],
            )
        )

        BandPlanService(
            subworld_manager=_SubworldManager(),
            world_contract_service=_WorldContracts(),
        ).ensure_current_band_plan(
            session=session,
            request=BandPlanningRequest(
                project_id=project.id,
                arc_id=arc.id,
                activation_chapter=4,
                detailed_band_size=3,
                chapter_plans=chapters,
                structure=ArcStructureDraftData(
                    phase_layout=["setup", "pressure", "payoff"],
                    key_beats=["开局", "压力", "兑现"],
                    thread_priorities=[],
                    hotspot_candidates=[],
                    compression_candidates=[],
                ),
                arc_experience=ArcExperienceBundle(
                    reader_promise=ReaderPromise(genre_promise="科幻悬疑"),
                    arc_payoff_map=ArcPayoffMap(),
                ),
            ),
        )
        chapter_four = session.execute(
            select(ChapterPlan).where(ChapterPlan.project_id == project.id, ChapterPlan.chapter_number == 4)
        ).scalar_one()

    chapter_payload = json.loads(chapter_four.experience_plan_json)
    target_names = {item["entity_name"] for item in chapter_payload["chapter_entry_targets"]}
    assert "陈潮白" in target_names
    assert "馆员陈潮白" not in target_names
    assert "003号分割体" not in target_names
    assert "L-7" not in target_names
    assert "QT-7741" not in target_names
```

- [x] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_band_plan_service.py::test_band_plan_service_uses_reference_classifier_for_entry_targets -v
```

Expected: fail because the current planner does not infer `陈潮白` from the record phrasing and does not share classifier filtering.

## Task 3: Classifier Implementation

**Files:**
- Create: `forwin/checker/reference_classifier.py`
- Modify: `forwin/checker/rules.py`
- Test: `tests/test_subworld_control.py`

- [x] **Step 1: Create classifier module**

Create `forwin/checker/reference_classifier.py`:

```python
from __future__ import annotations

import re

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
    "残影",
    "调度员",
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
ROLE_PREFIXED_PERSON_NAME_PREFIXES = (
    "馆员",
    "审计员",
    "调度员",
    "接线员",
    "管理员",
    "工程师",
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
TECHNICAL_ID_RE = re.compile(
    r"^(?=.*(?:[A-Za-zＡ-Ｚａ-ｚ]|[0-9０-９]))[A-Za-zＡ-Ｚａ-ｚ0-9０-９]+(?:[-_][A-Za-zＡ-Ｚａ-ｚ0-9０-９γΩαβ]+)+$"
)
NUMBERED_PLOT_ENTITY_RE = re.compile(
    r"^(?:第)?[0-9０-９]{1,4}(?:号|份|枚)(?:分割体|密钥|碎片|样本|载体|节点)$"
)
COMPOUND_IDENTITY_RE = re.compile(r"^[\u4e00-\u9fff·]{2,6}(?:/|与)[\u4e00-\u9fff·]{2,6}$")
COMPOUND_PERSONA_PAREN_RE = re.compile(r"^[\u4e00-\u9fff·]{2,6}[（(][\u4e00-\u9fff·]{2,8}人格[）)]$")


def has_malformed_parenthetical_annotation(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    for opener, closer in (("（", "）"), ("(", ")")):
        if text.count(opener) != text.count(closer):
            return True
        if opener in text and closer in text and text.rfind(opener) > text.rfind(closer):
            return True
    return False


def normalize_character_reference(name: str) -> str:
    text = str(name or "").strip()
    for opener, closer in (("（", "）"), ("(", ")")):
        if opener not in text or not text.endswith(closer):
            continue
        prefix, suffix = text.rsplit(opener, 1)
        suffix = suffix[: -len(closer)].strip()
        prefix = prefix.strip()
        if suffix in {"提及", "无名", "记录", "旁白", "幕后", "间接"} and prefix:
            text = prefix
        elif prefix and looks_like_generic_character_reference(prefix):
            text = prefix
    return strip_role_prefix_from_person_name(text)


def strip_role_prefix_from_person_name(name: str) -> str:
    text = str(name or "").strip()
    for prefix in ROLE_PREFIXED_PERSON_NAME_PREFIXES:
        if not text.startswith(prefix) or len(text) <= len(prefix):
            continue
        suffix = text[len(prefix) :].strip()
        if is_plain_chinese_person_name(suffix):
            return suffix
    return text


def is_plain_chinese_person_name(name: str) -> bool:
    text = str(name or "").strip()
    return (
        2 <= len(text) <= 4
        and "的" not in text
        and not any(ch.isdigit() for ch in text)
        and all("\u4e00" <= ch <= "\u9fff" or ch == "·" for ch in text)
        and not looks_like_generic_character_reference(text)
        and not looks_like_non_character_reference(text)
    )


def looks_like_technical_identifier(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    return bool(TECHNICAL_ID_RE.fullmatch(text))


def looks_like_numbered_plot_entity(name: str) -> bool:
    text = str(name or "").strip()
    return bool(NUMBERED_PLOT_ENTITY_RE.fullmatch(text))


def looks_like_compound_identity(name: str) -> bool:
    text = str(name or "").strip()
    return bool(COMPOUND_IDENTITY_RE.fullmatch(text) or COMPOUND_PERSONA_PAREN_RE.fullmatch(text))


def looks_like_generic_character_reference(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if text in GENERIC_CHARACTER_REFERENCES:
        return True
    if looks_like_numbered_plot_entity(text):
        return True
    if looks_like_technical_identifier(text):
        return True
    if looks_like_compound_identity(text):
        return True
    if "的" in text:
        _prefix, suffix = text.rsplit("的", 1)
        if suffix and any(suffix.endswith(role) for role in POSSESSIVE_GENERIC_ROLE_SUFFIXES):
            return True
    return len(text) <= 8 and any(text.endswith(suffix) for suffix in GENERIC_CHARACTER_ROLE_SUFFIXES)


def looks_like_non_character_reference(name: str) -> bool:
    text = str(name or "").strip()
    if any(text.endswith(suffix) for suffix in RELATIONAL_REFERENCE_SUFFIXES):
        return True
    return any(keyword in text for keyword in NON_CHARACTER_NAME_KEYWORDS)


def looks_like_named_character(name: str) -> bool:
    text = normalize_character_reference(name)
    if not text or looks_like_generic_character_reference(text):
        return False
    if looks_like_non_character_reference(text):
        return False
    return len(text) <= 12


def candidate_character_name(name: str) -> str:
    raw_text = str(name or "").strip()
    if has_malformed_parenthetical_annotation(raw_text):
        return ""
    text = normalize_character_reference(raw_text)
    return text if looks_like_named_character(text) else ""
```

- [x] **Step 2: Delegate checker static helpers**

In `forwin/checker/rules.py`, import:

```python
from forwin.checker.reference_classifier import (
    candidate_character_name,
    has_malformed_parenthetical_annotation,
    looks_like_generic_character_reference,
    looks_like_named_character,
    looks_like_non_character_reference,
    normalize_character_reference,
)
```

Update the static helper bodies:

```python
    @staticmethod
    def _looks_like_named_character(name: str) -> bool:
        return looks_like_named_character(name)

    @staticmethod
    def _candidate_character_name(name: str) -> str:
        return candidate_character_name(name)

    @staticmethod
    def _has_malformed_parenthetical_annotation(name: str) -> bool:
        return has_malformed_parenthetical_annotation(name)

    @staticmethod
    def _looks_like_generic_character_reference(name: str) -> bool:
        return looks_like_generic_character_reference(name)

    @staticmethod
    def _normalize_character_reference(name: str) -> str:
        return normalize_character_reference(name)

    @staticmethod
    def _looks_like_non_character_reference(name: str) -> bool:
        return looks_like_non_character_reference(name)
```

Remove the old duplicated constants from `forwin/checker/rules.py` once imports compile.

- [x] **Step 3: Run checker tests to verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_subworld_control.py::SubWorldControlTests::test_subworld_admission_generalizes_non_cast_reference_filtering tests/test_subworld_control.py::SubWorldControlTests::test_reference_classifier_direct_shapes_are_not_overfit_to_exact_strings -v
```

Expected: both tests pass.

## Task 4: Planner Integration

**Files:**
- Modify: `forwin/planning/band_plan_service.py`
- Test: `tests/test_band_plan_service.py`

- [x] **Step 1: Use classifier normalization in planner**

Import classifier helpers:

```python
from forwin.checker.reference_classifier import (
    candidate_character_name,
    looks_like_technical_identifier,
    normalize_character_reference,
)
```

Update `_ENTRY_TARGET_PATTERNS`:

```python
_ENTRY_TARGET_PATTERNS = [
    re.compile(r"(?:引入|介绍|接触|遇见|认识|结识)(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:作为|，|,|、|。|；|;|$)"),
    re.compile(r"与(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:接触|会面|交涉|交易|对话)"),
    re.compile(r"(?:让|使)(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:首次)?登场"),
    re.compile(r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:首次)?登场"),
    re.compile(r"(?:查明|确认|发现|得知|获知)?(?P<name>(?:馆员|审计员|调度员|接线员|管理员|工程师)?[\u4e00-\u9fff·]{2,4})(?:存在|持有|掌握|暴露|揭示)"),
]
```

Update `_normalize_entry_target_name`:

```python
def _normalize_entry_target_name(value: str) -> str:
    raw_name = str(value or "").strip(" \t\r\n：:，,。；;、")
    name = normalize_character_reference(raw_name)
    if not name or name in _ENTRY_TARGET_NON_NAMES:
        return ""
    if looks_like_technical_identifier(name):
        return ""
    if "的" in name or len(name) > 8:
        return ""
    if any(token in name for token in ("任务", "主线", "危机", "线索", "关系", "权限", "信息")):
        return ""
    return candidate_character_name(name)
```

- [x] **Step 2: Run planner test to verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_band_plan_service.py::test_band_plan_service_uses_reference_classifier_for_entry_targets -v
```

Expected: test passes.

## Task 5: Verification, Commit, Deploy, And Continue Production

**Files:**
- Modified files from earlier tasks.

- [x] **Step 1: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_subworld_control.py tests/test_band_plan_service.py
```

Expected: all tests pass.

- [x] **Step 2: Run compile sanity**

Run:

```bash
.venv/bin/python -m compileall forwin
```

Expected: exit 0.

- [ ] **Step 3: Commit implementation**

Run:

```bash
git add forwin/checker/reference_classifier.py forwin/checker/rules.py forwin/planning/band_plan_service.py tests/test_subworld_control.py tests/test_band_plan_service.py docs/superpowers/plans/2026-06-15-subworld-reference-classification.md
git commit -m "fix: generalize subworld reference classification"
```

- [ ] **Step 4: Push and deploy**

Run:

```bash
git push origin master
ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'
```

Expected: deploy exits 0 and reports all four swarm services running.

- [ ] **Step 5: Continue production through MCP**

Use production MCP endpoint `http://10.0.0.126:8896/mcp`:

```python
from fastmcp import Client
import asyncio

URL = "http://10.0.0.126:8896/mcp"

async def main():
    async with Client(URL) as client:
        sixty = "634f037db38443a7b9a4b8c6534f549f"
        long = "ed259b9ad0a44f65b7f84250168f91cc"
        for project_id, chapter_number in ((sixty, 54), (long, 68)):
            project = (await client.call_tool("project_get", {"project_id": project_id})).data
            active = (await client.call_tool("task_active_generation_check", {"project_id": project_id})).data
            print(project["next_gate"], active)
            if active["has_active_generation_task"]:
                continue
            if str(project.get("next_gate") or "").startswith("chapter_"):
                result = await client.call_tool(
                    "chapter_review_retry",
                    {
                        "project_id": project_id,
                        "chapter_number": chapter_number,
                        "continue_generation": True,
                        "reason": "retry after generalized subworld reference classification fix",
                    },
                )
                print(result.data)
```

If the 240-chapter project remains at `band_checkpoint_warn` before chapter retry, inspect the checkpoint with `band_checkpoint_get` and use the matching MCP checkpoint workflow if available. Do not read or mutate the database directly.
