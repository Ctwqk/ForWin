class QualityDiagnosticsStage:
    """Task-level diagnostic identity retained by the sequence coordinator."""

    def _audit_operation_id(self) -> str:
        return str(self._audit_task_id or self._audit_root_event_id or "").strip()
