from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

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
                    self._prepare_publish_page(page, platform, book_name)
                    self._fill_title(page, platform, chapter_title)
                    self._fill_body(page, platform, body)
                    self._click_action(page, platform, publish)
                    page.wait_for_timeout(1800)
                    return ServerUploadResult(
                        ok=True,
                        current_url=page.url,
                        message="后端已提交上传动作。",
                        result_payload={"mode": "publish" if publish else "draft", "executor": "server"},
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

    def _prepare_publish_page(self, page, platform: str, book_name: str) -> None:
        if platform == "qidian":
            self._prepare_qidian_editor(page, book_name)
            return
        if platform == "fanqie":
            self._prepare_fanqie_editor(page, book_name)
            return
        self._click_by_keywords(page, ["作品管理", "小说管理", "我的作品"], timeout_ms=2500)
        if book_name:
            self._click_by_keywords(page, [book_name], timeout_ms=2500)
        self._click_by_keywords(
            page,
            ["写新章", "新建章节", "创建章节", "上传章节", "新增章节", "继续创作", "开始创作"],
            timeout_ms=5000,
        )

    def _fill_title(self, page, platform: str, value: str) -> None:
        if platform == "qidian":
            locator = page.locator("#inputTitle").first
            try:
                if locator.count():
                    locator.fill(value)
                    return
            except self._timeout_error:
                pass
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
                            node.innerHTML = '';
                            document.execCommand('insertText', false, text);
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
            try:
                page.get_by_role("button", name="发布" if publish else "保存").click(timeout=12000)
                return
            except self._timeout_error:
                pass
        keywords = ["发布", "提交", "保存并发布", "发布章节", "提交审核"] if publish else ["保存", "存草稿", "保存草稿"]
        if not self._click_by_keywords(page, keywords, timeout_ms=12000):
            raise RuntimeError(f"未找到{'发布' if publish else '保存草稿'}按钮。")

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

    def _prepare_fanqie_editor(self, page, book_name: str) -> None:
        self._dismiss_fanqie_modal(page)
        page_text = str(page.locator("body").inner_text())
        entry = self._fanqie_entry_links(page, book_name)
        publish_href = str(entry.get("publish_href", "")).strip()
        if "/main/writer/" in page.url and publish_href:
            page.goto(urljoin(page.url, publish_href), wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1800)
            self._dismiss_fanqie_modal(page)
            if "/publish/" in page.url or "存草稿" in str(page.locator("body").inner_text()):
                return
        if (
            "/main/writer/" in page.url
            and book_name
            and book_name in page_text
            and "创建章节" in page_text
        ):
            try:
                page.locator("text=创建章节").first.click(timeout=4000)
                page.wait_for_timeout(1800)
                if "/publish/" in page.url or "存草稿" in str(page.locator("body").inner_text()):
                    return
            except self._timeout_error:
                pass
        if (
            "/main/writer/" in page.url
            and "创建新书" in page_text
            and "去写作" in page_text
            and (not book_name or book_name not in page_text)
            and self._open_fanqie_direct_chapter_editor(page)
        ):
            page.wait_for_timeout(1800)
            self._dismiss_fanqie_modal(page)
            return
        if self._fanqie_has_empty_work_state(page):
            if self._open_fanqie_direct_chapter_editor(page):
                page.wait_for_timeout(1800)
                self._dismiss_fanqie_modal(page)
                return
            raise RuntimeError("番茄当前账号还没有可上传的作品，请先在平台创建新书。")
        self._click_by_keywords(page, ["作品管理", "小说管理", "我的作品"], timeout_ms=2500)
        if book_name:
            self._click_by_keywords(page, [book_name], timeout_ms=2500)
        self._click_by_keywords(
            page,
            ["去写作", "写新章", "新建章节", "创建章节", "上传章节", "新增章节", "继续创作", "开始创作"],
            timeout_ms=5000,
        )
        page.wait_for_timeout(1200)
        self._dismiss_fanqie_modal(page)
        if self._fanqie_has_empty_work_state(page):
            if self._open_fanqie_direct_chapter_editor(page):
                page.wait_for_timeout(1800)
                self._dismiss_fanqie_modal(page)
                return
            raise RuntimeError("番茄当前账号还没有可上传的作品，请先在平台创建新书。")

    def _open_fanqie_direct_chapter_editor(self, page) -> bool:
        try:
            page.locator("text=创建新书").first.click(timeout=4000)
        except self._timeout_error:
            if not self._click_by_keywords(page, ["创建新书"], timeout_ms=4000):
                return False
        page.wait_for_timeout(1200)
        try:
            page.locator("text=去写章节").first.click(timeout=4000)
        except self._timeout_error:
            if not self._click_by_keywords(page, ["去写章节"], timeout_ms=4000):
                return False
        page.wait_for_timeout(1800)
        return "/publish/" in page.url or "存草稿" in str(page.locator("body").inner_text())

    def _fanqie_entry_links(self, page, book_name: str) -> dict[str, str]:
        return page.evaluate(
            """([wantedBook]) => {
              const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
              const items = Array.from(document.querySelectorAll('.home-book-item, .home-book-item-home'));
              const matchItem = items.find((item) => {
                const text = textOf(item);
                return wantedBook ? text.includes(wantedBook) : true;
              }) || items[0] || null;
              const publishAnchor = matchItem
                ? Array.from(matchItem.querySelectorAll('a[href]')).find((node) => {
                    const href = String(node.getAttribute('href') || '');
                    return href.includes('/publish/');
                  })
                : null;
              const manageAnchor = matchItem
                ? Array.from(matchItem.querySelectorAll('a[href]')).find((node) => {
                    const href = String(node.getAttribute('href') || '');
                    return href.includes('/chapter-manage/');
                  })
                : null;
              return {
                book_text: matchItem ? textOf(matchItem) : '',
                publish_href: publishAnchor ? String(publishAnchor.getAttribute('href') || '') : '',
                chapter_manage_href: manageAnchor ? String(manageAnchor.getAttribute('href') || '') : '',
              };
            }""",
            [book_name],
        )

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
                  node.innerHTML = '';
                  node.innerText = text;
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
        except self._timeout_error:
            return False

    def _fill_fanqie_body(self, page, value: str) -> bool:
        editor = page.locator('.ProseMirror[contenteditable="true"]').first
        try:
            if editor.count() == 0:
                return False
            editor.evaluate(
                """(node, text) => {
                  const lines = String(text || '').split(/\\n+/);
                  const html = lines
                    .map((line) => line.trim())
                    .filter((line, index, all) => line || index === all.length - 1)
                    .map((line) => `<p>${line ? line.replace(/[&<>]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[char])) : '<br>'}</p>`)
                    .join('');
                  node.focus();
                  node.innerHTML = html || '<p><br></p>';
                  node.dispatchEvent(new Event('input', { bubbles: true }));
                  node.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
            page.wait_for_timeout(400)
            return True
        except self._timeout_error:
            return False

    def _dismiss_fanqie_modal(self, page) -> None:
        try:
            locator = page.locator(".byte-modal-close-icon").first
            if locator.count():
                locator.click(timeout=2000)
                page.wait_for_timeout(600)
        except self._timeout_error:
            return

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

    def _click_by_keywords(self, page, keywords: list[str], timeout_ms: int = 3000) -> bool:
        script = """
        ([keywords]) => {
          const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
          const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'));
          const target = nodes.find((node) => keywords.some((keyword) => textOf(node).includes(keyword)));
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
