#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
from pathlib import Path


SETTINGS_KEY = "forwinPublisherSettings"
CLIENT_ID_KEY = "forwinPublisherClientId"
MARKER_NAME = ".forwin-extension-profile.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_extension_dir() -> Path:
    return repo_root() / "browser_extension" / "forwin-publisher"


def default_profile_dir() -> Path:
    return repo_root() / "data" / "chrome_profiles" / "forwin-extension-test"


def dotenv_values() -> dict[str, str]:
    path = repo_root() / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key] = value
    return values


DOTENV = dotenv_values()


def config_value(name: str, default: str = "") -> str:
    return os.environ.get(name) or DOTENV.get(name) or default


def normalized_backend_url(raw: str) -> str:
    return str(raw or "").strip().rstrip("/")


def api_key() -> str:
    return (config_value("FORWIN_PUBLISHER_EXTENSION_API_KEY") or config_value("PUBLISHER_EXTENSION_API_KEY")).strip()


def api_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def profile_marker_path(profile_dir: Path) -> Path:
    return profile_dir / MARKER_NAME


def load_marker(profile_dir: Path) -> dict[str, object]:
    marker_path = profile_marker_path(profile_dir)
    if not marker_path.exists():
        return {}
    try:
        return json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def profile_is_qualified(profile_dir: Path, extension_dir: Path, backend_url: str, key: str) -> tuple[bool, str]:
    if not key:
        return False, "FORWIN_PUBLISHER_EXTENSION_API_KEY is required for a qualified profile"
    marker = load_marker(profile_dir)
    if not marker:
        return False, f"profile marker is missing: {profile_marker_path(profile_dir)}"
    if marker.get("backendBaseUrl") != backend_url:
        return False, "profile marker backend URL does not match current FORWIN_BACKEND_URL"
    if marker.get("extensionDir") != str(extension_dir):
        return False, "profile marker extension directory does not match current extension path"
    marker_hash = str(marker.get("apiKeySha256") or "")
    if marker_hash != api_key_hash(key):
        return False, "profile marker API key hash does not match current extension key"
    preferred_client_id = config_value("FORWIN_PUBLISHER_PREFERRED_CLIENT_ID").strip()
    if preferred_client_id and marker.get("clientId") != preferred_client_id:
        return False, "profile marker client id does not match current preferred client id"
    if not marker.get("extensionId"):
        return False, "profile marker is missing extensionId"
    return True, "profile is qualified"


def find_browser(preferred: str) -> str:
    if preferred:
        preferred_path = shutil.which(preferred) or preferred
        if Path(preferred_path).exists():
            return preferred_path

    for candidate in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(candidate)
        if found:
            return found

    patterns = (
        "/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/ms-playwright/chromium-*/chrome-linux/chrome",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        str(Path.home() / ".cache" / "ms-playwright" / "chromium-*" / "chrome-linux64" / "chrome"),
        str(Path.home() / ".cache" / "ms-playwright" / "chromium-*" / "chrome-linux" / "chrome"),
    )
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(glob.glob(pattern))
    if matches:
        return sorted(matches)[-1]

    raise SystemExit(
        "chrome/chromium not found. Install chromium, set FORWIN_EXTENSION_TEST_BROWSER, "
        "or run `python -m playwright install chromium`."
    )


def qualify_profile(profile_dir: Path, extension_dir: Path, backend_url: str, key: str) -> None:
    if not key:
        raise SystemExit("FORWIN_PUBLISHER_EXTENSION_API_KEY is required to qualify the profile.")
    if not extension_dir.exists():
        raise SystemExit(f"extension directory not found: {extension_dir}")

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise SystemExit(
            "playwright is required to qualify the extension profile. "
            "Install the project dependencies first, for example `pip install -e .`."
        ) from error

    profile_dir.mkdir(parents=True, exist_ok=True)
    browser = find_browser(os.environ.get("FORWIN_EXTENSION_TEST_BROWSER", "").strip())
    args = [
        f"--disable-extensions-except={extension_dir}",
        f"--load-extension={extension_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=DialMediaRouteProvider",
    ]

    no_sandbox = os.environ.get("FORWIN_EXTENSION_NO_SANDBOX", "auto").strip().lower()
    if no_sandbox in {"1", "true", "yes"} or (no_sandbox == "auto" and hasattr(os, "geteuid") and os.geteuid() == 0):
        args.append("--no-sandbox")

    settings = {
        "backendBaseUrl": backend_url,
        "apiKey": key,
        "syncSessionToBackend": config_value("FORWIN_EXTENSION_SYNC_SESSION_TO_BACKEND", "true").strip().lower()
        not in {"0", "false", "no"},
    }
    preferred_client_id = config_value("FORWIN_PUBLISHER_PREFERRED_CLIENT_ID").strip()
    storage_values: dict[str, object] = {SETTINGS_KEY: settings}
    if preferred_client_id:
        storage_values[CLIENT_ID_KEY] = preferred_client_id

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            executable_path=browser,
            headless=False,
            args=args,
        )
        try:
            worker = next((item for item in context.service_workers if item.url.startswith("chrome-extension://")), None)
            if worker is None:
                worker = context.wait_for_event("serviceworker", timeout=15000)
            extension_id = worker.url.split("/")[2]
            observed = worker.evaluate(
                """async ({ values }) => {
                  await chrome.storage.local.set(values);
                  return await chrome.storage.local.get(Object.keys(values));
                }""",
                {"values": storage_values},
            )
            if observed.get(SETTINGS_KEY) != settings:
                raise SystemExit("extension storage did not return the expected settings after bootstrap")
            if preferred_client_id and observed.get(CLIENT_ID_KEY) != preferred_client_id:
                raise SystemExit("extension storage did not return the expected client id after bootstrap")

            page = context.new_page()
            try:
                page.goto(f"{backend_url}/publishers", wait_until="domcontentloaded", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            except Exception:
                # Profile qualification is about extension storage. The backend may be down
                # when this script is run as an explicit preflight.
                pass

            marker = {
                "qualifiedAt": dt.datetime.now(dt.UTC).isoformat(),
                "backendBaseUrl": backend_url,
                "apiKeySha256": api_key_hash(key),
                "extensionId": extension_id,
                "extensionDir": str(extension_dir),
                "browser": browser,
                "settingsKey": SETTINGS_KEY,
                "clientId": preferred_client_id,
            }
            profile_marker_path(profile_dir).write_text(
                json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        finally:
            context.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check or qualify the ForWin Linux extension browser profile.")
    parser.add_argument("--check", action="store_true", help="only check whether the profile marker matches the env")
    parser.add_argument("--qualify", action="store_true", help="write extension settings into the profile")
    parser.add_argument("--profile-dir", default=config_value("FORWIN_EXTENSION_TEST_PROFILE", str(default_profile_dir())))
    parser.add_argument("--extension-dir", default=config_value("FORWIN_EXTENSION_DIR", str(default_extension_dir())))
    parser.add_argument("--backend-url", default=config_value("FORWIN_BACKEND_URL", "http://127.0.0.1:8899"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    extension_dir = Path(args.extension_dir).expanduser().resolve()
    backend_url = normalized_backend_url(args.backend_url)
    key = api_key()

    if args.check:
        ok, message = profile_is_qualified(profile_dir, extension_dir, backend_url, key)
        print(message)
        return 0 if ok else 1

    if args.qualify or not args.check:
        qualify_profile(profile_dir, extension_dir, backend_url, key)
        ok, message = profile_is_qualified(profile_dir, extension_dir, backend_url, key)
        print(message)
        return 0 if ok else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
