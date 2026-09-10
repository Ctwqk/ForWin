import inspect
from types import SimpleNamespace

import pytest

from forwin.canon.revision_model import EvidenceModel, model_identity
from forwin.llm.router import LLMCallRouter, RoutedModelAdapter
from tests.test_llm_router import FakeCodexClient, OrdinaryAdapter


def test_frozen_router_accepts_real_codex_attempt_schema():
    adapter = RoutedModelAdapter(
        LLMCallRouter(
            ordinary_adapter=OrdinaryAdapter(),
            codex_client=FakeCodexClient(),
            codex_enabled=True,
            codex_default_model="gpt-frozen",
        )
    )
    proxy = EvidenceModel(
        adapter, model_identity(SimpleNamespace(llm_client=adapter, max_tokens=8000))
    )
    proxy.chat(
        [{"role": "user", "content": "Review a frozen historical body"}],
        task_family="chapter_review_form",
        stage_key="chapter_review_form",
        max_tokens=8000,
        temperature=0.0,
    )
    assert proxy.evidence[-1]["provider"] == "codex_bridge"
    assert proxy.evidence[-1]["model"] == "gpt-frozen"
    assert proxy.evidence[-1]["requested_temperature"] == 0.0
    assert proxy.evidence[-1]["requested_max_tokens"] == 8000
    assert proxy.evidence[-1]["attempt_group_id"]


def test_generate_json_keeps_explicit_timeout_and_cannot_bypass_transport_evidence():
    class Client:
        provider = "fixture"
        model = "frozen"

        def __init__(self):
            self.llm_attempt_events = []

        def generate_json(
            self, *, messages, output_schema, temperature, max_tokens, timeout_seconds
        ):
            self.llm_attempt_events.append({"provider": "fixture", "model": "drifted"})
            return {}

    client = Client()
    proxy = EvidenceModel(
        client, model_identity(SimpleNamespace(llm_client=client, max_tokens=8000))
    )
    assert "timeout_seconds" in inspect.signature(proxy.generate_json).parameters
    with pytest.raises(ValueError, match="outside frozen routes"):
        proxy.generate_json(
            messages=[],
            output_schema={},
            temperature=0,
            max_tokens=8000,
            timeout_seconds=30,
        )
