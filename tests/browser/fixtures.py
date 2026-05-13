from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Route, expect


GENESIS_STAGES = ["brief", "world", "map", "story_engine", "book_blueprint", "bootstrap"]


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_reply(route: Route, payload: Any, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )


def api_error(route: Route, detail: str, status: int = 500) -> None:
    json_reply(route, {"detail": detail}, status=status)


def read_json(route: Route) -> dict[str, Any]:
    body = route.request.post_data or "{}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def install_extension_bridge(page: Page, *, backend_url_matches: bool = True) -> None:
    flag = "true" if backend_url_matches else "false"
    page.add_init_script(
        script="""
        (() => {
          const backendUrlMatches = __BACKEND_URL_MATCHES__;
          window.addEventListener('message', (event) => {
            const data = event.data || {};
            if (event.source !== window || data.channel !== 'forwin-publisher-extension') return;
            if (data.direction !== 'page-to-extension' || data.kind !== 'request') return;
            const payload = data.action === 'ping'
              ? {
                  browserName: 'chromium-test',
                  extensionVersion: 'e2e',
                  backendBaseUrl: backendUrlMatches ? window.location.origin : 'http://127.0.0.1:9999',
                }
              : { message: `${data.action} handled by browser test bridge` };
            window.postMessage({
              channel: 'forwin-publisher-extension',
              direction: 'extension-to-page',
              kind: 'response',
              correlationId: data.correlationId,
              ok: true,
              payload,
            }, window.location.origin);
          });
        })();
        """.replace("__BACKEND_URL_MATCHES__", flag),
    )


