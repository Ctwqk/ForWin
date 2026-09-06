from __future__ import annotations

import hashlib
from pathlib import Path


APP_ROOT = Path("/app")


def assert_base(path: Path, expected_sha256: str) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(f"unexpected runtime base for {path}: {actual}")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one matching block in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    context_path = APP_ROOT / "forwin/context/assembler_core/canon_quality_context.py"
    constraints_path = APP_ROOT / "forwin/writer/prompt_core/constraints.py"
    assert_base(
        context_path,
        "914b2072f8509f75832330595fb5f9c795998f485c0bf242d0ce1185fa060e9d",
    )
    assert_base(
        constraints_path,
        "e4b78dea848d6761d9823c669b5377442e7a3196db9137c9e6b5f73d6a03af1a",
    )
    replace_once(
        context_path,
        "from sqlalchemy import func, select\n\nfrom forwin.models.draft import CandidateDraftRecord, ChapterDraft\n",
        "from sqlalchemy import func, select\n\n"
        "from forwin.audit.events import DecisionEventType\n"
        "from forwin.models.audit import DecisionEvent\n"
        "from forwin.models.draft import CandidateDraftRecord, ChapterDraft\n",
    )
    replace_once(
        context_path,
        '        "open_signals": [],\n        "active_narrative_obligations": [],\n',
        '        "open_signals": [],\n'
        '        "operator_retry_constraints": [],\n'
        '        "active_narrative_obligations": [],\n',
    )
    replace_once(
        context_path,
        "        active_narrative_obligations = [\n",
        "        operator_retry_constraints = _operator_retry_constraints(\n"
        "            session=session,\n"
        "            project_id=project_id,\n"
        "            chapter_number=int(chapter_number or 0),\n"
        "        )\n"
        "        active_narrative_obligations = [\n",
    )
    replace_once(
        context_path,
        '            "open_signals": open_signals,\n            "active_narrative_obligations": active_narrative_obligations,\n',
        '            "open_signals": open_signals,\n'
        '            "operator_retry_constraints": operator_retry_constraints,\n'
        '            "active_narrative_obligations": active_narrative_obligations,\n',
    )
    replace_once(
        context_path,
        "    return anchors\n\n\ndef _invariant_status(value: Any) -> str:\n",
        '''    return anchors


def _operator_retry_constraints(
    *,
    session,
    project_id: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    if chapter_number <= 0:
        return []
    event = session.execute(
        select(DecisionEvent)
        .where(
            DecisionEvent.project_id == project_id,
            DecisionEvent.chapter_number == chapter_number,
            DecisionEvent.event_family == "audit_action",
            DecisionEvent.event_type == DecisionEventType.RETRY_ATTEMPT,
            DecisionEvent.actor_type.in_(("api", "manual_ui")),
        )
        .order_by(DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    reason = str(getattr(event, "reason", "") or "").strip()
    if not reason:
        return []
    return [
        {
            "chapter_number": chapter_number,
            "reason": reason,
            "event_id": str(getattr(event, "id", "") or ""),
        }
    ]


def _invariant_status(value: Any) -> str:
''',
    )
    replace_once(
        constraints_path,
        "    open_signals = [item for item in quality.get(\"open_signals\", []) or [] if isinstance(item, dict)]\n"
        "    active_obligations = [\n",
        "    open_signals = [item for item in quality.get(\"open_signals\", []) or [] if isinstance(item, dict)]\n"
        "    operator_retry_constraints = [\n"
        "        item for item in quality.get(\"operator_retry_constraints\", []) or [] if isinstance(item, dict)\n"
        "    ]\n"
        "    active_obligations = [\n",
    )
    replace_once(
        constraints_path,
        "        open_signals,\n        active_obligations,\n",
        "        open_signals,\n        operator_retry_constraints,\n        active_obligations,\n",
    )
    replace_once(
        constraints_path,
        "        *_final_chapter_constraint_section(is_final_chapter),\n"
        "        *_invariant_constraint_sections(invariant_constraints),\n",
        "        *_final_chapter_constraint_section(is_final_chapter),\n"
        "        *_operator_retry_constraint_sections(operator_retry_constraints),\n"
        "        *_invariant_constraint_sections(invariant_constraints),\n",
    )
    replace_once(
        constraints_path,
        "    ]\n\n\ndef _countdown_constraint_sections(\n",
        '''    ]


def _operator_retry_constraint_sections(items: list[dict]) -> list[ConstraintSection]:
    if not items:
        return []
    latest = items[-1]
    reason = str(latest.get("reason") or "").strip()
    if not reason:
        return []
    return [
        ConstraintSection(
            key="operator_retry",
            priority=12,
            must_inject=True,
            text="\\n".join(
                [
                    "  · 操作员定向重写约束：",
                    f"    · {reason}",
                    "    · 此约束用于本次重写，优先于旧摘要和默认章节计划；不得静默忽略或反向执行。",
                ]
            ),
        )
    ]


def _countdown_constraint_sections(
''',
    )


if __name__ == "__main__":
    main()
