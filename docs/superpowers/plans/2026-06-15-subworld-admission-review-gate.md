# Subworld Admission Review Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the production subworld admission review blockers for old role references, numbered split-body plot entities, and role-prefixed personal names, then retry the two blocked production chapters.

**Architecture:** Keep strict named-character admission. Add narrow checker normalization for proven false-positive shapes, and extend band-plan entry-target inference for explicit plan text so intentional new entities enter the chapter admission contract.

**Tech Stack:** Python, pytest, ForWin MCP, Docker Swarm deploy sync.

---

## File Structure

- Modify `tests/test_subworld_control.py`: checker regression tests for production false positives and role-prefix normalization.
- Modify `tests/test_band_plan_service.py`: planner regression test for explicit `003号分割体` and `馆员陈潮白` entry targets.
- Modify `forwin/checker/rules.py`: narrow helper constants and candidate-name normalization.
- Modify `forwin/planning/band_plan_service.py`: entry-target regexes and target-name normalization.

## Task 1: Checker Regression Tests

**Files:**
- Modify: `tests/test_subworld_control.py`
- Test: `tests/test_subworld_control.py`

- [ ] **Step 1: Add failing checker tests**

Add these methods near the existing subworld admission generic-role tests:

```python
    def test_subworld_admission_ignores_old_scheduler_role_and_numbered_plot_entity(self) -> None:
        class FakeRepo:
            def get_active_entities(self, _project_id: str) -> list[object]:
                return []

            def get_thread_by_name(self, _project_id: str, _name: str) -> object | None:
                return None

            def get_allowed_entity_names(self, _project_id: str, _chapter_number: int) -> set[str]:
                return {"沈岚"}

            def get_entities_by_names(self, _project_id: str, _names: list[str]) -> dict[str, object]:
                return {}

        checker = ContinuityChecker(FakeRepo())
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=52,
                title="特勤队的饵",
                body="沈岚收到老环线调度员留下的空回复，又与003号分割体交换旧站坐标。" * 50,
                end_of_chapter_summary="沈岚确认旧站坐标。",
                entity_mentions=[
                    EntityMention(entity_name="沈岚", entity_kind="character", is_named=True),
                    EntityMention(entity_name="老环线调度员", entity_kind="character", is_named=True),
                    EntityMention(entity_name="003号分割体", entity_kind="character", is_named=True),
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
        self.assertFalse(ContinuityChecker._looks_like_named_character("老环线调度员"))
        self.assertFalse(ContinuityChecker._looks_like_named_character("003号分割体"))

    def test_subworld_admission_normalizes_role_prefixed_person_name(self) -> None:
        class FakeRepo:
            def get_active_entities(self, _project_id: str) -> list[object]:
                return []

            def get_thread_by_name(self, _project_id: str, _name: str) -> object | None:
                return None

            def get_allowed_entity_names(self, _project_id: str, _chapter_number: int) -> set[str]:
                return {"陈潮白"}

            def get_entities_by_names(self, _project_id: str, _names: list[str]) -> dict[str, object]:
                return {}

        checker = ContinuityChecker(FakeRepo())
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=27,
                title="第27章",
                body="终端显示潮汐群环档案署馆员陈潮白存在双重死亡记录。" * 50,
                end_of_chapter_summary="陈潮白的死亡时间被篡改。",
                entity_mentions=[
                    EntityMention(entity_name="馆员陈潮白", entity_kind="character", is_named=True),
                ],
            ),
        )

        unknown = [
            issue.entity_names[0]
            for issue in verdict.issues
            if issue.rule_name == "sub_world_unknown_named_entity"
        ]
        self.assertEqual(unknown, [])
        self.assertEqual(ContinuityChecker._candidate_character_name("馆员陈潮白"), "陈潮白")
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest tests/test_subworld_control.py::SubWorldControlTests::test_subworld_admission_ignores_old_scheduler_role_and_numbered_plot_entity tests/test_subworld_control.py::SubWorldControlTests::test_subworld_admission_normalizes_role_prefixed_person_name -v
```

