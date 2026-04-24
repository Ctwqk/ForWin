# ForWin MCP Setup For Codex

Start the backend stack:

```bash
docker compose up -d forwin forwin-mcp
```

Add the MCP server to Codex:

```bash
codex mcp add forwin --url http://127.0.0.1:8898/mcp
```

Verify it is configured:

```bash
codex mcp list
```

Recommended next steps:

1. Open this repository in Codex.
2. Keep [AGENTS.md](/home/taiwei/ForWin/AGENTS.md:1) in the repo root so Codex sees the workflow rules.
3. Use the repo-local skill `$forwin-operator` when you want Codex to operate the running ForWin instance through MCP.
