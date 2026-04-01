from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from forwin.protocol.writer import WriterOutput


class ArtifactStore:
    """Stores writer artifacts under a project-scoped namespace."""

    def __init__(self, root_dir: str = "data/artifacts") -> None:
        self.root_dir = Path(root_dir)

    def save_writer_output(
        self,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
    ) -> dict[str, str]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        chapter_root = (
            self.root_dir / "projects" / project_id / "chapters" / str(chapter_number)
        )
        draft_dir = chapter_root / "drafts"
        meta_dir = chapter_root / "meta"
        draft_dir.mkdir(parents=True, exist_ok=True)
        meta_dir.mkdir(parents=True, exist_ok=True)

        draft_path = draft_dir / f"{timestamp}.txt"
        meta_path = meta_dir / f"{timestamp}.json"

        draft_path.write_text(writer_output.body, encoding="utf-8")
        meta_path.write_text(
            json.dumps(writer_output.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return {
            "draft_blob_path": str(draft_path),
            "meta_path": str(meta_path),
        }
