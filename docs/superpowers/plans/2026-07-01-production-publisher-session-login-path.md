# Production Publisher Session Login Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backend-synced publisher browser session restore and baseline verification the routine production login path, while keeping Discord QR delivery out of production automation.

**Architecture:** The running code already has the right core shape: qualified profiles disable QR notifications, browser startup restores backend sessions, baseline/smoke/supervisor report login blockers, and legacy service-level Discord webhook env is ignored. This plan locks that behavior in with policy tests and updates operator docs so routine production login expiry is handled by logging into the shared publisher browser profile and rerunning the baseline, not by sending QR images to Discord.

**Tech Stack:** Python 3.12, pytest, existing ForWin scripts, Markdown operations docs, Chrome extension README.

---

## Scope Check

The approved spec covers one coherent subsystem: production publisher-login continuity. It deliberately excludes upload-chain smoke implementation, long-form generation, platform quota confirmation, publish=true verification, and two-hour supervisor hardening. Those remain follow-up plans after this login path is fixed.

## File Structure

- Create: `tests/test_publisher_session_login_path_policy.py`
  - Static policy tests for the session-login path and no-routine-Discord contract.
- Modify: `README.md`
  - Replace routine QR-handoff instructions with the session restore plus baseline verifier path.
- Modify: `docs/operations/forwin-production-processes.md`
  - Make session restore and baseline verification the main operational flow.
  - Retain one-shot QR only as an emergency/manual legacy note, with no command block and no webhook URL example.
- Modify: `browser_extension/forwin-publisher/README.md`
  - Update extension capability notes to state that routine login continuity uses backend session sync and the shared production browser profile.
- Modify if the new tests reveal a runtime policy gap:
  - `scripts/check_production_publisher_baseline.py`
  - `scripts/smoke_production_publisher_upload_chain.py`
  - `scripts/supervise_forwin_interventions.py`

The implementation should avoid changing QR extraction internals unless a policy test proves a routine production script still calls them.

## Task 1: Add Session Login Policy Tests

**Files:**
- Create: `tests/test_publisher_session_login_path_policy.py`

- [ ] **Step 1: Write the failing policy tests**

Create `tests/test_publisher_session_login_path_policy.py` with this content:

```python
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_routine_automation_scripts_never_invoke_qr_delivery_paths() -> None:
    forbidden = (
        "/api/publishers/extension/login-qr",
        "/api/publishers/login-qr-one-shot",
        "start_publisher_login_qr_one_shot",
        "FORWIN_PUBLISHER_LOGIN_QR_ONE_SHOT_WEBHOOK_URL",
    )
    script_paths = (
        "scripts/check_production_publisher_baseline.py",
        "scripts/smoke_production_publisher_upload_chain.py",
        "scripts/supervise_forwin_interventions.py",
    )

    for script_path in script_paths:
        source = read_repo_file(script_path)
        for marker in forbidden:
            assert marker not in source, f"{script_path} must not invoke {marker}"


def test_operator_docs_make_session_restore_the_routine_login_path() -> None:
    docs = {
        "README.md": read_repo_file("README.md"),
        "docs/operations/forwin-production-processes.md": read_repo_file(
            "docs/operations/forwin-production-processes.md"
        ),
        "browser_extension/forwin-publisher/README.md": read_repo_file(
            "browser_extension/forwin-publisher/README.md"
        ),
    }

    for path, text in docs.items():
        assert "Routine production login continuity uses backend-synced browser sessions." in text, path
        assert "python scripts/check_production_publisher_baseline.py" in text, path
        assert "production publisher browser profile" in text, path


def test_routine_docs_do_not_show_discord_qr_handoff_commands() -> None:
    routine_docs = {
        "README.md": read_repo_file("README.md"),
        "browser_extension/forwin-publisher/README.md": read_repo_file(
            "browser_extension/forwin-publisher/README.md"
        ),
    }

    for path, text in routine_docs.items():
        assert "start_publisher_login_qr_one_shot.py" not in text, path
        assert "FORWIN_PUBLISHER_LOGIN_QR_ONE_SHOT_WEBHOOK_URL" not in text, path
        assert "login-success confirmation" not in text, path


def test_operations_doc_marks_one_shot_qr_as_emergency_manual_only() -> None:
    text = read_repo_file("docs/operations/forwin-production-processes.md")

    assert "Emergency-only legacy QR handoff" in text
    assert "not supported for routine production automation" in text
    assert "must not be scheduled" in text
    assert "must not be used by baseline, smoke, supervisor, deploy, or recurring jobs" in text
    assert "https://discord.com/api/webhooks/" not in text
```

- [ ] **Step 2: Run the new tests to verify the documentation policy fails first**

Run:

```bash
.venv/bin/python -m pytest tests/test_publisher_session_login_path_policy.py -q
```

