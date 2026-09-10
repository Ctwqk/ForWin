from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from forwin.canon.identity import active_commit_predicate
from forwin.knowledge_system.checkpoints import (
    PROJECTION_COMPONENTS,
    ProjectionCheckpointStore,
    ProjectionEventIdentity,
    ProjectionTarget,
    sanitize_projection_error,
    session_transaction,
    validate_projection_component,
)
from forwin.llm_kb import LLMKnowledgeBaseCompiler
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan
from forwin.obsidian import ObsidianExporter

ComponentRunner = Callable[[ProjectionTarget], Any]
_MAX_TARGET_CONVERGENCE_PASSES = 8


@dataclass(frozen=True, slots=True)
class ProjectionRefreshError(RuntimeError):
    project_id: str
    target: ProjectionTarget
    failures: Mapping[str, str]
    results: Mapping[str, dict[str, Any]]

    def __str__(self) -> str:
        components = ", ".join(sorted(self.failures))
        return f"projection refresh failed for: {components}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "project_id": self.project_id,
            "target_canon_commit_id": self.target.canon_commit_id,
            "target_chapter_number": self.target.chapter_number,
            "failures": dict(self.failures),
            "partial_success": any(
                bool(result.get("ok")) for result in self.results.values()
            ),
            "components": dict(self.results),
        }


