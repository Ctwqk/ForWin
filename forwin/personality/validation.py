from __future__ import annotations

from typing import Any

from .assignment import PersonalityLoadoutAssigner
from .models import PersonalityAssignmentValidationReport


def validate_personality_loadout(loadout: dict[str, Any]) -> PersonalityAssignmentValidationReport:
    return PersonalityLoadoutAssigner().validate(loadout)