@dataclass
class MockForWinBackend:
    projects: list[dict[str, Any]] = field(default_factory=list)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    upload_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    captured: list[dict[str, Any]] = field(default_factory=list)
    fail: dict[str, tuple[int, str]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    platforms: list[dict[str, Any]] = field(default_factory=list)
    genesis: dict[str, dict[str, Any]] = field(default_factory=dict)
    world_pages: list[dict[str, Any]] = field(default_factory=list)
    world_snapshots: list[dict[str, Any]] = field(default_factory=list)
    world_conflicts: list[dict[str, Any]] = field(default_factory=list)
    world_proposals: list[dict[str, Any]] = field(default_factory=list)
    personality_skills: list[dict[str, Any]] = field(default_factory=list)
    character_personalities: list[dict[str, Any]] = field(default_factory=list)
    personality_coverage: dict[str, Any] = field(default_factory=dict)
    personality_metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.settings = self.settings or sample_settings()
        self.platforms = self.platforms or sample_platforms()
        self.projects = self.projects or [sample_project()]
        self.tasks = self.tasks or {"task-1": sample_generation_task("task-1", project_id=self.projects[0]["id"])}
        self.upload_jobs = self.upload_jobs or {"upload-1": sample_upload_job("upload-1")}
        self.genesis = self.genesis or {self.projects[0]["id"]: sample_genesis_detail(self.projects[0]["id"])}
        self.world_pages = self.world_pages or sample_world_pages(self.projects[0]["id"])
        self.world_snapshots = self.world_snapshots or sample_world_snapshots(self.projects[0]["id"])
        self.world_conflicts = self.world_conflicts or sample_world_conflicts()
        self.world_proposals = self.world_proposals or sample_world_proposals()
        self.personality_skills = self.personality_skills or sample_personality_skills()
        self.character_personalities = self.character_personalities or sample_character_personalities()
        self.personality_coverage = self.personality_coverage or sample_personality_coverage(self.projects[0]["id"], self.character_personalities)
        self.personality_metrics = self.personality_metrics or sample_personality_metrics(self.projects[0]["id"])

    def install(self, page: Page) -> None:
        page.route("**/api/**", self.handle)

    def capture(self, route: Route, payload: Any | None = None) -> None:
        parsed = urlparse(route.request.url)
        self.captured.append(
            {
                "method": route.request.method,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "payload": payload if payload is not None else read_json(route),
            }
        )

    def captured_payloads(self, path: str, method: str = "POST") -> list[dict[str, Any]]:
        return [
            item["payload"]
            for item in self.captured
            if item["path"] == path and item["method"] == method
        ]

    def project_ref(self, project_id: str) -> dict[str, Any]:
        project = next((item for item in self.projects if item["id"] == project_id), None)
        if project is None:
            project = sample_project(project_id)
            self.projects.append(project)
        return project

    def mark_generation_started(self, project_id: str, task_id: str, action: str) -> None:
        project = self.project_ref(project_id)
        chapters = project.setdefault(
            "chapters",
            [
                sample_chapter(1),
                sample_chapter(2, status="needs_review", has_review=True),
                sample_chapter(3, status="planned"),
            ],
        )
        if action == "start-writing":
            project["creation_status"] = "writing"
        project["chapter_count"] = len(chapters)
        project["generated_chapter_count"] = len(
            [item for item in chapters if item.get("status") in {"accepted", "drafted", "needs_review"}]
        )
        project["accepted_chapter_count"] = len(
            [item for item in chapters if item.get("status") == "accepted"]
        )
        project["generation_control"] = sample_generation_control(can_resume=True)
        self.tasks[task_id] = sample_generation_task(task_id, project_id=project_id)

    def handle(self, route: Route) -> None:
        parsed = urlparse(route.request.url)
        path = parsed.path
        method = route.request.method
        if path in self.fail:
            status, message = self.fail.pop(path)
            api_error(route, message, status)
            return

        if path == "/api/settings/llm" and method == "GET":
            json_reply(route, self.settings)
            return
        if path == "/api/settings/codex/health":
            json_reply(route, {"enabled": False, "healthy": False, "status": "disabled", "bridge_url": "", "message": "disabled"})
            return
        if path == "/api/settings/llm/preferences" and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            self.settings.update(payload)
            self.settings["message"] = "运行偏好已保存"
            json_reply(route, self.settings)
            return
        if path == "/api/settings/llm/profiles" and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            profile_id = payload.get("profile_id") or f"profile-{len(self.settings['profiles']) + 1}"
            profile = {
                "id": profile_id,
                "name": payload.get("name") or "测试模型",
                "model": payload.get("model") or "test-model",
                "base_url": payload.get("base_url") or "https://example.invalid/v1",
                "has_api_key": bool(payload.get("api_key")),
            }
            self.settings["profiles"] = [item for item in self.settings["profiles"] if item["id"] != profile_id] + [profile]
            if payload.get("set_as_default"):
                self.settings["default_profile_id"] = profile_id
            json_reply(route, {**self.settings, "message": "模型配置已保存"})
            return
        if path == "/api/settings/llm/default-profile" and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            self.settings["default_profile_id"] = payload.get("profile_id")
            json_reply(route, {**self.settings, "message": "默认模型已切换"})
            return
        profile_delete = re.fullmatch(r"/api/settings/llm/profiles/([^/]+)", path)
        if profile_delete and method == "DELETE":
            self.capture(route, {})
            profile_id = profile_delete.group(1)
            self.settings["profiles"] = [item for item in self.settings["profiles"] if item["id"] != profile_id]
            json_reply(route, {**self.settings, "message": "模型配置已删除"})
            return

        if path == "/api/personality-skills" and method == "GET":
            json_reply(route, {"skills": self.personality_skills})
            return
        character_personality_list = re.fullmatch(r"/api/projects/([^/]+)/book-state/characters/personality", path)
        if character_personality_list and method == "GET":
            project_id = character_personality_list.group(1)
            json_reply(
                route,
                {
                    "schema_version": "book_state.character_personality.v1",
                    "project_id": project_id,
                    "as_of_chapter": 0,
                    "characters": self.character_personalities,
                },
            )
            return
        character_personality_loadout = re.fullmatch(r"/api/projects/([^/]+)/book-state/characters/([^/]+)/personality-loadout", path)
        if character_personality_loadout and method in {"GET", "PUT"}:
            project_id, character_id = character_personality_loadout.groups()
            character = self._character_personality(character_id)
            if method == "PUT":
                payload = read_json(route)
                self.capture(route, payload)
                character["personality_loadout"] = payload.get("personality_loadout") or character.get("personality_loadout") or {}
            json_reply(route, {"schema_version": "book_state.character_personality.v1", "project_id": project_id, **character})
            return
        personality_coverage = re.fullmatch(r"/api/projects/([^/]+)/characters/personality/coverage", path)
        if personality_coverage and method == "GET":
            json_reply(route, {**self.personality_coverage, "project_id": personality_coverage.group(1)})
            return
        personality_metrics = re.fullmatch(r"/api/projects/([^/]+)/characters/personality/metrics", path)
        if personality_metrics and method == "GET":
            json_reply(route, {**self.personality_metrics, "project_id": personality_metrics.group(1)})
            return
        personality_preview = re.fullmatch(r"/api/projects/([^/]+)/characters/personality/preview", path)
        if personality_preview and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(route, sample_personality_preview(personality_preview.group(1), payload))
            return
        character_create = re.fullmatch(r"/api/projects/([^/]+)/characters", path)
        if character_create and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            character_id = f"char-{len(self.character_personalities) + 1}"
            created = {
                "character_id": character_id,
                "character_name": payload.get("name") or "新角色",
                "personality_loadout": payload.get("personality_loadout") or sample_personality_loadout(),
            }
            self.character_personalities.append(created)
            json_reply(
                route,
                {
                    "schema_version": "character.creation.v1",
                    "character_id": character_id,
                    "personality_loadout": created["personality_loadout"],
                    "personality_assignment": sample_personality_assignment(),
                },
            )
            return
        personality_assignment_report = re.fullmatch(r"/api/projects/([^/]+)/characters/([^/]+)/personality/assignment-report", path)
        if personality_assignment_report and method == "GET":
            project_id, character_id = personality_assignment_report.groups()
            character = self._character_personality(character_id)
            json_reply(
                route,
                {
                    "schema_version": "character.personality_assignment_report.v1",
                    "project_id": project_id,
                    "character_id": character_id,
                    "character_name": character.get("character_name") or "",
                    "personality_assignment": sample_personality_assignment(),
                    "decision_events": [],
                },
            )
            return
        personality_reassign = re.fullmatch(r"/api/projects/([^/]+)/characters/([^/]+)/personality/reassign", path)
        if personality_reassign and method == "POST":
            project_id, character_id = personality_reassign.groups()
            payload = read_json(route)
            self.capture(route, payload)
            character = self._character_personality(character_id)
            character["personality_loadout"] = sample_personality_loadout("trait-cautious-strategist")
            json_reply(
                route,
                {
                    "schema_version": "character.personality_reassign.v1",
                    "project_id": project_id,
                    "character_id": character_id,
                    "preserved": False,
                    "personality_loadout": character["personality_loadout"],
                    "personality_assignment": sample_personality_assignment("auto_rule"),
                    "diff": {"reason": payload.get("reason") or ""},
                },
            )
            return
        active_context_preview = re.fullmatch(r"/api/projects/([^/]+)/characters/personality/active-context/preview", path)
        if active_context_preview and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(
                route,
                {
                    "schema_version": "character.active_personality_context_preview.v1",
                    "project_id": active_context_preview.group(1),
                    "active_personality_context": {
                        "character_id": payload.get("character_id") or "",
                        "character_name": payload.get("character_name") or "",
                        "dominant": "trait-loyal-protector",
                        "scene_flags": payload.get("scene_flags") or [],
                    },
                    "validation": {"ok": True, "errors": [], "warnings": []},
                },
            )
            return
        relationship_enrichment = re.fullmatch(r"/api/projects/([^/]+)/characters/personality/relationships/enrich", path)
        if relationship_enrichment and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(
                route,
                {
                    "schema_version": "character.relationship_personality_enrichment.v1",
                    "project_id": relationship_enrichment.group(1),
                    "updated": 1,
                    "preserved": 0,
                    "skipped": 0,
                },
            )
            return

        if path == "/api/publishers/platforms":
            json_reply(route, self.platforms)
            return
        if path == "/api/publishers/upload-jobs" and method == "GET":
            json_reply(route, list(self.upload_jobs.values()))
            return
        if path == "/api/publishers/upload-jobs" and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            job_id = f"upload-{len(self.upload_jobs) + 1}"
            job = sample_upload_job(job_id, payload)
            self.upload_jobs[job_id] = job
            json_reply(route, job)
            return
        upload_match = re.fullmatch(r"/api/publishers/upload-jobs/([^/]+)(?:/(terminate))?", path)
        if upload_match:
            job_id, action = upload_match.groups()
            if method == "GET":
                json_reply(route, self.upload_jobs.get(job_id) or sample_upload_job(job_id))
                return
            self.capture(route, {})
            if action == "terminate" and method == "POST":
                self.upload_jobs.setdefault(job_id, sample_upload_job(job_id))["status"] = "cancelled"
                json_reply(route, {"message": "upload terminated", **self.upload_jobs[job_id]})
                return
            if method == "DELETE":
                self.upload_jobs.pop(job_id, None)
                json_reply(route, {"message": "upload deleted", "deleted_id": f"upload:{job_id}"})
                return

        if path == "/api/projects" and method == "GET":
            json_reply(route, self.projects)
            return
        if path == "/api/projects" and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            project_id = f"project-{len(self.projects) + 1}"
            project = sample_project(project_id, title=payload.get("title") or "测试新书", creation_status="creating")
            project.update(
                {
                    "premise": payload.get("premise") or "",
                    "genre": payload.get("genre") or "玄幻",
                    "target_total_chapters": payload.get("target_total_chapters") or 3,
                    "automation": {"publish_bindings": payload.get("publish_bindings") or []},
                }
            )
            self.projects.append(project)
            self.genesis[project_id] = sample_genesis_detail(project_id)
            json_reply(route, {"project_id": project_id, "message": "书本已创建"})
            return
        if path == "/api/projects/bulk-delete" and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            ids = set(payload.get("project_ids") or [])
            self.projects = [item for item in self.projects if item["id"] not in ids]
            json_reply(route, {"deleted_ids": list(ids), "message": "批量删除完成"})
            return
        project_match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if project_match:
            project_id = project_match.group(1)
            if method == "GET":
                json_reply(route, self.project_detail(project_id))
                return
            if method == "DELETE":
                self.capture(route, {})
                self.projects = [item for item in self.projects if item["id"] != project_id]
                json_reply(route, {"project_id": project_id, "message": "书本已删除"})
                return

        if method == "GET" and re.fullmatch(r"/api/projects/[^/]+/genesis", path):
            project_id = path.split("/")[3]
            json_reply(route, self.genesis.setdefault(project_id, sample_genesis_detail(project_id)))
            return
        if method == "PATCH" and re.fullmatch(r"/api/projects/[^/]+/genesis", path):
            project_id = path.split("/")[3]
            payload = read_json(route)
            self.capture(route, payload)
            detail = self.genesis.setdefault(project_id, sample_genesis_detail(project_id))
            apply_genesis_patch(detail, payload)
            json_reply(route, detail)
            return
        stage_action = re.fullmatch(r"/api/projects/([^/]+)/genesis/stages/([^/]+)/(generate|rerun|lock|refine)", path)
        if stage_action:
            project_id, stage, action = stage_action.groups()
            payload = read_json(route)
            self.capture(route, payload)
            detail = self.genesis.setdefault(project_id, sample_genesis_detail(project_id))
            state = detail["pack"]["stage_states"].setdefault(stage, {"stage_key": stage})
            state["status"] = "complete"
            state["updated_at"] = now_text()
            if action == "lock":
                state["locked"] = True
            if action in {"generate", "rerun", "refine"}:
                state["locked"] = False
                ensure_stage_payload(detail, stage, action)
            detail["can_start_writing"] = all(detail["pack"]["stage_states"].get(item, {}).get("locked") for item in GENESIS_STAGES)
            json_reply(route, detail)
            return
        if method == "POST" and re.fullmatch(r"/api/projects/[^/]+/genesis/generate-name", path):
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(route, {"value": f"{payload.get('kind') or 'name'}-测试名"})
            return
        start_writing = re.fullmatch(r"/api/projects/([^/]+)/(start-writing|continue-generation)", path)
        if start_writing and method == "POST":
            project_id, action = start_writing.groups()
            payload = read_json(route)
            self.capture(route, payload)
            task_id = f"task-{len(self.tasks) + 1}"
            self.mark_generation_started(project_id, task_id, action)
            json_reply(route, {"task_id": task_id, "message": "已启动写作" if action == "start-writing" else "已继续生成"})
            return

        if path == "/api/generate" and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            task_id = f"task-{len(self.tasks) + 1}"
            self.tasks[task_id] = sample_generation_task(task_id, project_id=payload.get("project_id") or "")
            json_reply(route, {"task_id": task_id, **self.tasks[task_id]})
            return
        if path == "/api/task-center/items" and method == "GET":
            json_reply(route, self.task_center_items())
            return
        task_detail = re.fullmatch(r"/api/task-center/items/(generation|upload)/([^/]+)", path)
        if task_detail and method == "GET":
            kind, task_id = task_detail.groups()
            if kind == "upload":
                json_reply(route, {"task_kind": "upload", "task_id": task_id, **(self.upload_jobs.get(task_id) or sample_upload_job(task_id))})
            else:
                json_reply(route, self.tasks.get(task_id) or sample_generation_task(task_id, project_id=self.projects[0]["id"]))
            return
        task_mutation = re.fullmatch(r"/api/tasks/([^/]+)(?:/(pause|terminate))?", path)
        if task_mutation:
            task_id, action = task_mutation.groups()
            self.capture(route, {})
            if action == "pause" and method == "POST":
                self.tasks.setdefault(task_id, sample_generation_task(task_id))["pause_requested"] = True
                json_reply(route, {"message": "已发送安全暂停请求。"})
                return
            if action == "terminate" and method == "POST":
                self.tasks.setdefault(task_id, sample_generation_task(task_id))["status"] = "cancelled"
                json_reply(route, {"message": "已发送终止请求。"})
                return
            if method == "DELETE":
                self.tasks.pop(task_id, None)
                json_reply(route, {"message": "task deleted", "deleted_id": f"generation:{task_id}"})
                return
        if path == "/api/tasks/bulk-delete" and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            deleted = [f"{item.get('task_kind')}:{item.get('task_id')}" for item in payload.get("items", [])]
            json_reply(route, {"deleted_ids": deleted, "message": "批量删除完成"})
            return

        self.handle_project_subroutes(route, path, method)

    def handle_project_subroutes(self, route: Route, path: str, method: str) -> None:
        chapter_page = re.fullmatch(r"/api/projects/([^/]+)/chapters/page", path)
        if chapter_page and method == "GET":
            parsed = urlparse(route.request.url)
            query = parse_qs(parsed.query)
            offset = int((query.get("offset") or ["0"])[0] or 0)
            limit = int((query.get("limit") or ["60"])[0] or 60)
            all_chapters = self.project_detail(chapter_page.group(1))["chapters"]
            page = all_chapters[offset:offset + limit]
            self.capture(route, {})
            json_reply(route, {
                "project_id": chapter_page.group(1),
                "total": len(all_chapters),
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(page) < len(all_chapters),
                "chapters": page,
            })
            return
        chapters = re.fullmatch(r"/api/projects/([^/]+)/chapters", path)
        if chapters and method == "GET":
            json_reply(route, self.project_detail(chapters.group(1))["chapters"])
            return
        chapter_detail = re.fullmatch(r"/api/projects/([^/]+)/chapters/(\d+)", path)
        if chapter_detail and method == "GET":
            _, chapter_number = chapter_detail.groups()
            json_reply(route, sample_chapter(int(chapter_number), body=True))
            return
        review = re.fullmatch(r"/api/projects/([^/]+)/chapters/(\d+)/review(?:/(approve))?", path)
        if review:
            project_id, chapter_number, action = review.groups()
            if action == "approve" and method == "POST":
                payload = read_json(route)
                self.capture(route, payload)
                json_reply(route, {"message": "review approved", "task_id": payload.get("continue_generation") and "task-continue" or ""})
                return
            json_reply(route, sample_review(project_id, int(chapter_number)))
            return
        if method == "PUT" and re.fullmatch(r"/api/projects/[^/]+/governance", path):
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(route, {"message": "项目治理设置已保存"})
            return
        if method == "POST" and re.fullmatch(r"/api/projects/[^/]+/manual-checkpoints", path):
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(route, {"message": "manual checkpoint 已创建"})
            return
        if method == "POST" and re.fullmatch(r"/api/projects/[^/]+/constraints", path):
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(route, {"message": "constraint created", "id": "constraint-new"})
            return
        if method == "PATCH" and re.fullmatch(r"/api/projects/[^/]+/constraints/[^/]+", path):
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(route, {"message": "constraint updated"})
            return
        if method == "PUT" and re.fullmatch(r"/api/projects/[^/]+/(?:bands/[^/]+|chapters/\d+)/task-contract", path):
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(route, {"items": payload.get("items") or [], "message": "task contract updated"})
            return
        if method == "GET" and re.fullmatch(r"/api/projects/[^/]+/(?:bands/[^/]+|chapters/\d+)/task-contract", path):
            json_reply(route, {"items": [{"task_type": "plot_advance", "description": "推进主线"}]})
            return
        if method == "POST" and re.fullmatch(r"/api/projects/[^/]+/bands/[^/]+/checkpoint/approve", path):
            payload = read_json(route)
            self.capture(route, payload)
            json_reply(route, {"message": "checkpoint approved"})
            return
        if method == "GET" and re.fullmatch(r"/api/projects/[^/]+/causal-replay", path):
            json_reply(route, sample_causal_replay())
            return
        if method == "GET" and re.fullmatch(r"/api/projects/[^/]+/governance-insights", path):
            json_reply(route, sample_governance_insights())
            return
        v4 = re.fullmatch(r"/api/projects/([^/]+)/world-model/v4/(debug|lines|gaps|reveals|export)", path)
        if v4 and method == "GET":
            _, endpoint = v4.groups()
            json_reply(route, {"project_id": v4.group(1), "endpoint": endpoint, "items": [{"id": f"{endpoint}-1", "title": "V4 调试项"}]})
            return
        world = re.fullmatch(r"/api/projects/([^/]+)/world-model/(pages|snapshots|conflicts|proposals)", path)
        if world and method == "GET":
            kind = world.group(2)
            payload = {
                "pages": self.world_pages,
                "snapshots": self.world_snapshots,
                "conflicts": self.world_conflicts,
                "proposals": self.world_proposals,
            }[kind]
            json_reply(route, payload)
            return
        export = re.fullmatch(r"/api/projects/([^/]+)/world-model/(export-obsidian|import-obsidian)", path)
        if export and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            action = export.group(2)
            if action == "export-obsidian":
                json_reply(route, {"ok": True, "vault_root": payload.get("vault_root") or "/tmp/forwin-vault", "exported_count": len(self.world_pages), "message": "已导出 WorldModel。"})
            else:
                json_reply(route, {"ok": True, "vault_root": payload.get("vault_root") or "/tmp/forwin-vault", "proposal_count": len(self.world_proposals), "changed_paths": ["world.md"], "message": "已导入 proposal。"})
            return
        proposal_review = re.fullmatch(r"/api/projects/([^/]+)/world-model/proposals/([^/]+)/review", path)
        if proposal_review and method == "POST":
            payload = read_json(route)
            self.capture(route, payload)
            proposal_id = proposal_review.group(2)
            for proposal in self.world_proposals:
                if proposal["id"] == proposal_id:
                    proposal["status"] = payload.get("status") or "accepted"
            json_reply(route, next((item for item in self.world_proposals if item["id"] == proposal_id), sample_world_proposals()[0]))
            return
        api_error(route, f"Unhandled mock API route: {method} {path}", 501)

    def _character_personality(self, character_id: str) -> dict[str, Any]:
        for character in self.character_personalities:
            if character.get("character_id") == character_id:
                return character
        fallback = {
            "character_id": character_id,
            "character_name": character_id,
            "personality_loadout": sample_personality_loadout(),
        }
        self.character_personalities.append(fallback)
        return fallback

    def task_center_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for task in self.tasks.values():
            items.append({**task, "task_kind": "generation"})
        for job in self.upload_jobs.values():
            items.append(upload_job_as_task_item(job))
        return items

    def project_detail(self, project_id: str) -> dict[str, Any]:
        project = deepcopy(self.project_ref(project_id))
        project["id"] = project_id
        project.setdefault("chapters", [sample_chapter(1), sample_chapter(2, status="needs_review", has_review=True)])
        project.setdefault("governance", sample_governance())
        project.setdefault("latest_band_checkpoint", sample_band_checkpoint())
        project.setdefault("narrative_constraints", [sample_constraint()])
        project.setdefault("decision_timeline", sample_decision_events())
        project.setdefault("automation", sample_automation())
        project.setdefault("generation_control", sample_generation_control(can_resume=True))
        project.setdefault("active_arc_id", "arc-1")
        project.setdefault("next_gate", "chapter_accepted")
        return project


def sample_settings() -> dict[str, Any]:
    return {
        "has_api_key": True,
        "api_key": "",
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M2.7",
        "operation_mode": "blackbox",
        "freeze_failed_candidates": True,
        "min_chapter_chars": 2500,
        "review_interval_chapters": 2,
        "progression_mode": "serial_canon_band_guard",
        "auto_band_checkpoint": True,
        "manual_checkpoints_enabled": True,
        "future_constraints_enabled": True,
        "default_profile_id": "profile-1",
        "profiles": [
            {
                "id": "profile-1",
                "name": "测试 MiniMax",
                "model": "MiniMax-M2.7",
                "base_url": "https://api.minimaxi.com/v1",
                "has_api_key": True,
            }
        ],
    }


def sample_platforms() -> list[dict[str, Any]]:
    return [
        {
            "platform_id": "fanqie",
            "display_name": "番茄小说",
            "connected": True,
            "extension_online": True,
            "extension_client_id": "client-a",
            "last_heartbeat_at": "2026-04-24T12:00:00Z",
            "last_error": "",
            "login_url": "https://fanqie.example/login",
            "supported_login_methods": ["browser_extension"],
            "supported_actions": ["upload_chapter", "sync_comments"],
        },
        {
            "platform_id": "qidian",
            "display_name": "起点中文网",
            "connected": False,
            "extension_online": False,
            "extension_client_id": "",
            "last_heartbeat_at": "",
            "last_error": "未登录",
            "login_url": "https://qidian.example/login",
            "supported_login_methods": ["browser_extension"],
            "supported_actions": ["upload_chapter"],
        },
    ]


def sample_governance() -> dict[str, Any]:
    return {
        "default_operation_mode": "checkpoint",
        "progression_mode": "serial_canon_band_guard",
        "review_interval_chapters": 2,
        "auto_band_checkpoint": True,
        "manual_checkpoints_enabled": True,
        "future_constraints_enabled": False,
    }


def sample_automation() -> dict[str, Any]:
    return {
        "enabled": True,
        "daily_chapter_quota": 2,
        "daily_start_time": "09:30",
        "auto_publish": True,
        "publish": {"platform": "fanqie", "book_name": "雾港潮生录", "create_if_missing": False},
        "publish_bindings": [{"platform": "fanqie", "book_name": "雾港潮生录", "create_if_missing": False}],
    }


def sample_project(project_id: str = "project-1", *, title: str = "雾港潮生录", creation_status: str = "active") -> dict[str, Any]:
    return {
        "id": project_id,
        "title": title,
        "genre": "玄幻",
        "premise": "潮雾笼罩的港城里，见习记录员追查失踪航线。",
        "created_at": "2026-04-24T12:00:00Z",
        "creation_status": creation_status,
        "target_total_chapters": 6,
        "chapter_count": 3,
        "generated_chapter_count": 2,
        "uploaded_chapter_count": 1,
        "needs_review_chapter_count": 1,
        "latest_stage": "paused_for_review",
        "pacing_summary": "稳定推进",
        "chapters": [sample_chapter(1), sample_chapter(2, status="needs_review", has_review=True), sample_chapter(3, status="planned")],
        "governance": sample_governance(),
        "automation": sample_automation(),
        "generation_control": sample_generation_control(can_resume=True),
    }


def sample_chapter(chapter_number: int, *, status: str = "accepted", has_review: bool = False, body: bool = False) -> dict[str, Any]:
    payload = {
        "chapter_number": chapter_number,
        "title": f"潮声第{chapter_number}章",
        "status": status,
        "char_count": 3200 if status in {"accepted", "drafted", "needs_review"} else 0,
        "has_draft": status in {"accepted", "drafted", "needs_review"},
        "has_review": has_review,
    }
    if body:
        payload["body"] = f"第{chapter_number}章正文。潮声从雾港尽头传来。"
        payload["summary"] = "主角发现航线异常。"
    return payload


def sample_generation_control(*, can_resume: bool = False) -> dict[str, Any]:
    return {
        "can_resume": can_resume,
        "plan_state": "in_progress",
        "writing_state": "paused",
        "review_state": "needs_review",
        "next_chapter": 3,
        "pending_review_chapters": [2],
        "failed_chapters": [],
        "review_interval_chapters": 2,
        "chapters_until_review": 1,
        "chapters_until_replan_eligible": 2,
        "blocking_reason": {"code": "review_required", "message": "第 2 章需要人工检查", "decision_event_id": "decision-2"},
        "latest_band_checkpoint": sample_band_checkpoint(),
        "next_gate": "chapter_accepted",
    }


def sample_generation_task(task_id: str, project_id: str = "project-1") -> dict[str, Any]:
    return {
        "task_kind": "generation",
        "task_id": task_id,
        "title": "雾港潮生录",
        "subtitle": "书本生成 · 玄幻 · 6 章",
        "status": "needs_review",
        "project_id": project_id,
        "current_stage": "paused_for_review",
        "current_chapter": 2,
        "requested_chapters": 6,
        "completed_chapters": [1],
        "failed_chapters": [],
        "paused_chapters": [2],
        "frozen_artifacts": ["artifact://draft/chapter-2"],
        "stage_history": [
            {"stage": "queued", "at": "2026-04-24T12:00:00Z"},
            {"stage": "planning_arc", "at": "2026-04-24T12:00:01Z"},
            {"stage": "assembling_context", "chapter": 1, "at": "2026-04-24T12:00:02Z"},
            {"stage": "writing_chapter", "chapter": 1, "at": "2026-04-24T12:00:03Z"},
            {"stage": "paused_for_review", "chapter": 2, "at": "2026-04-24T12:00:04Z", "message": "人工检查点"},
        ],
        "generation_control": sample_generation_control(can_resume=True),
        "pausable": True,
        "terminable": True,
        "deletable": True,
        "updated_at": "2026-04-24T12:00:04Z",
        "message": "等待人工 review",
        "error": "",
    }


def sample_upload_job(job_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "job_id": job_id,
        "task_id": job_id,
        "status": "succeeded",
        "platform": payload.get("platform") or "fanqie",
        "display_name": "番茄小说",
        "book_name": payload.get("book_name") or "雾港潮生录",
        "title": payload.get("book_name") or "雾港潮生录",
        "chapter_title": payload.get("chapter_title") or "潮声第1章",
        "subtitle": payload.get("chapter_title") or "潮声第1章",
        "publish": payload.get("publish", True),
        "create_if_missing": payload.get("create_if_missing", False),
        "extension_client_id": "client-a",
        "current_url": "https://fanqie.example/editor",
        "result_payload": {"phase": "submitted"},
        "message": "上传完成",
        "error": "",
        "created_at": "2026-04-24T12:00:00Z",
        "updated_at": "2026-04-24T12:00:02Z",
        "started_at": "2026-04-24T12:00:01Z",
        "finished_at": "2026-04-24T12:00:02Z",
        "deletable": True,
        "terminable": False,
        "pausable": False,
    }


def upload_job_as_task_item(job: dict[str, Any]) -> dict[str, Any]:
    return {
        **job,
        "task_kind": "upload",
        "task_id": job.get("job_id") or job.get("task_id"),
        "title": job.get("book_name") or job.get("title"),
        "subtitle": job.get("chapter_title") or job.get("subtitle"),
    }


def sample_band_checkpoint() -> dict[str, Any]:
    return {
        "band_id": "band-1",
        "status": "warn",
        "summary": "节奏略慢，需要人工确认。",
        "boundary_kind": "band_end",
        "boundary_chapter": 2,
        "issues": [{"severity": "warn", "issue_group": "pacing", "category": "density", "description": "爽点密度不足"}],
        "decision_refs": [{"id": "decision-3", "event_type": "band_checkpoint_hit"}],
    }


def sample_constraint() -> dict[str, Any]:
    return {
        "id": "constraint-1",
        "status": "active",
        "level": "hard",
        "constraint_type": "secret_withhold",
        "subject_name": "潮门密钥",
        "description": "第 4 章前不能揭示真相",
        "effective_from_chapter": 1,
        "protect_until_chapter": 4,
    }


def sample_decision_events() -> list[dict[str, Any]]:
    return [
        {"id": "decision-1", "scope": "project", "event_family": "business_event", "event_type": "run_started", "summary": "生成启动", "created_at": "2026-04-24T12:00:00Z", "causal_root_id": "decision-1"},
        {"id": "decision-2", "scope": "chapter", "chapter_number": 2, "event_family": "evaluation_verdict", "event_type": "review_verdict_recorded", "summary": "第 2 章需要人工检查", "reason": "人工检查间隔", "parent_event_id": "decision-1", "causal_root_id": "decision-1"},
        {"id": "decision-3", "scope": "band", "band_id": "band-1", "event_family": "audit_action", "event_type": "band_checkpoint_hit", "summary": "Band checkpoint warn", "parent_event_id": "decision-2", "causal_root_id": "decision-1"},
    ]


def sample_review(project_id: str, chapter_number: int) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "chapter_number": chapter_number,
        "title": f"潮声第{chapter_number}章",
        "status": "needs_review",
        "verdict": "needs_review",
        "recommended_action": "manual_accept",
        "review_summary": "章节可接受，但需要人工确认伏笔密度。",
        "issues": [{"severity": "warn", "issue_group": "pacing", "issue_type": "delight_density", "description": "爽点密度偏低"}],
        "decision_refs": [{"id": "decision-2", "event_type": "review_verdict_recorded"}],
    }


