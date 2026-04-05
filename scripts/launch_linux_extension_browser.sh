#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXTENSION_DIR="$REPO_ROOT/browser_extension/forwin-publisher"
PROFILE_DIR="${FORWIN_EXTENSION_TEST_PROFILE:-$REPO_ROOT/data/chrome_profiles/forwin-extension-test}"
BACKEND_URL="${FORWIN_BACKEND_URL:-http://127.0.0.1:8899}"
PREFERRED_BROWSER="${FORWIN_EXTENSION_TEST_BROWSER:-}"
REMOTE_DEBUGGING_PORT="${FORWIN_EXTENSION_TEST_REMOTE_DEBUGGING_PORT:-}"

find_chrome() {
  local candidate
  if [[ -n "$PREFERRED_BROWSER" ]] && command -v "$PREFERRED_BROWSER" >/dev/null 2>&1; then
    command -v "$PREFERRED_BROWSER"
    return 0
  fi
  for candidate in chromium chromium-browser google-chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if [[ ! -d "$EXTENSION_DIR" ]]; then
  echo "extension directory not found: $EXTENSION_DIR" >&2
  exit 1
fi

if ! CHROME_BIN="$(find_chrome)"; then
  echo "chrome/chromium not found" >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"

CHROME_ARGS=(
  --user-data-dir="$PROFILE_DIR"
  --no-first-run
  --no-default-browser-check
  --disable-features=DialMediaRouteProvider
  --disable-extensions-except="$EXTENSION_DIR"
  --load-extension="$EXTENSION_DIR"
  --new-window
  "$BACKEND_URL/publishers"
)

if [[ -n "$REMOTE_DEBUGGING_PORT" ]]; then
  CHROME_ARGS=(--remote-debugging-port="$REMOTE_DEBUGGING_PORT" "${CHROME_ARGS[@]}")
fi

cat <<EOF
Launching Linux extension test browser
  Chrome:        $CHROME_BIN
  Profile:       $PROFILE_DIR
  Extension:     $EXTENSION_DIR
  Backend URL:   $BACKEND_URL

This profile persists extension settings, cookies, and login state for server-side smoke tests.
EOF

exec xvfb-run -a "$CHROME_BIN" "${CHROME_ARGS[@]}"
