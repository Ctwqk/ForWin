#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXTENSION_DIR="$REPO_ROOT/browser_extension/forwin-publisher"
PROFILE_DIR="${FORWIN_EXTENSION_TEST_PROFILE:-$REPO_ROOT/data/chrome_profiles/forwin-extension-test}"
BACKEND_URL="${FORWIN_BACKEND_URL:-http://127.0.0.1:8899}"
PREFERRED_BROWSER="${FORWIN_EXTENSION_TEST_BROWSER:-}"
REMOTE_DEBUGGING_PORT="${FORWIN_EXTENSION_TEST_REMOTE_DEBUGGING_PORT:-}"
REMOTE_DEBUGGING_ADDRESS="${FORWIN_EXTENSION_TEST_REMOTE_DEBUGGING_ADDRESS:-127.0.0.1}"
AUTO_QUALIFY_PROFILE="${FORWIN_EXTENSION_AUTO_QUALIFY_PROFILE:-true}"
REQUIRE_QUALIFIED_PROFILE="${FORWIN_EXTENSION_REQUIRE_QUALIFIED_PROFILE:-true}"
DISPLAY_MODE="${FORWIN_EXTENSION_DISPLAY_MODE:-auto}"
PREFERRED_DISPLAY="${FORWIN_EXTENSION_DISPLAY:-:100}"
XVFB_SERVER_NUM="${FORWIN_EXTENSION_XVFB_SERVER_NUM:-}"
XVFB_SCREEN="${FORWIN_EXTENSION_XVFB_SCREEN:-1280x1024x24}"
PROFILE_QUALIFIER="$REPO_ROOT/scripts/qualify_linux_extension_profile.py"
SESSION_RESTORE_SCRIPT="$REPO_ROOT/scripts/restore_linux_extension_browser_sessions.py"
PYTHON_BIN="${FORWIN_EXTENSION_PYTHON:-}"
RUN_DISPLAY=""
USE_XVFB_RUN=false

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
  for candidate in \
    /ms-playwright/chromium-*/chrome-linux64/chrome \
    /ms-playwright/chromium-*/chrome-linux/chrome \
    /root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome \
    /root/.cache/ms-playwright/chromium-*/chrome-linux/chrome \
    "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux64/chrome \
    "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux/chrome; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

is_truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

find_python() {
  if [[ -n "$PYTHON_BIN" ]] && command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    command -v "$PYTHON_BIN"
    return 0
  fi
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

infer_xvfb_server_num() {
  if [[ -n "$XVFB_SERVER_NUM" ]]; then
    printf '%s\n' "$XVFB_SERVER_NUM"
    return 0
  fi
  if [[ "$PREFERRED_DISPLAY" =~ ^:([0-9]+)(\.[0-9]+)?$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return 0
  fi
  printf '99\n'
}

display_exists() {
  local display="$1"
  if [[ -z "$display" ]] || ! command -v xdpyinfo >/dev/null 2>&1; then
    return 1
  fi
  xdpyinfo -display "$display" >/dev/null 2>&1
}

is_reserved_manager_display() {
  [[ "$1" =~ ^:99(\..*)?$ ]]
}

start_xvfb() {
  local base="$XVFB_SERVER_NUM"
  local offset
  local candidate
  local log_file
  for offset in $(seq 0 20); do
    candidate=":$((base + offset))"
    if display_exists "$candidate"; then
      continue
    fi
    log_file="/tmp/forwin-xvfb-${candidate#:}.log"
    Xvfb "$candidate" -screen 0 "$XVFB_SCREEN" -nolisten tcp >"$log_file" 2>&1 &
    XVFB_PID=$!
    sleep 0.5
    if kill -0 "$XVFB_PID" >/dev/null 2>&1 && { ! command -v xdpyinfo >/dev/null 2>&1 || display_exists "$candidate"; }; then
      RUN_DISPLAY="$candidate"
      return 0
    fi
    wait "$XVFB_PID" >/dev/null 2>&1 || true
  done
  echo "failed to start Xvfb at or after :$XVFB_SERVER_NUM" >&2
  return 1
}

profile_has_browser_process() {
  local cmdline
  local cmd
  for cmdline in /proc/[0-9]*/cmdline; do
    cmd="$(tr '\0' ' ' <"$cmdline" 2>/dev/null || true)"
    case "$cmd" in
      *chrome*"--user-data-dir=$PROFILE_DIR"*|*chromium*"--user-data-dir=$PROFILE_DIR"*)
        return 0
        ;;
    esac
  done
  return 1
}

clear_stale_profile_locks() {
  if [[ -d "$PROFILE_DIR" ]] && ! profile_has_browser_process; then
    rm -f \
      "$PROFILE_DIR/SingletonCookie" \
      "$PROFILE_DIR/SingletonLock" \
      "$PROFILE_DIR/SingletonSocket"
  fi
}

select_display_mode() {
  case "${DISPLAY_MODE,,}" in
    external)
      if display_exists "$PREFERRED_DISPLAY"; then
        RUN_DISPLAY="$PREFERRED_DISPLAY"
        USE_XVFB_RUN=false
        return 0
      fi
      echo "requested external display is not available: $PREFERRED_DISPLAY" >&2
      return 1
      ;;
    xvfb|xvfb-run)
      USE_XVFB_RUN=true
      return 0
      ;;
    auto)
      if display_exists "$PREFERRED_DISPLAY"; then
        RUN_DISPLAY="$PREFERRED_DISPLAY"
        USE_XVFB_RUN=false
        return 0
      fi
      if [[ -n "${DISPLAY:-}" ]] && ! is_reserved_manager_display "$DISPLAY" && display_exists "$DISPLAY"; then
        RUN_DISPLAY="$DISPLAY"
        USE_XVFB_RUN=false
        return 0
      fi
      USE_XVFB_RUN=true
      return 0
      ;;
    *)
      echo "unknown FORWIN_EXTENSION_DISPLAY_MODE: $DISPLAY_MODE" >&2
      return 1
      ;;
  esac
}

