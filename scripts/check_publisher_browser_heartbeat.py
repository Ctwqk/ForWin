#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forwin.publishers.healthcheck import get_preferred_client_heartbeat


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether the preferred Linux publisher browser client is heartbeating."
    )
    parser.add_argument(
        "--api-base-url",
        default=(
            os.environ.get("FORWIN_BACKEND_URL")
            or os.environ.get("FORWIN_API_BASE_URL")
            or "http://localhost:8899"
        ),
    )
    parser.add_argument(
        "--profile-dir",
        default=os.environ.get("FORWIN_EXTENSION_TEST_PROFILE", "data/chrome_profiles/forwin-extension-test"),
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("FORWIN_PUBLISHER_PREFERRED_CLIENT_ID", ""),
    )
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=int(os.environ.get("FORWIN_PUBLISHER_HEARTBEAT_STALE_SECONDS", "90")),
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=int(os.environ.get("FORWIN_EXTENSION_STARTUP_HEARTBEAT_TIMEOUT_SECONDS", "0")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    deadline = time.monotonic() + max(int(args.wait_seconds or 0), 0)
    last = None
    while True:
        last = get_preferred_client_heartbeat(
            args.api_base_url,
            preferred_client_id=args.client_id,
            profile_dir=args.profile_dir,
            stale_seconds=args.stale_seconds,
            allow_latest_recent_fallback=not str(args.client_id or "").strip(),
        )
        if last.ok:
            print(json.dumps(last.to_dict(), ensure_ascii=False))
            return 0
        if time.monotonic() >= deadline:
            print(json.dumps(last.to_dict(), ensure_ascii=False), file=sys.stderr)
            return 1
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
