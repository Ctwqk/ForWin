from __future__ import annotations

import json
import threading
from pathlib import Path


class RuntimeSettingsStore:
    """Persist mutable runtime settings outside code and environment files."""

    def __init__(
        self,
        path: str,
        *,
        default_api_key: str = "",
        default_base_url: str = "https://api.minimaxi.com/v1",
        default_model: str = "MiniMax-M2.7",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._defaults = {
            "api_key": default_api_key,
            "base_url": default_base_url,
            "model": default_model,
        }

    def _load_unlocked(self) -> dict[str, str]:
        payload = dict(self._defaults)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
            payload.update(
                {
                    "api_key": str(raw.get("api_key", payload["api_key"])),
                    "base_url": str(raw.get("base_url", payload["base_url"])),
                    "model": str(raw.get("model", payload["model"])),
                }
            )
        return payload

    def get(self) -> dict[str, str]:
        with self._lock:
            return self._load_unlocked()

    def save(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> dict[str, str]:
        with self._lock:
            payload = self._load_unlocked()
            if api_key is not None:
                payload["api_key"] = api_key.strip()
            if base_url is not None:
                payload["base_url"] = base_url.strip() or self._defaults["base_url"]
            if model is not None:
                payload["model"] = model.strip() or self._defaults["model"]
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return payload
