from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ArcWorldContract": ".world_contracts",
    "BandWorldContract": ".world_contracts",
    "ChapterWorldDeltaIntent": ".world_contracts",
    "ReaderCognitionTransition": ".world_contracts",
    "RevealLadderStep": ".world_contracts",
    "WorldContractRepository": ".world_contracts",
    "ScenarioRehearsalRepository": ".scenario_rehearsal",
    "ScenarioRehearsalRunner": ".scenario_rehearsal",
    "ScenarioTriggerContext": ".scenario_triggers",
    "ScenarioTriggerEvaluator": ".scenario_triggers",
    "FuturePlanAuditIssue": ".future_plan_auditor",
    "FuturePlanAuditRepository": ".future_plan_auditor",
    "FuturePlanAuditRun": ".future_plan_auditor",
    "FuturePlanAuditor": ".future_plan_auditor",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
