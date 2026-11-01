"""Keep a platform on publish-hub when the project's automation settings are saved.

Spec 11.2 leaves no switch-back path for a platform that went to publish-hub: the route is not
a switch to flip back, and falling back means redeploying old code after resolving the
platform's publish-hub jobs. Two things in the settings update path could undo it:

* a save that left `route` / `hub_destination_id` out (the home form did) read as "the default
  route" and put the platform back on ForWin's own extension queue (R28): an omitted field now
  keeps what is stored;
* an explicit route other than "hub" (R31): refused, and so is clearing a hub binding's
  destination.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from forwin.api_schema import ProjectAutomationSettings
from forwin.api_schema.project import ProjectAutomationPublishSettings
from forwin.publish_hub.gate import HUB_ROUTE


def stored_publish_settings(
    current: ProjectAutomationSettings,
) -> dict[str, ProjectAutomationPublishSettings]:
    """The stored publish settings of each platform (the primary entry is also in the list)."""
    stored = {
        binding.platform: binding
        for binding in current.publish_bindings
        if binding.platform
    }
    if current.publish.platform:
        stored.setdefault(current.publish.platform, current.publish)
    return stored


def publish_entry_payload(
    entry: ProjectAutomationPublishSettings,
    stored: dict[str, ProjectAutomationPublishSettings],
) -> dict[str, Any]:
    """A requested publish entry as stored data; fields the request left out keep their value.

    Explicit values win, including an explicit null destination. What was stored is looked up
    by platform, so an entry for another platform never inherits this one's routing.
    """
    payload = entry.model_dump(mode="json")
    previous = stored.get(entry.platform)
    if previous is None:
        return payload
    if "route" not in entry.model_fields_set:
        payload["route"] = previous.route
    if "hub_destination_id" not in entry.model_fields_set:
        payload["hub_destination_id"] = previous.hub_destination_id
    return payload


def _refuse_leaving_hub(
    platform: str,
    before: ProjectAutomationPublishSettings,
    route: object,
    destination_id: object,
) -> None:
    """400 when a binding that was on publish-hub would not be on it, or would lose its destination."""
    if before.route != HUB_ROUTE:
        return
    if route != HUB_ROUTE:
        raise HTTPException(
            400,
            f"平台 {platform} 已走 publish-hub，不能把 route 改回旧上传路径："
            "切换后不保留可以切回去的旧路径（publish-hub 设计文档 §11.2）。"
            "真要回退时，先停止向 publish-hub 提交该平台的任务，并在 publish-hub 上"
            "处理完已开始改动的任务（resolve），再从 git 历史重新部署旧代码，"
            "而不是改这个设置。",
        )
    if before.hub_destination_id and not destination_id:
        raise HTTPException(
            400,
            f"平台 {platform} 走 publish-hub 时不能清空 hub_destination_id："
            "没有发布目标，章节只会被挂起、不会发出。要换目标，请直接填新的 hub_destination_id。",
        )


def refuse_leaving_publish_hub(
    stored: dict[str, ProjectAutomationPublishSettings],
    requested: list[dict[str, Any]],
) -> None:
    """400 when a requested entry takes a stored hub binding off publish-hub or clears its destination.

    `requested` are the entries `publish_entry_payload` built, so a field the request left out
    already holds the stored value. The entries are checked as they were asked for, not as the
    normalised settings come out: a request to leave the hub that the primary entry would have
    outvoted is refused too, rather than dropped without a word. Moving a platform to the hub
    and pointing a hub binding at another destination stay allowed, and a platform the request
    does not mention is not touched here (`refuse_dropping_publish_hub` sees the whole result).
    """
    for entry in requested:
        platform = str(entry.get("platform") or "")
        before = stored.get(platform)
        if before is not None:
            _refuse_leaving_hub(
                platform, before, entry.get("route"), entry.get("hub_destination_id")
            )


def refuse_dropping_publish_hub(
    stored: dict[str, ProjectAutomationPublishSettings],
    updated: ProjectAutomationSettings,
) -> None:
    """400 when the settings that would be saved no longer keep a stored hub binding on the hub.

    A request replaces the binding lists wholesale, so a hub binding can be lost without any
    entry naming it: left out of `publish_bindings`, the lists emptied, or pushed off the end.
    Without a binding nothing is published to the platform, but adding it again starts from the
    default route and a release by job id never looks at the binding at all, so the platform
    would come back on ForWin's extension queue past publish-hub's own duplicate check. Run on
    the normalised result, it catches whatever the way the settings got there.
    """
    kept = {binding.platform: binding for binding in updated.publish_bindings}
    for platform, before in stored.items():
        if before.route != HUB_ROUTE:
            continue
        after = kept.get(platform)
        if after is None:
            raise HTTPException(
                400,
                f"平台 {platform} 已走 publish-hub，不能从设置里移除它的发布绑定："
                "移除后重新添加会回到旧上传路径，切换后不保留可以切回去的旧路径"
                "（publish-hub 设计文档 §11.2）。真要回退时，先停止向 publish-hub 提交"
                "该平台的任务，并在 publish-hub 上处理完已开始改动的任务（resolve），"
                "再从 git 历史重新部署旧代码。",
            )
        _refuse_leaving_hub(platform, before, after.route, after.hub_destination_id)