Expected: both tests fail because `老环线调度员`, `003号分割体`, or `馆员陈潮白` are still treated as unknown named entities.

## Task 2: Planner Regression Test

**Files:**
- Modify: `tests/test_band_plan_service.py`
- Test: `tests/test_band_plan_service.py`

- [ ] **Step 1: Add failing planner test**

Add this test after `test_band_plan_service_admits_named_entry_target_from_chapter_goal`:

```python
def test_band_plan_service_admits_contact_and_record_entry_targets_from_chapter_goal() -> None:
    engine = get_engine(postgres_test_url("band-plan-service-contact-entry-target"))
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
                "与003号分割体接触，查明馆员陈潮白存在双重死亡记录",
                ["与003号分割体接触", "馆员陈潮白存在双重死亡记录"],
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
    assert "003号分割体" in target_names
    assert "陈潮白" in target_names
    assert "馆员陈潮白" not in target_names
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
pytest tests/test_band_plan_service.py::test_band_plan_service_admits_contact_and_record_entry_targets_from_chapter_goal -v
```

Expected: fail because the existing inference does not extract both targets from the production phrasing.

## Task 3: Checker Implementation

**Files:**
- Modify: `forwin/checker/rules.py`
- Test: `tests/test_subworld_control.py`

- [ ] **Step 1: Add narrow constants and helpers**

Add constants near the existing character-reference constants:

```python
ROLE_PREFIXED_PERSON_NAME_PREFIXES = (
    "馆员",
    "审计员",
    "调度员",
    "接线员",
    "管理员",
    "工程师",
)
NUMBERED_NON_CAST_ENTITY_SUFFIXES = (
    "分割体",
)
```

Add these static helpers near `_candidate_character_name`:

```python
    @staticmethod
    def _strip_role_prefix_from_person_name(name: str) -> str:
        text = str(name or "").strip()
        for prefix in ROLE_PREFIXED_PERSON_NAME_PREFIXES:
            if not text.startswith(prefix) or len(text) <= len(prefix):
                continue
            suffix = text[len(prefix) :].strip()
            if (
                2 <= len(suffix) <= 4
                and "的" not in suffix
                and not any(ch.isdigit() for ch in suffix)
                and all("\u4e00" <= ch <= "\u9fff" or ch == "·" for ch in suffix)
                and not ContinuityChecker._looks_like_generic_character_reference(suffix)
                and not ContinuityChecker._looks_like_non_character_reference(suffix)
            ):
                return suffix
        return text

    @staticmethod
    def _looks_like_numbered_non_cast_entity(name: str) -> bool:
        text = str(name or "").strip()
        if not text:
            return False
        return bool(
            re.fullmatch(
                rf"[0-9０-９]{{2,4}}号(?:{'|'.join(NUMBERED_NON_CAST_ENTITY_SUFFIXES)})",
                text,
            )
        )
```

- [ ] **Step 2: Wire helpers into candidate checks**

Update `_candidate_character_name`:

```python
    @staticmethod
    def _candidate_character_name(name: str) -> str:
        raw_text = str(name or "").strip()
        if ContinuityChecker._has_malformed_parenthetical_annotation(raw_text):
            return ""
        text = ContinuityChecker._normalize_character_reference(raw_text)
        text = ContinuityChecker._strip_role_prefix_from_person_name(text)
        return text if ContinuityChecker._looks_like_named_character(text) else ""
```

Update `_looks_like_generic_character_reference`:

```python
        if ContinuityChecker._looks_like_numbered_non_cast_entity(text):
            return True
```

Add `"调度员"` to `GENERIC_CHARACTER_ROLE_SUFFIXES`.

- [ ] **Step 3: Run checker GREEN**

Run:

```bash
pytest tests/test_subworld_control.py::SubWorldControlTests::test_subworld_admission_ignores_old_scheduler_role_and_numbered_plot_entity tests/test_subworld_control.py::SubWorldControlTests::test_subworld_admission_normalizes_role_prefixed_person_name -v
```

