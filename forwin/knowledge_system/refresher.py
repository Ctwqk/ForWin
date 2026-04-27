from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from forwin.llm_kb import LLMKnowledgeBaseCompiler
from forwin.obsidian import ObsidianExporter


@dataclass
class KnowledgeProjectionRefreshResult:
    project_id: str
    as_of_chapter: int
    trigger: str
    ok: bool = True
    obsidian: dict[str, Any] = field(default_factory=dict)
    llm_kb: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "as_of_chapter": self.as_of_chapter,
            "trigger": self.trigger,
            "ok": self.ok,
            "obsidian": self.obsidian,
            "llm_kb": self.llm_kb,
            "errors": list(self.errors),
        }


class KnowledgeProjectionRefresher:
    """Rebuild non-canon projections after BookState canon changes.

    Projection refresh must not decide canon truth. It reads BookState canon and
    rewrites the Obsidian and LLM KB outputs as disposable projections.
    """

    def __init__(
        self,
        session: Session,
        *,
        obsidian_root: Path | None = None,
        llm_kb_root: Path | None = None,
    ) -> None:
        self.session = session
        self.obsidian_root = obsidian_root
        self.llm_kb_root = llm_kb_root

    def refresh(
        self,
        project_id: str,
        *,
        as_of_chapter: int = 0,
        trigger: str = "",
    ) -> KnowledgeProjectionRefreshResult:
        result = KnowledgeProjectionRefreshResult(
            project_id=project_id,
            as_of_chapter=int(as_of_chapter or 0),
            trigger=trigger,
        )
        try:
            obsidian = ObsidianExporter(self.session).export_project(
                project_id,
                vault_root=self.obsidian_root,
                as_of_chapter=as_of_chapter,
            )
            result.obsidian = {
                "vault_root": obsidian.vault_root,
                "exported_count": obsidian.exported_count,
                "as_of_chapter": obsidian.as_of_chapter,
            }
        except Exception as exc:  # noqa: BLE001 - projection refresh is non-canon.
            result.ok = False
            result.errors.append(f"obsidian: {exc}")

        try:
            kb = LLMKnowledgeBaseCompiler(self.session, root=self.llm_kb_root).rebuild(
                project_id,
                as_of_chapter=as_of_chapter,
            )
            result.llm_kb = {
                "root": kb.root,
                "files": list(kb.files),
                "source_digest": kb.source_digest,
                "as_of_chapter": kb.as_of_chapter,
                "vector_index": kb.vector_index,
            }
        except Exception as exc:  # noqa: BLE001 - projection refresh is non-canon.
            result.ok = False
            result.errors.append(f"llm_kb: {exc}")

        return result
