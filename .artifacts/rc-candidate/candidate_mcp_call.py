#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


MCP_URL = "http://127.0.0.1:18897/mcp"


def direct_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        auth=auth,
        trust_env=False,
        follow_redirects=False,
    )


def direct_mcp_client(url: str, *, timeout: int = 900) -> Client:
    transport = StreamableHttpTransport(
        url,
        httpx_client_factory=direct_mcp_http_client,
    )
    return Client(transport, timeout=timeout)


def result_payload(result: Any) -> Any:
    if result.structured_content is not None:
        return result.structured_content
    if result.data is not None:
        payload = getattr(result.data, "root", result.data)
        return (
            payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
    for content in result.content:
        raw = getattr(content, "text", None)
        if raw:
            return json.loads(raw)
    raise RuntimeError("candidate MCP returned no payload")


async def call(tool_name: str, arguments: dict[str, Any]) -> Any:
    async with direct_mcp_client(MCP_URL) as client:
        result = await client.call_tool(tool_name, arguments)
    return result_payload(result)


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tool_name")
    parser.add_argument("arguments_json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    arguments = json.loads(args.arguments_json)
    if not isinstance(arguments, dict):
        raise ValueError("arguments_json must decode to an object")
    body = json.dumps(
        asyncio.run(call(args.tool_name, arguments)),
        ensure_ascii=False,
        indent=2,
        default=str,
    ) + "\n"
    if args.output is not None:
        atomic_write(args.output.resolve(), body)
        print(args.output.resolve())
    else:
        print(body, end="")


if __name__ == "__main__":
    main()
