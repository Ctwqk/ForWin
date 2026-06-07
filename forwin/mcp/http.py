from __future__ import annotations

import os
from typing import Callable, Literal

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .client import ForWinAPIClient
from .models import (
    BandCheckpointView,
    ChapterListView,
    ChapterReviewApproveView,
    MutationResult,
    ProjectDecisionEventsView,
    ProjectListView,
    StageKey,
    TaskListView,
    WorldModelConflictListView,
)


def build_mcp_server(*, api_client: ForWinAPIClient | None = None) -> FastMCP:
    client = api_client or ForWinAPIClient(
        base_url=os.environ.get("FORWIN_API_BASE_URL", "http://127.0.0.1:8899"),
        timeout=_env_api_timeout_seconds(),
    )
    mcp = FastMCP(
        name="ForWin",
        instructions=(
            "Use these tools to operate the ForWin backend. "
            "Read project/task state first, then mutate. "
            "Do not bypass these tools with direct DB inspection when a matching MCP tool exists."
        ),
        on_duplicate_tools="error",
    )

    def register_read_tool(
        name: str,
        description: str,
    ) -> Callable[[Callable[..., object]], Callable[..., object]]:
        return mcp.tool(
            name=name,
            description=description,
            annotations={
                "title": name.replace("_", " ").title(),
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        )

    def register_write_tool(
        name: str,
        description: str,
    ) -> Callable[[Callable[..., object]], Callable[..., object]]:
        return mcp.tool(
            name=name,
            description=description,
            annotations={
                "title": name.replace("_", " ").title(),
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        )

    @register_read_tool(
        "project_list",
        "List ForWin projects. Use this when you need the authoritative project roster before choosing a project_id. Do not inspect the database directly for this.",
    )
    async def project_list():
        return ProjectListView(projects=await client.project_list())

    @register_read_tool(
        "project_get",
        "Get one ForWin project's current state. Use this when you need creation_status, can_start_writing, next_gate, or chapter progress for a known project_id.",
    )
    async def project_get(project_id: str):
        return await client.project_get(project_id)

    @register_read_tool(
        "project_decision_events",
        "List recent ForWin decision events for a project. Use this when inspecting generation audit checkpoints, future plan audits, pauses, and other audit records without reading the database.",
    )
    async def project_decision_events(
        project_id: str,
        event_type: str = "",
        event_family: str = "",
        limit: int = 50,
    ) -> ProjectDecisionEventsView:
        return await client.project_decision_events(
            project_id=project_id,
            event_type=event_type,
            event_family=event_family,
            limit=limit,
        )

    @register_write_tool(
        "project_create",
        "Create a new Genesis-backed ForWin project. Use this when starting a new book from title and premise. This does not start chapter writing.",
    )
    async def project_create(
        title: str,
        premise: str,
        genre: str = "玄幻",
        setting_summary: str = "",
        target_total_chapters: int = 3,
    ) -> MutationResult:
        return await client.project_create(
            title=title,
            premise=premise,
            genre=genre,
            setting_summary=setting_summary,
            target_total_chapters=target_total_chapters,
        )

    @register_read_tool(
        "genesis_get",
        "Read the current Genesis pack for a project. Use this when inspecting stage state, world scaffolding, or whether Genesis is ready to hand off into writing.",
    )
    async def genesis_get(project_id: str):
        return await client.genesis_get(project_id)

    @register_write_tool(
        "genesis_stage_generate",
        "Generate one Genesis stage for a project. Use this when a stage is incomplete and you want ForWin to draft that specific stage.",
    )
    async def genesis_stage_generate(project_id: str, stage_key: StageKey) -> MutationResult:
        return await client.genesis_stage_generate(project_id=project_id, stage_key=stage_key)

    @register_write_tool(
        "genesis_stage_refine",
        "Refine one Genesis stage or target_path with an instruction. Use this when you want a focused rewrite inside Genesis rather than regenerating the whole stage.",
    )
    async def genesis_stage_refine(
        project_id: str,
        stage_key: StageKey,
        instruction: str,
        target_path: str = "",
        reason: str = "",
    ) -> MutationResult:
        return await client.genesis_stage_refine(
            project_id=project_id,
            stage_key=stage_key,
            instruction=instruction,
            target_path=target_path,
            reason=reason,
        )

    @register_write_tool(
        "genesis_stage_lock",
        "Lock one Genesis stage as current truth. Use this when a stage is accepted and should become the stable handoff state for later stages and writing.",
    )
    async def genesis_stage_lock(project_id: str, stage_key: StageKey) -> MutationResult:
        return await client.genesis_stage_lock(project_id=project_id, stage_key=stage_key)

    @register_write_tool(
        "project_start_writing",
        "Start writing for a Genesis-ready project. Normal generation auto-continues until the target chapters unless blocked by review, repair, budget, or manual gates. Use auto_continue, run_until_chapter, or max_chapters only for limiting, debugging, or explicit control. Use this when Genesis is complete and there is no active generation task.",
    )
    async def project_start_writing(
        project_id: str,
        auto_continue: bool | None = None,
        run_until_chapter: int | None = None,
        max_chapters: int | None = None,
    ) -> MutationResult:
        return await client.project_start_writing(
            project_id=project_id,
            auto_continue=auto_continue,
            run_until_chapter=run_until_chapter,
            max_chapters=max_chapters,
        )

    @register_write_tool(
        "project_continue_generation",
        "Continue chapter generation for an existing writing project. Normal generation auto-continues until the target chapters unless blocked by review, repair, budget, or manual gates. Use max_chapters, auto_continue, or run_until_chapter only for limiting, debugging, or explicit control. Use this when the project is already in writing state and no active generation task exists.",
    )
    async def project_continue_generation(
        project_id: str,
        max_chapters: int | None = None,
        auto_continue: bool | None = None,
        run_until_chapter: int | None = None,
    ) -> MutationResult:
        return await client.project_continue_generation(
            project_id=project_id,
            max_chapters=max_chapters,
            auto_continue=auto_continue,
            run_until_chapter=run_until_chapter,
        )

    @register_write_tool(
        "project_extend_generation",
        "Append planned future chapters to an existing writing project before continuing generation. Use this when the user asks to extend the target chapter count; existing planned chapters are allowed, but active, review, drafted, or failed gates must be handled first.",
    )
    async def project_extend_generation(
        project_id: str,
        additional_chapters: int = 12,
        continuity_guard: str = "",
        arc_title: str = "",
        arc_synopsis: str = "",
        reason: str = "",
    ) -> MutationResult:
        return await client.project_extend_generation(
            project_id=project_id,
            additional_chapters=additional_chapters,
            continuity_guard=continuity_guard,
            arc_title=arc_title,
            arc_synopsis=arc_synopsis,
            reason=reason,
        )

    @register_read_tool(
        "task_list",
        "List recent ForWin generation tasks. Use this when you need to inspect queued, running, paused, or failed generation work across projects.",
    )
    async def task_list(limit: int = 20):
        return TaskListView(tasks=await client.task_list(limit=limit))

    @register_read_tool(
        "task_get",
        "Get one generation task by task_id. Use this when polling a known task for current_stage, pause state, or recovery guidance.",
    )
    async def task_get(task_id: str):
        return await client.task_get(task_id)

    @register_read_tool(
        "task_active_generation_check",
        "Check whether generation is currently active. Use this when you are about to start writing, continue generation, restart services, or create another task.",
    )
    async def task_active_generation_check(project_id: str = ""):
        return await client.task_active_generation_check(project_id=project_id)

    @register_write_tool(
        "task_pause",
        "Request a safe pause for a running generation task. Use this when you need to stop generation safely instead of terminate or restart behavior.",
    )
    async def task_pause(task_id: str) -> MutationResult:
        return await client.task_pause(task_id=task_id)

    @register_read_tool(
        "chapter_list",
        "List chapters for a project. Use this when you need chapter numbers, statuses, summaries, or to choose a chapter_number for inspection.",
    )
    async def chapter_list(project_id: str):
        return ChapterListView(chapters=await client.chapter_list(project_id=project_id))

    @register_read_tool(
        "chapter_get",
        "Get one chapter draft. Use this when you need the actual body text, summary, and review-related status for a specific chapter_number.",
    )
    async def chapter_get(project_id: str, chapter_number: int):
        return await client.chapter_get(project_id=project_id, chapter_number=chapter_number)

    @register_write_tool(
        "chapter_review_approve",
        "Accept one drafted chapter review into canon. Use this when project_get next_gate is chapter_N_accept and there is no active generation task; set continue_generation to also start the next generation task.",
    )
    async def chapter_review_approve(
        project_id: str,
        chapter_number: int,
        reason: str,
        continue_generation: bool = False,
    ) -> ChapterReviewApproveView:
        return await client.chapter_review_approve(
            project_id=project_id,
            chapter_number=chapter_number,
            reason=reason,
            continue_generation=continue_generation,
        )

    @register_write_tool(
        "chapter_review_retry",
        "Reset one drafted or needs_review chapter back to planned for regeneration. Use this when a chapter review gate is stale or must be rerun and there is no active generation task; set continue_generation to start the retry task.",
    )
    async def chapter_review_retry(
        project_id: str,
        chapter_number: int,
        reason: str,
        continue_generation: bool = False,
        allow_accepted: bool = False,
    ) -> ChapterReviewApproveView:
        return await client.chapter_review_retry(
            project_id=project_id,
            chapter_number=chapter_number,
            reason=reason,
            continue_generation=continue_generation,
            allow_accepted=allow_accepted,
        )

    @register_read_tool(
        "band_checkpoint_get",
        "Get the latest band checkpoint for a project band. Use this when project_get reports a band_checkpoint_* gate before deciding whether to fix, pass, or override it.",
    )
    async def band_checkpoint_get(project_id: str, band_id: str) -> BandCheckpointView:
        return await client.band_checkpoint_get(project_id=project_id, band_id=band_id)

    @register_write_tool(
        "band_checkpoint_approve",
        "Approve or override a band checkpoint after inspecting its issues. Use this when a band checkpoint warning or failure has been reviewed and there is no active generation task.",
    )
    async def band_checkpoint_approve(
        project_id: str,
        band_id: str,
        reason: str,
        status: Literal["pass", "overridden"] = "overridden",
    ) -> BandCheckpointView:
        return await client.band_checkpoint_approve(
            project_id=project_id,
            band_id=band_id,
            reason=reason,
            status=status,
        )

    @register_read_tool(
        "world_model_get",
        "Get the latest compiled WorldModel snapshot for a project. Use this when you need canonical world state as of a chapter.",
    )
    async def world_model_get(project_id: str, as_of_chapter: int | None = None):
        return await client.world_model_get(project_id=project_id, as_of_chapter=as_of_chapter)

    @register_read_tool(
        "world_page_get",
        "Get one compiled WorldModel page by page_key. Use this when you need a character, faction, region, promise, secret, or overview wiki projection.",
    )
    async def world_page_get(project_id: str, page_key: str):
        return await client.world_page_get(project_id=project_id, page_key=page_key)

    @register_read_tool(
        "world_conflict_list",
        "List deterministic WorldModel conflicts for a project. Use this when checking world-state contradictions before writing or reviewing.",
    )
    async def world_conflict_list(project_id: str):
        return WorldModelConflictListView(conflicts=await client.world_conflict_list(project_id=project_id))

    @register_read_tool(
        "world_export_obsidian",
        "Export the compiled WorldModel projection to an Obsidian vault. Use this when the user wants a read-only Markdown wiki refresh.",
    )
    async def world_export_obsidian(project_id: str, vault_root: str = ""):
        return await client.world_export_obsidian(project_id=project_id, vault_root=vault_root)

    return mcp


def build_asgi_app(
    *,
    api_client: ForWinAPIClient | None = None,
    mcp_server: FastMCP | None = None,
) -> Starlette:
    client = api_client or ForWinAPIClient(
        base_url=os.environ.get("FORWIN_API_BASE_URL", "http://127.0.0.1:8899"),
        timeout=_env_api_timeout_seconds(),
    )
    server = mcp_server or build_mcp_server(api_client=client)
    mcp_app = server.http_app(path="/mcp")

    async def health_endpoint(_request):
        try:
            await client.health()
        except Exception as exc:  # pragma: no cover - exercised in tests
            return JSONResponse(
                {"status": "degraded", "upstream": "error", "detail": str(exc)},
                status_code=503,
            )
        return JSONResponse({"status": "ok", "upstream": "ok"})

    return Starlette(
        routes=[
            Route("/health", health_endpoint),
            Mount("/", app=mcp_app),
        ],
        lifespan=mcp_app.lifespan,
    )


def _env_host() -> str:
    return str(os.environ.get("FORWIN_MCP_HOST", "0.0.0.0")).strip() or "0.0.0.0"


def _env_port() -> int:
    try:
        return int(os.environ.get("FORWIN_MCP_PORT", "8898"))
    except ValueError:
        return 8898


def _env_api_timeout_seconds() -> float:
    raw = os.environ.get("FORWIN_MCP_API_TIMEOUT_SECONDS", "300")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 300.0
    return max(30.0, value)


def main() -> None:
    uvicorn.run("forwin.mcp.http:app", host=_env_host(), port=_env_port())


mcp = build_mcp_server()
app = build_asgi_app(mcp_server=mcp)