def sample_causal_replay() -> dict[str, Any]:
    return {
        "root_event": {"summary": "生成启动"},
        "current_outcome": "needs_review",
        "linked_review_refs": [{"id": "decision-2"}],
        "linked_checkpoint_refs": [{"id": "decision-3"}],
        "timeline": sample_decision_events(),
    }


def sample_governance_insights() -> dict[str, Any]:
    return {
        "most_common_blocking_reasons": [{"name": "review_required", "count": 2}],
        "top_override_rule_types": [{"name": "band_checkpoint", "count": 1}],
        "forced_accept_frequency": 0,
        "recent_band_checkpoint_distribution": [{"name": "warn", "count": 1}],
        "issue_group_distribution": [{"name": "pacing", "count": 2}],
        "recommended_adjustments": [{"type": "tighten", "target": "pacing", "reason": "Review warn 较多", "count": 2}],
        "recent_examples": [{"event_id": "decision-2", "event_type": "review_verdict_recorded", "summary": "第 2 章需要人工检查", "chapter_number": 2}],
    }


def sample_genesis_detail(project_id: str) -> dict[str, Any]:
    stage_states = {
        stage: {"stage_key": stage, "status": "drafted", "locked": False, "updated_at": "2026-04-24T12:00:00Z", "last_trace_id": ""}
        for stage in GENESIS_STAGES
    }
    return {
        "project_id": project_id,
        "creation_status": "creating",
        "revision": 1,
        "can_start_writing": False,
        "pack": {
            "stage_states": stage_states,
            "book_brief": {
                "title": "雾港潮生录",
                "one_line": "见习记录员追查失踪航线。",
                "audience": "男频",
                "core_emotion": "压迫感",
                "core_delight": "谜题揭示",
                "promise": "每章都有潮雾中的新线索。",
                "guardrails": ["不提前揭示密钥"],
            },
            "world": {
                "world_bible": {
                    "overview": "潮雾覆盖港城。",
                    "axioms": ["潮汐会吞掉记忆"],
                    "history_slice": "旧航线在十年前消失。",
                    "naming_style": "雾、潮、灯塔",
                    "forbidden_zones": ["不能出现现代网络"],
                    "culture_profiles": [{"id": "culture-1", "name": "灯塔守望者"}],
                },
                "minimum_world_system": {},
                "minimum_extension_pack": {},
                "institution_profiles": [],
                "resource_economy_profiles": [],
                "world_extensions": {
                    "daily_life_profiles": [],
                    "belief_mythos_profiles": [],
                    "information_profiles": [],
                    "ecology_profiles": [],
                    "aesthetic_profiles": [],
                    "secrets_codex": [],
                    "value_conflicts": [],
                    "story_interfaces": [],
                },
                "map_atlas": {
                    "overview": "港城、外海、灯塔三层空间。",
                    "topology_rules": ["退潮时才能进入旧码头"],
                    "submaps": [{"id": "subworld-1", "name": "雾港"}],
                    "regions": [{"id": "region-1", "name": "旧码头", "level": 1}],
                    "nodes": [{"id": "node-1", "name": "潮汐档案馆"}],
                },
                "story_engine": {
                    "relationship_axes": ["师徒互疑"],
                    "reader_promises": ["每卷揭开一个航线秘密"],
                    "long_arcs": ["寻找潮门密钥"],
                    "core_cast": [{"id": "char-1", "name": "林夜"}],
                    "factions": [{"id": "faction-1", "name": "灯塔会"}],
                    "arcs": [{"arc_number": 1, "title": "雾港初潮", "chapter_start": 1, "chapter_end": 6, "chapter_count": 6}],
                },
            },
            "book_arc_blueprint": {
                "summary": "第一卷从失踪航线切入。",
                "arcs": [{"arc_number": 1, "title": "雾港初潮", "chapter_start": 1, "chapter_end": 6, "chapter_count": 6}],
            },
            "execution_bootstrap": {
                "operation_mode": "blackbox",
                "start_policy": "explicit_start_writing_only",
                "root_ready": False,
            },
        },
        "prompt_traces": [],
    }