Expected: both tests pass.

## Task 4: Planner Implementation

**Files:**
- Modify: `forwin/planning/band_plan_service.py`
- Test: `tests/test_band_plan_service.py`

- [ ] **Step 1: Extend patterns and normalization**

Update `_ENTRY_TARGET_PATTERNS`:

```python
_ENTRY_TARGET_PATTERNS = [
    re.compile(r"(?:引入|介绍|接触|遇见|认识|结识)(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?=作为|并|，|,|、|。|；|;|$)"),
    re.compile(r"与(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:接触|会面|交涉|交易|对话)"),
    re.compile(r"(?:让|使)(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:首次)?登场"),
    re.compile(r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:首次)?登场"),
    re.compile(r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:存在|持有|掌握|暴露|揭示)"),
]
```

Add this helper near `_normalize_entry_target_name`:

```python
_ENTRY_TARGET_ROLE_PREFIXES = (
    "馆员",
    "审计员",
    "调度员",
    "接线员",
    "管理员",
    "工程师",
)


def _strip_entry_target_role_prefix(name: str) -> str:
    text = str(name or "").strip()
    for prefix in _ENTRY_TARGET_ROLE_PREFIXES:
        if not text.startswith(prefix) or len(text) <= len(prefix):
            continue
        suffix = text[len(prefix) :].strip()
        if (
            2 <= len(suffix) <= 4
            and "的" not in suffix
            and not any(ch.isdigit() for ch in suffix)
            and all("\u4e00" <= ch <= "\u9fff" or ch == "·" for ch in suffix)
        ):
            return suffix
    return text
```

Update `_normalize_entry_target_name` to call the helper immediately after trimming:

```python
    name = _strip_entry_target_role_prefix(name)
```

- [ ] **Step 2: Run planner GREEN**

Run:

```bash
pytest tests/test_band_plan_service.py::test_band_plan_service_admits_contact_and_record_entry_targets_from_chapter_goal -v
```

Expected: test passes.

## Task 5: Full Verification, Commit, Deploy, And Retry

**Files:**
- Modify: code and tests from earlier tasks
- Test: focused pytest files

- [ ] **Step 1: Run focused verification**

Run:

```bash
pytest tests/test_subworld_control.py tests/test_band_plan_service.py
```

Expected: all tests in both files pass.

- [ ] **Step 2: Commit implementation**

Run:

```bash
git add forwin/checker/rules.py forwin/planning/band_plan_service.py tests/test_subworld_control.py tests/test_band_plan_service.py docs/superpowers/plans/2026-06-15-subworld-admission-review-gate.md
git commit -m "fix: narrow subworld admission false positives"
```

- [ ] **Step 3: Push and deploy**

Run:

```bash
git push origin master
ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'
```

Expected: deploy script exits 0 and updates ForWin services.

- [ ] **Step 4: Retry production chapters through MCP**

Use the production MCP endpoint `http://10.0.0.126:8896/mcp`:

```python
from fastmcp import Client
import asyncio

URL = "http://10.0.0.126:8896/mcp"
CASES = [
    ("634f037db38443a7b9a4b8c6534f549f", 52),
    ("ed259b9ad0a44f65b7f84250168f91cc", 27),
]

async def main():
    async with Client(URL) as client:
        for project_id, chapter_number in CASES:
            project = (await client.call_tool("project_get", {"project_id": project_id})).data
            active = (await client.call_tool("task_active_generation_check", {"project_id": project_id})).data
            print(project["next_gate"], active)
            if not active["has_active_generation_task"]:
                result = await client.call_tool(
                    "chapter_review_retry",
                    {
                        "project_id": project_id,
                        "chapter_number": chapter_number,
                        "continue_generation": True,
                        "reason": "retry after subworld admission false-positive fix",
                    },
                )
                print(result.data)

asyncio.run(main())
```

Expected: each retry is accepted or a task is active/queued without duplicate active generation for the same project.
