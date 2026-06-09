from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_schema_ownership_doc_defines_owner_domains_and_split_rules() -> None:
    doc = _read("docs/operations/forwin-schema-ownership.md")

    required_phrases = [
        "Logical Schema Ownership",
        "shared production Postgres/Qdrant/MinIO layer on 150",
        "generation task state",
        "BookState/canon state",
        "review/governance state",
        "publisher runtime state",
        "knowledge/projection state",
        "observability/artifact state",
        "outbox event state",
        "MCP workflow state",
        "Physical Database Split Candidates",
        "separate design is required before any physical database split",
        "Do not create physical split migrations in this phase",
    ]
    for phrase in required_phrases:
        assert phrase in doc

    split_order = [
        "1. publisher runtime data",
        "2. observability/artifact metadata",
        "3. knowledge index metadata",
        "4. generation task history",
        "5. BookState/canon",
    ]
    cursor = -1
    for phrase in split_order:
        index = doc.index(phrase)
        assert index > cursor
        cursor = index


def test_outbox_event_model_is_owned_by_outbox_and_approved_adapters() -> None:
    allowed_exact = {
        "forwin/models/__init__.py",
        "forwin/models/outbox.py",
        "forwin/knowledge_system/projection_jobs.py",
    }
    allowed_prefixes = ("forwin/outbox/",)

    offenders: list[str] = []
    for path in sorted((ROOT / "forwin").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "OutboxEvent" not in source and "forwin.models.outbox" not in source:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in allowed_exact:
            continue
        if any(relative.startswith(prefix) for prefix in allowed_prefixes):
            continue
        offenders.append(relative)

    assert offenders == []
