"""Task 11: the /publishers page shows publish-hub's state on the rows publish-hub owns.

Two halves. The upload-job payload the page fetches carries the hub columns (and the
serializer keeps the legacy action flags off hub-owned rows). The page script, run in node
against a fake DOM, renders the hub status, the link to publish-hub's /ops page (only when
FORWIN_PUBLISH_HUB_OPS_URL is set), the waiting and superseded reasons, and no legacy
action buttons on a hub-owned row, while a legacy row reads as it always did.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from forwin.api_pages import render_publishers_page
from forwin.api_schema import PublisherUploadJobResponse
from forwin.http.adapters.api_system_routes import build_handlers
from forwin.models.publisher import PublisherUploadJob
from tests.test_canon_publisher_jobs import _fixture, _materialize

OPS_URL = "https://hub.example.com/ops"
HUB_JOB = "5b1f0c2e-0000-4000-8000-0000000000aa"
SUBMIT_BODY = {"action": "publish_chapter", "destination_id": "d-1"}


# --- the payload the page fetches -------------------------------------------------------


def _shown(fixture, job_id: str) -> dict:
    return fixture.runtime.upload_jobs.get_upload_job(job_id)


def _edit(fixture, job_id: str, **columns) -> None:
    with fixture.runtime.session_factory.begin() as session:
        job = session.get(PublisherUploadJob, job_id)
        for name, value in columns.items():
            setattr(job, name, value)


def test_a_legacy_row_carries_empty_hub_fields_and_keeps_its_actions() -> None:
    fixture = _fixture("publishers-page-hub-legacy-row")
    try:
        [created] = _materialize(fixture)
        _edit(
            fixture,
            created["job_id"],
            status="paused",
            pause_reason="captcha",
            result_payload_json=json.dumps({"risk_pause": {"pause_token": "tok-1"}}),
        )

        shown = _shown(fixture, created["job_id"])

        assert shown["hub_owned"] is False
        assert (shown["hub_job_id"], shown["hub_status"], shown["hub_superseded_reason"]) == (
            "",
            "",
            "",
        )
        assert shown["resumable"] is True and shown["terminable"] is True
    finally:
        fixture.engine.dispose()


def test_a_row_posted_to_the_hub_carries_its_job_id_and_status_and_no_legacy_actions() -> None:
    fixture = _fixture("publishers-page-hub-posted-row")
    try:
        [created] = _materialize(fixture)
        _edit(
            fixture,
            created["job_id"],
            status="paused",
            pause_reason="captcha",
            result_payload_json=json.dumps({"risk_pause": {"pause_token": "tok-1"}}),
            hub_submit_body_json=SUBMIT_BODY,
            hub_job_id=HUB_JOB,
            hub_status="paused",
        )

        shown = _shown(fixture, created["job_id"])

        assert shown["hub_owned"] is True
        assert shown["hub_job_id"] == HUB_JOB and shown["hub_status"] == "paused"
        assert shown["resumable"] is False
        assert shown["terminable"] is False
    finally:
        fixture.engine.dispose()


def test_a_stored_request_not_posted_yet_is_hub_owned_without_a_hub_job() -> None:
    fixture = _fixture("publishers-page-hub-stored-row")
    try:
        [created] = _materialize(fixture)
        _edit(
            fixture,
            created["job_id"],
            status="pending",
            hub_submit_body_json=SUBMIT_BODY,
            result_message="请求已保存，等第 6 章在平台上完成后再提交到 publish-hub。",
        )

        shown = _shown(fixture, created["job_id"])

        assert shown["hub_owned"] is True
        assert shown["hub_job_id"] == "" and shown["hub_status"] == ""
        assert shown["message"].startswith("请求已保存")
        assert shown["resumable"] is False and shown["terminable"] is False
    finally:
        fixture.engine.dispose()


def test_a_create_book_row_is_hub_owned_before_anything_is_stored() -> None:
    fixture = _fixture("publishers-page-hub-create-book")
    try:
        [created] = _materialize(fixture)
        _edit(fixture, created["job_id"], task_kind="create_book")

        shown = _shown(fixture, created["job_id"])

        assert shown["task_kind"] == "create_book"
        assert shown["hub_owned"] is True and shown["hub_job_id"] == ""
        assert shown["resumable"] is False and shown["terminable"] is False
    finally:
        fixture.engine.dispose()


def test_a_superseded_row_carries_the_reason_and_still_deletes_by_todays_rules() -> None:
    fixture = _fixture("publishers-page-hub-superseded-row")
    try:
        [created] = _materialize(fixture)
        _edit(
            fixture,
            created["job_id"],
            status="superseded",
            hub_submit_body_json=SUBMIT_BODY,
            hub_job_id=HUB_JOB,
            hub_status="succeeded",
            hub_superseded_reason="publication_already_published",
        )

        shown = _shown(fixture, created["job_id"])

        assert shown["status"] == "superseded"
        assert shown["hub_superseded_reason"] == "publication_already_published"
        assert shown["deletable"] is True
        fixture.runtime.upload_jobs.delete_upload_job(created["job_id"])
    finally:
        fixture.engine.dispose()


def test_the_response_model_keeps_the_hub_fields() -> None:
    fixture = _fixture("publishers-page-hub-response-model")
    try:
        [created] = _materialize(fixture)
        _edit(
            fixture,
            created["job_id"],
            hub_submit_body_json=SUBMIT_BODY,
            hub_job_id=HUB_JOB,
            hub_status="running",
        )

        response = PublisherUploadJobResponse(**_shown(fixture, created["job_id"]))

        dumped = response.model_dump(mode="json")
        assert dumped["hub_owned"] is True
        assert (dumped["hub_job_id"], dumped["hub_status"]) == (HUB_JOB, "running")
        assert dumped["hub_superseded_reason"] == ""
        assert PublisherUploadJobResponse(
            **{**dumped, "hub_owned": False, "hub_job_id": ""}
        ).hub_owned is False
    finally:
        fixture.engine.dispose()


# --- the page: where the ops link comes from ---------------------------------------------


def _page(ops_url=None) -> str:
    kwargs = {} if ops_url is None else {"publish_hub_ops_url": ops_url}
    return render_publishers_page(
        backend_ready={"extension_api_key_configured": True},
        extension_install_path="browser_extension/forwin-publisher",
        **kwargs,
    )


def test_the_page_embeds_the_configured_ops_url() -> None:
    assert f"const PUBLISH_HUB_OPS_URL = {json.dumps(OPS_URL)};" in _page(OPS_URL)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_the_page_has_no_ops_url_by_default(value) -> None:
    assert 'const PUBLISH_HUB_OPS_URL = "";' in _page(value)


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "//hub.example.com/ops",
        "hub.example.com/ops",
        "https://",
        "http://[bad",
    ],
)
def test_the_page_drops_an_ops_url_that_is_not_an_absolute_http_address(value) -> None:
    html = _page(value)

    assert 'const PUBLISH_HUB_OPS_URL = "";' in html
    assert "javascript:alert" not in html


def test_the_ops_url_cannot_close_the_script_element() -> None:
    html = _page("https://hub.example.com/ops?next=</script><script>alert(1)</script>")

    assert "</script><script>alert" not in html
    assert html.count("<script>") == 1 and html.count("</script>") == 1
    assert "\\u003c/script\\u003e" in html


def test_a_value_that_looks_like_a_placeholder_is_not_expanded() -> None:
    html = _page("https://hub.example.com/ops?a=@@EXTENSION_INSTALL_PATH@@&b={PAGE_DOM_HELPERS_JS}")

    line = next(line for line in html.splitlines() if "const PUBLISH_HUB_OPS_URL" in line)
    assert "@@EXTENSION_INSTALL_PATH@@" in line and "{PAGE_DOM_HELPERS_JS}" in line


def _handlers(config, rendered: list):
    return build_handlers(
        get_config=lambda: config,
        get_publisher_manager=lambda: None,
        get_session=lambda: None,
        render_home_page=lambda **_kwargs: "",
        render_publishers_page=lambda **kwargs: rendered.append(kwargs) or "",
    )


def test_the_publishers_route_reads_the_ops_url_from_the_config() -> None:
    rendered: list[dict] = []
    config = SimpleNamespace(publish_hub_ops_url=OPS_URL)

    _handlers(config, rendered)["publishers_page"]()

    assert rendered[0]["publish_hub_ops_url"] == OPS_URL


def test_the_publishers_route_passes_nothing_but_the_ops_url_from_the_hub_settings() -> None:
    rendered: list[dict] = []
    config = SimpleNamespace(
        publish_hub_ops_url=OPS_URL,
        publish_hub_url="http://host.docker.internal:18930",
        publish_hub_api_key="hub-api-key-that-must-stay-server-side",
    )

    _handlers(config, rendered)["publishers_page"]()

    assert "hub-api-key-that-must-stay-server-side" not in repr(rendered)
    assert "host.docker.internal" not in repr(rendered)


def test_the_real_page_never_contains_the_hub_key_or_its_internal_address() -> None:
    config = SimpleNamespace(
        publish_hub_ops_url=OPS_URL,
        publish_hub_url="http://host.docker.internal:18930",
        publish_hub_api_key="hub-api-key-that-must-stay-server-side",
    )
    handlers = build_handlers(
        get_config=lambda: config,
        get_publisher_manager=lambda: None,
        get_session=lambda: None,
        render_home_page=lambda **_kwargs: "",
        render_publishers_page=render_publishers_page,
    )

    html = handlers["publishers_page"]().body.decode("utf-8")

    assert OPS_URL in html
    assert "hub-api-key-that-must-stay-server-side" not in html
    assert "host.docker.internal" not in html


def test_the_publishers_route_without_a_configured_url_asks_for_no_link() -> None:
    rendered: list[dict] = []

    _handlers(SimpleNamespace(), rendered)["publishers_page"]()
    _handlers(None, rendered)["publishers_page"]()

    assert [call["publish_hub_ops_url"] for call in rendered] == ["", ""]


# --- the page script, run in node ---------------------------------------------------------

NODE = shutil.which("node")

HARNESS = r"""
class FakeElement {
  constructor(tag) {
    this.tag = tag; this.children = []; this.className = ''; this.textContent = '';
    this.listeners = {}; this.href = undefined; this.target = undefined; this.rel = undefined;
  }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, listener) { (this.listeners[name] = this.listeners[name] || []).push(listener); }
  replaceChildren(...nodes) { this.children = nodes; }
  set innerHTML(_value) { throw new Error('the page must not assign innerHTML'); }
  set outerHTML(_value) { throw new Error('the page must not assign outerHTML'); }
  insertAdjacentHTML() { throw new Error('the page must not insert HTML'); }
}
const elements = {};
const document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => { const node = new FakeElement('#text'); node.textContent = text; return node; },
  getElementById: (id) => (elements[id] = elements[id] || new FakeElement('div')),
};
const window = {
  location: { origin: 'http://forwin.test' },
  addEventListener() {}, setTimeout, clearTimeout, postMessage() {},
  crypto: { randomUUID: () => 'id' },
};
function dump(el) {
  if (el.tag === '#text') return { text: el.textContent };
  return {
    tag: el.tag, cls: el.className, text: el.textContent,
    href: el.href, target: el.target, rel: el.rel, children: el.children.map(dump),
  };
}
"""


def _page_script(html: str) -> str:
    """The page's own bridge and upload scripts, as rendered (after the i18n/topbar scripts,
    before the bootstrap that needs the whole DOM)."""
    script = html[html.index("<script>") : html.rindex("</script>")]
    return script[script.index("const EXTENSION_BRIDGE_CHANNEL") : script.index("async function boot()")]


def _run(items: list[dict], *, ops_url: str = OPS_URL, script_edit=None) -> dict:
    if NODE is None:
        pytest.skip("node is required to run the publishers page script")
    script = _page_script(_page(ops_url))
    if script_edit is not None:
        script = script_edit(script)
    program = (
        HARNESS
        + script
        + "\nconst busy = renderUploadJobs("
        + json.dumps(items)
        + ");\nconsole.log(JSON.stringify({busy,"
        " status: dump(document.getElementById('upload_jobs_status')),"
        " list: dump(document.getElementById('upload_jobs_list'))}));\n"
    )
    result = subprocess.run(
        [NODE, "-"], input=program, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _row(**overrides) -> dict:
    row = {
        "task_kind": "chapter_upload",
        "job_id": "job-1",
        "platform": "fanqie",
        "display_name": "番茄小说",
        "status": "pending",
        "book_name": "雾港",
        "chapter_title": "第三章",
        "message": "",
        "error": "",
        "result_payload": {},
        "extension_client_id": "",
        "created_at": "2026-10-31T10:00:00",
        "current_url": "",
        "resumable": False,
        "terminable": False,
        "deletable": False,
        "hub_owned": False,
        "hub_job_id": "",
        "hub_status": "",
        "hub_superseded_reason": "",
    }
    row.update(overrides)
    return row


def _hub_row(**overrides) -> dict:
    return _row(
        **{"hub_owned": True, "hub_job_id": HUB_JOB, "hub_status": "running", **overrides}
    )


def _walk(node):
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


def _texts(node) -> list[str]:
    return [item["text"] for item in _walk(node) if item.get("text")]


def _cards(rendered) -> list[dict]:
    return rendered["list"]["children"]


def _buttons(card) -> list[str]:
    return [item["text"] for item in _walk(card) if item.get("tag") == "button"]


def _links(card) -> list[dict]:
    return [item for item in _walk(card) if item.get("tag") == "a"]


def _card_text(card) -> str:
    return "\n".join(_texts(card))


def test_a_hub_row_shows_the_hub_status_and_a_link_to_the_ops_page() -> None:
    [card] = _cards(_run([_hub_row()]))

    text = _card_text(card)
    assert "publish-hub 状态：running" in text
    assert HUB_JOB in text
    [link] = _links(card)
    assert link["href"] == OPS_URL
    assert link["target"] == "_blank" and "noreferrer" in link["rel"]


def test_a_hub_row_whose_status_has_not_synced_yet_says_so() -> None:
    [card] = _cards(_run([_hub_row(hub_status="")]))

    assert "publish-hub 状态：尚未同步" in _card_text(card)


def test_without_a_configured_ops_url_the_hub_status_shows_and_no_link_is_drawn() -> None:
    [card] = _cards(_run([_hub_row(hub_status="succeeded")], ops_url=""))

    assert "publish-hub 状态：succeeded" in _card_text(card)
    assert _links(card) == []


def test_the_page_script_refuses_an_ops_url_that_is_not_http_even_if_one_got_in() -> None:
    def swap(script: str) -> str:
        assert f"const PUBLISH_HUB_OPS_URL = {json.dumps(OPS_URL)};" in script
        return script.replace(json.dumps(OPS_URL), json.dumps("javascript:alert(1)"))

    [card] = _cards(_run([_hub_row()], script_edit=swap))

    assert _links(card) == []


def test_the_link_comes_from_the_config_never_from_the_row() -> None:
    row = _hub_row(hub_status="https://evil.example/ops", current_url="")

    [card] = _cards(_run([row]))

    assert [link["href"] for link in _links(card)] == [OPS_URL]


def test_a_hub_owned_row_has_no_resume_button_even_if_the_row_says_it_is_resumable() -> None:
    row = _hub_row(
        status="paused",
        resumable=True,
        pause_reason="captcha",
        pause_token="tok",
        terminable=True,
    )

    [card] = _cards(_run([row]))

    assert _buttons(card) == []


@pytest.mark.parametrize(
    "reason,label",
    [
        ("needs_human", "需要人工处理"),
        ("account_risk", "账号风险"),
        ("captcha", "CAPTCHA / 人机验证"),
        ("mfa", "MFA / 二次验证"),
    ],
)
def test_a_paused_hub_row_names_its_pause_reason(reason, label) -> None:
    """Every pause reason the sync mirrors from publish-hub has a label of its own."""
    row = _hub_row(status="paused", hub_status="paused", pause_reason=reason)

    [card] = _cards(_run([row]))

    assert f"暂停类型：{label}" in _card_text(card)


def test_a_stored_request_not_posted_yet_hides_resume_and_shows_the_waiting_reason() -> None:
    reason = "请求已保存，等第 2 章在平台上完成后再提交到 publish-hub（同一本书的章节按顺序发出）。"
    row = _row(
        hub_owned=True,
        status="paused",
        resumable=True,
        pause_reason="captcha",
        pause_token="tok",
        message=reason,
    )

    [card] = _cards(_run([row]))

    text = _card_text(card)
    assert _buttons(card) == []
    assert f"等待原因：{reason}" in text
    assert f"说明：{reason}" not in text
    assert _links(card) == [], "a request publish-hub has not seen has no job to look at there"
    assert "publish-hub：尚未提交" in text


def test_a_create_book_row_waiting_for_the_hub_shows_its_reason_and_no_actions() -> None:
    row = _row(
        task_kind="create_book",
        hub_owned=True,
        status="pending",
        message="新书的发布目标由运维创建，等待中。",
        resumable=True,
    )

    [card] = _cards(_run([row]))

    assert _buttons(card) == []
    assert "等待原因：新书的发布目标由运维创建，等待中。" in _card_text(card)
    assert "任务类型：create_book" in _card_text(card)


def test_a_chapter_held_by_the_release_gate_shows_why_it_is_waiting() -> None:
    reason = "publish-hub 账号因风控暂停（paused_risk），任务继续保持排程状态。"
    row = _row(
        status="scheduled",
        message=reason,
        result_payload={"hub_hold": {"result": "wait_account", "cause": "account_paused"}},
    )

    [card] = _cards(_run([row]))

    text = _card_text(card)
    assert f"等待原因：{reason}" in text
    assert "执行端：等待分配" not in text


def test_a_hub_row_already_at_the_hub_keeps_its_message_as_a_note() -> None:
    [card] = _cards(_run([_hub_row(message="publish-hub 已接收。")]))

    text = _card_text(card)
    assert "说明：publish-hub 已接收。" in text
    assert "等待原因" not in text


def test_a_superseded_row_shows_why() -> None:
    row = _hub_row(
        status="superseded",
        hub_status="succeeded",
        hub_superseded_reason="publication_already_published",
    )

    [card] = _cards(_run([row]))

    text = _card_text(card)
    assert "被取代原因：publication_already_published" in text
    assert "publish-hub 状态：succeeded" in text


def test_a_superseded_row_without_a_recorded_reason_says_so() -> None:
    [card] = _cards(_run([_hub_row(status="superseded", hub_superseded_reason="")]))

    assert "被取代原因：未记录" in _card_text(card)


def test_a_superseded_row_without_a_hub_job_shows_its_result_not_a_wait() -> None:
    # `_supersede` records no hub job: a chapter found already published never went out.
    row = _row(
        hub_owned=True,
        status="superseded",
        hub_superseded_reason="publication_already_published",
        message="该章节已在平台上发布，本任务被取代。",
    )

    [card] = _cards(_run([row]))

    text = _card_text(card)
    assert "被取代原因：publication_already_published" in text
    assert "说明：该章节已在平台上发布，本任务被取代。" in text
    assert "等待原因" not in text and "尚未提交" not in text
    assert _links(card) == [] and _buttons(card) == []


def test_a_failed_row_without_a_hub_job_shows_the_failure_not_a_wait() -> None:
    row = _row(
        hub_owned=True,
        status="failed",
        message="publish-hub 拒绝了这个请求。",
        error="publication_conflict",
    )

    [card] = _cards(_run([row]))

    text = _card_text(card)
    assert "说明：publish-hub 拒绝了这个请求。" in text
    assert "错误：publication_conflict" in text
    assert "等待原因" not in text and "尚未提交" not in text
    assert "执行端" not in text


@pytest.mark.parametrize("status", ["succeeded", "cancelled", "failed", "superseded"])
def test_no_terminal_row_is_labelled_as_waiting(status) -> None:
    held = {"hub_hold": {"result": "wait_destination", "cause": "no_destination"}}

    cards = _cards(
        _run(
            [
                _row(hub_owned=True, status=status, message="结果。"),
                _row(job_id="job-2", status=status, message="结果。", result_payload=held),
            ]
        )
    )

    for card in cards:
        text = _card_text(card)
        assert "等待原因" not in text and "尚未提交" not in text
        assert "说明：结果。" in text


@pytest.mark.parametrize("status", ["scheduled", "pending", "paused"])
def test_a_row_waits_only_in_the_statuses_that_can_wait(status) -> None:
    [card] = _cards(_run([_row(hub_owned=True, status=status, message="等第 2 章。")]))

    text = _card_text(card)
    assert "等待原因：等第 2 章。" in text and "publish-hub：尚未提交" in text
    assert "说明：等第 2 章。" not in text


def test_a_hub_row_does_not_claim_to_wait_for_an_extension_client() -> None:
    [card] = _cards(_run([_hub_row()]))

    assert "执行端" not in _card_text(card)


def test_a_legacy_row_reads_as_it_did_before() -> None:
    row = _row(
        status="paused",
        resumable=True,
        pause_reason="captcha",
        pause_token="tok",
        message="等待操作员处理验证码。",
        current_url="https://fanqie.example/page",
    )

    [card] = _cards(_run([row]))

    text = _card_text(card)
    assert "番茄小说 | paused | 雾港 / 第三章" in text
    assert "执行端：等待分配" in text
    assert "说明：等待操作员处理验证码。" in text
    assert "暂停类型：CAPTCHA / 人机验证" in text
    assert _buttons(card) == ["恢复任务"]
    assert [link["href"] for link in _links(card)] == ["https://fanqie.example/page"]
    for hub_only in ("publish-hub", "等待原因", "被取代原因"):
        assert hub_only not in text


def test_a_row_publish_hub_never_saw_shows_no_hub_lines() -> None:
    [card] = _cards(_run([_row(status="pending", message="排队中。")]))

    assert "publish-hub" not in _card_text(card)


def test_every_hub_value_is_rendered_as_text() -> None:
    attack = '<img src=x onerror="alert(1)">'
    row = _hub_row(
        status="superseded",
        hub_status=attack,
        hub_superseded_reason=attack,
        message=attack,
        book_name=attack,
    )
    waiting = _row(hub_owned=True, message=attack)

    rendered = _run([row, waiting])

    tags = {item.get("tag") for card in _cards(rendered) for item in _walk(card)}
    assert "img" not in tags
    assert any(attack in text for card in _cards(rendered) for text in _texts(card))


def test_the_list_status_line_is_unchanged_by_hub_rows() -> None:
    rendered = _run([_hub_row(), _hub_row(job_id="job-2", status="paused")])

    assert rendered["busy"] is True
    assert "仍在执行或排队" in rendered["status"]["text"]
