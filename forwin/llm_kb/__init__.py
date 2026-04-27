from __future__ import annotations

from typing import Any

__all__ = [
    "LLMKBCompileResult",
    "LLMKnowledgeBaseCompiler",
    "LLMKnowledgeBaseRetriever",
    "LLMKnowledgeBaseStore",
]


def __getattr__(name: str) -> Any:
    if name in {"LLMKBCompileResult", "LLMKnowledgeBaseCompiler"}:
        from .compiler import LLMKBCompileResult, LLMKnowledgeBaseCompiler

        return {
            "LLMKBCompileResult": LLMKBCompileResult,
            "LLMKnowledgeBaseCompiler": LLMKnowledgeBaseCompiler,
        }[name]
    if name == "LLMKnowledgeBaseStore":
        from .store import LLMKnowledgeBaseStore

        return LLMKnowledgeBaseStore
    if name == "LLMKnowledgeBaseRetriever":
        from .retriever import LLMKnowledgeBaseRetriever

        return LLMKnowledgeBaseRetriever
    raise AttributeError(name)