run_with_display() {
  if [[ "$USE_XVFB_RUN" == "true" ]]; then
    start_xvfb
    set +e
    DISPLAY="$RUN_DISPLAY" "$@"
    local status=$?
    set -e
    if [[ -n "${XVFB_PID:-}" ]]; then
      kill "$XVFB_PID" >/dev/null 2>&1 || true
      wait "$XVFB_PID" >/dev/null 2>&1 || true
      unset XVFB_PID
    fi
    return "$status"
  else
    DISPLAY="$RUN_DISPLAY" "$@"
  fi
}

exec_with_display() {
  if [[ "$USE_XVFB_RUN" == "true" ]]; then
    start_xvfb
    export DISPLAY="$RUN_DISPLAY"
    exec "$@"
  else
    export DISPLAY="$RUN_DISPLAY"
    exec "$@"
  fi
}

cleanup_display() {
  if [[ -n "${XVFB_PID:-}" ]]; then
    kill "$XVFB_PID" >/dev/null 2>&1 || true
    wait "$XVFB_PID" >/dev/null 2>&1 || true
    unset XVFB_PID
  fi
}

if [[ ! -d "$EXTENSION_DIR" ]]; then
  echo "extension directory not found: $EXTENSION_DIR" >&2
  exit 1
fi

if ! CHROME_BIN="$(find_chrome)"; then
  echo "chrome/chromium not found" >&2
  exit 1
fi

if ! PYTHON_BIN="$(find_python)"; then
  echo "python3/python not found. Install Python before starting the Linux extension browser." >&2
  exit 1
fi

XVFB_SERVER_NUM="$(infer_xvfb_server_num)"
select_display_mode
if [[ "$USE_XVFB_RUN" == "true" ]] && ! command -v Xvfb >/dev/null 2>&1; then
  echo "Xvfb not found and no reusable display was available. Install xvfb or start an external display." >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"
clear_stale_profile_locks

if is_truthy "$REQUIRE_QUALIFIED_PROFILE"; then
  if ! "$PYTHON_BIN" "$PROFILE_QUALIFIER" --check >/dev/null; then
    if is_truthy "$AUTO_QUALIFY_PROFILE"; then
      echo "Extension profile is not qualified yet; bootstrapping it with the configured backend URL."
      run_with_display "$PYTHON_BIN" "$PROFILE_QUALIFIER" --qualify
    else
      "$PYTHON_BIN" "$PROFILE_QUALIFIER" --check
      echo "Refusing to start because FORWIN_EXTENSION_REQUIRE_QUALIFIED_PROFILE is enabled." >&2
      exit 1
    fi
  fi
fi
clear_stale_profile_locks

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

if [[ "${FORWIN_EXTENSION_NO_SANDBOX:-auto}" == "true" ]] || [[ "${FORWIN_EXTENSION_NO_SANDBOX:-auto}" == "1" ]] || { [[ "${FORWIN_EXTENSION_NO_SANDBOX:-auto}" == "auto" ]] && [[ "$(id -u)" == "0" ]]; }; then
  CHROME_ARGS=(--no-sandbox "${CHROME_ARGS[@]}")
fi

if [[ -n "$REMOTE_DEBUGGING_PORT" ]]; then
  CHROME_ARGS=(
    --remote-debugging-address="$REMOTE_DEBUGGING_ADDRESS"
    --remote-debugging-port="$REMOTE_DEBUGGING_PORT"
    "${CHROME_ARGS[@]}"
  )
fi

cat <<EOF
Launching Linux extension test browser
  Chrome:        $CHROME_BIN
  Profile:       $PROFILE_DIR
  Extension:     $EXTENSION_DIR
  Backend URL:   $BACKEND_URL
  Qualified:     $REQUIRE_QUALIFIED_PROFILE
  Display mode:  $([[ "$USE_XVFB_RUN" == "true" ]] && printf 'managed Xvfb starting at :%s' "$XVFB_SERVER_NUM" || printf 'external %s' "$RUN_DISPLAY")

This profile persists extension settings, cookies, and login state for server-side smoke tests.
EOF

if [[ "$USE_XVFB_RUN" == "true" ]]; then
  start_xvfb
  export DISPLAY="$RUN_DISPLAY"
else
  export DISPLAY="$RUN_DISPLAY"
fi

trap cleanup_display EXIT

"$CHROME_BIN" "${CHROME_ARGS[@]}" &
CHROME_PID=$!

if [[ -n "$REMOTE_DEBUGGING_PORT" ]] && [[ -f "$SESSION_RESTORE_SCRIPT" ]]; then
  if ! "$PYTHON_BIN" "$SESSION_RESTORE_SCRIPT" --cdp-url "http://127.0.0.1:$REMOTE_DEBUGGING_PORT"; then
    echo "Warning: failed to restore backend browser sessions into the Linux extension browser." >&2
  fi
fi

wait "$CHROME_PID"
