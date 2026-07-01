# Production Longform Publish-False Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the current production deployment can run a fresh ForWin long-form generation flow and upload one generated chapter as `publish=false` without public publication.

**Architecture:** This is a controlled production operation, not a feature build. Project, Genesis, task, and chapter state are operated only through the repo-local ForWin MCP endpoint, while publisher and observability checks use existing read/write API surfaces that have no equivalent MCP tool. All evidence is written to local ignored `.codex-monitor/` logs with generated chapter bodies and sensitive browser/session values redacted.

**Tech Stack:** Python 3.13 virtualenv, FastMCP client, ForWin MCP tools, FastAPI production endpoints, Docker Swarm service checks, existing publisher baseline and supervisor scripts.

---

## File Structure

- Read: `docs/superpowers/specs/2026-07-01-production-longform-publishfalse-experiment-design.md`
  - Approved design and safety boundary.
- Read: `docs/codex-forwin-mcp.md`
  - Operator workflow and MCP rules.
- Read: `docs/operations/forwin-production-processes.md`
  - Production baseline, upload-chain, supervisor, and login policy.
- Read/Run: `scripts/check_codex_operator_ready.py`
  - Confirms local API/MCP/operator path.
- Read/Run: `scripts/check_production_publisher_baseline.py`
  - Confirms production services, publisher browser, Fanqie/Qidian login, and Discord policy.
- Read/Run: `scripts/supervise_forwin_interventions.py`
  - Final read-only supervisor check.
- Output only: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.json`
  - Structured experiment evidence. This directory is ignored by git.
- Output only: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.jsonl`
  - Polling timeline and task/upload status samples. This directory is ignored by git.
- Modify only if a bug is found: focused source/test files directly related to the failure.

No committed code changes are planned unless the experiment exposes a reproducible bug.

## Task 1: Preflight And Evidence Log Setup

**Files:**
- Read: `docs/superpowers/specs/2026-07-01-production-longform-publishfalse-experiment-design.md`
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.json`
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.jsonl`

- [ ] **Step 1: Confirm the worktree and deployed source baseline**

Run:

```bash
pwd
git status --short
git log --oneline --decorate --max-count=5
git rev-parse HEAD
git rev-parse origin/master
```

Expected:

- `pwd` is `/Users/magi1/ForWin-source-github`.
- `git status --short` is empty.
- `HEAD` and `origin/master` both point at or include `d694cf4`.

- [ ] **Step 2: Create local experiment log paths**

Run:

```bash
EXPERIMENT_TS="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p .codex-monitor
printf '%s\n' "$EXPERIMENT_TS" > .codex-monitor/production-longform-publishfalse-current.txt
touch ".codex-monitor/production-longform-publishfalse-${EXPERIMENT_TS}.jsonl"
printf '{"experiment_ts":"%s","events":[]}\n' "$EXPERIMENT_TS" > ".codex-monitor/production-longform-publishfalse-${EXPERIMENT_TS}.json"
```

Expected:

- `.codex-monitor/production-longform-publishfalse-current.txt` exists.
- Both JSON and JSONL evidence files exist.
- `git status --short` remains empty because `.codex-monitor/` is ignored.

- [ ] **Step 3: Run operator and production readiness checks**

Run:

```bash
.venv/bin/python scripts/check_codex_operator_ready.py
.venv/bin/python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged \
  --wait-heartbeat-seconds 60 \
  --heartbeat-poll-interval-seconds 10
```

Expected:

- Operator readiness prints `[OK]` for API health, MCP health, plugin MCP config, swarm services, and Python environment.
- Baseline JSON has `status:"ok"` and `blocked_items: []`.
- Baseline JSON shows both `fanqie` and `qidian` connected with `login_visible:false`.
- Baseline JSON shows Discord publisher login env policy `ok:true`.

- [ ] **Step 4: Record Swarm service image state**

Run:

```bash
docker --context swarm-manager-150 service ls \
  --format '{{.Name}} {{.Image}} {{.Replicas}}' | rg '^forwin-'
```

