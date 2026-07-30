# MCP Basic Authentication and Recovery Probe Design

## Problem

ForWin can protect the HTTP API with `FORWIN_HTTP_BASIC_USER` and
`FORWIN_HTTP_BASIC_PASSWORD`, but `ForWinAPIClient` does not forward those
credentials. Enabling Basic authentication therefore breaks MCP and CLI
control-plane calls. The recovery controller compounds the problem by treating
an HTTP 200 from the MCP `/health` route as sufficient, even when real MCP
tools cannot reach the authenticated API.

## Decision

Make API authentication an explicit `ForWinAPIClient` dependency:

- accept a complete Basic credential pair and reject partial pairs;
- pass the resulting `httpx.BasicAuth` object on every API request;
- construct CLI and MCP default clients from the same
  `FORWIN_HTTP_BASIC_USER` and `FORWIN_HTTP_BASIC_PASSWORD` environment;
- keep callers that inject a client or transport unchanged.

Strengthen the recovery controller's `forwin-mcp` functional probe so it calls
the read-only `task_active_generation_check` MCP tool through the live MCP HTTP
endpoint. The probe must reject tool errors or malformed payloads. A standalone
MCP health-page response is not release evidence.

## Rejected Alternatives

- Exempt project and task API routes from Basic authentication. This preserves
  connectivity by weakening the authentication boundary.
- Depend only on trusted-proxy authentication. This couples local recovery and
  direct CLI use to proxy/network identity and does not solve authenticated
  direct API clients.

## Failure Behavior

- Empty credentials keep the current unauthenticated-client behavior.
- Exactly one credential fails during client construction.
- Invalid credentials produce the existing API 401 error through the normal
  client exception mapping.
- Recovery fails closed when the MCP endpoint is reachable but a real tool call
  cannot reach the API or returns an unexpected active-task payload.

Credentials remain external operator configuration. Tests and evidence may
assert header presence or auth method, but must not serialize credential
values.

## Verification

Use TDD to prove:

1. the API client emits the expected Basic header and rejects partial pairs;
2. CLI and MCP default clients read the shared environment contract;
3. the recovery MCP probe executes `task_active_generation_check` and rejects
   a health-only or malformed result;
4. focused MCP/recovery suites and the full RC harness pass;
5. a live candidate with Basic authentication enabled can execute the MCP tool.