def apply_genesis_patch(detail: dict[str, Any], payload: dict[str, Any]) -> None:
    pack = detail.setdefault("pack", {})
    if "book_brief" in payload:
        pack["book_brief"] = payload["book_brief"]
    if "world" in payload:
        pack["world"] = payload["world"]
    if "book_arc_blueprint" in payload:
        pack["book_arc_blueprint"] = payload["book_arc_blueprint"]
    if "execution_bootstrap" in payload:
        pack["execution_bootstrap"] = payload["execution_bootstrap"]


def ensure_stage_payload(detail: dict[str, Any], stage: str, action: str) -> None:
    pack = detail["pack"]
    if stage == "brief":
        pack["book_brief"]["one_line"] = f"{action} 后的创意简报"
    if stage == "world":
        pack["world"]["world_bible"]["overview"] = f"{action} 后的世界概览"
    if stage == "map":
        pack["world"]["map_atlas"]["overview"] = f"{action} 后的地图概览"
    if stage == "story_engine":
        pack["world"]["story_engine"]["reader_promises"] = [f"{action} 后的读者承诺"]
    if stage == "book_blueprint":
        pack["book_arc_blueprint"]["summary"] = f"{action} 后的蓝图"
    if stage == "bootstrap":
        pack["execution_bootstrap"]["root_ready"] = action != "generate"