Expected:

- Six ForWin services are present.
- Each service has `1/1`.
- Images are current deployed ForWin images.

## Task 2: Select Or Create The Experiment Project Through MCP

**Files:**
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.jsonl`

- [ ] **Step 1: Check for active generation before any write**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        active = json_loads_result(await client.call_tool("task_active_generation_check", {}))
        print(json.dumps(redact_sensitive(active), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- `has_active_generation_task` is `false`.
- If it is `true`, stop project creation and inspect the active task with `task_get` instead of starting new work.

- [ ] **Step 2: List existing same-day experiment projects**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

TITLE_PREFIX = "ForWin系统测试-长文发布前验证-20260701"

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        payload = json_loads_result(await client.call_tool("project_list", {}))
        projects = payload.get("projects") or payload.get("items") or []
        matches = [
            {
                "id": item.get("id") or item.get("project_id"),
                "title": item.get("title"),
                "creation_status": item.get("creation_status"),
                "generated_chapter_count": item.get("generated_chapter_count"),
                "accepted_chapter_count": item.get("accepted_chapter_count"),
                "needs_review_chapter_count": item.get("needs_review_chapter_count"),
                "target_total_chapters": item.get("target_total_chapters"),
                "next_gate": item.get("next_gate"),
            }
            for item in projects
            if isinstance(item, dict) and str(item.get("title") or "").startswith(TITLE_PREFIX)
        ]
        selected = next(
            (
                item
                for item in matches
                if item.get("id")
                and item.get("creation_status") in {"creating", "genesis_ready", "writing"}
                and int(item.get("generated_chapter_count") or 0) < int(item.get("target_total_chapters") or 9999)
            ),
            None,
        )
        if selected:
            from pathlib import Path

            Path(".codex-monitor").mkdir(exist_ok=True)
            Path(".codex-monitor/production-longform-publishfalse-project-id.txt").write_text(
                selected["id"] + "\n",
                encoding="utf-8",
            )
        print(json.dumps(redact_sensitive({"matches": matches, "selected": selected}), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- If an unfinished matching experiment project exists, record its `project_id` and continue with it.
- If no matching project exists, proceed to create one.
- When a suitable project is selected, `.codex-monitor/production-longform-publishfalse-project-id.txt` is written.

- [ ] **Step 3: Create one experiment project when none exists**

Run only if Step 2 found no suitable unfinished project:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        result = json_loads_result(
            await client.call_tool(
                "project_create",
                {
                    "title": "ForWin系统测试-长文发布前验证-20260701",
                    "premise": "这是一个用于验证 ForWin 生产长文生成和发布前草稿上传链路的安全测试项目。故事围绕一座潮汐档案城展开，主角需要在不公开真实平台内容的前提下完成多章节生成验证。",
                    "genre": "科幻悬疑",
                    "setting_summary": "潮汐档案城每晚会重排城市记忆，测试项目只用于系统验证，不作为公开发表内容。",
                    "target_total_chapters": 3,
                },
            )
        )
        from pathlib import Path

        project = result.get("project") if isinstance(result.get("project"), dict) else result
        project_id = project.get("id") or project.get("project_id")
        if project_id:
            Path(".codex-monitor").mkdir(exist_ok=True)
            Path(".codex-monitor/production-longform-publishfalse-project-id.txt").write_text(
                project_id + "\n",
                encoding="utf-8",
            )
        print(json.dumps(redact_sensitive(result), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- Result contains `project.id`.
- Project `creation_status` is `creating`.
- Project `can_start_writing` is `false`.
- `.codex-monitor/production-longform-publishfalse-project-id.txt` contains the new project id.

- [ ] **Step 4: Verify selected project id file**

Run:

```bash
test -s .codex-monitor/production-longform-publishfalse-project-id.txt
sed -n '1p' .codex-monitor/production-longform-publishfalse-project-id.txt
```

Expected:

- `.codex-monitor/production-longform-publishfalse-project-id.txt` contains one project id.

## Task 3: Complete Genesis Through MCP

**Files:**
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.jsonl`

