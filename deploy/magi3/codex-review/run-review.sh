#!/bin/bash
# Lets Codex clear ForWin's review gates on magi3: chapters left in needs_review, band checkpoints and
# world conflicts waiting for a person. Run hourly by com.forwin.codex-review; safe to run by hand.
# Codex starts only when some project is waiting, so idle runs cost nothing.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
here="$(cd "$(dirname "$0")" && pwd)"
state="${FORWIN_CODEX_REVIEW_HOME:-$HOME/forwin/codex-review}"
api="${FORWIN_CODEX_REVIEW_API:-http://127.0.0.1:8899}"
mcp="${FORWIN_CODEX_REVIEW_MCP:-http://127.0.0.1:8896/mcp}"
timeout="${FORWIN_CODEX_REVIEW_TIMEOUT_SECONDS:-1800}"
logs="$state/logs"
mkdir -p "$logs"
stamp=$(date +%Y%m%d-%H%M%S)
log="$logs/review-$stamp.log"

lock="$state/.lock"
if ! mkdir "$lock" 2>/dev/null; then
  echo "$(date '+%F %T') another run holds $lock, skipping" >> "$logs/skipped.log"
  exit 0
fi
trap 'rmdir "$lock"' EXIT

pending=$(python3 - "$api" <<'EOF'
import json
import sys
import urllib.request

base = sys.argv[1].rstrip("/")
waiting = []
for project in json.load(urllib.request.urlopen(base + "/api/projects", timeout=30)):
    pid = project.get("id") or project.get("project_id")
    detail = json.load(urllib.request.urlopen(f"{base}/api/projects/{pid}", timeout=30))
    blocking = (detail.get("blocking_reason") or {}).get("code") or ""
    # next_gate is also set when nothing waits (chapter_N_write and the like): only a review or a
    # blocking reason (world conflict, checkpoint) needs someone.
    if detail.get("needs_review_chapter_count") or blocking or (detail.get("next_gate") or "").endswith("_review"):
        waiting.append(f"{pid}:{detail.get('title')}:review={detail.get('needs_review_chapter_count')}"
                       f":gate={detail.get('next_gate')}:blocking={blocking}")
print(" ".join(waiting))
EOF
)
status=$?
if [ $status -ne 0 ]; then
  echo "$(date '+%F %T') ForWin API at $api not reachable (exit $status)" >> "$logs/skipped.log"
  exit 1
fi
if [ -z "$pending" ]; then
  echo "$(date '+%F %T') nothing waiting" >> "$logs/idle.log"
  exit 0
fi

{
  echo "$(date '+%F %T') waiting: $pending"
  # The ForWin MCP is given to this run only: `codex mcp add` would rewrite the shared
  # ~/.codex/config.toml (the ChatGPT app uses it too).
  perl -e 'alarm shift; exec @ARGV' "$timeout" \
    codex exec \
      -c "mcp_servers.forwin.url=\"$mcp\"" \
      -c 'approval_policy="never"' \
      -s read-only \
      --skip-git-repo-check \
      -C "$state" \
      -o "$logs/review-$stamp.summary.md" \
      - < "$here/review-prompt.md"
  echo "$(date '+%F %T') codex exit $?"
} >> "$log" 2>&1
# Keep two weeks of logs.
find "$logs" -name 'review-*' -mtime +14 -delete