def sample_world_snapshots(project_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "snapshot-1",
            "project_id": project_id,
            "as_of_chapter": 2,
            "version": 1,
            "status": "current",
            "source_digest": "abcdef1234567890",
            "snapshot": {"entities": ["林夜"]},
            "created_at": "2026-04-24T12:00:00Z",
            "updated_at": "2026-04-24T12:00:00Z",
        }
    ]


def sample_world_pages(project_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "page-1",
            "project_id": project_id,
            "page_key": "entity/linye",
            "page_type": "entity",
            "title": "林夜",
            "vault_path": "Entities/林夜.md",
            "markdown": "# 林夜\n\n见习记录员。",
            "frontmatter": {"status": "active"},
            "content_hash": "1234567890abcdef",
            "revision": 1,
            "status": "current",
            "as_of_chapter": 2,
            "updated_at": "2026-04-24T12:00:00Z",
        },
        {
            "id": "page-2",
            "project_id": project_id,
            "page_key": "location/fog-port",
            "page_type": "location",
            "title": "雾港",
            "vault_path": "Locations/雾港.md",
            "markdown": "# 雾港\n\n潮雾之城。",
            "frontmatter": {},
            "content_hash": "abcdef1234567890",
            "revision": 1,
            "status": "current",
            "as_of_chapter": 2,
            "updated_at": "2026-04-24T12:00:00Z",
        },
    ]


