from __future__ import annotations

import inspect
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
