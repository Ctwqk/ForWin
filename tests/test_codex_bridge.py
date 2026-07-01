from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from forwin.codex_bridge.http import build_app
from forwin.codex_bridge.runner import CodexExecRequest, CodexExecResult, CodexExecRunner
from forwin.llm.codex_client import CodexBridgeClient
from forwin.llm.router import LLMCallIntent
from forwin.writer.chapter_writer import ChapterWriter


class FakeCodexRunner:
    def __init__(self, *, content: str | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.content = content or json.dumps({"answer": "桥接成功"}, ensure_ascii=False)

    def health(self) -> dict[str, object]:
        return {"available": True, "version": "codex-cli-test"}

    def run(self, request, *, timeout_seconds: float | None = None) -> CodexExecResult:
        self.calls.append(
            {
                "prompt": request.prompt,
                "output_schema": request.output_schema,
                "timeout_seconds": timeout_seconds,
            }
        )
        return CodexExecResult(
            ok=True,
            content=self.content,
            raw_events=[{"type": "message"}],
            returncode=0,
        )


class FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    def post(self, url: str, *, headers=None, json=None) -> FakeHttpResponse:  # noqa: ANN001
        self.posts.append({"url": url, "headers": headers, "json": json})
        return FakeHttpResponse({"ok": True, "content": '{"ok":true}'})

    def close(self) -> None:
        return None


class FakeWriterLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls.append(dict(kwargs))
        return '{"ok": true}'


class CodexBridgeTests(unittest.TestCase):
    def test_bridge_requires_bearer_token_when_configured(self) -> None:
        app = build_app(token="secret-token", runner=FakeCodexRunner())
        client = TestClient(app)

        response = client.post(
            "/v1/codex/chat",
            json={"prompt": "ping", "output_schema": {"type": "object"}},
        )

        self.assertEqual(response.status_code, 401)

    def test_sync_chat_invokes_codex_runner_and_returns_content(self) -> None:
        runner = FakeCodexRunner()
        app = build_app(token="secret-token", runner=runner)
        client = TestClient(app)

        response = client.post(
            "/v1/codex/chat",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "prompt": "请只返回 JSON",
                "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
                "timeout_seconds": 3,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["content"], '{"answer": "桥接成功"}')
        self.assertEqual(payload["backend"], "codex_bridge")
        self.assertEqual(runner.calls[0]["timeout_seconds"], 3)

    def test_async_job_lifecycle_records_result(self) -> None:
        app = build_app(token="", runner=FakeCodexRunner())
        client = TestClient(app)

        submitted = client.post(
            "/v1/codex/jobs",
            json={"prompt": "job", "output_schema": {"type": "object"}},
        )

        self.assertEqual(submitted.status_code, 200)
        job_id = submitted.json()["job_id"]
        result = client.get(f"/v1/codex/jobs/{job_id}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "succeeded")
        self.assertEqual(result.json()["result"]["content"], '{"answer": "桥接成功"}')

    def test_async_job_marks_invalid_schema_output_failed(self) -> None:
        app = build_app(token="", runner=FakeCodexRunner(content="not json"))
        client = TestClient(app)

        submitted = client.post(
            "/v1/codex/jobs",
            json={"prompt": "job", "output_schema": {"type": "object"}},
        )

        self.assertEqual(submitted.status_code, 200)
        job_id = submitted.json()["job_id"]
        result = client.get(f"/v1/codex/jobs/{job_id}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "failed")
        self.assertIn("schema_parse_failed", result.json()["error"])

    def test_codex_runner_uses_current_cli_approval_config(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["input"] = kwargs.get("input")
            schema_index = cmd.index("--output-schema") + 1
            with open(cmd[schema_index], encoding="utf-8") as handle:
                captured["schema"] = json.load(handle)
            output_index = cmd.index("--output-last-message") + 1
            with open(cmd[output_index], "w", encoding="utf-8") as handle:
                handle.write('{"ok": true}')
            return SimpleNamespace(returncode=0, stdout='{"type":"done"}\n', stderr="")

        runner = CodexExecRunner(default_cwd=".")
        with patch("forwin.codex_bridge.runner.subprocess.run", side_effect=fake_run):
            result = runner.run(CodexExecRequest(prompt="ping", output_schema={"type": "object"}))

        cmd = captured["cmd"]
        self.assertTrue(result.ok)
        self.assertIn("-c", cmd)
        self.assertIn('approval_policy="never"', cmd)
        self.assertNotIn("--ask-for-approval", cmd)
        self.assertEqual(captured["input"], "ping")
        self.assertEqual(captured["schema"]["additionalProperties"], False)

    def test_codex_client_does_not_send_generic_object_schema(self) -> None:
        fake_http = FakeHttpClient()
        with patch("forwin.llm.codex_client.httpx.Client", return_value=fake_http):
            client = CodexBridgeClient(bridge_url="http://bridge")
            client.chat(
                [{"role": "user", "content": "只输出 JSON"}],
                intent=LLMCallIntent(
                    task_family="writer",
                    stage_key="state_event_extraction",
                    output_schema={"type": "object"},
                ),
                response_format={"type": "json_object"},
            )

        request_json = fake_http.posts[0]["json"]
        self.assertIsInstance(request_json, dict)
        self.assertIsNone(request_json["output_schema"])
        self.assertIn("JSON mode", request_json["prompt"])

    def test_codex_client_sends_shaped_schema(self) -> None:
        fake_http = FakeHttpClient()
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        with patch("forwin.llm.codex_client.httpx.Client", return_value=fake_http):
            client = CodexBridgeClient(bridge_url="http://bridge")
            client.chat(
                [{"role": "user", "content": "只输出 JSON"}],
                intent=LLMCallIntent(
                    task_family="writer",
                    stage_key="state_event_extraction",
                    output_schema=schema,
                ),
                response_format={"type": "json_object"},
            )

        request_json = fake_http.posts[0]["json"]
        self.assertIsInstance(request_json, dict)
        self.assertEqual(request_json["output_schema"], schema)

    def test_codex_client_sends_requested_model(self) -> None:
        fake_http = FakeHttpClient()
        with patch("forwin.llm.codex_client.httpx.Client", return_value=fake_http):
            client = CodexBridgeClient(bridge_url="http://bridge")
            client.chat(
                [{"role": "user", "content": "review"}],
                intent=LLMCallIntent(task_family="reviewer", stage_key="chapter_review"),
                model="gpt-5.3-codex-spark",
            )

        request_json = fake_http.posts[0]["json"]
        self.assertIsInstance(request_json, dict)
        self.assertEqual(request_json["model"], "gpt-5.3-codex-spark")

    def test_chapter_writer_does_not_invent_generic_output_schema(self) -> None:
        llm = FakeWriterLLM()
        writer = ChapterWriter(llm)

        writer._call_chat(
            [{"role": "user", "content": "只输出 JSON"}],
            temperature=0.1,
            max_tokens=100,
            response_format={"type": "json_object"},
            stage_key="state_event_extraction",
        )

        self.assertNotIn("output_schema", llm.calls[0])


if __name__ == "__main__":
    unittest.main()