def sample_world_conflicts() -> list[dict[str, Any]]:
    return [
        {
            "id": "conflict-1",
            "conflict_type": "location_mismatch",
            "severity": "warn",
            "subject_key": "entity/linye",
            "description": "林夜同章出现在两个地点。",
            "status": "open",
            "created_at": "2026-04-24T12:00:00Z",
        }
    ]


def sample_world_proposals() -> list[dict[str, Any]]:
    return [
        {
            "id": "proposal-1",
            "source": "obsidian",
            "target_page_key": "entity/linye",
            "target_field": "summary",
            "proposed_patch": {"summary": "林夜开始怀疑灯塔会。"},
            "reason": "Obsidian 更新",
            "status": "pending",
            "created_by": "browser-test",
            "created_at": "2026-04-24T12:00:00Z",
            "reviewed_at": "",
        }
    ]


def sample_personality_loadout(skill: str = "trait-loyal-protector") -> dict[str, Any]:
    return {
        "dominant": {"skill": skill, "weight": 0.82},
        "secondary": [],
        "social_mask": [],
        "stress_modes": [],
        "relationship_patterns": [],
        "overrides": {},
    }


def sample_personality_assignment(mode: str = "auto_rule") -> dict[str, Any]:
    return {
        "assignment_id": "assignment-1",
        "policy_version": "character_personality_assignment.v1",
        "assignment_mode": mode,
        "confidence": 0.86,
        "status": "valid",
        "manual_override": False,
        "selected_skill_ids": ["trait-loyal-protector"],
        "reason_tags": ["browser-test-fixture"],
    }


