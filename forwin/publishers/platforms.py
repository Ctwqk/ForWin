from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    platform_id: str
    display_name: str
    login_url: str
    dashboard_url: str
    publish_url: str
    supported_login_methods: tuple[str, ...] = ("scan",)
    supported_actions: tuple[str, ...] = ("save_draft", "publish")


SUPPORTED_PLATFORMS: dict[str, PlatformSpec] = {
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
