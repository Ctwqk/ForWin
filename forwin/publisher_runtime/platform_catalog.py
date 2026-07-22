from __future__ import annotations

from forwin.publishers.platforms import SUPPORTED_PLATFORMS, PlatformSpec


class PlatformCatalog:
    def __init__(self, platforms: dict[str, PlatformSpec] | None = None) -> None:
        self.platforms = platforms if platforms is not None else SUPPORTED_PLATFORMS

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