- [ ] **Step 1: Inspect Genesis state**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        result = json_loads_result(await client.call_tool("genesis_get", {"project_id": project_id}))
        print(json.dumps(redact_sensitive(result), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- Output includes `stage_states`.
- If `can_start_writing` is already `true`, skip to Task 4.

- [ ] **Step 2: Generate and lock each incomplete Genesis stage**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()
stage_order = ("brief", "world", "map", "story_engine", "book_blueprint", "bootstrap")

def stage_map(genesis):
    return {
        item.get("stage_key"): item
        for item in genesis.get("stage_states", [])
        if isinstance(item, dict)
    }

async def main():
    warnings.simplefilter("ignore")
    events = []
    async with Client("http://127.0.0.1:8896/mcp") as client:
        genesis = json_loads_result(await client.call_tool("genesis_get", {"project_id": project_id}))
        stages = stage_map(genesis)
        for stage_key in stage_order:
            state = stages.get(stage_key, {})
            if bool(state.get("locked")):
                events.append({"stage_key": stage_key, "action": "already_locked"})
                continue
            generated = json_loads_result(
                await client.call_tool(
                    "genesis_stage_generate",
                    {"project_id": project_id, "stage_key": stage_key},
                )
            )
            events.append({"stage_key": stage_key, "action": "generated", "ok": bool(generated.get("ok", True))})
            locked = json_loads_result(
                await client.call_tool(
                    "genesis_stage_lock",
                    {"project_id": project_id, "stage_key": stage_key},
                )
            )
            events.append({"stage_key": stage_key, "action": "locked", "ok": bool(locked.get("ok", True))})
            genesis = json_loads_result(await client.call_tool("genesis_get", {"project_id": project_id}))
            stages = stage_map(genesis)
        final_genesis = json_loads_result(await client.call_tool("genesis_get", {"project_id": project_id}))
        project = json_loads_result(await client.call_tool("project_get", {"project_id": project_id}))
        print(json.dumps(redact_sensitive({"events": events, "genesis": final_genesis, "project": project}), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- Each stage is either `already_locked` or gets `generated` then `locked`.
- Final `genesis.can_start_writing` or `project.can_start_writing` is `true`.
- If a stage generation fails, stop and record the failing stage and error. Do not start writing.

## Task 4: Start Writing And Poll The First Generation Task

**Files:**
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.jsonl`

- [ ] **Step 1: Confirm no active generation for this project**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        result = json_loads_result(await client.call_tool("task_active_generation_check", {"project_id": project_id}))
        print(json.dumps(redact_sensitive(result), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- `has_active_generation_task` is `false`.
- If `true`, inspect the active task with `task_get`; do not call `project_start_writing`.

- [ ] **Step 2: Start writing with a one-chapter cap**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        result = json_loads_result(
            await client.call_tool(
                "project_start_writing",
                {"project_id": project_id, "auto_continue": False, "max_chapters": 1},
            )
        )
        task = result.get("task") if isinstance(result.get("task"), dict) else {}
        if task.get("task_id"):
            Path(".codex-monitor").mkdir(exist_ok=True)
            Path(".codex-monitor/production-longform-publishfalse-task-id.txt").write_text(
                task["task_id"] + "\n",
                encoding="utf-8",
            )
        print(json.dumps(redact_sensitive(result), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- Result includes a queued or running task.
- `.codex-monitor/production-longform-publishfalse-task-id.txt` contains `task.task_id`.

- [ ] **Step 3: Poll the task until terminal, paused, or gated**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, time, warnings
from pathlib import Path
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

task_id = Path(".codex-monitor/production-longform-publishfalse-task-id.txt").read_text(encoding="utf-8").strip()
terminal = {"completed", "failed", "cancelled", "paused"}

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        for _ in range(180):
            task = json_loads_result(await client.call_tool("task_get", {"task_id": task_id}))
            print(json.dumps(redact_sensitive({
                "task_id": task.get("task_id"),
                "status": task.get("status"),
                "current_stage": task.get("current_stage"),
                "current_chapter": task.get("current_chapter"),
                "completed_chapters": task.get("completed_chapters", []),
                "failed_chapters": task.get("failed_chapters", []),
                "heartbeat_at": task.get("heartbeat_at"),
                "message": str(task.get("message") or "")[:240],
                "error": str(task.get("error") or "")[:240],
            }), ensure_ascii=False))
            if str(task.get("status") or "").lower() in terminal:
                break
            time.sleep(10)

asyncio.run(main())
PY
```

Expected:

- Task reaches `completed`, `paused`, or a clearly reportable gate.
- If task reaches `failed`, record failed chapter and error, then inspect project/chapter state before deciding whether a code fix is needed.

## Task 5: Exercise Pause And Resume Safely

**Files:**
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.jsonl`

- [ ] **Step 1: Decide whether a pause can be requested**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        active = json_loads_result(await client.call_tool("task_active_generation_check", {"project_id": project_id}))
        task_id = active.get("active_task_id") or active.get("task_id")
        if task_id:
            Path(".codex-monitor").mkdir(exist_ok=True)
            Path(".codex-monitor/production-longform-publishfalse-active-task-id.txt").write_text(
                task_id + "\n",
                encoding="utf-8",
            )
        tasks = json_loads_result(await client.call_tool("task_list", {"limit": 8}))
        print(json.dumps(redact_sensitive({"active": active, "tasks": tasks}), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- If an active task exists and the task is pausable, use Step 2.
- If no active task exists because the one-chapter task completed quickly, record `pause_not_exercised_task_completed_too_quickly` and proceed to Step 4.

- [ ] **Step 2: Request safe pause for an active task**

Run only when Step 1 writes `.codex-monitor/production-longform-publishfalse-active-task-id.txt`:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, warnings
from pathlib import Path
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

task_id = Path(".codex-monitor/production-longform-publishfalse-active-task-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        result = json_loads_result(await client.call_tool("task_pause", {"task_id": task_id}))
        print(json.dumps(redact_sensitive(result), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- Result message includes safe pause.
- `task.pause_requested` is `true`.

- [ ] **Step 3: Poll paused or terminal state**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, time, warnings
from pathlib import Path
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

task_id = Path(".codex-monitor/production-longform-publishfalse-active-task-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        for _ in range(60):
            task = json_loads_result(await client.call_tool("task_get", {"task_id": task_id}))
            print(json.dumps(redact_sensitive({
                "task_id": task.get("task_id"),
                "status": task.get("status"),
                "pause_requested": task.get("pause_requested"),
                "current_stage": task.get("current_stage"),
                "current_chapter": task.get("current_chapter"),
                "message": str(task.get("message") or "")[:240],
            }), ensure_ascii=False))
            if str(task.get("status") or "").lower() in {"paused", "completed", "failed", "cancelled"}:
                break
            time.sleep(10)

asyncio.run(main())
PY
```

Expected:

- Task becomes `paused` or terminal.
- Do not kill or restart the worker to force a pause.

- [ ] **Step 4: Continue generation only when project gate allows**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        project = json_loads_result(await client.call_tool("project_get", {"project_id": project_id}))
        active = json_loads_result(await client.call_tool("task_active_generation_check", {"project_id": project_id}))
        print(json.dumps(redact_sensitive({"project": project, "active": active}), ensure_ascii=False, indent=2))
        if not active.get("has_active_generation_task") and project.get("creation_status") == "writing":
            result = json_loads_result(
                await client.call_tool(
                    "project_continue_generation",
                    {"project_id": project_id, "max_chapters": 1, "auto_continue": False},
                )
            )
            print(json.dumps(redact_sensitive({"continue_result": result}), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- If project is blocked by review/accept gate, the command reports the gate and does not create a conflicting task.
- If continuation is valid, result includes a new queued/running task id.

## Task 6: Inspect Chapters, WorldModel, BookState, And Observability

**Files:**
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.json`

- [ ] **Step 1: Summarize chapter output without printing bodies**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    result = {"project_id": project_id, "chapters": []}
    async with Client("http://127.0.0.1:8896/mcp") as client:
        chapters_payload = json_loads_result(await client.call_tool("chapter_list", {"project_id": project_id}))
        chapters = chapters_payload.get("chapters") or []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            number = int(chapter.get("chapter_number") or 0)
            if number <= 0 or chapter.get("status") == "planned":
                continue
            detail = json_loads_result(await client.call_tool("chapter_get", {"project_id": project_id, "chapter_number": number}))
            body = str(detail.get("body") or "")
            result["chapters"].append({
                "chapter_number": number,
                "title": detail.get("title"),
                "status": detail.get("status"),
                "char_count": len(body),
                "line_count": body.count("\\n") + (1 if body else 0),
                "summary_present": bool(detail.get("summary")),
                "residual_review_issue_count": len(detail.get("residual_review_issues") or []),
            })
        print(json.dumps(redact_sensitive(result), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- At least one non-planned chapter is present.
- Each generated chapter has a positive `char_count`.
- Full body text is not printed.

- [ ] **Step 2: Summarize WorldModel and BookState read surfaces**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from urllib.parse import urlencode
from urllib.request import urlopen
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()
api_base = "http://10.0.0.126:8899"

def api_summary(path):
    with urlopen(api_base + path, timeout=15) as response:
        payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
    summary = {"status": response.status, "keys": sorted(payload.keys())[:16] if isinstance(payload, dict) else []}
    if isinstance(payload, dict):
        for key in ("nodes", "edges", "deltas", "facts"):
            if isinstance(payload.get(key), list):
                summary[f"{key}_count"] = len(payload[key])
    return summary

async def main():
    warnings.simplefilter("ignore")
    result = {"project_id": project_id}
    async with Client("http://127.0.0.1:8896/mcp") as client:
        chapters_payload = json_loads_result(await client.call_tool("chapter_list", {"project_id": project_id}))
        generated = [
            int(item.get("chapter_number") or 0)
            for item in chapters_payload.get("chapters", [])
            if isinstance(item, dict) and item.get("status") != "planned"
        ]
        as_of = max(generated or [0])
        result["as_of_chapter"] = as_of
        for tool, args in (
            ("world_model_get", {"project_id": project_id, "as_of_chapter": as_of}),
            ("world_page_get", {"project_id": project_id, "page_key": "world:index"}),
            ("world_conflict_list", {"project_id": project_id}),
        ):
            payload = json_loads_result(await client.call_tool(tool, args))
            result[tool] = {
                "keys": sorted(payload.keys())[:16],
                "as_of_chapter": payload.get("as_of_chapter"),
                "page_key": payload.get("page_key"),
                "conflict_count": len(payload.get("conflicts") or []) if tool == "world_conflict_list" else None,
            }
        result["book_state_nodes"] = api_summary(f"/api/projects/{project_id}/book-state/nodes?" + urlencode({"as_of_chapter": as_of}))
        result["book_state_edges"] = api_summary(f"/api/projects/{project_id}/book-state/edges?" + urlencode({"as_of_chapter": as_of}))
        result["book_state_deltas"] = api_summary(f"/api/projects/{project_id}/book-state/deltas?" + urlencode({"through_chapter": as_of}))
    print(json.dumps(redact_sensitive(result), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- MCP WorldModel tools return JSON summaries without errors.
- BookState API routes return HTTP 200 summaries.
- `world_conflict_list.conflict_count` is recorded.

- [ ] **Step 3: Summarize observability read surfaces**

Run:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()
task_file = Path(".codex-monitor/production-longform-publishfalse-task-id.txt")
task_id = task_file.read_text(encoding="utf-8").strip() if task_file.exists() else ""
api_base = "http://10.0.0.126:8899"

def summarize(path):
    with urlopen(api_base + path, timeout=15) as response:
        payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
    summary = {"status": response.status, "keys": sorted(payload.keys())[:16] if isinstance(payload, dict) else []}
    if isinstance(payload, dict):
        for key in ("total_duration_ms", "task_id", "project_id", "chapter_number"):
            if key in payload:
                summary[key] = payload[key]
        for key in ("component_breakdown", "stage_breakdown", "llm_breakdown", "db_breakdown", "top_slow_spans", "recommendations"):
            value = payload.get(key)
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
            elif isinstance(value, dict):
                summary[f"{key}_keys"] = sorted(value.keys())[:12]
    return summary

paths = {
    "task": f"/api/observability/performance/tasks/{task_id}" if task_id else "",
    "project": f"/api/observability/performance/projects/{project_id}?" + urlencode({"limit": 100}),
    "chapter_1": f"/api/observability/performance/projects/{project_id}/chapters/1?" + urlencode({"limit": 100}),
    "llm": "/api/observability/performance/llm?" + urlencode({"project_id": project_id, "days": 30}),
    "db": "/api/observability/performance/db?" + urlencode({"project_id": project_id, "days": 30}),
}
print(json.dumps({name: summarize(path) for name, path in paths.items() if path}, ensure_ascii=False, indent=2))
PY
```

Expected:

- Each requested observability endpoint returns HTTP 200.
- The report records `total_duration_ms` or an equivalent empty performance report.

## Task 7: Upload One Generated Chapter With Publish False

**Files:**
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.jsonl`

- [ ] **Step 1: Re-run publisher baseline**

Run:

```bash
.venv/bin/python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged \
  --wait-heartbeat-seconds 60 \
  --heartbeat-poll-interval-seconds 10
```

Expected:

- `status:"ok"`.
- `blocked_items: []`.
- Both Fanqie and Qidian remain connected.

- [ ] **Step 2: Choose the generated chapter for upload**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, os, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

from pathlib import Path

project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        chapters = json_loads_result(await client.call_tool("chapter_list", {"project_id": project_id})).get("chapters") or []
        candidates = [
            {
                "chapter_number": int(item.get("chapter_number") or 0),
                "title": item.get("title"),
                "status": item.get("status"),
                "char_count": item.get("char_count"),
            }
            for item in chapters
            if isinstance(item, dict) and item.get("status") in {"drafted", "accepted", "needs_review"}
        ]
        if candidates:
            Path(".codex-monitor").mkdir(exist_ok=True)
            Path(".codex-monitor/production-longform-publishfalse-upload-chapter.txt").write_text(
                str(candidates[0]["chapter_number"]) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(redact_sensitive({"candidates": candidates}), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- At least one candidate exists.
- Choose the lowest generated chapter number unless a later chapter is cleaner.
- `.codex-monitor/production-longform-publishfalse-upload-chapter.txt` contains the selected chapter number.

- [ ] **Step 3: Create project upload jobs with `publish=false`**

Run:

```bash
.venv/bin/python - <<'PY'
import json, os
from pathlib import Path
from urllib.request import Request, urlopen

api_base = "http://10.0.0.126:8899"
project_id = Path(".codex-monitor/production-longform-publishfalse-project-id.txt").read_text(encoding="utf-8").strip()
chapter_number = int(Path(".codex-monitor/production-longform-publishfalse-upload-chapter.txt").read_text(encoding="utf-8").strip())
platforms = ["fanqie", "qidian"]
results = {}
job_ids = []
for platform in platforms:
    payload = json.dumps({"platform": platform, "chapter_number": chapter_number, "publish": False}).encode("utf-8")
    request = Request(
        f"{api_base}/api/projects/{project_id}/publishers/upload-jobs",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
            results[platform] = {"status": response.status, "payload": payload}
            job_id = payload.get("job_id") or payload.get("id")
            if job_id:
                job_ids.append(str(job_id))
    except Exception as exc:
        results[platform] = {"error": f"{exc.__class__.__name__}: {exc}"}
Path(".codex-monitor/production-longform-publishfalse-upload-job-ids.txt").write_text(
    ",".join(job_ids) + "\n",
    encoding="utf-8",
)
print(json.dumps(results, ensure_ascii=False, indent=2))
PY
```

Expected:

- Each platform either returns an upload job id or a safe structured block such as missing binding or login required.
- Every created job has `publish:false`.
- If both platforms are blocked by missing binding or platform policy, stop upload execution and record the block.

- [ ] **Step 4: Poll every created upload job to terminal**

Run:

```bash
.venv/bin/python - <<'PY'
import json, time
from pathlib import Path
from urllib.request import urlopen

api_base = "http://10.0.0.126:8899"
job_ids = [
    item
    for item in Path(".codex-monitor/production-longform-publishfalse-upload-job-ids.txt").read_text(encoding="utf-8").strip().split(",")
    if item
]
terminal = {"succeeded", "failed", "cancelled"}
for job_id in job_ids:
    for _ in range(120):
        with urlopen(f"{api_base}/api/publishers/upload-jobs/{job_id}", timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        summary = {
            "job_id": payload.get("job_id") or job_id,
            "platform": payload.get("platform"),
            "status": payload.get("status"),
            "publish": payload.get("publish"),
            "book_name": payload.get("book_name"),
            "chapter_title": payload.get("chapter_title"),
            "message": str(payload.get("message") or "")[:240],
            "error": str(payload.get("error") or "")[:240],
            "updated_at": payload.get("updated_at"),
        }
        print(json.dumps(summary, ensure_ascii=False))
        if str(payload.get("status") or "").lower() in terminal:
            break
        time.sleep(10)
PY
```

Expected:

- Every created upload job reaches `succeeded`, `failed`, or `cancelled`.
- Successful jobs report saved draft messages.
- Failed jobs are classified with safe error text and do not publish content.

## Task 8: Final Supervisor, Cleanup Check, And Summary

**Files:**
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.json`
- Output: `.codex-monitor/production-longform-publishfalse-TIMESTAMP.jsonl`
- Modify only if needed: source/tests/docs for discovered bugs.

- [ ] **Step 1: Confirm no active generation task remains**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio, json, warnings
from fastmcp import Client
from scripts.monitor_forwin_runtime import json_loads_result, redact_sensitive

async def main():
    warnings.simplefilter("ignore")
    async with Client("http://127.0.0.1:8896/mcp") as client:
        result = json_loads_result(await client.call_tool("task_active_generation_check", {}))
        print(json.dumps(redact_sensitive(result), ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

Expected:

- `has_active_generation_task:false`.
- If an active task remains from this experiment and should stop, use `task_pause` and poll; do not kill the worker.

- [ ] **Step 2: Run read-only supervisor**

Run:

```bash
.venv/bin/python scripts/supervise_forwin_interventions.py \
  --api-base http://127.0.0.1:8899 \
  --mcp-url http://127.0.0.1:8896/mcp \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --skip-github \
  --output-jsonl ".codex-monitor/forwin-supervisor-post-longform-$(date -u +%Y%m%dT%H%M%SZ).jsonl" \
  --latest-json .codex-monitor/latest-supervisor-post-longform.json
```

Expected:

- Exit code is 0.
- `blocked_items: []`, or only safe human-action blocks for platform login/risk-control.

- [ ] **Step 3: Fix code only for reproducible bugs found during the experiment**

Run this only if a code bug is discovered:

```bash
git status --short
git diff --check
```

Expected:

- Use `superpowers:systematic-debugging` to isolate root cause before editing.
- Use `superpowers:test-driven-development` for the focused regression when code changes are required.
- Run the concrete focused test node that covers the bug, then `git diff --check`.
- Commit and push only the concrete bugfix files after the focused verification passes.

- [ ] **Step 4: Produce the final evidence summary**

Run:

```bash
git status --short
docker --context swarm-manager-150 service ls \
  --format '{{.Name}} {{.Image}} {{.Replicas}}' | rg '^forwin-'
```

Expected:

- Worktree is clean except ignored `.codex-monitor/` logs.
- Six ForWin services are still `1/1`.
- Final response includes project id, task ids, generated chapter numbers, approximate character counts, upload job ids, platform statuses, supervisor status, and any blocked items.
