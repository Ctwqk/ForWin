from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


def call_chat_compat(llm_client, messages: list[dict], **kwargs: Any) -> str:
    signature = inspect.signature(llm_client.chat)
    parameters = signature.parameters
    accepts_var_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    filtered = {
        key: value
        for key, value in kwargs.items()
        if accepts_var_kwargs or key in parameters
    }
    return llm_client.chat(messages, **filtered)


def filter_supported_kwargs(
    callable_obj: Callable[..., Any],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    target_callable = callable_obj
    side_effect = getattr(callable_obj, "side_effect", None)
    if callable(side_effect):
        target_callable = side_effect
    try:
        signature = inspect.signature(target_callable)
    except (TypeError, ValueError):
        return dict(kwargs)
    parameters = signature.parameters
    if any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    ):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in parameters}
