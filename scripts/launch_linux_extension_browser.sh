#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXTENSION_DIR="$REPO_ROOT/browser_extension/forwin-publisher"
PROFILE_DIR="${FORWIN_EXTENSION_TEST_PROFILE:-$REPO_ROOT/data/chrome_profiles/forwin-extension-test}"
BACKEND_URL="${FORWIN_BACKEND_URL:-http://127.0.0.1:8899}"

find_chrome() {
  local candidate
  for candidate in google-chrome chromium chromium-browser; do
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

cat <<EOF
Launching Linux extension test browser
  Chrome:        $CHROME_BIN
  Profile:       $PROFILE_DIR
  Extension:     $EXTENSION_DIR
  Backend URL:   $BACKEND_URL

This profile persists extension settings, cookies, and login state for server-side smoke tests.
EOF

exec xvfb-run -a "$CHROME_BIN" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  --disable-features=DialMediaRouteProvider \
  --disable-extensions-except="$EXTENSION_DIR" \
  --load-extension="$EXTENSION_DIR" \
  --new-window \
  "$BACKEND_URL/publishers"
