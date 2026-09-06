from __future__ import annotations

import hashlib
from pathlib import Path


APP_ROOT = Path("/app")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one matching block in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def assert_base(path: Path, expected_sha256: str) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(f"unexpected runtime base for {path}: {actual}")


def main() -> None:
    context_path = APP_ROOT / "forwin/context/assembler_core/canon_quality_context.py"
    constraints_path = APP_ROOT / "forwin/writer/prompt_core/constraints.py"
    assert_base(
        context_path,
        "69a4c1dc41230add3f01ced181a45a5b3a68d40b9f6a090e5d5e4f7dec7ad3f6",
    )
    assert_base(
        constraints_path,
        "e8ce62a1e20b4f59abd3db1b441488e482775bcd7e414688d4af5b7b3920eec7",
    )
    replace_once(
        context_path,
        "        invariant_constraints = _invariant_constraints_from_countdowns(countdown_constraints)\n",
        "        invariant_constraints = [\n"
        "            *_invariant_constraints_from_countdowns(countdown_constraints),\n"
        "            *_accepted_future_chapter_anchor_constraints(\n"
        "                session=session,\n"
        "                project_id=project_id,\n"
        "                chapter_number=int(chapter_number or 0),\n"
        "            ),\n"
        "        ]\n",
    )
    replace_once(
        context_path,
        "\n\ndef _invariant_status(value: Any) -> str:\n",
        '''


def _accepted_future_chapter_anchor_constraints(
    *,
    session,
    project_id: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ChapterPlan, ChapterDraft)
        .join(
            CandidateDraftRecord,
            CandidateDraftRecord.chapter_plan_id == ChapterPlan.id,
        )
        .join(
            ChapterDraft,
            ChapterDraft.id == CandidateDraftRecord.candidate_draft_id,
        )
        .where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.status == "accepted",
            CandidateDraftRecord.canon_status == "canon",
            ChapterPlan.chapter_number > int(chapter_number or 0),
        )
        .order_by(ChapterPlan.chapter_number.asc())
        .limit(3)
    ).all()
    anchors: list[dict[str, Any]] = []
    for plan, draft in rows:
        accepted_chapter = int(plan.chapter_number or 0)
        if accepted_chapter <= 0:
            continue
        anchors.append(
            {
                "invariant_key": f"accepted_future_chapter:{accepted_chapter}",
                "kind": "accepted_future_anchor",
                "subject_key": f"chapter:{accepted_chapter}",
                "label": f"第{accepted_chapter}章已接受结果",
                "current_value": {
                    "chapter_number": accepted_chapter,
                    "title": str(plan.title or "").strip(),
                    "summary": str(draft.summary or "").strip(),
                },
                "status": "accepted",
                "latest_chapter": accepted_chapter,
                "constraints": {
                    "immutable_definition": True,
                    "historical_rewrite_anchor": True,
                    "must_remain_compatible": True,
                },
                "evidence_refs": [f"accepted_chapter:{accepted_chapter}"],
                "source": "accepted_future_chapter",
            }
        )
    return anchors


def _invariant_status(value: Any) -> str:
''',
    )
    replace_once(
        constraints_path,
        "    for item in items[:8]:\n",
        "    selected = list(items[:8])\n"
        "    selected_keys = {\n"
        "        str(item.get(\"invariant_key\") or \"\").strip()\n"
        "        for item in selected\n"
        "        if str(item.get(\"invariant_key\") or \"\").strip()\n"
        "    }\n"
        "    for item in items[8:]:\n"
        "        invariant_key = str(item.get(\"invariant_key\") or \"\").strip()\n"
        "        kind = str(item.get(\"kind\") or \"\")\n"
        "        if invariant_key in selected_keys:\n"
        "            continue\n"
        "        if kind == \"accepted_future_anchor\" or (\n"
        "            kind == \"active_rule\"\n"
        "            and bool((item.get(\"constraints\") or {}).get(\"immutable_definition\"))\n"
        "        ):\n"
        "            selected.append(item)\n"
        "            selected_keys.add(invariant_key)\n"
        "    for item in selected:\n",
    )
    replace_once(
        constraints_path,
        '''        if kind == "active_rule":
            lines.append(
                f"    · {label}：当前 active rule 仍生效；本章必须遵守规则边界，撤销或豁免需要正文证据。"
            )
            continue
''',
        '''        if kind == "active_rule":
            lines.append(
                f"    · {label}：当前 active rule 仍生效；本章必须遵守规则边界，撤销或豁免需要正文证据。"
            )
            continue
        if kind == "accepted_future_anchor":
            anchor = current_value if isinstance(current_value, dict) else {}
            anchor_chapter = int(anchor.get("chapter_number") or latest_chapter or 0)
            anchor_title = str(anchor.get("title") or "").strip()
            anchor_summary = str(anchor.get("summary") or "").strip()
            lines.append(
                "    · 已接受后续章节的冻结锚点："
                f"第{anchor_chapter}章 {anchor_title}；结果={anchor_summary}。"
                "这是历史章节重写：本章必须作为该结果的前置事件保持兼容，"
                "不得提前完成、重排、否定或改写锚点结果。"
            )
            continue
''',
    )


if __name__ == "__main__":
    main()