def sample_personality_skills() -> list[dict[str, Any]]:
    return [
        {
            "name": "trait-loyal-protector",
            "version": "1.0",
            "description": "保护重要关系并承担风险。",
            "skill_type": "trait",
            "path": "forwin_skills/character_personality/skills/traits/trait-loyal-protector/SKILL.md",
        },
        {
            "name": "trait-cautious-strategist",
            "version": "1.0",
            "description": "先观察，再行动。",
            "skill_type": "trait",
            "path": "forwin_skills/character_personality/skills/traits/trait-cautious-strategist/SKILL.md",
        },
    ]


def sample_character_personalities() -> list[dict[str, Any]]:
    return [
        {
            "character_id": "char-1",
            "character_name": "林夜",
            "personality_loadout": sample_personality_loadout(),
        }
    ]


def sample_personality_coverage(project_id: str, characters: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "character.personality_coverage.v1",
        "project_id": project_id,
        "character_count": len(characters),
        "with_valid_loadout": len(characters),
        "missing_loadout": 0,
        "fallback_used": 0,
        "manual_override": 0,
        "needs_review": 0,
        "coverage_ratio": 1.0 if characters else 0.0,
        "issue_counts": {},
        "characters": [
            {
                "character_id": character["character_id"],
                "character_name": character["character_name"],
                "assignment_mode": "auto_rule",
                "assignment_status": "valid",
                "manual_override": False,
                "issues": [],
            }
            for character in characters
        ],
    }


