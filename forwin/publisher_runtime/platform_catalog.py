from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    platform_id: str
    display_name: str
    login_url: str
    dashboard_url: str
    publish_url: str
    supported_login_methods: tuple[str, ...] = ("scan",)
    supported_actions: tuple[str, ...] = ("create_book", "save_draft", "publish")


_FALLBACK_SUPPORTED_PLATFORMS: dict[str, PlatformSpec] = {
    "fanqie": PlatformSpec(
        platform_id="fanqie",
        display_name="番茄小说",
        login_url="https://fanqienovel.com/main/writer/",
        dashboard_url="https://fanqienovel.com/main/writer/",
        publish_url="https://fanqienovel.com/main/writer/",
    ),
    "qidian": PlatformSpec(
        platform_id="qidian",
        display_name="起点小说",
        login_url="https://write.qq.com/portal/login",
        dashboard_url="https://write.qq.com/portal/dashboard",
        publish_url="https://write.qq.com/portal/dashboard",
    ),
}


def _load_supported_platforms() -> dict[str, PlatformSpec]:
    try:
        module = import_module("forwin.publishers.platforms")
        platforms = getattr(module, "SUPPORTED_PLATFORMS", None)
    except ImportError:
        platforms = None
    if isinstance(platforms, dict):
        return platforms
    return _FALLBACK_SUPPORTED_PLATFORMS


class PlatformCatalog:
    def __init__(self, platforms: dict[str, PlatformSpec] | None = None) -> None:
        self.platforms = platforms if platforms is not None else _load_supported_platforms()

    def list_ids(self) -> list[str]:
        return list(self.platforms.keys())

    def values(self) -> list[PlatformSpec]:
        return list(self.platforms.values())

    def has(self, platform: str) -> bool:
        return str(platform or "").strip() in self.platforms

    def get(self, platform: str) -> PlatformSpec:
        spec = self.platforms.get(str(platform or "").strip())
        if spec is None:
            raise ValueError(f"不支持的平台: {platform}")
        return spec
