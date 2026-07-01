# Production Publisher Upload Chain Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repeatable, redacted production smoke runner for the ForWin publisher API and safe `publish=false` browser upload chain.

**Architecture:** Add a dedicated Python CLI that reuses existing monitor redaction patterns, checks publisher API surfaces, optionally creates safe non-publishing upload jobs, and waits for the production publisher browser extension to claim and finish them. The tool never sends Discord login QR messages, never prints extension keys or browser session payloads, and reports human login blockers as structured JSON.

**Tech Stack:** Python 3.13, standard library `argparse`/`json`/`os`/`time`/`urllib`, pytest, existing `scripts.monitor_forwin_runtime` helpers, existing publisher API endpoints.

---

## File Structure

- Create `scripts/smoke_production_publisher_upload_chain.py`
  - Owns redacted report assembly, endpoint smoke, optional direct upload-job smoke, optional explicit project/chapter upload smoke, polling, cleanup, and CLI exit codes.
  - Uses HTTP API endpoints only for publisher smoke. It does not inspect SQLite and does not choose a project/chapter by itself.
- Create `tests/test_smoke_production_publisher_upload_chain.py`
  - Unit tests for redaction, endpoint classification, upload job polling, optional project/chapter path, CLI output, and failure isolation.
- Modify `docs/operations/forwin-production-processes.md`
  - Add the production command sequence after the existing baseline verifier.
- Optionally modify `README.md`
  - Add one short pointer to the operations doc if the production section does not already reference it.

## Safety Rules

- All created jobs use `publish=false`, `create_if_missing=false`, `cover_generation_enabled=false`, `auto_cover_upload_enabled=false`, and `publisher_compliance_required=false`.
- The script must not call `/api/publishers/extension/login-qr`.
- The script must not call `/api/publishers/extension/browser-sessions/{platform}`.
- The script may call `/api/publishers/extension/heartbeat-status` only when an extension key is provided by environment variable. The key value must never appear in output.
- The project/chapter phase runs only when `--project-id` and `--chapter-number` are explicitly provided by the operator after MCP project truth has been checked.
- A non-publishing smoke can end as `degraded` when a human/platform blocker remains. It should still emit a useful report.

## Task 1: Redaction And Summary Helpers

**Files:**
- Create: `tests/test_smoke_production_publisher_upload_chain.py`
- Create: `scripts/smoke_production_publisher_upload_chain.py`

- [ ] **Step 1: Write failing helper tests**

Create `tests/test_smoke_production_publisher_upload_chain.py` with:

```python
from __future__ import annotations

import json
from types import SimpleNamespace

import scripts.smoke_production_publisher_upload_chain as smoke


def args(**overrides):
    base = {
        "api_base": "http://forwin.example",
        "expect_platform_connected": ["fanqie", "qidian"],
        "extension_key_env": "FORWIN_PUBLISHER_EXTENSION_API_KEY",
        "endpoint_platform": "fanqie",
        "book_name": "ForWin Smoke Test",
        "chapter_title": "ForWin smoke chapter",
        "body": "This is a safe smoke chapter body.",
        "poll_seconds": 0.1,
        "poll_interval_seconds": 0.01,
        "run_upload_smoke": False,
        "run_project_upload_smoke": False,
        "project_id": "",
        "chapter_number": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_redact_report_removes_nested_secret_material() -> None:
    payload = {
        "authorization": "Bearer secret",
        "cookie_names": ["AppAuthToken"],
        "cookies": [{"name": "AppAuthToken", "value": "secret-cookie"}],
        "result_payload": {"token": "secret-token", "safe": "ok"},
        "webhook_url": "https://discord.example/secret",
        "nested": {"session_secret": "secret-session", "body": "chapter body"},
    }

    redacted = smoke.redact_report(payload)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert "secret-cookie" not in serialized
    assert "secret-token" not in serialized
    assert "discord.example" not in serialized
    assert "secret-session" not in serialized
    assert redacted["cookie_names"] == ["AppAuthToken"]
    assert redacted["result_payload"]["safe"] == "ok"


def test_summarize_upload_job_omits_body_and_sensitive_payload() -> None:
    summary = smoke.summarize_upload_job(
        {
            "job_id": "job-1",
            "platform": "fanqie",
            "status": "succeeded",
            "book_name": "Book",
            "chapter_title": "Chapter",
            "body": "body must not be logged",
            "publish": False,
            "result_payload": {"token": "secret-token", "mode": "draft"},
            "current_url": "https://fanqienovel.com/main/writer/",
        }
    )

    serialized = json.dumps(summary, ensure_ascii=False)
    assert summary["job_id"] == "job-1"
    assert summary["publish"] is False
    assert "body" not in summary
    assert "body must not be logged" not in serialized
    assert "secret-token" not in serialized
    assert summary["result_payload"]["mode"] == "draft"


def test_safe_upload_payload_forces_non_publishing_flags() -> None:
    payload = smoke.safe_upload_payload(
        platform="qidian",
        book_name="ForWin Smoke Test",
        chapter_title="Smoke Chapter",
        body="Safe body",
    )

    assert payload["platform"] == "qidian"
    assert payload["publish"] is False
    assert payload["create_if_missing"] is False
    assert payload["cover_generation_enabled"] is False
    assert payload["auto_cover_upload_enabled"] is False
    assert payload["publisher_compliance_required"] is False
```

