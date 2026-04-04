from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from .platforms import SUPPORTED_PLATFORMS


@dataclass(slots=True)
class ServerUploadResult:
    ok: bool
    current_url: str
    message: str
    error: str = ""
    result_payload: dict[str, Any] | None = None


class ServerPublisherUploader:
    """Server-side uploader that reuses browser cookies synced from the extension."""

    def __init__(self) -> None:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - exercised in runtime deployment
            raise RuntimeError(
                "缺少 playwright 依赖，当前后端还不能使用同步会话执行上传。"
            ) from exc
        self._sync_playwright = sync_playwright
        self._timeout_error = PlaywrightTimeoutError

    def upload(
        self,
        *,
        platform: str,
        cookies: list[dict[str, Any]],
        book_name: str,
        chapter_title: str,
        body: str,
        publish: bool,
        upload_url: str | None = None,
    ) -> ServerUploadResult:
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"不支持的平台: {platform}")
        spec = SUPPORTED_PLATFORMS[platform]
        browser_cookies = [self._cookie_to_playwright(item) for item in cookies if item.get("name")]
        if not browser_cookies:
            return ServerUploadResult(
                ok=False,
                current_url="",
                message="后端没有可用的浏览器会话。",
                error="missing-cookies",
            )

        with self._sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.add_cookies(browser_cookies)
                page = context.new_page()
                try:
                    page.goto(upload_url or spec.publish_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1500)
                    if self._looks_like_login(page.url):
                        return ServerUploadResult(
                            ok=False,
                            current_url=page.url,
                            message="后端会话未进入作者后台，请重新同步登录凭证。",
                            error="login-required",
                        )
                    page = self._prepare_publish_page(page, platform, book_name)
                    self._fill_title(page, platform, chapter_title)
                    self._fill_body(page, platform, body)
                    self._click_action(page, platform, publish)
                    action_meta = self._confirm_post_action(page, platform, publish)
                    self._settle_after_action(page)
                    return self._verify_submission(
                        page=page,
                        platform=platform,
                        book_name=book_name,
                        chapter_title=chapter_title,
                        publish=publish,
                        action_meta=action_meta,
                    )
                except RuntimeError as exc:
                    message = str(exc)
                    return ServerUploadResult(
                        ok=False,
                        current_url=page.url,
                        message=message,
                        error=self._classify_runtime_error(message),
                        result_payload={"executor": "server", "phase": "validation"},
                    )
                except self._timeout_error:
                    return ServerUploadResult(
                        ok=False,
                        current_url=page.url,
                        message="平台页面加载或控件响应超时，请稍后重试。",
                        error="timeout",
                        result_payload={"executor": "server", "phase": "timeout"},
                    )
            finally:
                browser.close()

    @staticmethod
    def _cookie_to_playwright(cookie: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "name": str(cookie.get("name", "")),
            "value": str(cookie.get("value", "")),
            "domain": str(cookie.get("domain", "")),
            "path": str(cookie.get("path", "/") or "/"),
            "secure": bool(cookie.get("secure")),
            "httpOnly": bool(cookie.get("httpOnly")),
            "sameSite": str(cookie.get("sameSite", "Lax") or "Lax"),
        }
        expires = cookie.get("expires", -1)
        try:
            expires_value = float(expires)
        except (TypeError, ValueError):
            expires_value = -1
        if expires_value > 0:
            payload["expires"] = expires_value
        return payload

    @staticmethod
    def _looks_like_login(url: str) -> bool:
        lowered = str(url).lower()
        return "login" in lowered or "qrconnect" in lowered

    @staticmethod
    def _classify_runtime_error(message: str) -> str:
        text = str(message or "")
        if "创建新书" in text or "创建书本" in text:
            return "book-required"
        if "登录" in text:
            return "login-required"
        return "upload-blocked"

    @staticmethod
    def _title_selectors() -> list[str]:
        return [
            'input[placeholder*="标题"]',
            'textarea[placeholder*="标题"]',
            'input[name*="title"]',
            'input[id*="title"]',
            '[contenteditable="true"][data-placeholder*="标题"]',
        ]

    @staticmethod
    def _body_selectors() -> list[str]:
        return [
            'textarea[placeholder*="正文"]',
            'textarea[placeholder*="内容"]',
            'textarea',
            '[contenteditable="true"]',
            'div[role="textbox"]',
        ]

    def _prepare_publish_page(self, page, platform: str, book_name: str):
        if platform == "qidian":
            self._prepare_qidian_editor(page, book_name)
            return page
        if platform == "fanqie":
            return self._prepare_fanqie_editor(page, book_name)
        self._click_by_keywords(page, ["作品管理", "小说管理", "我的作品"], timeout_ms=2500)
        if book_name:
            self._click_by_keywords(page, [book_name], timeout_ms=2500)
        self._click_by_keywords(
            page,
            ["写新章", "新建章节", "创建章节", "上传章节", "新增章节", "继续创作", "开始创作"],
            timeout_ms=5000,
        )
        return page

    def _capture_page_after_action(
        self,
        page,
        action,
        *,
        timeout_ms: int = 3000,
        settle_ms: int = 1200,
    ):
        context = getattr(page, "context", None)
        pages_before = list(getattr(context, "pages", [])) if context is not None else []
        popup = None
        used_expect_popup = False
        if hasattr(page, "expect_popup"):
            try:
                with page.expect_popup(timeout=timeout_ms) as popup_info:
                    action()
                popup = popup_info.value
                used_expect_popup = True
            except self._timeout_error:
                popup = None
        if not used_expect_popup:
            action()
        target = popup
        if target is None and context is not None:
            try:
                page.wait_for_timeout(settle_ms)
            except Exception:
                pass
            for candidate in getattr(context, "pages", []):
                if candidate not in pages_before:
                    target = candidate
                    break
        if target is None:
            return page
        try:
            target.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            target.wait_for_timeout(settle_ms)
        except Exception:
            pass
        return target

    def _fill_title(self, page, platform: str, value: str) -> None:
        if platform == "qidian":
            locator = page.locator("#inputTitle").first
            try:
                if locator.count():
                    locator.fill(value)
                    return
            except self._timeout_error:
                pass
        if platform == "fanqie":
            if self._fill_fanqie_title(page, value):
                return
        self._fill_first(page, self._title_selectors(), value, "章节标题")

    def _fill_body(self, page, platform: str, value: str) -> None:
        if platform == "qidian":
            if self._fill_qidian_body(page, value):
                return
        if platform == "fanqie":
            if self._fill_fanqie_body(page, value):
                return
        self._fill_first(page, self._body_selectors(), value, "正文")

    def _fill_first(self, page, selectors: list[str], value: str, field_name: str) -> None:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                if selector.startswith("[contenteditable") or selector == 'div[role="textbox"]':
                    locator.click()
                    page.evaluate(
                        """([sel, text]) => {
                          const node = document.querySelector(sel);
                          if (!node) return false;
                          node.focus();
                          if (node.isContentEditable) {
                            node.replaceChildren();
                            node.textContent = text;
                          }
                          node.dispatchEvent(new Event('input', { bubbles: true }));
                          return true;
                        }""",
                        [selector, value],
                    )
                else:
                    locator.fill(value)
                return
            except self._timeout_error:
                continue
        raise RuntimeError(f"未找到可填写的{field_name}输入框。")

    def _click_action(self, page, platform: str, publish: bool) -> None:
        if platform == "qidian":
            if publish:
                self._ensure_qidian_saved_draft(page)
            try:
                page.get_by_role("button", name="发布" if publish else "保存").click(timeout=12000)
                return
            except self._timeout_error:
                pass
        if platform == "fanqie":
            self._dismiss_fanqie_modal(page)
            if publish:
                # Fanqie's first "下一步" may already trigger an official
                # platform validation response. Leave the actual click to
                # _confirm_fanqie_publish so the response listener is attached
                # before the button is pressed.
                return
            if (not publish) and self._click_exact_text(page, ["存草稿"], timeout_ms=12000):
                return
        keywords = ["发布", "提交", "保存并发布", "发布章节", "提交审核"] if publish else ["保存", "存草稿", "保存草稿"]
        if not self._click_by_keywords(page, keywords, timeout_ms=12000):
            raise RuntimeError(f"未找到{'发布' if publish else '保存草稿'}按钮。")

    def _confirm_post_action(self, page, platform: str, publish: bool) -> dict[str, Any] | None:
        if not publish:
            return None
        if platform == "qidian":
            return self._confirm_qidian_publish(page)
        if platform == "fanqie":
            return self._confirm_fanqie_publish(page)
        keywords = [
            "确认发布",
            "确认提交",
            "确定发布",
            "确定",
            "继续发布",
        ]
        try:
            clicked = page.evaluate(
                """([keywords]) => {
                  const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
                  const selectors = [
                    '.el-message-box',
                    '.byte-modal-content',
                    '.ant-modal-content',
                    '[role="dialog"]',
                    '.modal',
                  ];
                  const containers = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
                  const nodes = (containers.length ? containers : [document]).flatMap((root) =>
                    Array.from(root.querySelectorAll('button, [role="button"], a, span, div'))
                  );
                  const target = nodes.find((node) => keywords.some((keyword) => textOf(node) === keyword || textOf(node).includes(keyword)));
                  if (!target) return false;
                  target.click();
                  return true;
                }""",
                [keywords],
            )
            if clicked:
                page.wait_for_timeout(1200)
        except self._timeout_error:
            return None
        return None

    def _confirm_qidian_publish(self, page) -> dict[str, Any] | None:
        self._wait_for_any_text(page, ["确认章节信息", "确认发布", "同意"], timeout_ms=12000)
        self._wait_for_qidian_confirm_enabled(page, timeout_ms=8000)
        clicked_state = {"value": False}
        publish_response = self._capture_response_during_click(
            page,
            "publishChapter",
            lambda: clicked_state.__setitem__("value", self._click_qidian_confirm(page, timeout_ms=4000)) or clicked_state["value"],
            timeout_ms=10000,
        )
        current_url = str(getattr(page, "url", "") or "")
        clicked = bool(clicked_state["value"]) or self._extract_qidian_ccid(current_url) != ""
        if clicked and publish_response is None:
            publish_response = self._read_qidian_publish_response(page)
        if clicked:
            page.wait_for_timeout(1200)
        if self._click_exact_text(page, ["同意"], timeout_ms=4000):
            page.wait_for_timeout(1200)
        if isinstance(publish_response, dict) and "response" in publish_response:
            response_payload = publish_response.get("response")
        else:
            response_payload = publish_response
        return {
            "response": response_payload if isinstance(response_payload, dict) else None,
            "current_url": current_url,
            "clicked": clicked,
        }

    def _confirm_fanqie_publish(self, page) -> dict[str, Any] | None:
        self._dismiss_fanqie_modal(page)
        self._wait_for_any_text(page, ["下一步", "确认发布", "开启检测"], timeout_ms=12000)
        publish_meta: dict[str, Any] = {"responses": {}}
        live_payloads = self._listen_for_fanqie_publish_responses(page)
        try:
            if not self._fanqie_has_exact_button(page, "确认发布"):
                self._dismiss_fanqie_modal(page)
                clicked_first_step = self._fanqie_click_next_step(page, timeout_ms=8000)
                if clicked_first_step:
                    page.wait_for_timeout(1200)
                self._fanqie_confirm_risk_detection(page)
                if not self._fanqie_wait_for_publish_settings(page, timeout_ms=2500):
                    self._fanqie_tab_enter_publish_intermediate(page)
                    self._fanqie_wait_for_publish_settings(page, timeout_ms=8000)
                self._fanqie_wait_for_live_publish_payloads(page, live_payloads["data"], timeout_ms=6000)
                self._merge_fanqie_publish_payloads(
                    publish_meta,
                    self._read_fanqie_publish_responses(page),
                    live_payloads["data"],
                )
                first_step_payload = publish_meta["responses"].get("check_trafficed_book")
                first_step_data = None
                if isinstance(first_step_payload, dict):
                    first_step_data = first_step_payload.get("data") if isinstance(first_step_payload, dict) else None
                if isinstance(first_step_data, dict) and not bool(first_step_data.get("is_valid", True)):
                    return publish_meta
                if not self._fanqie_has_exact_button(page, "确认发布"):
                    self._dismiss_fanqie_modal(page)
                    self._fanqie_accept_publish_intermediate_dialog(page)
                    if not self._fanqie_wait_for_publish_settings(page, timeout_ms=2500):
                        self._fanqie_tab_enter_publish_intermediate(page)
                        self._fanqie_wait_for_publish_settings(page, timeout_ms=8000)
                    self._fanqie_wait_for_live_publish_payloads(page, live_payloads["data"], timeout_ms=6000)
                    self._merge_fanqie_publish_payloads(
                        publish_meta,
                        self._read_fanqie_publish_responses(page),
                        live_payloads["data"],
                    )
                first_step_payload = publish_meta["responses"].get("check_trafficed_book")
                first_step_data = None
                if isinstance(first_step_payload, dict):
                    first_step_data = first_step_payload.get("data") if isinstance(first_step_payload, dict) else None
                if isinstance(first_step_data, dict) and not bool(first_step_data.get("is_valid", True)):
                    return publish_meta
                if not self._fanqie_has_exact_button(page, "确认发布"):
                    return publish_meta
            self._fanqie_select_non_ai_publish_option(page)
            clicked = False
            initial_response_keys = {
                key
                for key in publish_meta["responses"].keys()
                if key in {"check_trafficed_book", "modify_book", "upload_pic", "new_article", "edit_article"}
            }
            for _ in range(3):
                clicked_state = {"value": False}
                check_payload = self._capture_response_during_click(
                    page,
                    "check_trafficed_book",
                    lambda: clicked_state.__setitem__("value", self._fanqie_click_publish_confirm(page, timeout_ms=8000)) or clicked_state["value"],
                    timeout_ms=10000,
                )
                clicked = clicked or bool(clicked_state["value"])
                if clicked_state["value"] and check_payload is None:
                    check_payload = self._read_single_response_payload(page, "check_trafficed_book", timeout_ms=10000)
                if check_payload is not None:
                    publish_meta["responses"]["check_trafficed_book"] = check_payload
                self._fanqie_wait_for_live_publish_payloads(page, live_payloads["data"], timeout_ms=6000)
                self._merge_fanqie_publish_payloads(
                    publish_meta,
                    self._read_fanqie_publish_responses(page),
                    live_payloads["data"],
                )
                current_response_keys = {
                    key
                    for key in publish_meta["responses"].keys()
                    if key in {"check_trafficed_book", "modify_book", "upload_pic", "new_article", "edit_article"}
                }
                if current_response_keys - initial_response_keys or not self._fanqie_has_exact_button(page, "确认发布"):
                    break
            if clicked:
                page.wait_for_timeout(1500)
            return publish_meta
        finally:
            cleanup = live_payloads.get("cleanup")
            if callable(cleanup):
                cleanup()

    def _settle_after_action(self, page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except self._timeout_error:
            pass
        page.wait_for_timeout(1800)

    def _verify_submission(
        self,
        *,
        page,
        platform: str,
        book_name: str,
        chapter_title: str,
        publish: bool,
        action_meta: dict[str, Any] | None = None,
    ) -> ServerUploadResult:
        if platform == "qidian":
            return self._verify_qidian_submission(
                page=page,
                book_name=book_name,
                chapter_title=chapter_title,
                publish=publish,
                action_meta=action_meta,
            )
        if platform == "fanqie":
            return self._verify_fanqie_submission(
                page=page,
                book_name=book_name,
                chapter_title=chapter_title,
                publish=publish,
                action_meta=action_meta,
            )
        return ServerUploadResult(
            ok=False,
            current_url=page.url,
            message="暂不支持该平台的发布结果验证。",
            error="verification-unsupported",
            result_payload={"executor": "server", "phase": "verify"},
        )

    def _verify_qidian_submission(
        self,
        *,
        page,
        book_name: str,
        chapter_title: str,
        publish: bool,
        action_meta: dict[str, Any] | None = None,
    ) -> ServerUploadResult:
        if self._looks_like_login(page.url):
            return ServerUploadResult(
                ok=False,
                current_url=page.url,
                message="后端会话未进入作者后台，请重新同步登录凭证。",
                error="login-required",
                result_payload={"executor": "server", "phase": "verify"},
            )
        if publish:
            published_result = self._verify_qidian_publish_result(
                page=page,
                chapter_title=chapter_title,
                action_meta=action_meta,
            )
            if published_result is not None:
                return published_result
            status = self._read_qidian_book_status(page, book_name)
            latest_text = status.get("latest_text", "")
            if "无最新章节" in latest_text or not latest_text.strip():
                return ServerUploadResult(
                    ok=False,
                    current_url=page.url,
                    message="起点官方后台仍显示无最新章节，未确认发布成功。",
                    error="publish-not-confirmed",
                    result_payload={"executor": "server", "phase": "verify", "status_text": latest_text},
                )
            draft_titles = self._read_qidian_editor_titles(page)
            if chapter_title and chapter_title not in latest_text:
                return ServerUploadResult(
                    ok=False,
                    current_url=page.url,
                    message="起点官方后台未看到刚提交的章节标题，未确认发布成功。",
                    error="publish-not-confirmed",
                    result_payload={
                        "executor": "server",
                        "phase": "verify",
                        "status_text": latest_text,
                        "draft_titles": draft_titles[:8],
                        "action_meta": action_meta,
                    },
                )
            return ServerUploadResult(
                ok=True,
                current_url=page.url,
                message="起点官方后台已确认最新章节更新。",
                result_payload={
                    "mode": "publish",
                    "executor": "server",
                    "phase": "verified",
                    "status_text": latest_text,
                    "draft_titles": draft_titles[:8],
                    "action_meta": action_meta,
                },
            )
        draft_titles = self._read_qidian_editor_titles(page)
        if chapter_title and chapter_title in draft_titles:
            return ServerUploadResult(
                ok=True,
                current_url=page.url,
                message="起点官方后台已确认章节草稿保存。",
                result_payload={
                    "mode": "draft",
                    "executor": "server",
                    "phase": "verified",
                    "draft_titles": draft_titles[:8],
                },
            )
        return ServerUploadResult(
            ok=False,
            current_url=page.url,
            message="起点官方后台未看到新保存的章节草稿。",
            error="draft-not-confirmed",
            result_payload={"executor": "server", "phase": "verify", "draft_titles": draft_titles[:8]},
        )

    def _verify_qidian_publish_result(
        self,
        *,
        page,
        chapter_title: str,
        action_meta: dict[str, Any] | None = None,
    ) -> ServerUploadResult | None:
        payload = action_meta or {}
        response_payload = payload.get("response") if isinstance(payload, dict) else None
        if isinstance(response_payload, dict):
            response_code = str(
                response_payload.get("code")
                or response_payload.get("returnCode")
                or ""
            ).strip()
            if response_code == "1000000":
                result = response_payload.get("result") if isinstance(response_payload.get("result"), dict) else {}
                ccid = str(result.get("ccid") or result.get("CCID") or "").strip()
                chapter_title_from_result = str(result.get("chaptertitle") or "").strip()
                if ccid:
                    verified_url = self._open_qidian_chapter(page, ccid)
                    body_text = self._safe_body_text(page)
                    wanted_title = chapter_title_from_result or chapter_title
                    if wanted_title and wanted_title in body_text:
                        return ServerUploadResult(
                            ok=True,
                            current_url=verified_url,
                            message="起点官方后台已确认章节进入审核队列。",
                            result_payload={
                                "mode": "publish",
                                "executor": "server",
                                "phase": "verified",
                                "response": response_payload,
                                "ccid": ccid,
                            },
                        )
        current_url = str(payload.get("current_url") or page.url or "")
        current_ccid = self._extract_qidian_ccid(current_url)
        if current_ccid:
            verified_url = self._open_qidian_chapter(page, current_ccid)
            body_text = self._safe_body_text(page)
            status_markers = ("审核", "待审核", "章节信息", "章节详情")
            if chapter_title and chapter_title in body_text and any(marker in body_text for marker in status_markers):
                return ServerUploadResult(
                    ok=True,
                    current_url=verified_url,
                    message="起点官方后台已确认章节页存在并带有审核状态。",
                    result_payload={
                        "mode": "publish",
                        "executor": "server",
                        "phase": "verified",
                        "response": response_payload,
                        "ccid": current_ccid,
                    },
                )
        return None

    def _verify_fanqie_submission(
        self,
        *,
        page,
        book_name: str,
        chapter_title: str,
        publish: bool,
        action_meta: dict[str, Any] | None = None,
    ) -> ServerUploadResult:
        if self._looks_like_login(page.url):
            return ServerUploadResult(
                ok=False,
                current_url=page.url,
                message="后端会话未进入作者后台，请重新同步登录凭证。",
                error="login-required",
                result_payload={"executor": "server", "phase": "verify"},
            )
        status = self._read_fanqie_book_status(page, book_name)
        latest_text = status.get("book_text", "")
        chapter_manage_text = status.get("chapter_manage_text", "")
        if publish:
            bad_markers = ["暂未发布章节", "0 章", "0字", "0 字"]
            for _ in range(4):
                if chapter_title and chapter_title in chapter_manage_text and "审核中" in chapter_manage_text:
                    break
                if not any(marker in latest_text for marker in bad_markers):
                    break
                page.wait_for_timeout(2000)
                status = self._read_fanqie_book_status(page, book_name)
                latest_text = status.get("book_text", "")
                chapter_manage_text = status.get("chapter_manage_text", "")
            if chapter_title and chapter_title in chapter_manage_text and "审核中" in chapter_manage_text:
                return ServerUploadResult(
                    ok=True,
                    current_url=page.url,
                    message="番茄章节管理页已确认章节进入审核中。",
                    result_payload={
                        "mode": "publish",
                        "executor": "server",
                        "phase": "verified",
                        "status_text": latest_text,
                        "chapter_manage_text": chapter_manage_text[:400],
                        "action_meta": action_meta,
                    },
                )
            publish_check = self._fanqie_publish_check_payload(action_meta)
            if publish_check is not None and not bool(publish_check.get("is_valid", True)):
                invalid_reason = str(publish_check.get("invalid_reason", "")).strip() or "番茄官方后台拦截了当前作品的发布。"
                return ServerUploadResult(
                    ok=False,
                    current_url=page.url,
                    message=f"番茄官方后台阻止了发布：{invalid_reason}",
                    error="publish-blocked",
                    result_payload={
                        "executor": "server",
                        "phase": "verify",
                        "status_text": latest_text,
                        "check_trafficed_book": publish_check,
                    },
                )
            setup_payloads = self._fanqie_publish_setup_payloads(action_meta)
            if setup_payloads and any(marker in latest_text for marker in bad_markers):
                return ServerUploadResult(
                    ok=False,
                    current_url=page.url,
                    message="番茄当前停留在首次发布设置流程，书本信息更新已提交，但章节尚未真正发布。",
                    error="publish-setup-required",
                    result_payload={
                        "executor": "server",
                        "phase": "verify",
                        "status_text": latest_text,
                        "action_meta": action_meta,
                    },
                )
            if any(marker in latest_text for marker in bad_markers):
                return ServerUploadResult(
                    ok=False,
                    current_url=page.url,
                    message="番茄官方后台仍显示暂未发布章节，未确认发布成功。",
                    error="publish-not-confirmed",
                    result_payload={
                        "executor": "server",
                        "phase": "verify",
                        "status_text": latest_text,
                        "chapter_manage_text": chapter_manage_text[:400],
                        "action_meta": action_meta,
                    },
                )
            if chapter_title and chapter_title not in latest_text and chapter_manage_text:
                if chapter_title not in chapter_manage_text:
                    return ServerUploadResult(
                        ok=False,
                        current_url=page.url,
                        message="番茄官方后台未看到刚提交的章节标题，未确认发布成功。",
                        error="publish-not-confirmed",
                        result_payload={
                            "executor": "server",
                            "phase": "verify",
                            "status_text": latest_text,
                            "chapter_manage_text": chapter_manage_text[:400],
                            "action_meta": action_meta,
                        },
                    )
            return ServerUploadResult(
                ok=True,
                current_url=page.url,
                message="番茄官方后台已确认章节已提交。",
                result_payload={
                    "mode": "publish",
                    "executor": "server",
                    "phase": "verified",
                    "status_text": latest_text,
                    "action_meta": action_meta,
                },
            )
        chapter_manage_text = status.get("chapter_manage_text", "")
        if chapter_title and chapter_title in chapter_manage_text:
            return ServerUploadResult(
                ok=True,
                current_url=page.url,
                message="番茄官方后台已确认章节草稿保存。",
                result_payload={
                    "mode": "draft",
                    "executor": "server",
                    "phase": "verified",
                    "chapter_manage_text": chapter_manage_text[:400],
                },
            )
        return ServerUploadResult(
            ok=False,
            current_url=page.url,
            message="番茄官方后台未看到新保存的章节。",
            error="draft-not-confirmed",
            result_payload={"executor": "server", "phase": "verify", "chapter_manage_text": chapter_manage_text[:400]},
        )

    def _read_qidian_book_status(self, page, book_name: str) -> dict[str, str]:
        dashboard_url = "https://write.qq.com/portal/dashboard/books"
        page.goto(dashboard_url, wait_until="domcontentloaded", timeout=45000)
        self._settle_after_action(page)
        payload = page.evaluate(
            """([wantedBook]) => {
              const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
              const items = Array.from(document.querySelectorAll('.g-prodution-item'));
              const item = items.find((node) => {
                const text = textOf(node);
                return wantedBook ? text.includes(wantedBook) : true;
              }) || items[0] || null;
              if (!item) return { book_text: '', latest_text: '' };
              const text = textOf(item);
              const latestNode = Array.from(item.querySelectorAll('*')).find((node) => {
                const current = textOf(node);
                return current.includes('无最新章节') || current.includes('最新章节');
              });
              return {
                book_text: text,
                latest_text: latestNode ? textOf(latestNode) : text,
              };
            }""",
            [book_name],
        )
        return payload or {"book_text": "", "latest_text": ""}

    def _open_qidian_chapter(self, page, ccid: str) -> str:
        target_url = page.url
        if "#ccid=" in target_url:
            target_url = f"{target_url.split('#ccid=', 1)[0]}#ccid={ccid}"
        else:
            separator = "&" if "?" in target_url else "?"
            target_url = f"{target_url}{separator}ccid={ccid}"
        page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        self._settle_after_action(page)
        return page.url

    def _safe_body_text(self, page) -> str:
        try:
            return str(page.locator("body").inner_text())
        except self._timeout_error:
            return ""

    def _read_qidian_editor_titles(self, page) -> list[str]:
        try:
            titles = page.evaluate(
                """() => Array.from(document.querySelectorAll('.ne-st-item, .ne-catalog li, .catalog-list li'))
                  .map((node) => String(node.innerText || node.textContent || '').trim())
                  .filter(Boolean)"""
            )
        except self._timeout_error:
            return []
        if not isinstance(titles, list):
            return []
        return [str(item).strip() for item in titles if str(item).strip()]

    def _read_fanqie_book_status(self, page, book_name: str) -> dict[str, str]:
        dashboard_url = "https://fanqienovel.com/main/writer/"
        page.goto(dashboard_url, wait_until="domcontentloaded", timeout=45000)
        self._settle_after_action(page)
        self._dismiss_fanqie_modal(page)
        payload = self._fanqie_entry_links(page, book_name)
        book_text = str(payload.get("book_text", "")).strip()
        chapter_manage_href = str(payload.get("chapter_manage_href", "")).strip()
        chapter_manage_text = ""
        if chapter_manage_href:
            page.goto(
                self._normalize_fanqie_chapter_manage_href(
                    page.url,
                    chapter_manage_href,
                    str(payload.get("book_id", "")).strip(),
                ),
                wait_until="domcontentloaded",
                timeout=45000,
            )
            self._settle_after_action(page)
            self._dismiss_fanqie_modal(page)
            try:
                chapter_manage_text = str(page.locator("body").inner_text())
            except self._timeout_error:
                chapter_manage_text = ""
        return {
            "book_text": book_text,
            "chapter_manage_text": chapter_manage_text,
        }

    def _fanqie_publish_check_payload(self, action_meta: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(action_meta, dict):
            return None
        responses = action_meta.get("responses")
        if not isinstance(responses, dict):
            return None
        payload = responses.get("check_trafficed_book")
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def _fanqie_publish_setup_payloads(self, action_meta: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(action_meta, dict):
            return {}
        responses = action_meta.get("responses")
        if not isinstance(responses, dict):
            return {}
        payloads: dict[str, Any] = {}
        for key in ("modify_book", "upload_pic"):
            payload = responses.get(key)
            if isinstance(payload, dict):
                payloads[key] = payload
        return payloads

    def _prepare_qidian_editor(self, page, book_name: str) -> None:
        if "/portal/dashboard" in page.url:
            target = page.evaluate(
                """([wantedBook]) => {
                  const items = Array.from(document.querySelectorAll('.g-prodution-item'));
                  const matchItem = items.find((item) => {
                    const text = String(item.innerText || item.textContent || '');
                    return wantedBook ? text.includes(wantedBook) : true;
                  });
                  const scopedItem = matchItem || items[0] || null;
                  const anchor = scopedItem
                    ? Array.from(scopedItem.querySelectorAll('a')).find((node) => {
                        const text = String(node.innerText || node.textContent || '').trim();
                        const href = String(node.getAttribute('href') || '');
                        return text.includes('去写作') || href.includes('/addType/');
                      })
                    : null;
                  return anchor ? anchor.getAttribute('href') : '';
                }""",
                [book_name],
            )
            if target:
                page.goto(urljoin(page.url, target), wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1800)

        # Qidian's "去写作" opens the latest draft. Click "新建章节" to avoid overwriting it.
        page.evaluate(
            """() => {
              const target = document.querySelector('a.ne-st-item[href="#ccid=new"]');
              if (target) target.click();
            }"""
        )
        page.wait_for_timeout(1500)
        if "#ccid=-1" not in page.url:
            page.wait_for_timeout(1200)

    def _ensure_qidian_saved_draft(self, page) -> None:
        if "#ccid=-1" not in page.url:
            return
        page.get_by_role("button", name="保存").click(timeout=12000)
        self._settle_after_action(page)
        if not self._wait_for_qidian_real_ccid(page, timeout_ms=15000):
            raise RuntimeError("起点保存草稿后仍未拿到真实章节编号，无法继续发布。")

    def _wait_for_qidian_real_ccid(self, page, timeout_ms: int = 12000) -> bool:
        deadline = timeout_ms
        while deadline > 0:
            current_url = str(page.url or "")
            if "#ccid=" in current_url and "#ccid=-1" not in current_url:
                return True
            page.wait_for_timeout(250)
            deadline -= 250
        return False

    def _wait_for_qidian_confirm_enabled(self, page, timeout_ms: int = 6000) -> bool:
        deadline = timeout_ms
        while deadline > 0:
            ready = page.evaluate(
                """() => {
                  const button = document.querySelector('button.btn-send-sure');
                  if (!button) return false;
                  const className = String(button.getAttribute('class') || '');
                  return !className.split(/\\s+/).includes('disabled');
                }"""
            )
            if ready:
                return True
            page.wait_for_timeout(250)
            deadline -= 250
        return False

    def _click_qidian_confirm(self, page, timeout_ms: int = 3000) -> bool:
        try:
            locator = page.locator("button.btn-send-sure").first
            if locator.count():
                locator.click(timeout=timeout_ms, force=True)
                return True
        except Exception:
            return False
        return False

    def _fanqie_click_next_step(self, page, timeout_ms: int = 4000) -> bool:
        try:
            locator = page.locator("button.publish-button.auto-editor-next").first
            if locator.count():
                locator.click(timeout=timeout_ms, force=True)
                return True
        except Exception:
            pass
        try:
            locator = page.get_by_role("button", name="下一步").first
            if locator.count():
                locator.click(timeout=timeout_ms, force=True)
                return True
        except Exception:
            pass
        try:
            clicked = page.evaluate(
                """() => {
                  const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"]'))
                    .filter((node) => !node.closest('.reactour__helper, [class*="reactour"]'));
                  const match = nodes.find((node) => textOf(node) === '下一步');
                  if (!match) return false;
                  match.click();
                  return true;
                }"""
            )
            if clicked:
                return True
        except Exception:
            pass
        return self._click_exact_text(page, ["下一步"], timeout_ms=timeout_ms)

    def _fanqie_click_publish_confirm(self, page, timeout_ms: int = 4000) -> bool:
        try:
            locator = page.get_by_role("button", name="确认发布").first
            if locator.count():
                locator.click(timeout=timeout_ms, force=True)
                return True
        except Exception:
            pass
        return self._click_exact_text(page, ["确认发布"], timeout_ms=timeout_ms)

    def _fanqie_accept_publish_intermediate_dialog(self, page) -> bool:
        labels = ["提交", "确定", "开启检测", "继续", "继续发布", "确认提交"]
        for _ in range(8):
            if self._fanqie_has_exact_button(page, "确认发布"):
                return True
            if self._click_exact_text(page, labels, timeout_ms=800):
                page.wait_for_timeout(1200)
                if self._fanqie_has_exact_button(page, "确认发布"):
                    return True
            page.wait_for_timeout(250)
        return self._fanqie_has_exact_button(page, "确认发布")

    def _fanqie_confirm_risk_detection(self, page) -> bool:
        if not self._wait_for_any_text(page, ["是否进行内容风险检测", "检测暂无风险"], timeout_ms=5000):
            return False
        if self._click_exact_text(page, ["确定"], timeout_ms=3000):
            page.wait_for_timeout(1200)
            return True
        return False

    def _fanqie_wait_for_publish_settings(self, page, timeout_ms: int = 6000) -> bool:
        deadline = timeout_ms
        while deadline > 0:
            try:
                ready = bool(
                    page.evaluate(
                        """() => {
                          const text = String(document.body?.innerText || '');
                          if (!text.includes('发布设置') || !text.includes('是否使用AI')) {
                            return false;
                          }
                          return Array.from(document.querySelectorAll('button, [role="button"]'))
                            .some((node) => String(node.innerText || node.textContent || '').trim() === '确认发布');
                        }"""
                    )
                )
            except Exception:
                ready = False
            if ready:
                return True
            page.wait_for_timeout(250)
            deadline -= 250
        return False

    def _fanqie_select_non_ai_publish_option(self, page) -> bool:
        if hasattr(page, "get_by_text"):
            try:
                locator = page.get_by_text("否", exact=True).last
                if locator.count():
                    locator.click(timeout=2000, force=True)
                    page.wait_for_timeout(300)
                    if self._fanqie_non_ai_selected(page):
                        return True
            except Exception:
                pass
        try:
            clicked = bool(
                page.evaluate(
                    """() => {
                      const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
                      const containers = Array.from(
                        document.querySelectorAll('[role="dialog"], .byte-modal-content, .arco-modal-content, .arco-modal, .byte-modal')
                      );
                      const container = containers.find((node) => {
                        const text = textOf(node);
                        return text.includes('发布设置') && text.includes('是否使用AI');
                      }) || document;
                      const labels = Array.from(container.querySelectorAll('label, .arco-radio, .byte-radio, [role="radio"]'));
                      const noNode = labels.find((node) => textOf(node) == '否' || textOf(node).endsWith('否'));
                      if (!noNode) {
                        return false;
                      }
                      const input = noNode.matches?.('input[type="radio"]')
                        ? noNode
                        : noNode.querySelector?.('input[type="radio"]');
                      if (input && input.checked) {
                        return true;
                      }
                      noNode.click();
                      return true;
                    }"""
                )
            )
            if clicked:
                page.wait_for_timeout(300)
                return self._fanqie_non_ai_selected(page)
            return False
        except Exception:
            return False

    def _fanqie_non_ai_selected(self, page) -> bool:
        try:
            return bool(
                page.evaluate(
                    """() => {
                      const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
                      const containers = Array.from(
                        document.querySelectorAll('[role="dialog"], .byte-modal-content, .arco-modal-content, .arco-modal, .byte-modal')
                      );
                      const container = containers.find((node) => {
                        const text = textOf(node);
                        return text.includes('发布设置') && text.includes('是否使用AI');
                      }) || document;
                      const labels = Array.from(container.querySelectorAll('label, .arco-radio, .byte-radio, [role="radio"]'));
                      const noNode = labels.find((node) => textOf(node) == '否' || textOf(node).endsWith('否'));
                      if (!noNode) {
                        return false;
                      }
                      const input = noNode.matches?.('input[type="radio"]')
                        ? noNode
                        : noNode.querySelector?.('input[type="radio"]');
                      if (input) {
                        return Boolean(input.checked);
                      }
                      const className = String(noNode.getAttribute?.('class') || noNode.className || '');
                      const ariaChecked = String(noNode.getAttribute?.('aria-checked') || '');
                      return ariaChecked == 'true' || className.includes('checked');
                    }"""
                )
            )
        except Exception:
            return False

    def _fanqie_tab_enter_publish_intermediate(self, page, steps: int = 2) -> bool:
        keyboard = getattr(page, "keyboard", None)
        if keyboard is None:
            return False
        for _ in range(steps):
            try:
                keyboard.press("Tab")
                page.wait_for_timeout(150)
                keyboard.press("Enter")
                page.wait_for_timeout(800)
            except Exception:
                return False
            if self._fanqie_wait_for_publish_settings(page, timeout_ms=1200):
                return True
            if self._fanqie_has_exact_button(page, "确认发布"):
                return True
        return False

    def _listen_for_fanqie_publish_responses(self, page) -> dict[str, Any]:
        data: dict[str, Any] = {}
        mapping = {
            "check_trafficed_book": "check_trafficed_book",
            "get_speak_popup": "get_speak_popup",
            "new_article": "new_article",
            "edit_article": "edit_article",
            "modify_book": "modify_book",
            "upload_pic": "upload_pic_v1",
        }
        if not hasattr(page, "on"):
            return {"data": data, "cleanup": None}

        def _listener(response) -> None:
            url = str(getattr(response, "url", ""))
            for key, fragment in mapping.items():
                if fragment not in url:
                    continue
                payload = self._response_to_payload(response)
                if isinstance(payload, dict):
                    data[key] = payload
                break

        page.on("response", _listener)

        def _cleanup() -> None:
            if hasattr(page, "remove_listener"):
                try:
                    page.remove_listener("response", _listener)
                except Exception:
                    pass

        return {"data": data, "cleanup": _cleanup}

    def _fanqie_wait_for_live_publish_payloads(
        self,
        page,
        live_data: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> None:
        remaining = timeout_ms
        seen_count = len(live_data)
        quiet_ms = 0
        while remaining > 0:
            if "check_trafficed_book" in live_data:
                return
            current_count = len(live_data)
            if current_count > seen_count:
                seen_count = current_count
                quiet_ms = 0
            elif current_count > 0:
                quiet_ms += 200
                if quiet_ms >= 1200:
                    return
            page.wait_for_timeout(200)
            remaining -= 200

    @staticmethod
    def _merge_fanqie_publish_payloads(
        publish_meta: dict[str, Any],
        *payload_groups: dict[str, Any] | None,
    ) -> None:
        responses = publish_meta.setdefault("responses", {})
        if not isinstance(responses, dict):
            responses = {}
            publish_meta["responses"] = responses
        for payload_group in payload_groups:
            if not isinstance(payload_group, dict):
                continue
            responses.update(payload_group)

    def _read_qidian_publish_response(self, page) -> dict[str, Any] | None:
        payload = self._read_single_response_payload(page, "publishChapter", timeout_ms=10000)
        if payload is None:
            return None
        return {"response": payload}

    def _prepare_fanqie_editor(self, page, book_name: str):
        self._dismiss_fanqie_modal(page)
        if "/publish/" in str(page.url or ""):
            self._wait_for_fanqie_editor_controls(page)
            self._dismiss_fanqie_modal(page)
            return page
        page_text = str(page.locator("body").inner_text())
        entry = self._fanqie_entry_links(page, book_name)
        publish_href = str(entry.get("publish_href", "")).strip()
        if "/main/writer/" in page.url and publish_href:
            page.goto(urljoin(page.url, publish_href), wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1800)
            self._dismiss_fanqie_modal(page)
            if "/publish/" in page.url or "存草稿" in str(page.locator("body").inner_text()):
                return page
        if (
            "/main/writer/" in page.url
            and book_name
            and book_name in page_text
            and "创建章节" in page_text
        ):
            try:
                page = self._capture_page_after_action(
                    page,
                    lambda: page.locator("text=创建章节").first.click(timeout=4000),
                    timeout_ms=4000,
                    settle_ms=1800,
                )
                if "/publish/" in page.url or "存草稿" in str(page.locator("body").inner_text()):
                    return page
            except self._timeout_error:
                pass
        if (
            "/main/writer/" in page.url
            and "创建新书" in page_text
            and "去写作" in page_text
            and (not book_name or book_name not in page_text)
            and (page := self._open_fanqie_direct_chapter_editor(page))
        ):
            self._dismiss_fanqie_modal(page)
            return page
        if self._fanqie_has_empty_work_state(page):
            page = self._open_fanqie_direct_chapter_editor(page)
            if page:
                self._dismiss_fanqie_modal(page)
                if self._fanqie_publish_editor_ready(page):
                    return page
                return page
            raise RuntimeError("番茄当前账号还没有可上传的作品，请先在平台创建新书。")
        self._click_by_keywords(page, ["作品管理", "小说管理", "我的作品"], timeout_ms=2500)
        if book_name:
            self._click_by_keywords(page, [book_name], timeout_ms=2500)
        page = self._capture_page_after_action(
            page,
            lambda: self._click_by_keywords(
                page,
                ["去写作", "写新章", "新建章节", "创建章节", "上传章节", "新增章节", "继续创作", "开始创作"],
                timeout_ms=5000,
            ),
            timeout_ms=5000,
            settle_ms=1200,
        )
        self._dismiss_fanqie_modal(page)
        if self._fanqie_publish_editor_ready(page):
            return page
        if self._fanqie_has_empty_work_state(page):
            page = self._open_fanqie_direct_chapter_editor(page)
            if page:
                self._dismiss_fanqie_modal(page)
                if self._fanqie_publish_editor_ready(page):
                    return page
                return page
            raise RuntimeError("番茄当前账号还没有可上传的作品，请先在平台创建新书。")
        return page

    def _open_fanqie_direct_chapter_editor(self, page):
        try:
            page.locator("text=创建新书").first.click(timeout=4000)
        except self._timeout_error:
            if not self._click_by_keywords(page, ["创建新书"], timeout_ms=4000):
                return None
        page.wait_for_timeout(1200)
        try:
            page = self._capture_page_after_action(
                page,
                lambda: page.locator("text=去写章节").first.click(timeout=4000),
                timeout_ms=4000,
                settle_ms=1800,
            )
        except self._timeout_error:
            page = self._capture_page_after_action(
                page,
                lambda: self._click_by_keywords(page, ["去写章节"], timeout_ms=4000),
                timeout_ms=4000,
                settle_ms=1800,
            )
            if page is None:
                return None
        if "/publish/" in page.url or "存草稿" in str(page.locator("body").inner_text()):
            return page
        return None

    def _fanqie_entry_links(self, page, book_name: str) -> dict[str, str]:
        return page.evaluate(
            """([wantedBook]) => {
              const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
              const hrefOf = (node) => String(node?.getAttribute?.('href') || '');
              const items = Array.from(document.querySelectorAll('.home-book-item, .home-book-item-home'));
              const matchItem = items.find((item) => {
                const text = textOf(item);
                return wantedBook ? text.includes(wantedBook) : true;
              }) || items[0] || null;
              const publishAnchor = matchItem
                ? Array.from(matchItem.querySelectorAll('a[href]')).find((node) => {
                    const href = hrefOf(node);
                    return href.includes('/publish/');
                  })
                : null;
              const manageAnchor = matchItem
                ? Array.from(matchItem.querySelectorAll('a[href]')).find((node) => {
                    const href = hrefOf(node);
                    return href.includes('/chapter-manage/');
                  })
                : null;
              const bookIdMatch = publishAnchor ? hrefOf(publishAnchor).match(/\\/publish\\/(\\d+)/) : null;
              return {
                book_text: matchItem ? textOf(matchItem) : '',
                publish_href: publishAnchor ? hrefOf(publishAnchor) : '',
                chapter_manage_href: manageAnchor ? hrefOf(manageAnchor) : '',
                book_id: bookIdMatch ? bookIdMatch[1] : '',
              };
            }""",
            [book_name],
        )

    def _normalize_fanqie_chapter_manage_href(
        self,
        current_url: str,
        raw_href: str,
        book_id: str,
    ) -> str:
        href = str(raw_href or "").strip()
        if not href:
            return ""
        if "/chapter-manage/" in href and "&" in href:
            href = href.split("&", 1)[0]
        if "?type=" not in href and "/chapter-manage/" in href:
            href = f"{href}?type=1"
        parsed = urlparse(href)
        if parsed.scheme and parsed.netloc:
            return href
        if book_id and "/chapter-manage/" not in href:
            href = f"/main/writer/chapter-manage/{book_id}?type=1"
        return urljoin(current_url, href)

    def _read_fanqie_publish_responses(self, page) -> dict[str, Any]:
        if not hasattr(page, "wait_for_response"):
            return {}
        mapping = {
            "check_trafficed_book": "check_trafficed_book",
            "get_speak_popup": "get_speak_popup",
            "new_article": "new_article",
            "edit_article": "edit_article",
            "modify_book": "modify_book",
            "upload_pic": "upload_pic_v1",
        }
        captured: dict[str, Any] = {}
        for key, fragment in mapping.items():
            payload = self._read_single_response_payload(page, fragment, timeout_ms=3000)
            if isinstance(payload, dict):
                captured[key] = payload
        return captured

    def _capture_response_during_click(
        self,
        page,
        fragment: str,
        clicker,
        *,
        timeout_ms: int,
    ) -> dict[str, Any] | None:
        if hasattr(page, "on"):
            captured: list[dict[str, Any]] = []

            def _listener(response) -> None:
                if captured:
                    return
                if fragment not in str(getattr(response, "url", "")):
                    return
                payload = self._response_to_payload(response)
                if isinstance(payload, dict):
                    captured.append(payload)

            page.on("response", _listener)
            try:
                clicked = clicker()
                if clicked is False:
                    return None
                remaining = timeout_ms
                while remaining > 0 and not captured:
                    page.wait_for_timeout(200)
                    remaining -= 200
            finally:
                if hasattr(page, "remove_listener"):
                    try:
                        page.remove_listener("response", _listener)
                    except Exception:
                        pass
            if captured:
                return captured[0]
        if hasattr(page, "expect_response"):
            try:
                with page.expect_response(lambda row, wanted=fragment: wanted in row.url, timeout=timeout_ms) as pending:
                    clicked = clicker()
                if clicked is False:
                    return None
                return self._response_to_payload(pending.value)
            except self._timeout_error:
                return None
        clicked = clicker()
        if clicked is False:
            return None
        return self._read_single_response_payload(page, fragment, timeout_ms=timeout_ms)

    @staticmethod
    def _extract_qidian_ccid(url: str) -> str:
        current = str(url or "")
        marker = "#ccid="
        if marker not in current:
            return ""
        ccid = current.split(marker, 1)[1].split("&", 1)[0].strip()
        if not ccid or ccid == "-1":
            return ""
        return ccid

    def _read_single_response_payload(
        self,
        page,
        fragment: str,
        *,
        timeout_ms: int,
    ) -> dict[str, Any] | None:
        if not hasattr(page, "wait_for_response"):
            return None
        try:
            response = page.wait_for_response(
                lambda row, wanted=fragment: wanted in row.url,
                timeout=timeout_ms,
            )
        except self._timeout_error:
            return None
        return self._response_to_payload(response)

    @staticmethod
    def _response_to_payload(response) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except Exception:
            try:
                payload = {"text": response.text()}
            except Exception:
                payload = None
        return payload if isinstance(payload, dict) else None

    def _fill_qidian_body(self, page, value: str) -> bool:
        iframe = page.locator("iframe#mce_0_ifr").first
        try:
            if iframe.count() == 0:
                return False
            frame_handle = iframe.element_handle()
            if frame_handle is None:
                return False
            frame = frame_handle.content_frame()
            if frame is None:
                return False
            body = frame.locator("body").first
            body.evaluate(
                """(node, text) => {
                  node.focus();
                  node.replaceChildren();
                  node.textContent = text;
                  node.dispatchEvent(new Event('input', { bubbles: true }));
                  node.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
            page.locator("textarea#mce_0").first.evaluate(
                """(node, text) => {
                  node.value = text;
                  node.dispatchEvent(new Event('input', { bubbles: true }));
                  node.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
            return True
        except Exception:
            return False

    def _fill_fanqie_body(self, page, value: str) -> bool:
        self._fill_fanqie_sequence(page)
        try:
            editor = page.locator('.ProseMirror[contenteditable="true"]').first
            if editor.count() == 0:
                return False
            minimum_chars = max(1, len(value) // 2)
            attempts = 0
            while attempts < 4:
                attempts += 1
                self._dismiss_fanqie_modal(page)
                focused = self._focus_fanqie_body(page)
                if not focused:
                    page.wait_for_timeout(250)
                    continue
                page.keyboard.insert_text(value)
                remaining_ms = 2500
                status = self._read_fanqie_editor_status(page)
                while remaining_ms > 0:
                    status = self._read_fanqie_editor_status(page)
                    if status["body_char_count"] >= minimum_chars and not status["next_disabled"]:
                        return True
                    page.wait_for_timeout(250)
                    remaining_ms -= 250
            return False
        except Exception:
            return False

    def _fill_fanqie_title(self, page, value: str) -> bool:
        locator = page.locator('input[placeholder="请输入标题"]').first
        try:
            self._fill_fanqie_sequence(page)
            self._dismiss_fanqie_modal(page)
            if locator.count() == 0:
                return False
            locator.click(timeout=2000, force=True)
            locator.fill(value, timeout=2000)
            page.wait_for_timeout(300)
            try:
                if locator.input_value().strip() == value:
                    return True
            except self._timeout_error:
                pass
            self._dismiss_fanqie_modal(page)
            locator.click(timeout=2000, force=True)
            page.keyboard.press("Control+A")
            page.keyboard.type(value, delay=20)
            page.wait_for_timeout(300)
            try:
                if locator.input_value().strip() == value:
                    return True
            except self._timeout_error:
                pass
            self._fill_fanqie_sequence(page)
            page.keyboard.press("Tab")
            page.keyboard.insert_text(value)
            page.wait_for_timeout(300)
            try:
                return locator.input_value().strip() == value
            except self._timeout_error:
                return False
        except self._timeout_error:
            return False

    def _fill_fanqie_sequence(self, page) -> None:
        locator = page.locator("input.serial-input.byte-input.byte-input-size-default").first
        for _ in range(3):
            try:
                if locator.count() == 0:
                    return
                current = locator.input_value().strip()
                if current:
                    return
                self._dismiss_fanqie_modal(page)
                locator.click(timeout=2000, force=True)
                locator.fill("1")
                page.wait_for_timeout(150)
                current = locator.input_value().strip()
                if current:
                    return
                self._dismiss_fanqie_modal(page)
                locator.click(timeout=2000, force=True)
                page.keyboard.press("Control+A")
                page.keyboard.type("1", delay=20)
                page.wait_for_timeout(150)
            except self._timeout_error:
                self._dismiss_fanqie_modal(page)
                continue
        return

    def _read_fanqie_editor_status(self, page) -> dict[str, Any]:
        payload = page.evaluate(
            """() => {
              const bodyText = String(document.body?.innerText || '');
              const countMatch = bodyText.match(/正文字数\\s*(\\d+)/);
              const nextButton =
                document.querySelector('button.publish-button.auto-editor-next') ||
                Array.from(document.querySelectorAll('button')).find((node) => String(node.innerText || node.textContent || '').trim() === '下一步');
              const disabled =
                !nextButton ||
                nextButton.hasAttribute('disabled') ||
                String(nextButton.getAttribute('class') || '').includes('arco-btn-disabled');
              const editor = document.querySelector('.ProseMirror[contenteditable="true"]');
              return {
                body_char_count: countMatch ? Number(countMatch[1]) : 0,
                next_disabled: Boolean(disabled),
                editor_text_length: editor ? String(editor.innerText || '').length : 0,
              };
            }"""
        )
        if not isinstance(payload, dict):
            return {"body_char_count": 0, "next_disabled": True, "editor_text_length": 0}
        return {
            "body_char_count": int(payload.get("body_char_count") or 0),
            "next_disabled": bool(payload.get("next_disabled", True)),
            "editor_text_length": int(payload.get("editor_text_length") or 0),
        }

    def _dismiss_fanqie_modal(self, page) -> None:
        if not hasattr(page, "locator"):
            self._dismiss_fanqie_tour(page)
            return
        try:
            locator = page.locator(".byte-modal-close-icon").first
            if locator.count():
                locator.click(timeout=2000)
                page.wait_for_timeout(600)
        except self._timeout_error:
            pass
        if hasattr(page, "get_by_role"):
            for label in ["我知道了", "知道了", "确认"]:
                try:
                    locator = page.get_by_role("button", name=label).first
                    if locator.count():
                        locator.click(timeout=1500)
                        page.wait_for_timeout(400)
                except Exception:
                    pass
        self._dismiss_fanqie_guide_steps(page)
        self._dismiss_fanqie_tour(page)
        self._dismiss_fanqie_guide_steps(page)

    def _focus_fanqie_body(self, page) -> bool:
        targets = [
            '.ProseMirror[contenteditable="true"] p',
            '.ProseMirror[contenteditable="true"]',
        ]
        for selector in targets:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                locator.click(force=True, timeout=2000)
                page.wait_for_timeout(250)
                focused = bool(
                    page.evaluate(
                        """() => {
                          const active = document.activeElement;
                          if (!active) {
                            return false;
                          }
                          if (String(active.className || '').includes('ProseMirror-focused')) {
                            return true;
                          }
                          return Boolean(active.closest && active.closest('.ProseMirror[contenteditable="true"]'));
                        }"""
                    )
                )
                if focused:
                    return True
            except self._timeout_error:
                continue
            except Exception:
                continue
        return False

    def _dismiss_fanqie_tour(self, page) -> bool:
        if not hasattr(page, "evaluate"):
            return False
        try:
            removed = page.evaluate(
                """() => {
                  const selectors = [
                    '#___reactour',
                    '.reactour__helper',
                    '.reactour__mask',
                    '[class*="reactour"]',
                    '.publish-guide-desc',
                    '.publish-guide-mask',
                    '.publish-guide-card',
                  ];
                  const nodes = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
                  nodes.forEach((node) => node.remove());
                  return nodes.length;
                }"""
            )
        except Exception:
            return False
        if int(removed or 0) > 0:
            page.wait_for_timeout(200)
            return True
        return False

    def _dismiss_fanqie_guide_steps(self, page, max_steps: int = 8) -> int:
        if not hasattr(page, "evaluate"):
            return 0
        steps = 0
        while steps < max_steps:
            try:
                label = page.evaluate(
                    """() => {
                      const button = document.querySelector('button.guide-card-footer-btn');
                      if (!(button instanceof HTMLElement)) {
                        return '';
                      }
                      const text = String(button.innerText || button.textContent || '').trim();
                      if (!text) {
                        return '';
                      }
                      button.click();
                      return text;
                    }"""
                )
            except Exception:
                return steps
            if not str(label or "").strip():
                return steps
            steps += 1
            page.wait_for_timeout(350)
        return steps

    def _fanqie_has_empty_work_state(self, page) -> bool:
        return bool(
            page.evaluate(
                """() => {
                  const text = String(document.body?.innerText || '');
                  const hasEmptyNode = Boolean(document.querySelector('.home-book-empty, .author-empty'));
                  const hasNoBookHints = [
                    '助力开书第一笔',
                    '书本信息未准备好',
                    '先发布章节再补充',
                    '创建书本',
                  ].some((item) => text.includes(item));
                  return hasEmptyNode || hasNoBookHints;
                }"""
            )
        )

    def _fanqie_publish_editor_ready(self, page, page_text: str | None = None) -> bool:
        text = str(page_text or "")
        if not text:
            try:
                text = str(page.locator("body").inner_text())
            except Exception:
                text = ""
        page_html = ""
        if hasattr(page, "content"):
            try:
                page_html = str(page.content())
            except Exception:
                page_html = ""
        if "/publish/" not in str(page.url or ""):
            return False
        markers = [
            "存草稿",
            "正文字数",
            "第\n章",
            "第 章",
        ]
        return any(marker in text for marker in markers) or 'placeholder="请输入标题"' in page_html

    def _wait_for_fanqie_editor_controls(self, page, timeout_ms: int = 5000) -> bool:
        if not hasattr(page, "evaluate"):
            return False
        remaining = timeout_ms
        while remaining > 0:
            try:
                ready = page.evaluate(
                    """() => {
                      const title = document.querySelector('input[placeholder="请输入标题"]');
                      const editor = document.querySelector('.ProseMirror[contenteditable="true"], [contenteditable="true"]');
                      const next = document.querySelector('button.publish-button.auto-editor-next');
                      return Boolean(title || (editor && next));
                    }"""
                )
            except Exception:
                ready = False
            if ready:
                return True
            page.wait_for_timeout(250)
            remaining -= 250
        return False

    def _click_by_keywords(self, page, keywords: list[str], timeout_ms: int = 3000) -> bool:
        script = """
        ([keywords]) => {
          const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
          const clickableOf = (node) => {
            if (!node) return null;
            if (node.matches?.('button, [role="button"], a')) return node;
            return node.closest?.('button, [role="button"], a') || node;
          };
          const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'));
          const match = nodes.find((node) => keywords.some((keyword) => textOf(node).includes(keyword)));
          const target = clickableOf(match);
          if (!target) return false;
          target.click();
          return true;
        }
        """
        deadline = timeout_ms
        while deadline > 0:
            clicked = page.evaluate(script, [keywords])
            if clicked:
                return True
            page.wait_for_timeout(350)
            deadline -= 350
        return False

    def _click_exact_text(self, page, labels: list[str], timeout_ms: int = 3000) -> bool:
        for label in labels:
            try:
                locator = page.get_by_role("button", name=label).first
                if locator.count():
                    locator.click(timeout=min(timeout_ms, 1000), force=True)
                    return True
            except Exception:
                pass
            try:
                locator = page.get_by_role("link", name=label).first
                if locator.count():
                    locator.click(timeout=min(timeout_ms, 1000), force=True)
                    return True
            except Exception:
                pass
        script = """
        ([labels]) => {
          const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
          const clickableOf = (node) => {
            if (!node) return null;
            if (node.matches?.('button, [role="button"], a')) return node;
            return node.closest?.('button, [role="button"], a') || node;
          };
          const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'));
          const match = nodes.find((node) => labels.some((label) => textOf(node) === label));
          const target = clickableOf(match);
          if (!target) return false;
          target.click();
          return true;
        }
        """
        deadline = timeout_ms
        while deadline > 0:
            clicked = page.evaluate(script, [labels])
            if clicked:
                return True
            page.wait_for_timeout(250)
            deadline -= 250
        return False

    def _fanqie_has_exact_button(self, page, label: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """([label]) => {
                      const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
                      return Array.from(document.querySelectorAll('button, [role="button"]'))
                        .some((node) => textOf(node) === label);
                    }""",
                    [label],
                )
            )
        except self._timeout_error:
            return False

    def _wait_for_any_text(self, page, labels: list[str], timeout_ms: int = 3000) -> bool:
        script = """
        ([labels]) => {
          const text = String(document.body?.innerText || document.body?.textContent || '');
          return labels.some((label) => text.includes(label));
        }
        """
        deadline = timeout_ms
        while deadline > 0:
            if page.evaluate(script, [labels]):
                return True
            page.wait_for_timeout(250)
            deadline -= 250
        return False
