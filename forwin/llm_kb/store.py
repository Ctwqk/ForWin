from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_LLM_KB_ROOT = Path("data/llm_kb")

ROOT_FILE_KEYS = {
    "CURRENT_STATE.md",
    "NEXT_CHAPTER_CONTEXT.md",
    "ACTIVE_THREADS.md",
    "CHARACTER_MEMORY.md",
    "FACTION_MEMORY.md",
    "MAP_CONTEXT.md",
    "READER_PROMISES.md",
    "KNOWLEDGE_GAPS.md",
    "REVEAL_LADDER.md",
    "MUST_NOT_REVEAL.md",
    "RECENT_CHANGES.md",
    "STYLE_AND_TONE.md",
    "CONSTRAINTS.md",
    "facts.jsonl",
    "events.jsonl",
    "graph_deltas.jsonl",
    "open_questions.jsonl",
    "retrieval_index.json",
}


class LLMKnowledgeBaseStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DEFAULT_LLM_KB_ROOT

    def project_root(self, project_id: str) -> Path:
        return self.root / project_id

    def list_files(self, project_id: str) -> list[dict[str, Any]]:
        root = self.project_root(project_id)
        if not root.exists():
            return []
        files = []
        for key in sorted(ROOT_FILE_KEYS):
            path = root / key
            if path.exists() and path.is_file():
                stat = path.stat()
                files.append({"file_key": key, "size": stat.st_size, "updated_at": stat.st_mtime})
        return files

    def read_file(self, project_id: str, file_key: str) -> str:
        key = str(file_key or "").strip().replace("\\", "/")
        if key not in ROOT_FILE_KEYS:
            raise ValueError("file_key is not in the LLM KB allowlist")
        path = self.project_root(project_id) / key
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(key)
        return path.read_text(encoding="utf-8")