Expected: FAIL. The current README and extension README still describe the one-shot Discord QR handoff as an allowed operator path, and they do not contain the new routine-session marker sentence.

- [ ] **Step 3: Commit the failing tests**

Run:

```bash
git add tests/test_publisher_session_login_path_policy.py
git commit -m "Test publisher session login path policy"
```

Expected: commit succeeds with only the new test file staged.

## Task 2: Update Routine Operator Documentation

**Files:**
- Modify: `README.md`
- Modify: `browser_extension/forwin-publisher/README.md`
- Modify: `docs/operations/forwin-production-processes.md`

- [ ] **Step 1: Replace the README publisher-login policy block**

In `README.md`, replace the first shared-production Discord QR paragraph that begins with:

```markdown
Shared production Swarm keeps Discord publisher login webhooks disabled. The
legacy `FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK`,
```

and ends with:

```markdown
Ordinary publisher heartbeat checks must only report `login-required`.
```

with:

```markdown
Routine production login continuity uses backend-synced browser sessions.
The shared `forwin-publisher-browser-swarm` profile is the production publisher
browser profile for both Fanqie and Qidian. On startup, the publisher browser
qualifies the extension profile, restores the latest backend-synced sessions
into the same browser context, opens `/publishers`, and waits for extension
heartbeat. Verify the routine path with:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

If the verifier reports `publisher_login_required`, complete the login inside
the production publisher browser profile and rerun the same command. Do not
send QR codes to Discord for routine production login expiry.

Shared production Swarm keeps Discord publisher login webhooks disabled. The
legacy `FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK`,
`FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL`, and
`FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE` settings are ignored by runtime
config and must not be used to route scan-login state to Discord. Do not put
Discord webhook env on browser or worker services. The publisher extension's
login QR notification setting is disabled by default; while disabled, the
extension must not capture a QR image or call
`/api/publishers/extension/login-qr`. A stale profile value of
`loginQrNotificationsEnabled=true` is not enough to re-enable QR forwarding.
Ordinary publisher heartbeat checks must only report `login-required`.
```

- [ ] **Step 2: Replace the second README production QR paragraph**

In `README.md`, replace the paragraph that begins with:

```markdown
For shared production Swarm, keep Discord login alerts disabled.
```

and ends with:

```markdown
sends one login-success confirmation.
```

with:

```markdown
For shared production Swarm, keep Discord login alerts disabled. Routine
production login continuity uses backend-synced browser sessions, not QR
delivery. If encrypted publisher sessions cannot be recovered because the
session secret changed or expired, log in again inside the production publisher
browser profile and rerun `python scripts/check_production_publisher_baseline.py`
with the production arguments shown above.
```

- [ ] **Step 3: Replace the extension README QR capability block**

In `browser_extension/forwin-publisher/README.md`, replace the bullet that begins with:

```markdown
- 扫码登录二维码的 Discord webhook 转发在共享生产 Swarm 已禁用，后端运行时会忽略
```

and ends with:

```markdown
  整页截图、已过期二维码和“二维码已失效 / 点击刷新”占位图会被拦截。
```

with:

```markdown
- Routine production login continuity uses backend-synced browser sessions.
  共享生产 Swarm 中，`forwin-publisher-browser-swarm` 的持久化 profile 是番茄和
  起点共用的 production publisher browser profile。浏览器启动时会恢复后端同步的
  session，打开 `/publishers`，并通过 extension heartbeat 把页面证据回写给后端。
  使用以下命令验证登录状态：

  ```bash
  python scripts/check_production_publisher_baseline.py \
    --api-base http://10.0.0.126:8899 \
    --mcp-health-url http://10.0.0.126:8896/health \
    --docker-context swarm-manager-150 \
    --colima-profile swarmbridged
  ```

  如果检查返回 `publisher_login_required`，只在 production publisher browser
  profile 中完成平台登录，然后重跑同一条 baseline 命令。共享生产的 routine
  登录恢复路径不向 Discord 发送二维码或登录确认。
  `FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK=true`、
  `FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL` 和
  `FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE` 会被后端运行时忽略。扩展设置里的
  二维码通知开关默认关闭；即使旧 profile 里遗留
  `loginQrNotificationsEnabled=true`，没有隐藏的
  `loginQrNotificationsAllowed=true` 和未来的
  `loginQrNotificationsAllowedUntilMs` 临时时间窗时，扩展也不会截图或 POST
  `/api/publishers/extension/login-qr`。扩展心跳检测到登录页时只回写
  `login-required`。
```

- [ ] **Step 4: Replace the routine QR handoff section in operations docs**

In `docs/operations/forwin-production-processes.md`, replace the section that begins with:

```markdown
For an explicit operator login QR handoff, use the one-shot CDP handoff instead
```