- [ ] **Step 2: Run the helper tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py -q
```

Expected: failure during import because `scripts/smoke_production_publisher_upload_chain.py` does not exist yet.

- [ ] **Step 3: Create helper implementation**

Create `scripts/smoke_production_publisher_upload_chain.py` with module constants and these public helpers:

```python
SENSITIVE_EXACT_KEYS = {"cookie", "cookies", "set-cookie", "image_data_url", "body"}
SENSITIVE_KEY_PARTS = ("api_key", "authorization", "csrf", "password", "secret", "token", "webhook")
TERMINAL_UPLOAD_STATUSES = {"succeeded", "failed", "cancelled"}


def redact_report(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in SENSITIVE_EXACT_KEYS or any(part in normalized for part in SENSITIVE_KEY_PARTS):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact_report(item)
        return redacted
    if isinstance(value, list):
        return [redact_report(item) for item in value]
    return value


def short_text(value, limit=240):
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def summarize_upload_job(job):
    payload = job.get("result_payload") if isinstance(job.get("result_payload"), dict) else {}
    return redact_report(
        {
            "job_id": job.get("job_id") or "",
            "task_kind": job.get("task_kind") or "chapter_upload",
            "project_id": job.get("project_id") or "",
            "platform": job.get("platform") or "",
            "status": job.get("status") or "",
            "book_name": short_text(job.get("book_name")),
            "chapter_title": short_text(job.get("chapter_title")),
            "publish": bool(job.get("publish")),
            "extension_client_id": job.get("extension_client_id") or "",
            "current_url": short_text(job.get("current_url")),
            "message": short_text(job.get("message")),
            "error": short_text(job.get("error")),
            "result_payload": payload,
            "abort_requested": bool(job.get("abort_requested")),
            "created_at": job.get("created_at") or "",
            "updated_at": job.get("updated_at") or "",
            "claimed_at": job.get("claimed_at") or "",
            "started_at": job.get("started_at") or "",
            "finished_at": job.get("finished_at") or "",
            "terminable": bool(job.get("terminable")),
            "deletable": bool(job.get("deletable")),
        }
    )


def safe_upload_payload(*, platform, book_name, chapter_title, body):
    return {
        "platform": platform,
        "book_name": book_name,
        "chapter_title": chapter_title,
        "body": body,
        "publish": False,
        "create_if_missing": False,
        "cover_generation_enabled": False,
        "cover_confirmation_required": False,
        "cover_candidate_count": 1,
        "auto_cover_upload_enabled": False,
        "publisher_compliance_required": False,
    }
```

- [ ] **Step 4: Run helper tests**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py -q
```

Expected: the three helper tests pass.

- [ ] **Step 5: Commit helpers**

Run:

```bash
git add scripts/smoke_production_publisher_upload_chain.py tests/test_smoke_production_publisher_upload_chain.py
git commit -m "Add publisher upload smoke report helpers"
```

## Task 2: Endpoint Smoke

**Files:**
- Modify: `tests/test_smoke_production_publisher_upload_chain.py`
- Modify: `scripts/smoke_production_publisher_upload_chain.py`

- [ ] **Step 1: Add failing endpoint smoke test**

Append this test:

```python
def test_endpoint_smoke_checks_safe_surfaces_and_cleans_api_job(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_http_json(method, url, *, payload=None, headers=None, timeout=10.0):
        calls.append((method, url))
        if url.endswith("/api/publishers/platforms"):
            return {
                "ok": True,
                "status": 200,
                "payload": [
                    {"platform_id": "fanqie", "connected": True, "preferred_client_state": {"connected": True}},
                    {"platform_id": "qidian", "connected": False, "preferred_client_state": {"connected": False}},
                ],
            }
        if url.endswith("/api/publishers/browser-sessions/fanqie"):
            return {"ok": True, "status": 200, "payload": {"platform": "fanqie", "cookie_names": ["sessionid"], "connected": True}}
        if url.endswith("/api/publishers/browser-sessions/qidian"):
            return {"ok": True, "status": 200, "payload": {"platform": "qidian", "cookie_names": [], "connected": False}}
        if url.endswith("/api/publishers/preflight"):
            assert payload["publish"] is False if "publish" in payload else True
            return {"ok": True, "status": 200, "payload": {"ok": True, "blocking": [], "warnings": []}}
        if url.endswith("/api/publishers/work-bindings"):
            return {"ok": True, "status": 200, "payload": []}
        if url.endswith("/api/publishers/chapter-bindings"):
            return {"ok": True, "status": 200, "payload": []}
        if url.endswith("/api/publishers/upload-jobs") and method == "POST":
            assert payload["publish"] is False
            return {"ok": True, "status": 200, "payload": {"job_id": "job-1", "platform": "fanqie", "status": "pending", "publish": False}}
        if url.endswith("/api/publishers/upload-jobs?limit=10"):
            return {"ok": True, "status": 200, "payload": [{"job_id": "job-1", "platform": "fanqie", "status": "pending", "publish": False}]}
        if url.endswith("/api/publishers/upload-jobs/job-1") and method == "GET":
            return {"ok": True, "status": 200, "payload": {"job_id": "job-1", "platform": "fanqie", "status": "pending", "publish": False}}
        if url.endswith("/api/publishers/upload-jobs/job-1/terminate"):
            return {"ok": True, "status": 200, "payload": {"ok": True, "status": "cancelled"}}
        if url.endswith("/api/publishers/upload-jobs/job-1") and method == "DELETE":
            return {"ok": True, "status": 200, "payload": {"ok": True, "status": "deleted"}}
        raise AssertionError(url)

    monkeypatch.setattr(smoke, "http_json", fake_http_json)
    monkeypatch.delenv("FORWIN_PUBLISHER_EXTENSION_API_KEY", raising=False)

    report = smoke.build_report(args(create_api_smoke_job=True))

    assert report["status"] == "degraded"
    assert report["publisher_api"]["platforms"]["ok"] is False
    assert report["endpoint_smoke"]["api_job"]["job_id"] == "job-1"
    assert report["endpoint_smoke"]["api_job_cleanup"]["deleted"] is True
    assert any(item["kind"] == "publisher_login_required" and item["platform"] == "qidian" for item in report["blocked_items"])
    assert any(item["kind"] == "extension_key_missing" for item in report["blocked_items"])
    assert ("POST", "http://forwin.example/api/publishers/upload-jobs") in calls
```

- [ ] **Step 2: Run the endpoint test and verify it fails**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py::test_endpoint_smoke_checks_safe_surfaces_and_cleans_api_job -q
```

Expected: failure because `build_report` and `http_json` are not implemented.

- [ ] **Step 3: Implement HTTP and endpoint smoke**

Add these functions to the script:

- `http_json(method, url, payload=None, headers=None, timeout=10.0)`
- `append_block(report, kind, severity, message, **extra)`
- `append_action(report, action, **extra)`
- `summarize_platform(item)`
- `summarize_browser_session(platform, payload)`
- `extension_headers_from_env(args, report)`
- `run_endpoint_smoke(args, report)`
- `cleanup_api_smoke_job(args, report, job_id)`
- `build_report(args)`

Implementation requirements:

- `http_json` uses `urllib.request.Request`, encodes JSON payloads as UTF-8, parses JSON responses, and returns `{"ok": bool, "status": int, "payload": ...}`.
- `build_report` starts with `checked_at`, `phase="publisher_upload_chain_smoke"`, `status="ok"`, `actions_taken=[]`, and `blocked_items=[]`.
- `run_endpoint_smoke` always checks platforms, public browser-session summaries for expected platforms, preflight, work bindings, and chapter bindings.
- If the extension key env var is absent, add a blocked item with `kind="extension_key_missing"` and do not call heartbeat-status.
- If `args.create_api_smoke_job` is true, create one `publish=false` job for `args.endpoint_platform`, list jobs, get the created job, terminate it, and delete it.
- `build_report` sets `status="degraded"` when any blocked item has severity `human` or `operator`; it sets `status="failed"` when endpoint HTTP shape is unavailable.

- [ ] **Step 4: Run endpoint tests**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py -q
```

Expected: all current smoke tests pass.

- [ ] **Step 5: Commit endpoint smoke**

Run:

```bash
git add scripts/smoke_production_publisher_upload_chain.py tests/test_smoke_production_publisher_upload_chain.py
git commit -m "Add publisher endpoint smoke runner"
```

## Task 3: Browser-Claimed Upload Smoke

**Files:**
- Modify: `tests/test_smoke_production_publisher_upload_chain.py`
- Modify: `scripts/smoke_production_publisher_upload_chain.py`

- [ ] **Step 1: Add failing upload smoke tests**

Append:

```python
def test_upload_smoke_skips_create_when_platform_not_connected(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "http_json", lambda method, url, **kwargs: {"ok": True, "status": 200, "payload": []})
    report = {
        "platforms": [{"platform_id": "fanqie", "connected": False}],
        "blocked_items": [],
        "actions_taken": [],
    }

    smoke.run_upload_smoke(args(run_upload_smoke=True, upload_platform=["fanqie"]), report)

    assert report["upload_jobs"] == []
    assert report["blocked_items"][0]["kind"] == "publisher_login_required"
    assert report["blocked_items"][0]["platform"] == "fanqie"


def test_upload_smoke_polls_until_terminal_and_redacts(monkeypatch) -> None:
    responses = [
        {"ok": True, "status": 200, "payload": {"job_id": "job-2", "platform": "fanqie", "status": "pending", "publish": False, "body": "secret body"}},
        {"ok": True, "status": 200, "payload": {"job_id": "job-2", "platform": "fanqie", "status": "running", "publish": False, "body": "secret body"}},
        {"ok": True, "status": 200, "payload": {"job_id": "job-2", "platform": "fanqie", "status": "succeeded", "publish": False, "result_payload": {"mode": "draft", "token": "secret-token"}}},
    ]

    def fake_http_json(method, url, *, payload=None, headers=None, timeout=10.0):
        return responses.pop(0)

    monkeypatch.setattr(smoke, "http_json", fake_http_json)
    report = {
        "platforms": [{"platform_id": "fanqie", "connected": True}],
        "blocked_items": [],
        "actions_taken": [],
    }

    smoke.run_upload_smoke(args(run_upload_smoke=True, upload_platform=["fanqie"]), report)

    assert report["upload_jobs"][0]["job_id"] == "job-2"
    assert report["upload_jobs"][0]["terminal_state"] == "succeeded"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "secret body" not in serialized
    assert "secret-token" not in serialized
```

- [ ] **Step 2: Run upload smoke tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py::test_upload_smoke_skips_create_when_platform_not_connected tests/test_smoke_production_publisher_upload_chain.py::test_upload_smoke_polls_until_terminal_and_redacts -q
```

Expected: failure because `run_upload_smoke` is not implemented.

- [ ] **Step 3: Implement upload smoke**

Add:

- `platform_connected(report, platform)`
- `poll_upload_job(args, job_id)`
- `run_upload_smoke(args, report)`

Implementation requirements:

- `run_upload_smoke` runs only when `args.run_upload_smoke` is true.
- For each `args.upload_platform`, if the platform is not connected in `report["platforms"]`, add `publisher_login_required` and do not create a job.
- Job creation uses `safe_upload_payload`.
- Poll `GET /api/publishers/upload-jobs/{job_id}` until a terminal status or timeout.
- Terminal summaries include `job_id`, `platform`, `publish`, `states`, `terminal_state`, `current_url`, `message`, `error`, and redacted `result_payload`.
- If timeout happens, call `POST /api/publishers/upload-jobs/{job_id}/terminate`, record the terminate response, and add blocked item `upload_job_timeout`.

- [ ] **Step 4: Run upload tests**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py -q
```

Expected: all smoke tests pass.

- [ ] **Step 5: Commit upload smoke**

Run:

```bash
git add scripts/smoke_production_publisher_upload_chain.py tests/test_smoke_production_publisher_upload_chain.py
git commit -m "Add browser-claimed publisher upload smoke"
```

## Task 4: Explicit Project-Chapter Upload Path

**Files:**
- Modify: `tests/test_smoke_production_publisher_upload_chain.py`
- Modify: `scripts/smoke_production_publisher_upload_chain.py`

- [ ] **Step 1: Add failing project path test**

Append:

```python
def test_project_upload_smoke_requires_explicit_project_and_chapter() -> None:
    report = {"blocked_items": [], "actions_taken": []}

    smoke.run_project_upload_smoke(args(run_project_upload_smoke=True), report)

    assert report["project_chapter_path"]["ok"] is False
    assert report["blocked_items"][0]["kind"] == "project_chapter_not_specified"


def test_project_upload_smoke_posts_safe_project_payload(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http_json(method, url, *, payload=None, headers=None, timeout=10.0):
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/api/projects/project-1/chapters/7"):
            return {"ok": True, "status": 200, "payload": {"chapter_number": 7, "title": "Chapter 7", "status": "accepted"}}
        if method == "POST" and url.endswith("/api/projects/project-1/publishers/upload-jobs"):
            assert payload["chapter_number"] == 7
            assert payload["publish"] is False
            assert payload["create_if_missing"] is False
            return {"ok": True, "status": 200, "payload": {"job_id": "job-project", "platform": "qidian", "status": "pending", "publish": False}}
        if method == "GET" and url.endswith("/api/publishers/upload-jobs/job-project"):
            return {"ok": True, "status": 200, "payload": {"job_id": "job-project", "platform": "qidian", "status": "succeeded", "publish": False}}
        raise AssertionError(url)

    monkeypatch.setattr(smoke, "http_json", fake_http_json)
    report = {"blocked_items": [], "actions_taken": []}

    smoke.run_project_upload_smoke(
        args(
            run_project_upload_smoke=True,
            project_id="project-1",
            chapter_number=7,
            project_platform="qidian",
        ),
        report,
    )

    assert report["project_chapter_path"]["ok"] is True
    assert report["project_chapter_path"]["job"]["job_id"] == "job-project"
```

- [ ] **Step 2: Run project path tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py::test_project_upload_smoke_requires_explicit_project_and_chapter tests/test_smoke_production_publisher_upload_chain.py::test_project_upload_smoke_posts_safe_project_payload -q
```

Expected: failure because `run_project_upload_smoke` is not implemented.

- [ ] **Step 3: Implement project path**

Add `run_project_upload_smoke(args, report)`:

- If disabled, set `project_chapter_path={"ok": True, "skipped": True}`.
- If enabled without `project_id` or positive `chapter_number`, set `ok=False` and add `project_chapter_not_specified`.
- GET `/api/projects/{project_id}/chapters/{chapter_number}` first and record a summary of chapter number, title, and status only.
- POST `/api/projects/{project_id}/publishers/upload-jobs` with platform, chapter number, book name, `publish=false`, `create_if_missing=false`, and disabled cover/compliance flags.
- Poll the created job with the same `poll_upload_job` helper.

- [ ] **Step 4: Run project path tests**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py -q
```

Expected: all smoke tests pass.

- [ ] **Step 5: Commit project path**

Run:

```bash
git add scripts/smoke_production_publisher_upload_chain.py tests/test_smoke_production_publisher_upload_chain.py
git commit -m "Add explicit project publisher smoke path"
```

## Task 5: CLI, Docs, And Production Commands

**Files:**
- Modify: `tests/test_smoke_production_publisher_upload_chain.py`
- Modify: `scripts/smoke_production_publisher_upload_chain.py`
- Modify: `docs/operations/forwin-production-processes.md`

- [ ] **Step 1: Add failing CLI test**

Append:

```python
def test_main_prints_redacted_json_and_returns_degraded(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        smoke,
        "build_report",
        lambda parsed: {
            "status": "degraded",
            "blocked_items": [{"kind": "publisher_login_required", "platform": "fanqie"}],
            "secret": "must-redact",
        },
    )

    code = smoke.main(["--api-base", "http://forwin.example", "--expect-platform-connected", "fanqie"])
    output = capsys.readouterr().out

    assert code == 1
    assert '"status": "degraded"' in output
    assert "must-redact" not in output
```

- [ ] **Step 2: Run CLI test and verify it fails**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py::test_main_prints_redacted_json_and_returns_degraded -q
```

Expected: failure because CLI parsing and `main` are not complete.

- [ ] **Step 3: Implement CLI**

Add `parse_args(argv=None)` and `main(argv=None)`:

- Defaults:
  - `--api-base http://127.0.0.1:8899`
  - `--expect-platform-connected` append list
  - `--extension-key-env FORWIN_PUBLISHER_EXTENSION_API_KEY`
  - `--endpoint-platform fanqie`
  - `--book-name "ForWin Smoke Test"`
  - `--chapter-title "ForWin smoke chapter"`
  - `--body "This is a safe non-publishing ForWin smoke chapter."`
  - `--create-api-smoke-job`
  - `--run-upload-smoke`
  - `--upload-platform` append list
  - `--poll-seconds 120`
  - `--poll-interval-seconds 5`
  - `--run-project-upload-smoke`
  - `--project-id ""`
  - `--chapter-number 0`
  - `--project-platform fanqie`
- Print `json.dumps(redact_report(report), ensure_ascii=False, indent=2, sort_keys=True)`.
- Return `0` for `status=="ok"`, `1` for `degraded`, and `2` for `failed`.

- [ ] **Step 4: Document production sequence**

Add to `docs/operations/forwin-production-processes.md` after the baseline verifier:

```bash
python scripts/smoke_production_publisher_upload_chain.py \
  --api-base http://10.0.0.126:8899 \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --endpoint-platform fanqie \
  --create-api-smoke-job
```

For browser-claimed upload smoke after both platforms are connected:

```bash
python scripts/smoke_production_publisher_upload_chain.py \
  --api-base http://10.0.0.126:8899 \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --endpoint-platform fanqie \
  --create-api-smoke-job \
  --run-upload-smoke \
  --upload-platform fanqie \
  --upload-platform qidian
```

If checking the extension heartbeat-status surface, first set `FORWIN_PUBLISHER_EXTENSION_API_KEY` in the shell through the local secret manager or an existing deployment secret file, then pass only the env var name:

```bash
python scripts/smoke_production_publisher_upload_chain.py \
  --api-base http://10.0.0.126:8899 \
  --extension-key-env FORWIN_PUBLISHER_EXTENSION_API_KEY \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian
```

The docs must state that the environment variable value is never committed, pasted into logs, or printed.

- [ ] **Step 5: Run CLI and docs tests**

Run:

```bash
.venv/bin/pytest tests/test_smoke_production_publisher_upload_chain.py -q
git diff --check
```

Expected: tests pass and whitespace check is clean.

- [ ] **Step 6: Commit CLI and docs**

Run:

```bash
git add scripts/smoke_production_publisher_upload_chain.py tests/test_smoke_production_publisher_upload_chain.py docs/operations/forwin-production-processes.md
git commit -m "Document publisher upload chain smoke"
```

## Task 6: Verification And Production Run

**Files:**
- No source changes unless tests expose a bug.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
.venv/bin/pytest \
  tests/test_smoke_production_publisher_upload_chain.py \
  tests/test_check_production_publisher_baseline.py \
  tests/test_supervise_forwin_interventions.py \
  tests/test_publisher_browser_session_secrets.py \
  tests/test_publisher_routes_security.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run formatting check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 3: Run production baseline**

Run:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --wait-heartbeat-seconds 10
```

Expected:

- `ok` when both platforms are connected by API and page evidence.
- `degraded` with `publisher_login_required` when production publisher-browser login is still missing.
- No Discord/webhook/QR notification violations.

- [ ] **Step 4: Run endpoint-only production smoke**

Run:

```bash
python scripts/smoke_production_publisher_upload_chain.py \
  --api-base http://10.0.0.126:8899 \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --endpoint-platform fanqie \
  --create-api-smoke-job
```

Expected: endpoint report is `ok` or `degraded` only for explicit human/platform blockers, and created API smoke job is terminated/deleted.

- [ ] **Step 5: Run browser-claimed smoke only after both platforms are connected**

Run:

```bash
python scripts/smoke_production_publisher_upload_chain.py \
  --api-base http://10.0.0.126:8899 \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --endpoint-platform fanqie \
  --create-api-smoke-job \
  --run-upload-smoke \
  --upload-platform fanqie \
  --upload-platform qidian \
  --poll-seconds 180 \
  --poll-interval-seconds 5
```

Expected: each platform either reaches terminal upload job state with `publish=false`, or emits a blocked item such as `publisher_login_required`, `quota_unconfirmed`, `publisher_test_work_missing`, or `upload_job_timeout`.

- [ ] **Step 6: Commit any production-run fixes**

If a bug is found and fixed, run the focused tests again, then commit with a narrow message such as:

```bash
git add scripts/smoke_production_publisher_upload_chain.py tests/test_smoke_production_publisher_upload_chain.py docs/operations/forwin-production-processes.md
git commit -m "Fix publisher upload smoke production run"
```

## Self-Review Checklist

- Spec coverage:
  - Source of truth and 246 retirement: covered by docs/spec, no 246 dependency in this plan.
  - Login evidence: baseline remains responsible for real browser page evidence; smoke consumes API/session summaries and reports blockers.
  - Endpoint smoke: Task 2 covers preflight, upload-job create/list/get/terminate/delete, bindings, and session summaries.
  - Browser-claimed smoke: Task 3 covers `publish=false` job creation and polling for extension results.
  - Project/chapter path: Task 4 covers explicit project/chapter arguments only.
  - Redaction: Tasks 1, 2, 3, and 5 cover sensitive payload removal and no QR endpoint usage.
- Placeholder scan: no placeholder tokens or unspecified code paths remain.
- Type consistency:
  - Upload job summaries use existing `PublisherUploadJobResponse` field names.
  - Project upload request uses existing `ProjectChapterPublishRequest` field names.
  - CLI args match function names used by tests.
