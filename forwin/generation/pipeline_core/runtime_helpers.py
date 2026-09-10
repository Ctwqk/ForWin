from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from forwin.checker.rules import ContinuityChecker
from forwin.llm.compat import filter_supported_kwargs as _filter_supported_kwargs
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater


class RuntimeSupportStage:
    """Owns the runtime helpers stage behavior."""

    def _make_state_helpers(
        self,
        session: Session,
    ) -> tuple[StateRepository, StateUpdater, ContinuityChecker]:
        repo = StateRepository(session)
        updater = StateUpdater(session)
        checker = ContinuityChecker(
            repo,
            min_chars=self.policy.chapter_length.min_chars,
            max_chars=self.policy.chapter_length.max_chars,
        )
        return repo, updater, checker

    def _select_skill_layers(
        self,
        *,
        scope: str,
        stage_key: str,
        task_family: str,
    ) -> list[object]:
        selections = self.skill_router.select(
            scope=scope,
            stage_key=stage_key,
            task_family=task_family,
        )
        return self.skill_prompt_layer_builder.build(selections)

    def _call_with_compatible_kwargs(
        self,
        callable_obj: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return callable_obj(
            *args, **self._filter_supported_kwargs(callable_obj, kwargs)
        )

    def _save_prompt_trace_payload(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        prompt_trace: dict[str, object] | None,
        parent_trace_id: str = "",
        decision_event_id: str = "",
    ) -> str:
        return self.trace_recorder.save_prompt_trace(
            session=session,
            updater=updater,
            project_id=project_id,
            prompt_trace=prompt_trace,
            parent_trace_id=parent_trace_id,
            decision_event_id=decision_event_id,
        )

    def _record_prompt_trace_performance_spans(
        self,
        *,
        project_id: str,
        chapter_number: int,
        prompt_trace_id: str,
        trace_payload: dict[str, object],
    ) -> None:
        return self.trace_recorder.record_performance_spans(
            project_id=project_id,
            chapter_number=chapter_number,
            prompt_trace_id=prompt_trace_id,
            trace_payload=trace_payload,
        )

    @staticmethod
    def _filter_supported_kwargs(
        callable_obj: Callable[..., Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        return _filter_supported_kwargs(callable_obj, kwargs)


__all__ = ["RuntimeSupportStage"]
