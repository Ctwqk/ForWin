#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROFILE_DIR="${FORWIN_EXTENSION_TEST_PROFILE:-$REPO_ROOT/data/chrome_profiles/forwin-extension-test}"

if [[ -d "$PROFILE_DIR" ]]; then
  rm -rf "$PROFILE_DIR"
  echo "Removed Linux extension test profile: $PROFILE_DIR"
else
  echo "Profile not found: $PROFILE_DIR"
fi
