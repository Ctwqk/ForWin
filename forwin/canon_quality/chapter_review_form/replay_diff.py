from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LogicalReplayRow(BaseModel):
    kind: str
    chapter_number: int
    subject: str
    fields: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.chapter_number}:{self.kind}:{self.subject}"


class ReplayDiff(BaseModel):
    kind: str
    chapter_number: int
    row_kind: str
    subject: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


def normalize_countdown_row(row: dict[str, Any]) -> LogicalReplayRow:
    payload = dict(row.get("payload") or {})
    return LogicalReplayRow(
        kind="countdown",
        chapter_number=int(row.get("chapter_number") or 0),
        subject=str(row.get("countdown_key") or row.get("subject") or ""),
        fields={
            "normalized_remaining_minutes": row.get("normalized_remaining_minutes"),
            "status": row.get("status"),
            "evidence_quote": payload.get("evidence_quote", row.get("evidence_quote", "")),
        },
    )


def normalize_character_row(row: dict[str, Any]) -> LogicalReplayRow:
    payload = dict(row.get("payload") or {})
    return LogicalReplayRow(
        kind="character_state",
        chapter_number=int(row.get("chapter_number") or 0),
        subject=str(row.get("character_name") or row.get("subject") or ""),
        fields={
            "to_state": row.get("to_state"),
            "terminality": row.get("terminality"),
            "evidence_quote": payload.get("evidence_quote", row.get("evidence_quote", "")),
            "subject_of_quote": payload.get("subject_of_quote", row.get("subject_of_quote", "")),
        },
    )


def _coerce(row: dict[str, Any]) -> LogicalReplayRow:
    if "fields" in row:
        return LogicalReplayRow(
            kind=str(row["kind"]),
            chapter_number=int(row["chapter_number"]),
            subject=str(row["subject"]),
            fields=dict(row["fields"]),
        )
    if str(row.get("kind") or "") == "character_state" or "character_name" in row:
        return normalize_character_row(row)
    return normalize_countdown_row(row)


def compute_diff(*, existing_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]) -> list[ReplayDiff]:
    existing = {_coerce(row).key: _coerce(row) for row in existing_rows}
    candidate = {_coerce(row).key: _coerce(row) for row in candidate_rows}
    diffs: list[ReplayDiff] = []
    for key in sorted(set(existing) | set(candidate)):
        before = existing.get(key)
        after = candidate.get(key)
        row = before or after
        assert row is not None
        if before is None:
            diffs.append(
                ReplayDiff(
                    kind="add",
                    chapter_number=row.chapter_number,
                    row_kind=row.kind,
                    subject=row.subject,
                    after=after.fields if after else None,
                )
            )
        elif after is None:
            diffs.append(
                ReplayDiff(
                    kind="remove",
                    chapter_number=row.chapter_number,
                    row_kind=row.kind,
                    subject=row.subject,
                    before=before.fields,
                )
            )
        elif before.fields != after.fields:
            diffs.append(
                ReplayDiff(
                    kind="change",
                    chapter_number=row.chapter_number,
                    row_kind=row.kind,
                    subject=row.subject,
                    before=before.fields,
                    after=after.fields,
                )
            )
    return diffs