and ends with:

```markdown
for one-shot delivery.
```

with:

```markdown
Routine production login continuity uses backend-synced browser sessions. The
shared `forwin-publisher-browser-swarm` profile is the production publisher
browser profile for both Fanqie and Qidian. On startup, the browser service
qualifies the extension profile, restores the latest backend-synced sessions
into the same browser context, opens `/publishers`, and waits for extension
heartbeat. If the production baseline reports `publisher_login_required`, log
in inside that production publisher browser profile and rerun the baseline
command. Do not send QR codes to Discord for routine production login expiry.

Emergency-only legacy QR handoff: the historical one-shot CDP QR handoff is not
supported for routine production automation. It must not be scheduled, must not
be used by baseline, smoke, supervisor, deploy, or recurring jobs, and must not
be documented as the normal way to recover publisher login. During a manual
incident, an operator may inspect the legacy script only after confirming that
session restore and direct production-browser login cannot complete the login
handoff. Any such run must keep webhook values out of service env, backend
runtime config, browser profile, deployment logs, and repository files.
```

- [ ] **Step 5: Update the operations network rules**

In `docs/operations/forwin-production-processes.md`, replace this network-rule group:

```markdown
- Keep Discord login QR webhooks disabled in shared production. The legacy
  `FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK`,
  `FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL`, and
  `FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE` keys are ignored by runtime
  config; do not commit webhook URLs or paste them into deployment logs. For
  manual QR handoff, use `scripts/start_publisher_login_qr_one_shot.py` with
  `FORWIN_PUBLISHER_LOGIN_QR_ONE_SHOT_WEBHOOK_URL` only in the operator shell.
- Keep the publisher extension's login QR notification setting disabled in the
  shared production browser profile by default. The supported operator handoff
  clears the extension QR-notification guards and uploads the direct CDP
  extraction itself. When disabled, the extension must not capture QR images or
  call `/api/publishers/extension/login-qr`. A stale
  `loginQrNotificationsEnabled=true` profile value does not re-enable QR
  forwarding.
- Keep QR forwarding disabled until a deployed browser build has verified a
  direct, non-expired QR capture source; screenshots and invalid QR placeholders
  such as "二维码已失效 / 点击刷新" are intentionally rejected.
- Publisher login QR reminders are only allowed for an active operator-requested
  login session. Ordinary heartbeat checks may record `login-required`, but they
  must not capture QR images or notify Discord just because a login page is
  visible. During incident triage, close stale login tabs before starting a fresh
  operator login session.
- Qidian/WeChat QR capture should prefer direct image extraction from the login
  iframe. The extension uses a scripting fallback for cross-frame QR images and
  rejects full-page screenshots as unsafe login QR payloads. Chrome extensions
  cannot generally read arbitrary network response bodies, so "response"
  extraction means fetching a page-visible QR image URL from the page context
  with credentials, or reading canvas/data URL content directly.
```

with:

```markdown
- Keep Discord login QR webhooks disabled in shared production. The legacy
  `FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK`,
  `FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL`, and
  `FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE` keys are ignored by runtime
  config; do not commit webhook URLs or paste them into deployment logs.
- Treat backend-synced session restore plus baseline verification as the
  routine production login continuity path. If login expires, complete login
  inside the production publisher browser profile and rerun the baseline.
- Keep the publisher extension's login QR notification setting disabled in the
  shared production browser profile by default. When disabled, the extension
  must not capture QR images or call `/api/publishers/extension/login-qr`. A
  stale `loginQrNotificationsEnabled=true` profile value does not re-enable QR
  forwarding.
- Baseline, smoke, supervisor, deploy, and recurring jobs must not call QR
  notification endpoints, run the one-shot QR handoff, or send login-success
  confirmations to Discord. Ordinary heartbeat checks may record
  `login-required`, but they must not capture QR images or notify Discord just
  because a login page is visible.
```

- [ ] **Step 6: Run the policy tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_publisher_session_login_path_policy.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the docs update**

Run:

```bash
git add README.md docs/operations/forwin-production-processes.md browser_extension/forwin-publisher/README.md
git commit -m "Document publisher session login path"
```

Expected: commit succeeds with only the three docs files staged.

## Task 3: Verify Existing Runtime Tests Cover The Contract

**Files:**
- Test: `tests/test_config_env_resolution.py`
- Test: `tests/test_linux_extension_profile.py`
- Test: `tests/test_check_production_publisher_baseline.py`
- Test: `tests/test_smoke_production_publisher_upload_chain.py`
- Test: `tests/test_supervise_forwin_interventions.py`
- Test: `tests/test_publisher_runtime_browser_sessions.py`