class CanonProjectionService:
    def __init__(
        self,
        session_factory: Any,
        *,
        obsidian_root: Path | None = None,
        llm_kb_root: Path | None = None,
        qdrant_url: str | None = None,
        qdrant_collection: str | None = None,
        qdrant_client: Any | None = None,
        qdrant_models: Any | None = None,
        memory_index_provider: Callable[[], Any] | None = None,
        component_runners: Mapping[str, ComponentRunner] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.checkpoints = ProjectionCheckpointStore(session_factory)
        self.obsidian_root = obsidian_root
        self.llm_kb_root = llm_kb_root
        self.qdrant_url = qdrant_url
        self.qdrant_collection = qdrant_collection
        self.qdrant_client = qdrant_client
        self.qdrant_models = qdrant_models
        self.memory_index_provider = memory_index_provider
        self.component_runners: dict[str, ComponentRunner] = {
            "obsidian": self._run_obsidian,
            "llm_kb": self._run_llm_kb,
            "chapter_memory": self._run_chapter_memory,
        }
        if component_runners:
            for kind, runner in component_runners.items():
                self.component_runners[validate_projection_component(kind)] = runner

    def refresh(
        self,
        project_id: str,
        *,
        components: Iterable[str],
        trigger: str = "",
        event_id: str = "",
        event_identity: ProjectionEventIdentity | None = None,
    ) -> dict[str, Any]:
        requested = {
            validate_projection_component(component) for component in components
        }
        ordered = [kind for kind in PROJECTION_COMPONENTS if kind in requested]
        if not ordered:
            raise ValueError("at least one projection component is required")

        target = self.checkpoints.resolve_target(
            project_id,
            event_identity=event_identity,
        )
        owner_event_id = str(event_id or trigger or "manual_projection_refresh")
        results: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}

        force = False
        for _pass_number in range(_MAX_TARGET_CONVERGENCE_PASSES):
            results = {}
            failures = {}
            for kind in ordered:
                ticket = None
                try:
                    ticket = self.checkpoints.begin_component(
                        target,
                        kind,
                        event_id=owner_event_id,
                        force=force,
                    )
                    if ticket is None:
                        results[kind] = {
                            "ok": True,
                            "skipped": True,
                            "target_chapter_number": target.chapter_number,
                        }
                        continue
                    result = _normalize_component_result(
                        self.component_runners[kind](target)
                    )
                    if result.get("ok") is False:
                        raise RuntimeError(_component_failure_message(result))
                    result["ok"] = True
                    result.setdefault("skipped", False)
                    self.checkpoints.complete_component(
                        ticket,
                        source_digest=_result_source_digest(result),
                    )
                    results[kind] = result
                except Exception as exc:  # noqa: BLE001 - aggregate components.
                    message = sanitize_projection_error(exc)
                    failures[kind] = message
                    results[kind] = {"ok": False, "error": message}
                    if ticket is not None:
                        self.checkpoints.fail_component(ticket, exc)

            latest_target = self.checkpoints.resolve_target(project_id)
            if latest_target != target:
                target = latest_target
                force = True
                continue
            break
        else:
            message = sanitize_projection_error(
                RuntimeError(
                    "authoritative Canon target did not stabilize during projection"
                )
            )
            failures = {kind: message for kind in ordered}
            results = {
                kind: {"ok": False, "error": message} for kind in ordered
            }

        if failures:
            raise ProjectionRefreshError(
                project_id=project_id,
                target=target,
                failures=failures,
                results=results,
            )
        return {
            "ok": True,
            "project_id": project_id,
            "target_canon_commit_id": target.canon_commit_id,
            "target_chapter_number": target.chapter_number,
            "components": results,
        }

    def _run_obsidian(self, target: ProjectionTarget) -> dict[str, Any]:
        with session_transaction(self.session_factory) as session:
            result = ObsidianExporter(session).export_project(
                target.project_id,
                vault_root=self.obsidian_root,
                as_of_chapter=target.chapter_number,
            )
            return {
                "ok": True,
                "vault_root": result.vault_root,
                "exported_count": result.exported_count,
                "pages": list(result.pages),
                "as_of_chapter": result.as_of_chapter,
                "source_digest": result.source_digest,
                "manifest_written": result.manifest_written,
                "deletion_enabled": result.deletion_enabled,
                "deletion_disabled_reason": result.deletion_disabled_reason,
                "deleted_files": list(result.deleted_files),
                "retained_human_modified": list(
                    result.retained_human_modified
                ),
                "retained_unsafe": list(result.retained_unsafe),
                "retired_page_count": result.retired_page_count,
            }

    def _run_llm_kb(self, target: ProjectionTarget) -> dict[str, Any]:
        with session_transaction(self.session_factory) as session:
            result = LLMKnowledgeBaseCompiler(
                session,
                root=self.llm_kb_root,
                qdrant_url=self.qdrant_url,
                qdrant_collection=self.qdrant_collection,
                qdrant_client=self.qdrant_client,
                qdrant_models=self.qdrant_models,
            ).rebuild(
                target.project_id,
                as_of_chapter=target.chapter_number,
            )
            return {
                "ok": True,
                "root": result.root,
                "files": list(result.files),
                "source_digest": result.source_digest,
                "as_of_chapter": result.as_of_chapter,
                "vector_index": dict(result.vector_index),
            }

    def _run_chapter_memory(self, target: ProjectionTarget) -> dict[str, Any]:
        with session_transaction(self.session_factory) as session:
            chapters = list(
                session.execute(
                    select(
                        ChapterPlan.chapter_number,
                        ChapterPlan.title,
                        ChapterDraft.summary,
                        ChapterDraft.body_text,
                    )
                    .select_from(CanonCommitRecord)
                    .join(
                        CandidateDraftRecord,
                        CandidateDraftRecord.id == CanonCommitRecord.candidate_id,
                    )
                    .join(
                        ChapterPlan,
                        ChapterPlan.id == CandidateDraftRecord.chapter_plan_id,
                    )
                    .join(
                        ChapterDraft,
                        ChapterDraft.id == CandidateDraftRecord.candidate_draft_id,
                    )
                    .where(
                        CanonCommitRecord.project_id == target.project_id,
                        CanonCommitRecord.status == "committed",
                    active_commit_predicate(),
                        CanonCommitRecord.chapter_number <= target.chapter_number,
                        CandidateDraftRecord.status == "accepted",
                        ChapterPlan.status == "accepted",
                    )
                    .order_by(ChapterPlan.chapter_number.asc())
                )
            )

        if chapters:
            if self.memory_index_provider is None:
                raise RuntimeError("chapter memory projection requires a memory index")
            memory_index = self.memory_index_provider()
            if memory_index is None:
                raise RuntimeError("memory index provider returned no index")
            for chapter_number, title, summary, body in chapters:
                memory_index.upsert_chapter(
                    project_id=target.project_id,
                    chapter_number=int(chapter_number),
                    title=str(title or ""),
                    summary=str(summary or ""),
                    body=str(body or ""),
                )

        digest_payload = [
            {
                "chapter_number": int(chapter_number),
                "title": str(title or ""),
                "summary": str(summary or ""),
                "body": str(body or ""),
            }
            for chapter_number, title, summary, body in chapters
        ]
        return {
            "ok": True,
            "chapter_count": len(chapters),
            "chapters": [item["chapter_number"] for item in digest_payload],
            "source_digest": _digest_payload(digest_payload),
            "as_of_chapter": target.chapter_number,
        }


def _normalize_component_result(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="python")
        if isinstance(payload, Mapping):
            return dict(payload)
    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
        if isinstance(payload, Mapping):
            return dict(payload)
    if getattr(result, "ok", None) is False:
        return {
            "ok": False,
            "error": str(getattr(result, "error", "") or "component returned ok=false"),
        }
    return {"ok": True, "result": result}


def _component_failure_message(result: Mapping[str, Any]) -> str:
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(error) for error in errors)
    return str(result.get("error") or "component returned ok=false")


def _result_source_digest(result: Mapping[str, Any]) -> str:
    source_digest = str(result.get("source_digest") or "").strip()
    return source_digest or _digest_payload(result)


def _digest_payload(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CanonProjectionService",
    "ProjectionRefreshError",
]
