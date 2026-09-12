"""Shared Writer failure classification, including terminal accepted-input errors."""

from __future__ import annotations


def terminal_input_category(exc: BaseException) -> str:
    # Local imports avoid the config -> Writer -> retrieval -> Canon import cycle.
    from forwin.retrieval.source_identity import CanonBaselineChanged
    from forwin.retrieval.requirements import RequiredContextError

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "error_category", None) == "input_limit":
            return "input_limit"
        if isinstance(current, RequiredContextError):
            return "required_context"
        if isinstance(current, CanonBaselineChanged):
            return "canon_baseline_changed"
        current = current.__cause__ or current.__context__
    return ""


def is_timeout_like(exc: Exception) -> bool:
    if terminal_input_category(exc):
        return False
    message = str(exc).lower()
    return any(
        token in message
        for token in ("timed out", "timeout", "read operation timed out")
    )


def is_transient_llm_like(exc: Exception) -> bool:
    if terminal_input_category(exc):
        return False
    current: BaseException | None = exc
    while current is not None:
        message = str(current).lower()
        compact = " ".join(message.split())
        if any(
            token in compact
            for token in (
                "http 529",
                "status code 529",
                "529 unknown status code",
                "429",
                "500",
                "502",
                "503",
                "504",
                "temporarily unavailable",
                "service unavailable",
                "rate limit",
                "too many requests",
                "overloaded",
                "connection reset",
                "connection refused",
                "remoteprotocolerror",
                "server disconnected",
                "network error",
                "timed out",
                "timeout",
                "read operation timed out",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def transient_retry_delay(attempt: int) -> float:
    return min(20.0, 3.0 * (2 ** max(0, attempt - 1)))


def error_category_from_attempts(
    attempts: list[dict[str, object]], exc: BaseException
) -> str:
    if category := terminal_input_category(exc):
        return category
    for attempt in reversed(attempts):
        category = str(attempt.get("error_category") or "").strip()
        if category and category != "unknown":
            return category
    message = str(exc).lower()
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "429" in message or "rate limit" in message:
        return "rate_limit"
    if any(
        token in message for token in ("529", "500", "502", "503", "504", "overload")
    ):
        return "provider_overload"
    if "400" in message or "bad request" in message:
        return "bad_request"
    if "parse" in message or "json" in message or "schema" in message:
        return "parse_or_schema"
    return "unknown"


def diagnostic_kind_for_failure(exc: BaseException, error_category: str) -> str:
    message = str(exc).lower()
    if error_category == "bad_request" or "400" in message or "bad request" in message:
        return "provider_bad_request"
    if error_category == "parse_or_schema" or any(
        token in message for token in ("parse", "json", "schema")
    ):
        return "parse_or_schema_failure"
    return "writer_failure_without_draft"


class TransientLLMChapterFailure(RuntimeError):
    """Current chapter failed because the upstream LLM looked temporarily unavailable."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause
