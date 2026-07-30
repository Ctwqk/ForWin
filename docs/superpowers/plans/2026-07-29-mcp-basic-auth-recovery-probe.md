# MCP Basic Authentication and Recovery Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep MCP and CLI control-plane calls functional when the ForWin API uses Basic authentication, and make recovery evidence prove a real MCP tool call.

**Architecture:** `ForWinAPIClient` owns outbound API authentication and validates the credential-pair invariant once. CLI and MCP default construction pass the shared environment values into that client. The recovery controller executes `task_active_generation_check` through the live MCP HTTP transport instead of accepting a standalone health-page response.

**Tech Stack:** Python 3.13, httpx, FastMCP, pytest/unittest, Docker Compose.

## Global Constraints

- Do not serialize Basic credentials into logs, reports, snapshots, or manifests.
- Empty username and password preserve unauthenticated operation.
- A partial credential pair fails closed before the first HTTP request.
- Recovery evidence must use a read-only MCP tool and must not mutate project or task state.
- Tests must describe general authentication and control-plane behavior, not one candidate, task, project, or chapter.

---

### Task 1: Authenticated API Client Construction

**Files:**
- Modify: `forwin/mcp/client.py`
- Modify: `forwin/mcp/http.py`
- Modify: `forwin/cli.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `FORWIN_HTTP_BASIC_USER`, `FORWIN_HTTP_BASIC_PASSWORD`
- Produces: `ForWinAPIClient(..., basic_username: str = "", basic_password: str = "")`
- Produces: `_default_api_client() -> ForWinAPIClient` in `forwin.mcp.http`

- [ ] **Step 1: Write failing client tests**

Add focused tests that use `httpx.MockTransport`:

```python
def test_api_client_sends_basic_auth_without_leaking_credentials():
    observed = {}

    def handler(request):
        observed["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=[], request=request)

    client = ForWinAPIClient(
        base_url="http://forwin.invalid",
        basic_username="operator",
        basic_password="private",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.project_list())
    assert observed["authorization"].startswith("Basic ")
    assert "private" not in repr(client)
```

Add a parameterized test proving username-only and password-only construction raise `ValueError` before transport use. Add an environment-construction test proving the MCP default client receives the pair.

- [ ] **Step 2: Verify the tests fail for the missing constructor contract**

Run:

```bash
uv run --offline python -m pytest -q \
  tests/test_mcp_server.py::ForWinAPIClientUnitTests
```

Expected: the new tests fail because `ForWinAPIClient` does not accept or send Basic credentials and the MCP default-client helper does not exist.

- [ ] **Step 3: Implement the minimal client and entry-point changes**

In `ForWinAPIClient.__init__`, validate pair completeness and construct an `httpx.BasicAuth` only for a complete pair. Pass it as `auth=` to every `httpx.AsyncClient`.

In `forwin.mcp.http`, add one helper that reads the API URL, timeout, and Basic pair from environment; use it in both `build_mcp_server` and `build_asgi_app`. In `forwin.cli._api_client`, pass the same two environment values.

- [ ] **Step 4: Verify focused MCP tests**

Run:

```bash
uv run --offline python -m pytest -q tests/test_mcp_server.py
```

Expected: all MCP client and server tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add forwin/mcp/client.py forwin/mcp/http.py forwin/cli.py tests/test_mcp_server.py
git commit -m "fix: authenticate internal ForWin API clients"
```

### Task 2: Real MCP Recovery Probe

**Files:**
- Modify: `.artifacts/rc-candidate/recovery_stack.py`
- Modify: `.artifacts/rc-candidate/test_recovery_stack.py`
- Modify: `.artifacts/rc-candidate/recovery-evidence-runbook.md`

**Interfaces:**
- Consumes: live `forwin-mcp` service at `http://127.0.0.1:8896/mcp`
- Produces: `functional_probe("forwin-mcp")` that exits zero only after a valid `task_active_generation_check` response through the verified published endpoint

- [ ] **Step 1: Write failing probe tests**

Assert that the Compose exec command for `forwin-mcp`:

```python
assert "fastmcp" in python_program
assert "task_active_generation_check" in python_program
assert "has_active_generation_task" in python_program
assert "/health" not in python_program
```

Add malformed-result coverage so a missing boolean, task-ID list, active count, or restart-safety boolean exits nonzero.

- [ ] **Step 2: Verify the probe tests fail against the health-only command**

Run:

```bash
uv run --offline python -m pytest -q \
  .artifacts/rc-candidate/test_recovery_stack.py -k "functional_probe and mcp"
```

Expected: failure because the command only performs `GET /health`.

- [ ] **Step 3: Implement the live tool probe**

Replace the MCP health request with the tracked `candidate_mcp_call.py` helper
executed on the controller host against the active run's Docker-verified
published MCP mapping. The request still traverses the live MCP container and
its API client. It must:

```python
async with Client("http://127.0.0.1:8896/mcp") as client:
    result = await client.call_tool("task_active_generation_check", {})
```

Normalize the FastMCP result without printing it, validate the four public fields and internal count consistency, and exit nonzero on tool errors or malformed payloads.

- [ ] **Step 4: Update the runbook contract**

State that fresh-up proves an MCP business call through the candidate's configured API authentication, not only endpoint reachability.

- [ ] **Step 5: Verify recovery and full harness suites**

Run:

```bash
uv run --offline python -m pytest -q \
  .artifacts/rc-candidate/test_recovery_stack.py \
  .artifacts/rc-candidate/test_publisher_recovery.py
uv run --offline python -m pytest -q .artifacts/rc-candidate
git diff --check
```

Expected: all tests pass and no whitespace errors remain.

- [ ] **Step 6: Commit Task 2**

```bash
git add \
  .artifacts/rc-candidate/recovery_stack.py \
  .artifacts/rc-candidate/test_recovery_stack.py \
  .artifacts/rc-candidate/recovery-evidence-runbook.md
git commit -m "test: require authenticated MCP recovery call"
```

### Task 3: Live Candidate Proof and Evidence Reset

**Files:**
- Modify: protected candidate runtime environment only; do not track credentials
- Replace: draft candidate and all post-change RC evidence under new, never-reused artifact directories

**Interfaces:**
- Consumes: rebuilt runtime/browser images labeled with the final source SHA
- Produces: successful live `task_active_generation_check` with Basic authentication enabled

- [ ] **Step 1: Rebuild and freeze the new SHA**

Build both candidate images with the full source revision label, collect a new draft manifest, and verify image IDs and labels.

- [ ] **Step 2: Enable a complete Basic pair in protected candidate configuration**

Store the pair only in the secured, ignored runtime env. Recreate application roles after confirming no active generation task.

- [ ] **Step 3: Run the live MCP proof**

Call `task_active_generation_check` through the candidate MCP endpoint and require the normal structured payload. Verify wrong API credentials return 401 without logging either credential.

- [ ] **Step 4: Retire old release evidence**

Treat all manifests produced before the source change as historical. Use new output roots for V1, immutable gates, matrix, recovery, fresh30, and L200.
