from __future__ import annotations

from pydantic import BaseModel, Field


class CharacterIntegrityIssue(BaseModel):
    code: str
    severity: str = "error"
    message: str = ""
    character_id: str = ""


class CharacterIntegrityReport(BaseModel):
    ok: bool = True
    errors: list[CharacterIntegrityIssue] = Field(default_factory=list)
    warnings: list[CharacterIntegrityIssue] = Field(default_factory=list)
    affected_character_ids: list[str] = Field(default_factory=list)


def failed_integrity(code: str, *, message: str, character_id: str = "") -> CharacterIntegrityReport:
    issue = CharacterIntegrityIssue(code=code, message=message, character_id=character_id)
    return CharacterIntegrityReport(ok=False, errors=[issue], affected_character_ids=[character_id] if character_id else [])