- [ ] **Step 1: Run the focused test suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_publisher_session_login_path_policy.py \
  tests/test_config_env_resolution.py::test_publisher_login_discord_webhook_env_is_disabled_by_default \
  tests/test_config_env_resolution.py::test_publisher_login_discord_webhook_env_is_ignored_even_when_enabled \
  tests/test_config_env_resolution.py::test_publisher_login_discord_webhook_file_is_ignored_even_when_enabled \
  tests/test_linux_extension_profile.py::test_qualified_profile_settings_disables_login_qr_notifications \
  tests/test_check_production_publisher_baseline.py \
  tests/test_smoke_production_publisher_upload_chain.py \
  tests/test_supervise_forwin_interventions.py \
  tests/test_publisher_runtime_browser_sessions.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: If a runtime script still invokes QR delivery from routine automation, remove that call**

If Task 1 fails on one of the script files, remove the routine QR invocation from that script. Use this exact policy when changing code:

```python
blocked_items.append(
    {
        "kind": "publisher_login_required",
        "platform": platform,
        "human_action": (
            f"Log in to {platform} in the production publisher browser profile, "
            "then rerun the baseline verifier."
        ),
    }
)
```

Do not replace it with another QR transport. Do not add a local QR-file output path.

- [ ] **Step 3: Rerun the focused test suite after any runtime edit**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_publisher_session_login_path_policy.py \
  tests/test_config_env_resolution.py::test_publisher_login_discord_webhook_env_is_disabled_by_default \
  tests/test_config_env_resolution.py::test_publisher_login_discord_webhook_env_is_ignored_even_when_enabled \
  tests/test_config_env_resolution.py::test_publisher_login_discord_webhook_file_is_ignored_even_when_enabled \
  tests/test_linux_extension_profile.py::test_qualified_profile_settings_disables_login_qr_notifications \
  tests/test_check_production_publisher_baseline.py \
  tests/test_smoke_production_publisher_upload_chain.py \
  tests/test_supervise_forwin_interventions.py \
  tests/test_publisher_runtime_browser_sessions.py \
  -q
```

Expected: PASS.

- [ ] **Step 4: Commit runtime policy edits only if Step 2 changed code**

Run:

```bash
git status --short
```

If the only changed files are runtime script files and related tests, run:

```bash
git add scripts/check_production_publisher_baseline.py scripts/smoke_production_publisher_upload_chain.py scripts/supervise_forwin_interventions.py tests/test_publisher_session_login_path_policy.py
git commit -m "Keep publisher automation off QR delivery"
```

Expected: either there is no runtime commit needed, or the commit contains only the runtime policy fix and tests.

## Task 4: Production Baseline Verification

**Files:**
- No source edits expected.

- [ ] **Step 1: Run the production baseline verifier**

Run:

```bash
.venv/bin/python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

Expected: `status` is `ok` when Fanqie and Qidian are still connected in the shared production publisher browser profile. If either login has expired, expected result is `degraded` with only `publisher_login_required` blocked items and no QR/webhook output.

- [ ] **Step 2: Verify the baseline output contains no QR actions**

Inspect the JSON from Step 1. Expected:

```json
"actions_taken": [{"kind": "checked_production_publisher_baseline"}]
```

The output must not contain `login-qr`, `webhook`, `Discord`, `image_data_url`, `cookie`, `authorization`, or `token` values except redacted key names inside policy summaries.

- [ ] **Step 3: Record verification in the final handoff**

Record:

- commit SHA
- production baseline status
- Fanqie connected or human-login-required status
- Qidian connected or human-login-required status
- whether Discord env policy was clean
- whether `actions_taken` avoided QR delivery

## Task 5: Final Review

**Files:**
- No source edits expected.

- [ ] **Step 1: Run final policy scans**

Run:

```bash
rg -n "https://discord\\.com/api/webhooks/|FORWIN_PUBLISHER_LOGIN_QR_ONE_SHOT_WEBHOOK_URL|start_publisher_login_qr_one_shot.py|login-success confirmation" README.md browser_extension/forwin-publisher/README.md
rg -n "https://discord\\.com/api/webhooks/" docs/operations/forwin-production-processes.md
rg -n "T[B]D|T[O]DO|F[I]XME" docs/superpowers/plans/2026-07-01-production-publisher-session-login-path.md docs/superpowers/specs/2026-07-01-production-publisher-session-login-path-design.md
```

Expected:

- The first command returns no matches.
- The second command returns no matches.
- The third command returns no matches.

- [ ] **Step 2: Check repository status**

Run:

```bash
git status --short
```

Expected: clean worktree after all required commits.

- [ ] **Step 3: Prepare final handoff**

The final handoff should include:

- plan file path
- design spec commit `5d25a7b`
- implementation commits created from this plan
- tests run and results
- production baseline result
- remaining blockers, if any, with only safe human actions
