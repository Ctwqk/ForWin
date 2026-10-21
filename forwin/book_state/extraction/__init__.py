"""BookState candidate extraction and deterministic admission review owner."""

from importlib import import_module


_EXPORT_MODULES = {
    "ChapterDeltaExtraction": ".chapter_delta_schema",
    "ExtractionIssue": ".chapter_delta_schema",
    "ChapterDeltaTranslator": ".translator",
    "TranslationResult": ".translator",
    "ChapterWorldReview": ".world_review",
    "review_chapter_extraction": ".world_review",
    "ChapterExtractionInput": ".chapter_input",
    "build_chapter_extraction_input": ".chapter_input",
    "normalize_for_evidence": ".evidence",
    "evidence_in_body": ".evidence",
    "validate_evidence": ".evidence",
    "dropped_ratio": ".evidence",
}


def __getattr__(name: str):
    # WriterOutput uses the schema without importing its own extraction consumers.
    if name not in _EXPORT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORT_MODULES[name], __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "ChapterDeltaExtraction",
    "ExtractionIssue",
    "ChapterDeltaTranslator",
    "TranslationResult",
    "ChapterWorldReview",
    "review_chapter_extraction",
    "ChapterExtractionInput",
    "build_chapter_extraction_input",
    "normalize_for_evidence",
    "evidence_in_body",
    "validate_evidence",
    "dropped_ratio",
]
