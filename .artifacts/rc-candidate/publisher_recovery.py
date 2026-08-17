#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import base64
import html
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

ARTIFACT_DIR = Path(__file__).resolve().parent
REPO_ROOT = ARTIFACT_DIR.parents[1]
if str(ARTIFACT_DIR) not in sys.path:
    sys.path.insert(0, str(ARTIFACT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from recovery_runner_common import (  # noqa: E402
    CandidateIdentity,
    EvidenceWriter,
    RecoveryController,
    RunnerError,
    SetupBlocked,
    atomic_write_json_new,
    bind_recovery_endpoints,
    candidate_identity,
    http_json,
    normalize_database_url,
    psycopg_connect,
    require_client_endpoint,
    required_url,
    validate_fault_id,
)


SUPPORTED_FAULTS = (
    "publisher_backend_unavailable",
    "publisher_browser_unavailable",
    "publisher_captcha",
    "publisher_mfa",
    "publisher_account_risk",
)
RISK_REASONS = {
    "publisher_captcha": "captcha",
    "publisher_mfa": "mfa",
    "publisher_account_risk": "account_risk",
}
FIXTURE_PLATFORM = "qidian"
FIXTURE_BOOK_NAME = "Publisher Recovery Fixture"
FIXTURE_CHAPTER_TITLE = "Recovery Chapter"
FIXTURE_BODY = "Generic publisher recovery fixture content."
OPERATOR_REASON = "Task 6 deterministic publisher recovery proof."
RISK_BOUNDARY = "pre-mutation"
ENV_NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "api_key",
)


def candidate_body_hash(body: str) -> str:
    return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PublisherFixture:
    fixture_id: str
    fault_kind: str
    fault_id: str
    job_id: str
    logical_key: str
    task_kind: str
    platform_id: str
    project_id: str
    arc_plan_id: str
    chapter_plan_id: str
    draft_id: str
    review_id: str
    candidate_id: str
    canon_commit_id: str
    canon_idempotency_key: str
    chapter_number: int
    book_name: str
    chapter_title: str
    body: str
    body_sha256: str
    upload_url: str
    remote_book_id: str
    remote_chapter_id: str
    publish: bool
    result_payload: dict[str, Any]

    def evidence_identity(self) -> dict[str, str]:
        return {
            "fixture_id": self.fixture_id,
            "fault_id": self.fault_id,
            "resource_type": "publisher_job",
            "resource_id": self.job_id,
            "logical_key": self.logical_key,
            "project_id": self.project_id,
            "canon_commit_id": self.canon_commit_id,
            "candidate_id": self.candidate_id,
        }

    def with_changes(self, **changes: Any) -> PublisherFixture:
        return replace(self, **changes)


def _fixture_suffix(fault_id: str) -> str:
    return hashlib.sha256(fault_id.encode("ascii")).hexdigest()[:24]


def publisher_fixture(fault_kind: str, fault_id: str) -> PublisherFixture:
    if fault_kind not in SUPPORTED_FAULTS:
        raise RunnerError(f"unsupported Task 6 fault: {fault_kind}")
    normalized_fault_id = validate_fault_id(fault_id)
    suffix = _fixture_suffix(normalized_fault_id)
    numeric = str(int(hashlib.sha256(normalized_fault_id.encode("ascii")).hexdigest(), 16))
    remote_book_id = numeric[:12]
    remote_chapter_id = numeric[12:24]
    chapter_number = (int(numeric[24:30]) % 900) + 1
    body = f"{FIXTURE_BODY} Identity {suffix}."
    fixture = PublisherFixture(
        fixture_id=f"publisher-recovery-fixture-{suffix}",
        fault_kind=fault_kind,
        fault_id=normalized_fault_id,
        job_id="",
        logical_key="",
        task_kind="chapter_upload",
        platform_id=FIXTURE_PLATFORM,
        project_id=f"publisher-recovery-project-{suffix}",
        arc_plan_id=f"publisher-recovery-arc-{suffix}",
        chapter_plan_id=f"publisher-recovery-plan-{suffix}",
        draft_id=f"publisher-recovery-draft-{suffix}",
        review_id=f"publisher-recovery-review-{suffix}",
        candidate_id=f"publisher-recovery-candidate-{suffix}",
        canon_commit_id=f"publisher-recovery-commit-{suffix}",
        canon_idempotency_key=f"publisher-recovery-canon:{suffix}",
        chapter_number=chapter_number,
        book_name=f"{FIXTURE_BOOK_NAME} {suffix[:8]}",
        chapter_title=f"{FIXTURE_CHAPTER_TITLE} {suffix[:8]}",
        body=body,
        body_sha256=candidate_body_hash(body),
        upload_url=(
            "https://write.qq.com/booknovelsvip/chaptertmp/"
            f"CBID/{remote_book_id}#ccid={remote_chapter_id}"
        ),
        remote_book_id=remote_book_id,
        remote_chapter_id=remote_chapter_id,
        publish=True,
        result_payload={},
    )
    validate_fixture_spec(fixture)
    return fixture


def _sensitive_paths(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SENSITIVE_KEY_FRAGMENTS):
                found.append(path)
            found.extend(_sensitive_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_sensitive_paths(nested, f"{prefix}[{index}]"))
    return found


def validate_fixture_spec(fixture: PublisherFixture) -> None:
    if fixture.fault_kind not in SUPPORTED_FAULTS:
        raise SetupBlocked("publisher recovery fixture fault kind is unsupported")
    suffix = _fixture_suffix(validate_fault_id(fixture.fault_id))
    if fixture.fixture_id != f"publisher-recovery-fixture-{suffix}":
        raise SetupBlocked("publisher recovery fixture identity drifted")
    canon_ids = (
        fixture.project_id,
        fixture.arc_plan_id,
        fixture.chapter_plan_id,
        fixture.draft_id,
        fixture.review_id,
        fixture.candidate_id,
        fixture.canon_commit_id,
        fixture.canon_idempotency_key,
    )
    if not all(canon_ids) or len(set(canon_ids)) != len(canon_ids):
        raise SetupBlocked("publisher recovery Canon identity is incomplete")
    if not fixture.publish:
        raise SetupBlocked("publisher recovery fixture must publish")
    if fixture.task_kind != "chapter_upload":
        raise SetupBlocked("publisher recovery fixture task kind drifted")
    if (
        fixture.platform_id != FIXTURE_PLATFORM
        or not fixture.book_name.startswith(FIXTURE_BOOK_NAME)
        or not fixture.chapter_title.startswith(FIXTURE_CHAPTER_TITLE)
        or not fixture.body.startswith(FIXTURE_BODY)
    ):
        raise SetupBlocked("publisher recovery fixture generic content drifted")
    if fixture.chapter_number <= 0:
        raise SetupBlocked("publisher recovery chapter number is invalid")
    if fixture.body_sha256 != candidate_body_hash(fixture.body):
        raise SetupBlocked("publisher recovery fixture body hash drifted")
    if (
        not fixture.remote_book_id.isdigit()
        or not fixture.remote_chapter_id.isdigit()
        or fixture.remote_book_id not in fixture.upload_url
        or fixture.remote_chapter_id not in fixture.upload_url
        or not fixture.upload_url.startswith("https://write.qq.com/")
    ):
        raise SetupBlocked("publisher recovery remote identity drifted")
    sensitive = _sensitive_paths(fixture.result_payload)
    if sensitive:
        raise SetupBlocked(
            "publisher recovery fixture stores credential material: "
            + ", ".join(sensitive)
        )
    payload_text = json.dumps(fixture.result_payload, sort_keys=True).lower()
    if "receipt" in payload_text:
        raise SetupBlocked("publisher recovery fixture stores an external receipt")
    if not fixture.job_id:
        if fixture.logical_key or fixture.result_payload:
            raise SetupBlocked("unmaterialized publisher fixture stores job state")
    elif not fixture.logical_key:
        raise SetupBlocked("materialized publisher fixture has no logical key")


def publisher_page_html(
    fixture: PublisherFixture,
    *,
    risk_reason: str = "",
) -> str:
    validate_fixture_spec(fixture)
    markers = {
        "": "",
        "captcha": (
            '<div id="publisher-recovery-captcha">Human verification required</div>'
        ),
        "mfa": (
            '<label>Identity verification<input id="publisher-recovery-mfa" '
            'autocomplete="one-time-code"></label>'
        ),
        "account_risk": (
            '<div id="publisher-recovery-account_risk" data-risk-control>'
            "Account verification required</div>"
        ),
    }
    if risk_reason not in markers:
        raise SetupBlocked("unsupported publisher risk fixture")
    fixture_id = html.escape(fixture.fixture_id, quote=True)
    effect_key = json.dumps(f"forwin-recovery-effect:{fixture.fixture_id}")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Publisher recovery</title></head>
<body data-publisher-recovery-fixture="{fixture_id}">
  <nav>作品管理</nav>
  {markers[risk_reason]}
  <label>Chapter title<input id="inputTitle"></label>
  <label>Chapter body<textarea id="mce_0"></textarea></label>
  <div id="publisher-recovery-word-count">本章字数：1</div>
  <button id="publisher-recovery-publish" type="button">发布章节</button>
  <output id="publisher-recovery-result"></output>
  <script>
  (() => {{
    const textarea = document.querySelector('#mce_0');
    const wordCount = document.querySelector('#publisher-recovery-word-count');
    const plainText = (value) => {{
      const node = document.createElement('div');
      node.innerHTML = String(value || '');
      return String(node.innerText || node.textContent || '').trim();
    }};
    const editor = {{
      focus() {{}}, fire() {{}}, nodeChanged() {{}},
      setContent(value) {{
        textarea.value = plainText(value);
        wordCount.textContent = `本章字数：${{textarea.value.length}}`;
      }},
      getContent(options) {{
        return options && options.format === 'text'
          ? textarea.value
          : `<p>${{textarea.value}}</p>`;
      }},
      save() {{}},
    }};
    window.tinymce = {{ activeEditor: editor }};
    window.tinyMCE = window.tinymce;
    document.querySelector('#publisher-recovery-publish').addEventListener('click', () => {{
      const key = {effect_key};
      const count = Number.parseInt(localStorage.getItem(key) || '0', 10) + 1;
      localStorage.setItem(key, String(count));
      document.querySelector('#publisher-recovery-result').textContent = '发布成功';
    }});
  }})();
  </script>
</body></html>"""


def validate_detector_evidence(
    evidence: Mapping[str, Any],
    *,
    expected_reason: str,
) -> dict[str, str]:
    normalized = {
        key: str(evidence.get(key) or "").strip()
        for key in (
            "detector",
            "boundary",
            "selector",
            "matched_text",
            "risk_reason",
        )
    }
    if normalized["detector"] != "publisher-risk-v1":
        raise SetupBlocked("publisher pause did not use the production detector")
    if normalized["boundary"] != RISK_BOUNDARY:
        raise SetupBlocked("publisher risk was not detected at pre-mutation")
    if normalized["risk_reason"] != expected_reason:
        raise SetupBlocked("publisher typed risk reason drifted")
    if not normalized["selector"]:
        raise SetupBlocked("publisher detector selector is empty")
    if not normalized["matched_text"]:
        raise SetupBlocked("publisher detector matched text is empty")
    return normalized


def publisher_lifecycle_evidence(
    event: Mapping[str, Any] | None,
    *,
    expected_action: str,
    fault_id: str,
) -> dict[str, str]:
    value = dict(event or {})
    evidence = {
        "action": str(value.get("action") or ""),
        "service": str(value.get("service") or ""),
        "fault_id": str(value.get("fault_id") or ""),
    }
    if evidence != {
        "action": expected_action,
        "service": "publisher-browser",
        "fault_id": fault_id,
    }:
        raise SetupBlocked("publisher browser lifecycle evidence is incomplete")
    return evidence


class CanonPublisherProvisioner:
    def __init__(
        self,
        *,
        database_url: str = "",
        extension_key: str = "",
        runtime: Any | None = None,
        source_writer: Callable[[PublisherFixture], None] | None = None,
    ) -> None:
        self.engine: Any | None = None
        if runtime is None:
            from forwin.models.base import get_engine, get_session_factory
            from forwin.publisher_runtime.service import PublisherRuntimeService

            self.engine = get_engine(database_url)
            session_factory = get_session_factory(self.engine)
            runtime = PublisherRuntimeService(
                session_factory=session_factory,
                extension_api_key=str(extension_key or ""),
                heartbeat_stale_seconds=90,
                preferred_client_id="",
                publisher_session_secret="",
                publisher_session_encryption_required=False,
            )
        self.runtime = runtime
        self.source_writer = source_writer or self._write_canon_source

    def _write_canon_source(self, fixture: PublisherFixture) -> None:
        from forwin.models.canon import CanonCommitRecord
        from forwin.models.draft import (
            CandidateDraftRecord,
            ChapterDraft,
            ChapterReview,
        )
        from forwin.models.project import ArcPlanVersion, ChapterPlan, Project

        session_factory = self.runtime.session_factory
        with session_factory.begin() as session:
            existing = session.get(Project, fixture.project_id)
            if existing is not None:
                raise SetupBlocked(
                    "publisher recovery Canon project identity already exists"
                )
            session.add(
                Project(
                    id=fixture.project_id,
                    title=fixture.book_name,
                    premise="Generic isolated publisher recovery fixture.",
                    genre="systems",
                    automation_json=json.dumps(
                        {
                            "publish_bindings": [
                                {
                                    "platform": fixture.platform_id,
                                    "book_name": fixture.book_name,
                                    "upload_url": fixture.upload_url,
                                }
                            ]
                        },
                        sort_keys=True,
                    ),
                )
            )
            session.flush()
            session.add(
                ArcPlanVersion(
                    id=fixture.arc_plan_id,
                    project_id=fixture.project_id,
                    version=1,
                    arc_synopsis="Generic recovery arc.",
                    status="active",
                )
            )
            session.flush()
            session.add(
                ChapterPlan(
                    id=fixture.chapter_plan_id,
                    project_id=fixture.project_id,
                    arc_plan_id=fixture.arc_plan_id,
                    chapter_number=fixture.chapter_number,
                    title=fixture.chapter_title,
                    one_line="Exercise publisher recovery boundaries.",
                    goals_json="[]",
                    status="accepted",
                )
            )
            session.flush()
            session.add(
                ChapterDraft(
                    id=fixture.draft_id,
                    chapter_plan_id=fixture.chapter_plan_id,
                    version=1,
                    body_text=fixture.body,
                    summary="Generic publisher recovery chapter.",
                    char_count=len(fixture.body),
                )
            )
            session.flush()
            session.add(
                ChapterReview(
                    id=fixture.review_id,
                    draft_id=fixture.draft_id,
                    verdict="pass",
                    issues_json="[]",
                    review_meta_json=json.dumps(
                        {"verdict": "pass", "fixture": "publisher_recovery"},
                        sort_keys=True,
                    ),
                )
            )
            session.flush()
            session.add(
                CandidateDraftRecord(
                    id=fixture.candidate_id,
                    project_id=fixture.project_id,
                    chapter_plan_id=fixture.chapter_plan_id,
                    chapter_number=fixture.chapter_number,
                    candidate_draft_id=fixture.draft_id,
                    review_id=fixture.review_id,
                    body_hash=fixture.body_sha256,
                    plan_revision=f"recovery-plan:{fixture.fixture_id}",
                    policy_version=1,
                    status="accepted",
                    canon_status="committed",
                    canon_commit_id=fixture.canon_commit_id,
                    idempotency_key=fixture.canon_idempotency_key,
                )
            )
            session.flush()
            session.add(
                CanonCommitRecord(
                    id=fixture.canon_commit_id,
                    idempotency_key=fixture.canon_idempotency_key,
                    candidate_id=fixture.candidate_id,
                    project_id=fixture.project_id,
                    chapter_number=fixture.chapter_number,
                    status="committed",
                )
            )

    def materialize(self, fixture: PublisherFixture) -> PublisherFixture:
        validate_fixture_spec(fixture)
        if fixture.job_id or fixture.logical_key:
            raise SetupBlocked("publisher fixture was materialized more than once")
        self.source_writer(fixture)
        jobs = self.runtime.canon_jobs.materialize(
            canon_commit_id=fixture.canon_commit_id,
            canon_idempotency_key=fixture.canon_idempotency_key,
            project_id=fixture.project_id,
            chapter_number=fixture.chapter_number,
            candidate_id=fixture.candidate_id,
            chapter_title=fixture.chapter_title,
            body_sha256=fixture.body_sha256,
            bindings=[
                {
                    "platform": fixture.platform_id,
                    "book_name": fixture.book_name,
                    "upload_url": fixture.upload_url,
                    "create_if_missing": False,
                    "publisher_compliance_required": False,
                }
            ],
            publish=True,
        )
        if len(jobs) != 1 or not isinstance(jobs[0], Mapping):
            raise SetupBlocked(
                "Canon publisher materialization did not return one job"
            )
        job = dict(jobs[0])
        expected = {
            "task_kind": "chapter_upload",
            "project_id": fixture.project_id,
            "canon_commit_id": fixture.canon_commit_id,
            "candidate_id": fixture.candidate_id,
            "chapter_number": fixture.chapter_number,
            "body_sha256": fixture.body_sha256,
            "platform": fixture.platform_id,
            "status": "scheduled",
            "publish": True,
            "book_name": fixture.book_name,
            "chapter_title": fixture.chapter_title,
            "body": fixture.body,
            "upload_url": fixture.upload_url,
        }
        if any(job.get(key) != value for key, value in expected.items()):
            raise SetupBlocked("Canon publisher job identity drifted")
        job_id = str(job.get("job_id") or "")
        logical_key = str(job.get("idempotency_key") or "")
        payload = job.get("result_payload")
        if not job_id or not logical_key or not isinstance(payload, Mapping):
            raise SetupBlocked("Canon publisher job materialization is incomplete")
        materialized = fixture.with_changes(
            job_id=job_id,
            logical_key=logical_key,
            result_payload=dict(payload),
        )
        validate_fixture_spec(materialized)
        return materialized

    def release(self, fixture: PublisherFixture) -> PublisherFixture:
        validate_fixture_spec(fixture)
        if not fixture.job_id:
            raise SetupBlocked("publisher job cannot be released before materialization")
        released = self.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[fixture.job_id],
            publish=True,
            actor_type="recovery_evidence",
        )
        if (
            len(released) != 1
            or released[0].get("job_id") != fixture.job_id
            or released[0].get("status") != "pending"
            or released[0].get("publish") is not True
        ):
            raise SetupBlocked("Canon publisher job release did not become pending")
        return fixture

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None


class PublisherAPI:
    def __init__(
        self,
        *,
        api_url: str,
        operator_username: str,
        operator_password: str,
        transport: Callable[..., dict[str, Any]] = http_json,
    ) -> None:
        self.api_url = str(api_url).rstrip("/")
        self.operator_username = str(operator_username or "")
        self.operator_password = str(operator_password or "")
        self.transport = transport
        if not self.operator_username or not self.operator_password:
            raise SetupBlocked("publisher operator Basic credentials are empty")

    @property
    def operator_headers(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{self.operator_username}:{self.operator_password}".encode(
                "utf-8"
            )
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def resume_twice(
        self,
        fixture: PublisherFixture,
        *,
        pause_token: str,
        risk_reason: str,
    ) -> dict[str, Any]:
        expected_reason = RISK_REASONS.get(fixture.fault_kind)
        if risk_reason != expected_reason:
            raise SetupBlocked("publisher typed risk reason drifted before resume")
        request = {
            "expected_pause_reason": risk_reason,
            "expected_pause_token": str(pause_token),
            "operator_reason": OPERATOR_REASON,
        }
        url = (
            f"{self.api_url}/api/publishers/upload-jobs/"
            f"{fixture.job_id}/resume"
        )
        first = self.transport(
            "POST",
            url,
            json_body=request,
            headers=self.operator_headers,
        )
        replay = self.transport(
            "POST",
            url,
            json_body=request,
            headers=self.operator_headers,
        )
        first_transition = first.get("transition")
        replay_transition = replay.get("transition")
        if (
            first.get("ok") is not True
            or replay.get("ok") is not True
            or first.get("disposition") != "applied"
            or replay.get("disposition") != "idempotent"
            or not isinstance(first_transition, Mapping)
            or not isinstance(replay_transition, Mapping)
            or first.get("job", {}).get("job_id") != fixture.job_id
            or replay.get("job", {}).get("job_id") != fixture.job_id
            or dict(first_transition) != dict(replay_transition)
            or first_transition.get("pause_token") != pause_token
            or first_transition.get("pause_reason") != risk_reason
            or first_transition.get("auth_method")
            not in {"basic", "trusted_proxy"}
            or first_transition.get("attempt_phase") != "claimed"
            or first_transition.get("old_state") != "paused"
            or first_transition.get("new_state") != "pending"
        ):
            raise SetupBlocked(
                "authenticated publisher resume/replay response drifted"
            )
        return {
            "pause_token": str(pause_token),
            "pause_reason": risk_reason,
            "first_disposition": str(first["disposition"]),
            "replay_disposition": str(replay["disposition"]),
        }


class PublisherBrowserDriver:
    JOURNAL_KEY = "forwinPublisherUploadJournalV1"
    CLIENT_ID_KEY = "forwinPublisherClientId"
    HEARTBEAT_ALARM = "forwinPublisherHeartbeat"

    def __init__(
        self,
        *,
        cdp_url: str = "http://127.0.0.1:19322",
        timeout_seconds: float = 180.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cdp_url = str(cdp_url).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.sleep = sleep
        self.monotonic = monotonic
        self.playwright: Any | None = None
        self.browser: Any | None = None
        self.context: Any | None = None
        self.page: Any | None = None
        self.fixture: PublisherFixture | None = None
        self.risk_reason = ""
        self.route_installed = False

    def _connect(self) -> None:
        if self.context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright

            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.connect_over_cdp(
                self.cdp_url,
                timeout=int(self.timeout_seconds * 1000),
            )
        except Exception as exc:
            self.disconnect()
            raise SetupBlocked(
                f"publisher browser CDP connection failed: {exc}"
            ) from exc
        contexts = list(self.browser.contexts)
        if len(contexts) != 1:
            self.disconnect()
            raise SetupBlocked(
                "publisher browser must expose exactly one CDP context"
            )
        self.context = contexts[0]
        self.route_installed = False

    def _install_route(self) -> None:
        if self.context is None:
            raise SetupBlocked("publisher browser context is not connected")
        if self.route_installed:
            return

        def handle(route: Any) -> None:
            if self.fixture is None:
                route.abort()
                return
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=publisher_page_html(
                    self.fixture,
                    risk_reason=self.risk_reason,
                ),
            )

        self.context.route("https://write.qq.com/**", handle)
        self.route_installed = True

    def _worker(self) -> Any:
        if self.context is None:
            raise SetupBlocked("publisher browser context is not connected")
        deadline = self.monotonic() + self.timeout_seconds
        while self.monotonic() < deadline:
            workers = list(self.context.service_workers)
            matches = [
                worker
                for worker in workers
                if str(worker.url).endswith("/background.js")
            ]
            if len(matches) == 1:
                return matches[0]
            self.sleep(0.1)
        raise SetupBlocked("publisher extension service worker was not observed")

    def _storage_value(self, key: str) -> Any:
        return self._worker().evaluate(
            """async (key) => {
              const value = await chrome.storage.local.get(key);
              return value[key] ?? null;
            }""",
            key,
        )

    def prepare(
        self,
        fixture: PublisherFixture,
        *,
        risk_reason: str = "",
    ) -> dict[str, str]:
        validate_fixture_spec(fixture)
        if risk_reason and RISK_REASONS.get(fixture.fault_kind) != risk_reason:
            raise SetupBlocked("publisher browser risk fixture drifted")
        self.fixture = fixture
        self.risk_reason = risk_reason
        self._connect()
        self._install_route()
        assert self.context is not None
        self.page = self.context.new_page()
        self.page.goto(
            fixture.upload_url,
            wait_until="domcontentloaded",
            timeout=int(self.timeout_seconds * 1000),
        )
        self.page.wait_for_selector(
            f'[data-publisher-recovery-fixture="{fixture.fixture_id}"]',
            timeout=int(self.timeout_seconds * 1000),
        )
        journal = self._storage_value(self.JOURNAL_KEY)
        records = journal.get("records", []) if isinstance(journal, Mapping) else []
        pending = [
            item
            for item in records
            if isinstance(item, Mapping) and item.get("local_phase") != "acked"
        ]
        if pending:
            raise SetupBlocked(
                "publisher browser profile contains a pre-existing pending journal"
            )
        browser_id = str(self._storage_value(self.CLIENT_ID_KEY) or "")
        if not browser_id:
            raise SetupBlocked("publisher extension browser identity is empty")
        return {
            "browser_id": browser_id,
            "status": "healthy",
            "probe": "extension_service_worker_cdp",
        }

    @staticmethod
    def terminal_fault_expression() -> str:
        return r"""({ jobId, mode }) => {
          if (globalThis.__forwinRecoveryOriginalFetch) {
            throw new Error('publisher recovery terminal fault already installed');
          }
          const original = globalThis.fetch.bind(globalThis);
          const observation = {
            job_id: jobId,
            mode,
            installed_at: new Date().toISOString(),
            observed_at: '',
            request_url: '',
          };
          globalThis.__forwinRecoveryOriginalFetch = original;
          globalThis.__forwinRecoveryTerminalFault = observation;
          globalThis.fetch = async (input, init) => {
            const url = String(input && input.url ? input.url : input || '');
            const encoded = encodeURIComponent(jobId);
            const terminal = url.includes(`/upload-jobs/${encoded}/attempts/`)
              && /\/(receipt|result)$/.test(url);
            if (!terminal) {
              return original(input, init);
            }
            observation.observed_at = new Date().toISOString();
            observation.request_url = url;
            if (mode === 'backend_unavailable') {
              throw new TypeError('publisher recovery backend unavailable');
            }
            return await new Promise(() => {});
          };
          return { ...observation };
        }"""

    def install_terminal_fault(
        self,
        fixture: PublisherFixture,
        *,
        mode: str,
    ) -> dict[str, str]:
        if mode not in {"backend_unavailable", "browser_shutdown_barrier"}:
            raise SetupBlocked("unsupported publisher terminal fault mode")
        if fixture.job_id == "":
            raise SetupBlocked("publisher terminal fault job identity is empty")
        payload = self._worker().evaluate(
            self.terminal_fault_expression(),
            {"jobId": fixture.job_id, "mode": mode},
        )
        if not isinstance(payload, Mapping) or payload.get("job_id") != fixture.job_id:
            raise SetupBlocked("publisher terminal fault installation drifted")
        return {key: str(value or "") for key, value in payload.items()}

    def trigger_dispatch(self, fixture: PublisherFixture) -> None:
        if self.fixture is None or self.fixture.job_id != fixture.job_id:
            raise SetupBlocked("publisher browser fixture identity drifted")
        self._worker().evaluate(
            """(alarmName) => {
              chrome.alarms.create(alarmName, { when: Date.now() + 50 });
              return true;
            }""",
            self.HEARTBEAT_ALARM,
        )

    def _journal_records(self) -> list[dict[str, Any]]:
        journal = self._storage_value(self.JOURNAL_KEY)
        if not isinstance(journal, Mapping):
            return []
        records = journal.get("records")
        if not isinstance(records, list):
            raise SetupBlocked("publisher upload journal records are malformed")
        return [dict(item) for item in records if isinstance(item, Mapping)]

    def wait_terminal_journal(
        self,
        fixture: PublisherFixture,
    ) -> dict[str, str]:
        deadline = self.monotonic() + self.timeout_seconds
        last: list[dict[str, Any]] = []
        while self.monotonic() < deadline:
            last = [
                item
                for item in self._journal_records()
                if (item.get("job") or {}).get("job_id") == fixture.job_id
            ]
            ready = [
                item
                for item in last
                if item.get("local_phase") == "ack_pending"
                and isinstance(item.get("receipt"), Mapping)
                and isinstance(item.get("result"), Mapping)
            ]
            if len(last) == 1 and len(ready) == 1:
                item = ready[0]
                attempt = item.get("attempt") or {}
                receipt = item.get("receipt") or {}
                fault = self._worker().evaluate(
                    "() => ({ ...(globalThis.__forwinRecoveryTerminalFault || {}) })"
                )
                if not isinstance(fault, Mapping) or not fault.get("observed_at"):
                    raise SetupBlocked(
                        "publisher terminal fault boundary was not observed"
                    )
                return {
                    "job_id": fixture.job_id,
                    "attempt_id": str(attempt.get("attempt_id") or ""),
                    "journal_phase": "ack_pending",
                    "receipt_key": str(receipt.get("receipt_key") or ""),
                    "content_sha256": str(receipt.get("content_sha256") or ""),
                    "fault_observed_at": str(fault.get("observed_at") or ""),
                    "fault_request_url": str(fault.get("request_url") or ""),
                }
            self.sleep(0.1)
        raise SetupBlocked(
            "publisher terminal journal did not become ack_pending: "
            f"{last}"
        )

    def external_effect(self, fixture: PublisherFixture) -> dict[str, Any]:
        if self.context is None:
            raise SetupBlocked("publisher browser is not connected")
        key = f"forwin-recovery-effect:{fixture.fixture_id}"
        pages = [
            page
            for page in self.context.pages
            if str(page.url).startswith("https://write.qq.com/")
        ]
        if not pages:
            raise SetupBlocked("publisher recovery platform page is missing")
        raw = pages[0].evaluate(
            "(key) => localStorage.getItem(key) || '0'",
            key,
        )
        try:
            count = int(str(raw))
        except ValueError as exc:
            raise SetupBlocked("publisher upload effect counter is malformed") from exc
        return {
            "fixture_id": fixture.fixture_id,
            "effect_key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "upload_effect_count": count,
        }

    def restore_terminal_backend(self, fixture: PublisherFixture) -> None:
        payload = self._worker().evaluate(
            """(jobId) => {
              const fault = globalThis.__forwinRecoveryTerminalFault || {};
              if (fault.job_id !== jobId || !globalThis.__forwinRecoveryOriginalFetch) {
                throw new Error('publisher recovery terminal fault is not installed');
              }
              globalThis.fetch = globalThis.__forwinRecoveryOriginalFetch;
              delete globalThis.__forwinRecoveryOriginalFetch;
              delete globalThis.__forwinRecoveryTerminalFault;
              return { job_id: jobId, restored_at: new Date().toISOString() };
            }""",
            fixture.job_id,
        )
        if not isinstance(payload, Mapping) or payload.get("job_id") != fixture.job_id:
            raise SetupBlocked("publisher backend recovery restoration drifted")

    def clear_risk(self, fixture: PublisherFixture) -> None:
        if self.fixture is None or self.fixture.job_id != fixture.job_id:
            raise SetupBlocked("publisher risk fixture identity drifted")
        self.risk_reason = ""
        if self.context is None:
            raise SetupBlocked("publisher browser is not connected")
        for page in self.context.pages:
            if not str(page.url).startswith("https://write.qq.com/"):
                continue
            page.locator(
                "#publisher-recovery-captcha, #publisher-recovery-mfa, "
                "#publisher-recovery-account_risk"
            ).evaluate_all("nodes => nodes.forEach(node => node.remove())")

    def disconnect(self) -> None:
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.route_installed = False

    def reconnect(self, fixture: PublisherFixture) -> None:
        self.fixture = fixture
        self._connect()
        self._install_route()
        assert self.context is not None
        pages = [
            page
            for page in self.context.pages
            if str(page.url).startswith("https://write.qq.com/")
        ]
        if pages:
            self.page = pages[0]
        else:
            self.page = self.context.new_page()
            self.page.goto(
                fixture.upload_url,
                wait_until="domcontentloaded",
                timeout=int(self.timeout_seconds * 1000),
            )

    def close(self) -> None:
        self.disconnect()


class PsycopgDatabase:
    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[[str], Any] = psycopg_connect,
    ) -> None:
        self.database_url = normalize_database_url(database_url)
        self.connect = connect

    def query(
        self,
        statement: str,
        parameters: Any = (),
    ) -> list[dict[str, Any]]:
        with self.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return [dict(row) for row in cursor.fetchall()]

    def execute(
        self,
        statement: str,
        parameters: Any = (),
    ) -> int:
        with self.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return int(cursor.rowcount or 0)


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise SetupBlocked(
            "publisher result payload must be a valid JSON object"
        ) from exc
    if not isinstance(value, Mapping):
        raise SetupBlocked("publisher result payload must be a JSON object")
    return dict(value)


def _optional_json_object(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    return _json_object(raw)


def _unsafe_payload_paths(value: Any, prefix: str = "") -> list[str]:
    return sorted(set(_sensitive_paths(value, prefix)))


class SQLCollector:
    def __init__(
        self,
        database: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.sleep = sleep
        self.monotonic = monotonic
        self._bound_endpoint_identity: dict[str, Any] | None = None

    def bind_endpoint_identity(
        self,
        endpoint_identity: Mapping[str, Any],
    ) -> None:
        value = dict(endpoint_identity)
        if self._bound_endpoint_identity is not None:
            raise SetupBlocked("publisher endpoint identity was already bound")
        if not value:
            raise SetupBlocked("publisher endpoint identity is empty")
        self._bound_endpoint_identity = value

    def _endpoint_identity(self) -> dict[str, Any]:
        if self._bound_endpoint_identity is None:
            raise SetupBlocked(
                "publisher endpoint identity was not bound before snapshot"
            )
        return dict(self._bound_endpoint_identity)

    def read_recovery_sentinel(self) -> dict[str, str]:
        rows = self.database.query(
            """
            SELECT
                sentinel_id,
                run_id,
                fault_id,
                source_sha
            FROM forwin_recovery_run_sentinel
            WHERE singleton = true
            """,
        )
        if len(rows) != 1:
            raise SetupBlocked(
                "database endpoint returned no unique recovery sentinel"
            )
        return {
            "table": "forwin_recovery_run_sentinel",
            **{
                key: str(rows[0].get(key) or "")
                for key in (
                    "sentinel_id",
                    "run_id",
                    "fault_id",
                    "source_sha",
                )
            },
        }

    def _job_rows(self, fixture: PublisherFixture) -> list[dict[str, Any]]:
        return self.database.query(
            """
            SELECT
                id AS job_id,
                idempotency_key AS logical_key,
                task_kind,
                project_id,
                canon_commit_id,
                candidate_id,
                chapter_number,
                platform_id,
                status,
                publish,
                book_name,
                chapter_title,
                body_text,
                body_sha256,
                upload_url,
                abort_requested,
                extension_client_id AS owner_token,
                extension_client_id,
                current_attempt_id,
                available_at,
                reconcile_after,
                claimed_at,
                started_at,
                finished_at,
                deleted_at,
                paused_at,
                pause_reason,
                current_url,
                result_message,
                error_message,
                result_payload_json,
                created_at,
                updated_at,
                CURRENT_TIMESTAMP AS database_now
            FROM publisher_upload_jobs
            WHERE id = %s OR idempotency_key = %s
            ORDER BY id
            """,
            (fixture.job_id, fixture.logical_key),
        )

    def _exact_row(self, fixture: PublisherFixture) -> dict[str, Any]:
        rows = self._job_rows(fixture)
        if len(rows) != 1:
            raise SetupBlocked(
                "publisher fixture identity has duplicate or missing natural keys"
            )
        row = rows[0]
        if (
            str(row.get("job_id") or "") != fixture.job_id
            or str(row.get("logical_key") or "") != fixture.logical_key
        ):
            raise SetupBlocked("publisher fixture job identity drifted")
        return row

    def wait_status(
        self,
        fixture: PublisherFixture,
        expected: str,
        *,
        timeout_seconds: float = 300.0,
    ) -> None:
        deadline = self.monotonic() + timeout_seconds
        last = ""
        while self.monotonic() < deadline:
            last = str(self._exact_row(fixture).get("status") or "")
            if last == expected:
                return
            if last in {"failed", "cancelled"} and last != expected:
                break
            self.sleep(0.5)
        raise SetupBlocked(
            f"publisher job did not reach {expected}; observed {last or 'empty'}"
        )

    def wait_retryable_attempt(
        self,
        fixture: PublisherFixture,
        attempt_id: str,
        *,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        deadline = self.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while self.monotonic() < deadline:
            job = self._canonical_job(fixture)
            attempts = self._attempts(fixture)
            receipts = self._receipts(fixture)
            matches = [
                item for item in attempts if item["attempt_id"] == attempt_id
            ]
            last = {
                "job": job,
                "attempts": attempts,
                "receipts": receipts,
            }
            if (
                job["status"] == "running"
                and job["current_attempt_id"] == attempt_id
                and len(matches) == 1
                and matches[0]["status"] == "running"
                and matches[0]["phase"] == "mutation_started"
                and not receipts
            ):
                return last
            self.sleep(0.1)
        raise SetupBlocked(
            "publisher job was not retryable at the terminal boundary: "
            f"{last}"
        )

    def wait_risk_pause(
        self,
        fixture: PublisherFixture,
        expected_reason: str,
        *,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        deadline = self.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while self.monotonic() < deadline:
            job = self._canonical_job(fixture)
            attempts = self._attempts(fixture)
            evidence = self._detector_evidence(fixture)
            last = {"job": job, "attempts": attempts, "evidence": evidence}
            if (
                job["status"] == "paused"
                and job["pause_reason"] == expected_reason
                and len(attempts) == 1
                and attempts[0]["status"] == "paused"
                and attempts[0]["phase"] == "claimed"
            ):
                evidence["risk_reason"] = expected_reason
                validate_detector_evidence(
                    evidence,
                    expected_reason=expected_reason,
                )
                return {
                    "pause_token": attempts[0]["attempt_id"],
                    "risk_reason": expected_reason,
                    "detector_evidence": evidence,
                }
            self.sleep(0.1)
        raise SetupBlocked(
            "publisher browser detector did not create a typed pause: "
            f"{last}"
        )

    def _attempt_rows(self, fixture: PublisherFixture) -> list[dict[str, Any]]:
        return self.database.query(
            """
            SELECT
                id AS attempt_id,
                upload_job_id AS job_id,
                attempt_number,
                attempt_kind,
                worker_id AS owner_token,
                lease_epoch,
                status,
                phase,
                content_sha256,
                error_code,
                result_json
            FROM publisher_upload_attempts
            WHERE upload_job_id = %s
            ORDER BY attempt_number, id
            """,
            (fixture.job_id,),
        )

    def _attempts(self, fixture: PublisherFixture) -> list[dict[str, Any]]:
        return [
            {
                key: (
                    int(row.get(key) or 0)
                    if key in {"attempt_number", "lease_epoch"}
                    else str(row.get(key) or "")
                )
                for key in (
                    "attempt_id",
                    "job_id",
                    "attempt_number",
                    "attempt_kind",
                    "owner_token",
                    "lease_epoch",
                    "status",
                    "phase",
                    "content_sha256",
                    "error_code",
                )
            }
            for row in self._attempt_rows(fixture)
        ]

    def _receipts(self, fixture: PublisherFixture) -> list[dict[str, str]]:
        rows = self.database.query(
            """
            SELECT
                id AS receipt_id,
                upload_job_id AS job_id,
                upload_attempt_id AS attempt_id,
                receipt_key AS natural_key,
                idempotency_key,
                platform_id,
                remote_book_id,
                remote_chapter_id,
                remote_url,
                official_state,
                content_sha256,
                source
            FROM publisher_upload_receipts
            WHERE upload_job_id = %s
            ORDER BY receipt_key, id
            """,
            (fixture.job_id,),
        )
        return [
            {
                key: str(row.get(key) or "")
                for key in (
                    "receipt_id",
                    "job_id",
                    "attempt_id",
                    "natural_key",
                    "idempotency_key",
                    "platform_id",
                    "remote_book_id",
                    "remote_chapter_id",
                    "remote_url",
                    "official_state",
                    "content_sha256",
                    "source",
                )
            }
            for row in rows
        ]

    def _detector_evidence(
        self,
        fixture: PublisherFixture,
    ) -> dict[str, str]:
        for row in self._attempt_rows(fixture):
            result = _optional_json_object(row.get("result_json"))
            evidence = result.get("evidence")
            if not isinstance(evidence, Mapping):
                continue
            normalized = {
                key: str(evidence.get(key) or "").strip()
                for key in (
                    "detector",
                    "boundary",
                    "selector",
                    "matched_text",
                    "message",
                )
            }
            normalized.update(
                risk_reason=str(result.get("risk_reason") or "").strip(),
                observed_at=str(
                    result.get("client_observed_at")
                    or result.get("paused_at")
                    or ""
                ).strip(),
                attempt_id=str(row.get("attempt_id") or ""),
            )
            return normalized
        return {}

    def _canon_source(self, fixture: PublisherFixture) -> dict[str, Any]:
        rows = self.database.query(
            """
            SELECT
                project.id AS project_id,
                chapter.id AS chapter_plan_id,
                draft.id AS draft_id,
                candidate.id AS candidate_id,
                commit.id AS canon_commit_id,
                commit.idempotency_key AS canon_idempotency_key,
                commit.status AS canon_status,
                candidate.status AS candidate_status,
                candidate.canon_status AS candidate_canon_status,
                chapter.status AS chapter_status,
                chapter.chapter_number,
                draft.body_text,
                candidate.body_hash
            FROM projects AS project
            JOIN chapter_plans AS chapter
              ON chapter.project_id = project.id
            JOIN chapter_drafts AS draft
              ON draft.chapter_plan_id = chapter.id
            JOIN candidate_draft_records AS candidate
              ON candidate.project_id = project.id
             AND candidate.chapter_plan_id = chapter.id
             AND candidate.candidate_draft_id = draft.id
            JOIN canon_commit_records AS commit
              ON commit.id = candidate.canon_commit_id
             AND commit.candidate_id = candidate.id
            WHERE project.id = %s
              AND chapter.id = %s
              AND draft.id = %s
              AND candidate.id = %s
              AND commit.id = %s
            """,
            (
                fixture.project_id,
                fixture.chapter_plan_id,
                fixture.draft_id,
                fixture.candidate_id,
                fixture.canon_commit_id,
            ),
        )
        if len(rows) != 1:
            raise SetupBlocked("publisher Canon source join is missing or ambiguous")
        row = rows[0]
        expected = {
            "project_id": fixture.project_id,
            "chapter_plan_id": fixture.chapter_plan_id,
            "draft_id": fixture.draft_id,
            "candidate_id": fixture.candidate_id,
            "canon_commit_id": fixture.canon_commit_id,
            "canon_idempotency_key": fixture.canon_idempotency_key,
            "canon_status": "committed",
            "candidate_status": "accepted",
            "candidate_canon_status": "committed",
            "chapter_status": "accepted",
            "chapter_number": fixture.chapter_number,
            "body_text": fixture.body,
            "body_hash": fixture.body_sha256,
        }
        if any(row.get(key) != value for key, value in expected.items()):
            raise SetupBlocked("publisher Canon source identity drifted")
        return {
            key: value
            for key, value in expected.items()
            if key != "body_text"
        }

    @staticmethod
    def _timestamp(value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError as exc:
                raise SetupBlocked(
                    "publisher job timestamp is malformed"
                ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()

    def _canonical_job(self, fixture: PublisherFixture) -> dict[str, Any]:
        row = self._exact_row(fixture)
        payload = _json_object(row.get("result_payload_json"))
        pause = payload.get("risk_pause")
        resume = payload.get("risk_resume")
        pause = pause if isinstance(pause, Mapping) else {}
        resume = resume if isinstance(resume, Mapping) else {}
        boundary = ""
        evidence = pause.get("evidence")
        if isinstance(evidence, Mapping):
            boundary = str(evidence.get("boundary") or "")
        if not boundary:
            for attempt in self._attempt_rows(fixture):
                result = _optional_json_object(attempt.get("result_json"))
                attempt_evidence = result.get("evidence")
                if isinstance(attempt_evidence, Mapping):
                    boundary = str(attempt_evidence.get("boundary") or "")
                    if boundary:
                        break
        return {
            "job_id": str(row.get("job_id") or ""),
            "logical_key": str(row.get("logical_key") or ""),
            "task_kind": str(row.get("task_kind") or ""),
            "project_id": str(row.get("project_id") or ""),
            "platform_id": str(row.get("platform_id") or ""),
            "status": str(row.get("status") or ""),
            "publish": bool(row.get("publish")),
            "book_name": str(row.get("book_name") or ""),
            "chapter_title": str(row.get("chapter_title") or ""),
            "body_sha256": str(row.get("body_sha256") or ""),
            "unsafe_payload_paths": _unsafe_payload_paths(payload),
            "canon_commit_id": str(row.get("canon_commit_id") or ""),
            "candidate_id": str(row.get("candidate_id") or ""),
            "chapter_number": int(row.get("chapter_number") or 0),
            "body_text": str(row.get("body_text") or ""),
            "upload_url": str(row.get("upload_url") or ""),
            "abort_requested": bool(row.get("abort_requested")),
            "owner_token": str(row.get("owner_token") or ""),
            "extension_client_id": str(
                row.get("extension_client_id") or ""
            ),
            "current_attempt_id": str(
                row.get("current_attempt_id") or ""
            ),
            "available_at": self._timestamp(row.get("available_at")),
            "reconcile_after": self._timestamp(row.get("reconcile_after")),
            "claimed_at": self._timestamp(row.get("claimed_at")),
            "started_at": self._timestamp(row.get("started_at")),
            "finished_at": self._timestamp(row.get("finished_at")),
            "deleted_at": self._timestamp(row.get("deleted_at")),
            "paused_at": self._timestamp(row.get("paused_at")),
            "pause_reason": str(row.get("pause_reason") or ""),
            "pause_token": str(
                pause.get("pause_token")
                or resume.get("pause_token")
                or ""
            ),
            "risk_boundary": boundary,
            "current_url": str(row.get("current_url") or ""),
            "result_message": str(row.get("result_message") or ""),
            "error_message": str(row.get("error_message") or ""),
            "result_payload": payload,
            "created_at": self._timestamp(row.get("created_at")),
            "updated_at": self._timestamp(row.get("updated_at")),
            "database_now": self._timestamp(row.get("database_now")),
        }

    def _resume_actions(self, fixture: PublisherFixture) -> list[dict[str, str]]:
        rows = self.database.query(
            """
            SELECT
                id AS action_id,
                upload_job_id AS job_id,
                action,
                pause_token,
                actor_id,
                auth_method,
                reason,
                old_state_json,
                new_state_json
            FROM publisher_operator_actions
            WHERE upload_job_id = %s
              AND action = 'resume'
            ORDER BY created_at, id
            """,
            (fixture.job_id,),
        )
        actions: list[dict[str, str]] = []
        for row in rows:
            old_state = _optional_json_object(row.get("old_state_json"))
            new_state = _optional_json_object(row.get("new_state_json"))
            pause_token = str(row.get("pause_token") or "")
            actions.append(
                {
                    "action_id": str(row.get("action_id") or ""),
                    "job_id": str(row.get("job_id") or ""),
                    "natural_key": (
                        f"{row.get('job_id')}:resume:{pause_token}"
                    ),
                    "action": str(row.get("action") or ""),
                    "pause_token": pause_token,
                    "actor_id": str(row.get("actor_id") or ""),
                    "auth_method": str(row.get("auth_method") or ""),
                    "reason_sha256": hashlib.sha256(
                        str(row.get("reason") or "").encode("utf-8")
                    ).hexdigest(),
                    "old_status": str(old_state.get("status") or ""),
                    "new_status": str(new_state.get("status") or ""),
                }
            )
        return actions

    def recovery_snapshot(
        self,
        *,
        stage: str,
        fixture: PublisherFixture,
        external: Mapping[str, Any],
    ) -> dict[str, Any]:
        if stage not in {"before", "during", "after"}:
            raise SetupBlocked("publisher recovery snapshot stage is invalid")
        validate_fixture_spec(fixture)
        job = self._canonical_job(fixture)
        if (
            job["task_kind"] != "chapter_upload"
            or job["project_id"] != fixture.project_id
            or job["canon_commit_id"] != fixture.canon_commit_id
            or job["candidate_id"] != fixture.candidate_id
            or job["chapter_number"] != fixture.chapter_number
            or job["publish"] is not True
            or job["body_sha256"] != fixture.body_sha256
            or job["upload_url"] != fixture.upload_url
        ):
            raise SetupBlocked("publisher recovery job is not the exact Canon upload")
        return {
            "schema_version": 2,
            "fault_kind": fixture.fault_kind,
            "fault_id": fixture.fault_id,
            "stage": stage,
            "state": {
                "target": {
                    "fixture": fixture.evidence_identity(),
                    "endpoint_identity": self._endpoint_identity(),
                },
                "mcp": {},
                "api": {},
                "database": {
                    "canon_source": self._canon_source(fixture),
                    "job": job,
                    "job_identity_count": len(self._job_rows(fixture)),
                    "status": job["status"],
                    "attempts": self._attempts(fixture),
                    "receipts": self._receipts(fixture),
                    "resume_actions": self._resume_actions(fixture),
                    "detector_evidence": self._detector_evidence(fixture),
                },
                "external": dict(external),
                "barrier": {},
            },
        }


def _failure_text(error: BaseException) -> str:
    return (
        " ".join(
            (str(error) or type(error).__name__)
            .replace("\x00", " ")
            .split()
        )[:512]
        or "Task 6 setup blocked"
    )


class PublisherRecoveryFlow:
    def __init__(
        self,
        *,
        fixture: PublisherFixture,
        source_sha: str,
        provisioner: Any,
        browser: Any,
        sql_collector: Any,
        api: Any,
        controller: Any,
    ) -> None:
        validate_fixture_spec(fixture)
        self.fixture = fixture
        self.source_sha = str(source_sha)
        self.provisioner = provisioner
        self.browser = browser
        self.sql = sql_collector
        self.api = api
        self.controller = controller

    @staticmethod
    def _effect(external: Mapping[str, Any], expected: int) -> None:
        observed = int(external.get("upload_effect_count") or 0)
        if observed != expected:
            raise SetupBlocked(
                "publisher external upload effect count drifted: "
                f"expected {expected}, observed {observed}"
            )

    def _snapshot(
        self,
        stage: str,
        external: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = self.sql.recovery_snapshot(
            stage=stage,
            fixture=self.fixture,
            external=dict(external),
        )
        snapshot.setdefault("schema_version", 2)
        snapshot.setdefault("source_sha", self.source_sha)
        snapshot.setdefault("fault_kind", self.fixture.fault_kind)
        snapshot.setdefault("fault_id", self.fixture.fault_id)
        return snapshot

    def run(self) -> dict[str, dict[str, Any]]:
        self.fixture = self.provisioner.materialize(self.fixture)
        validate_fixture_spec(self.fixture)
        risk_reason = RISK_REASONS.get(self.fixture.fault_kind, "")
        browser_identity = self.browser.prepare(
            self.fixture,
            risk_reason=risk_reason,
        )
        self.provisioner.release(self.fixture)
        before_external = {
            "browser": dict(browser_identity),
            **self.browser.external_effect(self.fixture),
        }
        before = self._snapshot("before", before_external)
        if self.fixture.fault_kind == "publisher_backend_unavailable":
            during, after = self._backend()
        elif self.fixture.fault_kind == "publisher_browser_unavailable":
            during, after = self._browser_outage()
        else:
            during, after = self._risk(risk_reason)
        return {"before": before, "during": during, "after": after}

    def _backend(self) -> tuple[dict[str, Any], dict[str, Any]]:
        terminal_fault = self.browser.install_terminal_fault(
            self.fixture,
            mode="backend_unavailable",
        )
        self.browser.trigger_dispatch(self.fixture)
        journal = self.browser.wait_terminal_journal(self.fixture)
        attempt_id = str(journal.get("attempt_id") or "")
        if not attempt_id:
            raise SetupBlocked("publisher terminal journal has no attempt identity")
        self.sql.wait_retryable_attempt(self.fixture, attempt_id)
        during_external = {
            "terminal_fault": dict(terminal_fault),
            "journal": dict(journal),
            **self.browser.external_effect(self.fixture),
        }
        self._effect(during_external, 1)
        during = self._snapshot("during", during_external)
        self.controller.mark(
            self.fixture.fault_kind,
            "fault",
            self.fixture.fault_id,
        )
        self.browser.restore_terminal_backend(self.fixture)
        self.browser.trigger_dispatch(self.fixture)
        self.sql.wait_status(self.fixture, "succeeded")
        after_external = {
            "journal_replay": {"attempt_id": attempt_id},
            **self.browser.external_effect(self.fixture),
        }
        self._effect(after_external, 1)
        after = self._snapshot("after", after_external)
        self.controller.mark(
            self.fixture.fault_kind,
            "recovery",
            self.fixture.fault_id,
        )
        return during, after

    def _browser_outage(self) -> tuple[dict[str, Any], dict[str, Any]]:
        terminal_fault = self.browser.install_terminal_fault(
            self.fixture,
            mode="browser_shutdown_barrier",
        )
        self.browser.trigger_dispatch(self.fixture)
        journal = self.browser.wait_terminal_journal(self.fixture)
        attempt_id = str(journal.get("attempt_id") or "")
        if not attempt_id:
            raise SetupBlocked("publisher browser journal has no attempt identity")
        self.sql.wait_retryable_attempt(self.fixture, attempt_id)
        effect = self.browser.external_effect(self.fixture)
        self._effect(effect, 1)
        self.browser.disconnect()
        stopped = self.controller.stop("publisher-browser", self.fixture.fault_id)
        during_external = {
            "terminal_fault": dict(terminal_fault),
            "journal": dict(journal),
            "browser_fault": publisher_lifecycle_evidence(
                stopped,
                expected_action="fault_service_stopped",
                fault_id=self.fixture.fault_id,
            ),
            **effect,
        }
        during = self._snapshot("during", during_external)
        started = self.controller.start("publisher-browser", self.fixture.fault_id)
        self.browser.reconnect(self.fixture)
        self.browser.trigger_dispatch(self.fixture)
        self.sql.wait_status(self.fixture, "succeeded")
        after_external = {
            "browser_recovery": publisher_lifecycle_evidence(
                started,
                expected_action="fault_service_recovered",
                fault_id=self.fixture.fault_id,
            ),
            "journal_replay": {"attempt_id": attempt_id},
            **self.browser.external_effect(self.fixture),
        }
        self._effect(after_external, 1)
        return during, self._snapshot("after", after_external)

    def _risk(
        self,
        risk_reason: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not risk_reason:
            raise SetupBlocked("publisher risk flow has no typed reason")
        self.browser.trigger_dispatch(self.fixture)
        pause = self.sql.wait_risk_pause(self.fixture, risk_reason)
        evidence = dict(pause.get("detector_evidence") or {})
        evidence["risk_reason"] = risk_reason
        validate_detector_evidence(evidence, expected_reason=risk_reason)
        during_external = {
            "detector_evidence": evidence,
            **self.browser.external_effect(self.fixture),
        }
        self._effect(during_external, 0)
        during = self._snapshot("during", during_external)
        self.controller.mark(
            self.fixture.fault_kind,
            "fault",
            self.fixture.fault_id,
        )
        self.browser.clear_risk(self.fixture)
        replay = self.api.resume_twice(
            self.fixture,
            pause_token=str(pause.get("pause_token") or ""),
            risk_reason=risk_reason,
        )
        self.browser.trigger_dispatch(self.fixture)
        self.sql.wait_status(self.fixture, "succeeded")
        after_external = {
            "operator_resume": dict(replay),
            **self.browser.external_effect(self.fixture),
        }
        self._effect(after_external, 1)
        after = self._snapshot("after", after_external)
        self.controller.mark(
            self.fixture.fault_kind,
            "recovery",
            self.fixture.fault_id,
        )
        return during, after


@dataclass(frozen=True, slots=True)
class LiveRunResult:
    status: str
    report_path: Path


class LiveRunner:
    def __init__(
        self,
        *,
        fault_kind: str,
        fault_id: str,
        source_sha: str,
        database_url: str,
        mcp_url: str,
        api_url: str,
        controller: Any,
        sql_collector: SQLCollector,
        api: PublisherAPI,
        writer: Any,
        provisioner: Any | None = None,
        browser: Any | None = None,
    ) -> None:
        if fault_kind not in SUPPORTED_FAULTS:
            raise RunnerError(f"unsupported Task 6 fault: {fault_kind}")
        self.fault_kind = fault_kind
        self.fault_id = validate_fault_id(fault_id)
        self.source_sha = source_sha
        self.database_url = database_url
        self.mcp_url = mcp_url
        self.api_url = api_url
        self.controller = controller
        self.sql = sql_collector
        self.api = api
        self.writer = writer
        self.provisioner = provisioner
        self.browser = browser
        self.fixture = publisher_fixture(fault_kind, fault_id)
        self.stage = "initial"
        self.stack_started = False
    def run(self) -> LiveRunResult:
        snapshots: dict[str, dict[str, Any]] | None = None
        failure: BaseException | None = None
        cleanup_errors: list[str] = []
        try:
            self.stage = "fresh_up"
            self.stack_started = True
            self.controller.fresh_up(self.fault_id)
            self.stage = "endpoint_binding"
            require_client_endpoint(
                self.api,
                attribute="api_url",
                expected_url=self.api_url,
                label="Publisher API",
            )
            endpoint_identity = bind_recovery_endpoints(
                controller=self.controller,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                api_url=self.api_url,
                mcp_url=self.mcp_url,
                database_url=self.database_url,
                sentinel_reader=self.sql.read_recovery_sentinel,
            )
            self.sql.bind_endpoint_identity(endpoint_identity)
            if self.provisioner is None or self.browser is None:
                raise SetupBlocked(
                    "publisher Canon provisioner or browser driver is missing"
                )
            self.stage = "publisher_recovery_flow"
            flow = PublisherRecoveryFlow(
                fixture=self.fixture,
                source_sha=self.source_sha,
                provisioner=self.provisioner,
                browser=self.browser,
                sql_collector=self.sql,
                api=self.api,
                controller=self.controller,
            )
            snapshots = flow.run()
            self.fixture = flow.fixture
        except BaseException as exc:
            failure = exc
        for label, resource in (
            ("publisher browser", self.browser),
            ("Canon provisioner", self.provisioner),
        ):
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except BaseException as exc:
                cleanup_errors.append(f"{label} cleanup: {exc}")
                if failure is None or not isinstance(exc, Exception):
                    failure = exc
        if failure is not None and not isinstance(failure, Exception):
            if self.stack_started:
                try:
                    self.controller.interrupt_cleanup(self.fault_id)
                except BaseException as exc:
                    cleanup_errors.append(f"interrupt cleanup: {exc}")
            raise failure.with_traceback(failure.__traceback__)
        if failure is not None:
            if self.stack_started:
                try:
                    self.controller.abort(
                        self.fault_id,
                        self.stage,
                        _failure_text(failure),
                    )
                except BaseException as exc:
                    cleanup_errors.append(f"controller abort: {exc}")
                    if not isinstance(exc, Exception):
                        try:
                            self.controller.interrupt_cleanup(self.fault_id)
                        except BaseException as cleanup_exc:
                            cleanup_errors.append(
                                f"interrupt cleanup: {cleanup_exc}"
                            )
                        raise exc.with_traceback(exc.__traceback__)
            report = self.writer.write_setup_blocked(
                fault_kind=self.fault_kind,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                failure_stage=self.stage,
                failure_reason=_failure_text(failure),
                cleanup_errors=cleanup_errors,
            )
            return LiveRunResult("setup_blocked", report)
        if snapshots is None:
            raise RunnerError("Task 6 runner produced no snapshots")
        self.stage = "terminal_destroy"
        try:
            self.controller.destroy()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                try:
                    self.controller.interrupt_cleanup(self.fault_id)
                except BaseException:
                    pass
                raise
            cleanup_errors.append(f"publisher stack destroy: {exc}")
            try:
                self.controller.abort(
                    self.fault_id,
                    self.stage,
                    _failure_text(exc),
                )
            except BaseException as abort_exc:
                cleanup_errors.append(f"controller abort: {abort_exc}")
            report = self.writer.write_setup_blocked(
                fault_kind=self.fault_kind,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                failure_stage=self.stage,
                failure_reason=_failure_text(exc),
                cleanup_errors=cleanup_errors,
            )
            return LiveRunResult("setup_blocked", report)
        self.stage = "evidence_write"
        report = self.writer.write_pass_report(
            fault_kind=self.fault_kind,
            fault_id=self.fault_id,
            source_sha=self.source_sha,
            snapshots=snapshots,
            event_log_path=self.controller.event_log_path,
            supplemental_artifacts={},
        )
        return LiveRunResult("pass", report)

@dataclass(frozen=True, slots=True)
class RunConfig:
    fault_kind: str
    fault_id: str
    candidate_manifest: Path
    source_sha: str
    mcp_url: str
    api_url: str
    database_url: str
    evidence_dir: Path
    extension_key: str
    operator_username: str
    operator_password: str


def _required_environment(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name) or "").strip()
    if not value:
        raise RunnerError(f"required environment is empty: {name}")
    return value


def resolve_run_config(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] = os.environ,
) -> RunConfig:
    fault_kind = str(args.fault_kind or "")
    if fault_kind not in SUPPORTED_FAULTS:
        raise RunnerError(f"unsupported Task 6 fault: {fault_kind}")
    fault_id = validate_fault_id(args.fault_id)
    identity = candidate_identity(Path(args.candidate_manifest))
    database_env = str(args.database_url_env or "")
    if ENV_NAME_PATTERN.fullmatch(database_env) is None:
        raise RunnerError("database URL environment name is invalid")
    database_url = normalize_database_url(
        _required_environment(environ, database_env)
    )
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise RunnerError("database URL must use PostgreSQL")
    evidence_dir = Path(args.evidence_dir).expanduser().resolve()
    if evidence_dir.exists() and any(evidence_dir.iterdir()):
        raise RunnerError(
            f"evidence directory is not empty; use a new directory: {evidence_dir}"
        )
    return RunConfig(
        fault_kind=fault_kind,
        fault_id=fault_id,
        candidate_manifest=identity.manifest_path,
        source_sha=identity.source_sha,
        mcp_url=required_url(args.mcp_url, "MCP URL"),
        api_url=required_url(args.api_url, "API URL"),
        database_url=database_url,
        evidence_dir=evidence_dir,
        extension_key=_required_environment(
            environ,
            "FORWIN_PUBLISHER_EXTENSION_API_KEY",
        ),
        operator_username=_required_environment(
            environ,
            "FORWIN_HTTP_BASIC_USER",
        ),
        operator_password=_required_environment(
            environ,
            "FORWIN_HTTP_BASIC_PASSWORD",
        ),
    )


def build_live_runner(config: RunConfig) -> LiveRunner:
    controller = RecoveryController(
        candidate_manifest=config.candidate_manifest,
        evidence_dir=config.evidence_dir,
    )
    collector = SQLCollector(PsycopgDatabase(config.database_url))
    return LiveRunner(
        fault_kind=config.fault_kind,
        fault_id=config.fault_id,
        source_sha=config.source_sha,
        database_url=config.database_url,
        mcp_url=config.mcp_url,
        api_url=config.api_url,
        controller=controller,
        sql_collector=collector,
        api=PublisherAPI(
            api_url=config.api_url,
            operator_username=config.operator_username,
            operator_password=config.operator_password,
        ),
        writer=EvidenceWriter(
            evidence_dir=config.evidence_dir,
            runner_path=Path(__file__),
        ),
        provisioner=CanonPublisherProvisioner(
            database_url=config.database_url,
            extension_key=config.extension_key,
        ),
        browser=PublisherBrowserDriver(),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run fault-local publisher recovery proofs."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument(
        "--fault-kind",
        choices=SUPPORTED_FAULTS,
        required=True,
    )
    run_parser.add_argument("--fault-id", required=True)
    run_parser.add_argument("--candidate-manifest", type=Path, required=True)
    run_parser.add_argument("--mcp-url", required=True)
    run_parser.add_argument("--api-url", required=True)
    run_parser.add_argument("--database-url-env", required=True)
    run_parser.add_argument("--evidence-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command != "run":
        raise RunnerError(f"unsupported command: {args.command}")
    result = build_live_runner(resolve_run_config(args)).run()
    print(result.report_path)
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
