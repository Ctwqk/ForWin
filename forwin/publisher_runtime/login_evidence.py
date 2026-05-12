from __future__ import annotations

from typing import Any


LOGIN_REQUIRED_ERRORS = {
    "login-required",
    "login_required",
    "platform-login-required",
    "platform_login_required",
}


def platform_login_evidence(platform_id: str, payload: dict[str, Any]) -> bool:
    current_url = str(payload.get("current_url") or payload.get("url") or "").strip()
    last_error = str(payload.get("last_error") or "").strip().lower()
    if bool(payload.get("page_authenticated")):
        return False
    if last_error in LOGIN_REQUIRED_ERRORS:
        return True
    if bool(payload.get("page_login_visible")) and not bool(payload.get("page_authenticated")):
        return True
    if platform_id == "qidian" and "write.qq.com" in current_url and "/portal/login" in current_url:
        return True
    if (
        platform_id == "fanqie"
        and "fanqienovel.com" in current_url
        and "/main/writer/login" in current_url
    ):
        return True
    return False
