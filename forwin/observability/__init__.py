from .context import OperationContext
from .llm_trace import (
    build_llm_decision_event_payloads,
    mark_latest_attempt_parse_failure,
    prepare_prompt_trace_payload,
)
from .payloads import AUDIT_SCHEMA_VERSION, audit_payload, event_error_payload
from .ports import NullObservability, ObservabilityPort, SpanHandle
from .redaction import redact_payload, stack_hash

__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "build_llm_decision_event_payloads",
    "LogRecorder",
    "mark_latest_attempt_parse_failure",
    "NullObservability",
    "OperationContext",
    "ObservabilityPort",
    "ObservabilityService",
    "prepare_prompt_trace_payload",
    "SpanHandle",
    "audit_payload",
    "event_error_payload",
    "redact_payload",
    "stack_hash",
]


def __getattr__(name: str):
    if name == "LogRecorder":
        from .recorder import LogRecorder

        return LogRecorder
    if name == "ObservabilityService":
        from .service import ObservabilityService

        return ObservabilityService
    raise AttributeError(name)