def sample_personality_metrics(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": "character.personality_metrics.v1",
        "project_id": project_id,
        "character_creation_total": 1,
        "character_creation_auto_personality_assigned_total": 1,
        "character_creation_manual_override_total": 0,
        "character_creation_fallback_used_total": 0,
        "character_creation_low_confidence_total": 0,
        "character_integrity_missing_loadout_total": 0,
        "personality_assignment_confidence_avg": 0.86,
        "personality_ooc_issue_total_by_assignment_mode": {},
        "most_used_dominant_skills": [{"skill": "trait-loyal-protector", "count": 1}],
    }


def sample_personality_preview(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip() or "新角色"
    return {
        "schema_version": "character.personality_preview.v1",
        "project_id": project_id,
        "character_name": name,
        "personality_loadout": sample_personality_loadout(),
        "personality_assignment": sample_personality_assignment(),
        "validation": {"ok": True, "errors": [], "warnings": []},
    }


def goto_home(page: Page, base_url: str, backend: MockForWinBackend) -> None:
    backend.install(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#global_status")).to_contain_text("首页已加载")


def goto_publishers(page: Page, base_url: str, backend: MockForWinBackend, *, bridge: bool = True) -> None:
    if bridge:
        install_extension_bridge(page)
    backend.install(page)
    page.goto(f"{base_url}/publishers", wait_until="domcontentloaded")
    expect(page.locator("#platform")).to_have_value("fanqie")


def goto_world_studio(page: Page, base_url: str, backend: MockForWinBackend) -> None:
    backend.install(page)
    page.goto(f"{base_url}/world-studio", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="世界档案")).to_be_visible()
