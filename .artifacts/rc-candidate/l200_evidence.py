#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import ipaddress
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import quote
from uuid import UUID

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forwin.mcp.models import (  # noqa: E402
    CostLedgerReportView,
    GateLedgerReportView,
    RuleProvenanceReportView,
)

RC_COLLECTOR_PATH = Path(__file__).with_name("collect_rc_manifest.py").resolve()
DEFAULT_OUTPUT = ROOT / ".artifacts/v5-l200"
HOST_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
    }
)
CONTROL_ENV_KEYS = frozenset(
    {
        "COMPOSE_DISABLE_ENV_FILE",
        "COMPOSE_ENV_FILES",
        "COMPOSE_FILE",
        "COMPOSE_PATH_SEPARATOR",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
)
CHECKPOINTS = tuple(range(25, 201, 25))
GENESIS_STAGES = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)
EXPECTED_PROJECTIONS = ("obsidian", "llm_kb", "chapter_memory")
EXPECTED_OUTBOX_TYPES = (
    "canon.projection.requested",
    "canon.phase3.requested",
    "canon.publisher.requested",
)
EXPECTED_MAINTENANCE_STEPS = ("planning", "arc", "world", "feedback")
CHAPTER_CSV_FIELDS = (
    "chapter_number",
    "title",
    "status",
    "char_count",
    "acceptance_mode",
    "repair_attempt_count",
    "canon_risk_level",
    "latest_repair_scope",
)
FINAL_ARTIFACT_NAMES = (
    "chapter-status.csv",
    "gate-ledger.json",
    "cost-report.json",
    "rule-provenance.json",
    "canon-integrity.json",
    "task-recovery.json",
    "projection-publisher.json",
    "final-report.md",
)
EXPECTED_LLM_KB_MARKDOWN_FILES = (
    "CURRENT_STATE.md",
    "NEXT_CHAPTER_CONTEXT.md",
    "ACTIVE_THREADS.md",
    "CHARACTER_MEMORY.md",
    "FACTION_MEMORY.md",
    "MAP_CONTEXT.md",
    "READER_PROMISES.md",
    "KNOWLEDGE_GAPS.md",
    "REVEAL_LADDER.md",
    "MUST_NOT_REVEAL.md",
    "RECENT_CHANGES.md",
    "STYLE_AND_TONE.md",
    "CONSTRAINTS.md",
)
EXPECTED_LLM_KB_JSONL_FILES = (
    "facts.jsonl",
    "events.jsonl",
    "graph_deltas.jsonl",
    "open_questions.jsonl",
)


def direct_sync_client(*, timeout: Any) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )


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


def command_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    control = sorted(
        key for key, value in values.items() if value and key in CONTROL_ENV_KEYS
    )
    if control:
        raise EvidenceError(
            "L200 control environment is not allowed: "
            + ", ".join(control)
        )
    return {
        key: value
        for key, value in values.items()
        if key in HOST_ENV_ALLOWLIST
    }
EXPECTED_LLM_KB_FILES = frozenset(
    (
        *EXPECTED_LLM_KB_MARKDOWN_FILES,
        *EXPECTED_LLM_KB_JSONL_FILES,
        "retrieval_index.json",
    )
)
TERMINAL_GENERATION_STATUSES = {
    "completed",
    "partial_failed",
    "failed",
    "needs_review",
    "cancelled",
    "canceled",
    "paused",
}
EXPECTED_RUNTIME_SERVICES = frozenset(
    {"forwin", "generation-worker", "outbox-worker", "forwin-mcp", "publisher-worker"}
)
EXPECTED_BROWSER_SERVICES = frozenset({"publisher-browser"})
EXPECTED_DEPENDENCY_SERVICES = frozenset({"postgres", "qdrant", "minio"})
CANDIDATE_VOLUME_DESTINATIONS = {
    "forwin": frozenset({"/app/data"}),
    "generation-worker": frozenset({"/app/data"}),
    "outbox-worker": frozenset({"/app/data"}),
    "forwin-mcp": frozenset(),
    "publisher-worker": frozenset({"/app/data"}),
    "publisher-browser": frozenset({"/app/data"}),
    "postgres": frozenset({"/var/lib/postgresql/data"}),
    "qdrant": frozenset({"/qdrant/storage"}),
    "minio": frozenset({"/data"}),
}
PUBLISHER_SETTLED_STATUSES = frozenset(
    {"scheduled", "succeeded", "cancelled", "canceled"}
)
_CREDENTIAL_URL = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s]+)@"
)
_AUTH_HEADER_RE = re.compile(
    r"(?im)^(\s*(?:proxy-)?authorization\s*:\s*)"
    r"(?:bearer|basic)\s+[^\r\n]+"
)
_COOKIE_HEADER_RE = re.compile(
    r"(?im)^(\s*(?:set-)?cookie\s*:\s*)[^\r\n]*"
)
_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
)
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"token|password|secret|session|cookie)=)[^&#\s]*"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"authorization|password|private[_-]?key|secret|session[_-]?key)"
    r"\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&#]+)"
)
_SECRET_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "database_url",
    "password",
    "private_key",
    "secret",
    "session_key",
    "access_token",
    "auth_token",
)


class EvidenceError(RuntimeError):
    pass


class FreezeViolation(EvidenceError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def projection_source_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def llm_kb_source_digest(engine: Any, project_id: str, as_of_chapter: int) -> str:
    from sqlalchemy.orm import Session

    from forwin.book_state.projection import BookStateProjection
    from forwin.book_state.repository import BookStateRepository

    with Session(bind=engine) as session:
        runtime = BookStateProjection(session).load_runtime_as_of(
            project_id,
            as_of_chapter=as_of_chapter,
        )
        nodes = list(runtime.world.nodes_by_id.values())
        facts = list(runtime.world.facts_by_id.values())
        safe_nodes = [
            node
            for node in nodes
            if not (
                str(node.status).lower()
                in {"hidden", "secret", "must_not_reveal"}
                or {
                    str(tag).lower() for tag in node.tags
                }.intersection({"hidden", "secret", "must_not_reveal"})
                or str(
                    (
                        node.metadata
                        if isinstance(node.metadata, dict)
                        else {}
                    ).get("visibility", "")
                ).lower()
                in {"hidden", "secret", "must_not_reveal"}
            )
        ]
        safe_facts = [
            fact
            for fact in facts
            if str(fact.sensitivity_level).lower()
            not in {"hidden", "secret", "must_not_reveal"}
        ]
        deltas = []
        for delta in BookStateRepository(session).list_graph_deltas(
            project_id,
            after_chapter=-1,
            through_chapter=as_of_chapter,
        ):
            metadata = delta.metadata if isinstance(delta.metadata, dict) else {}
            marker = " ".join(
                str(metadata.get(key, ""))
                for key in (
                    "visibility",
                    "sensitivity",
                    "sensitivity_level",
                    "role",
                )
            )
            if not any(
                token in marker.lower()
                for token in ("hidden", "secret", "must_not_reveal")
            ):
                deltas.append(delta)
    payload = {
        "as_of_chapter": as_of_chapter,
        "nodes": [node.model_dump(mode="json") for node in safe_nodes],
        "facts": [fact.model_dump(mode="json") for fact in safe_facts],
        "deltas": [
            delta.model_dump(mode="json")
            for delta in deltas[-20:]
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def chapter_memory_point_id(project_id: str, chapter_number: int) -> str:
    digest = hashlib.sha1(
        f"{project_id}:{chapter_number}".encode("utf-8")
    ).hexdigest()[:32]
    return str(UUID(digest))


def llm_kb_vector_payload_hashes(
    project_root: Path,
    *,
    project_id: str,
    source_digest: str,
    target_chapter: int,
) -> dict[str, str]:
    import forwin.retrieval  # noqa: F401 - establish package imports first.
    from forwin.llm_kb.vector_index import (
        _collect_project_sections,
        _desired_section_payload,
        _point_id,
    )

    sections = _collect_project_sections(
        project_root,
        source_digest=source_digest,
        as_of_chapter=target_chapter,
        projection_version="llm_kb_v2",
    )
    hashes: dict[str, str] = {}
    for section in sections:
        point_id = _point_id(
            project_id,
            section["file_key"],
            section["section_key"],
            section["role_scope"],
        )
        if point_id in hashes:
            raise EvidenceError(
                f"duplicate deterministic LLM KB point identity: {point_id}"
            )
        hashes[point_id] = canonical_hash(
            _desired_section_payload(project_id, section)
        )
    return hashes


def qdrant_projection_violations(
    *,
    chapter_points: dict[str, dict[str, Any]],
    llm_kb_points: dict[str, dict[str, Any]],
    expected_chapter_hashes: dict[str, str],
    expected_llm_kb_hashes: dict[str, str],
) -> list[str]:
    violations: list[str] = []
    if set(chapter_points) != set(expected_chapter_hashes):
        violations.append("chapter_memory Qdrant point set mismatch")
    for point_id in sorted(
        set(chapter_points).intersection(expected_chapter_hashes)
    ):
        if canonical_hash(chapter_points[point_id]) != str(
            expected_chapter_hashes[point_id]
        ):
            violations.append(
                f"chapter_memory Qdrant payload mismatch: {point_id}"
            )
    if set(llm_kb_points) != set(expected_llm_kb_hashes):
        violations.append("llm_kb Qdrant point set mismatch")
    for point_id in sorted(
        set(llm_kb_points).intersection(expected_llm_kb_hashes)
    ):
        if canonical_hash(llm_kb_points[point_id]) != str(
            expected_llm_kb_hashes[point_id]
        ):
            violations.append(
                f"llm_kb Qdrant payload mismatch: {point_id}"
            )
    return violations


def scroll_qdrant_points(
    base_url: str,
    *,
    collection: str,
    project_id: str,
    client: Any | None = None,
) -> dict[str, dict[str, Any]]:
    if not collection or not project_id:
        raise EvidenceError("Qdrant projection identity is incomplete")
    owns_client = client is None
    http = client or direct_sync_client(timeout=60)
    points: dict[str, dict[str, Any]] = {}
    offset: Any | None = None
    seen_offsets: set[str] = set()
    try:
        while True:
            request = {
                "filter": {
                    "must": [
                        {
                            "key": "project_id",
                            "match": {"value": project_id},
                        }
                    ]
                },
                "limit": 256,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                request["offset"] = offset
            try:
                response = http.post(
                    f"{base_url.rstrip('/')}/collections/"
                    f"{quote(collection, safe='')}/points/scroll",
                    json=request,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise EvidenceError(
                    "Qdrant scroll failed: "
                    f"{redact_text(str(exc))}"
                ) from exc
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict) or not isinstance(
                result.get("points"), list
            ):
                raise EvidenceError("Qdrant scroll returned an invalid payload")
            for point in result["points"]:
                if not isinstance(point, dict):
                    raise EvidenceError("Qdrant point payload is invalid")
                point_id = str(point.get("id") or "")
                point_payload = point.get("payload")
                if (
                    not point_id
                    or not isinstance(point_payload, dict)
                    or point_id in points
                ):
                    raise EvidenceError(
                        "Qdrant point identity is empty, duplicate, or invalid"
                    )
                points[point_id] = point_payload
            offset = result.get("next_page_offset")
            if offset is None:
                return points
            offset_key = canonical_hash(offset)
            if offset_key in seen_offsets:
                raise EvidenceError(
                    "Qdrant scroll repeated a page offset"
                )
            seen_offsets.add(offset_key)
    finally:
        if owns_client:
            http.close()


def _projection_file(root: Path, relative_path: str) -> Path:
    raw = str(relative_path or "")
    pure = PurePosixPath(raw)
    if (
        not raw
        or pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
        or "\\" in raw
    ):
        raise EvidenceError(f"unsafe projection file path: {raw!r}")
    path = root.joinpath(*pure.parts)
    if not path.resolve(strict=False).is_relative_to(root.resolve()):
        raise EvidenceError(f"projection file escapes root: {raw!r}")
    return path


def inspect_projection_artifact_tree(
    data_root: Path,
    *,
    project_id: str,
    target_chapter: int,
    expected_llm_kb_source_digest: str,
) -> dict[str, Any]:
    if (
        not project_id
        or project_id in {".", ".."}
        or "/" in project_id
        or "\\" in project_id
    ):
        raise EvidenceError("project_id is unsafe for projection artifact paths")
    violations: list[str] = []
    obsidian: dict[str, Any] = {
        "as_of_chapter": 0,
        "source_digest": "",
        "manifest_sha256": "",
        "file_hashes": {},
    }
    obsidian_root = data_root / "world_vaults" / project_id
    obsidian_manifest_path = (
        obsidian_root / ".forwin-projection-manifest.json"
    )
    try:
        obsidian_manifest = load_json(obsidian_manifest_path)
    except EvidenceError as exc:
        violations.append(f"obsidian manifest invalid: {exc}")
        obsidian_manifest = {}
    if obsidian_manifest:
        obsidian["manifest_sha256"] = sha256_file(obsidian_manifest_path)
        obsidian["source_digest"] = canonical_hash(obsidian_manifest)
        try:
            obsidian["as_of_chapter"] = int(
                obsidian_manifest.get("as_of_chapter") or 0
            )
        except (TypeError, ValueError):
            violations.append("obsidian as_of_chapter is invalid")
        if int(obsidian_manifest.get("schema_version") or 0) != 1:
            violations.append("obsidian manifest schema_version is not 1")
        if str(obsidian_manifest.get("project_id") or "") != project_id:
            violations.append("obsidian manifest project identity mismatch")
        if obsidian["as_of_chapter"] != target_chapter:
            violations.append(
                "obsidian as_of_chapter="
                f"{obsidian['as_of_chapter']}, expected={target_chapter}"
            )
        managed_files = obsidian_manifest.get("files")
        if not isinstance(managed_files, dict) or not managed_files:
            violations.append("obsidian manifest has no managed files")
            managed_files = {}
        for relative_path, metadata in sorted(managed_files.items()):
            if not isinstance(relative_path, str) or not isinstance(
                metadata, dict
            ):
                violations.append("obsidian manifest file entry is invalid")
                continue
            try:
                path = _projection_file(obsidian_root, relative_path)
            except EvidenceError as exc:
                violations.append(str(exc))
                continue
            expected_hash = str(metadata.get("sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                violations.append(
                    f"obsidian manifest hash is invalid: {relative_path}"
                )
                continue
            if path.is_symlink() or not path.is_file():
                violations.append(
                    f"obsidian managed file is missing or unsafe: {relative_path}"
                )
                continue
            actual_hash = sha256_file(path)
            obsidian["file_hashes"][relative_path] = actual_hash
            if actual_hash != expected_hash:
                violations.append(
                    f"obsidian file hash mismatch: {relative_path}"
                )

    llm_kb: dict[str, Any] = {
        "as_of_chapter": 0,
        "source_digest": "",
        "retrieval_index_sha256": "",
        "file_hashes": {},
        "vector_payload_hashes": {},
        "vector_index": {},
    }
    llm_root = data_root / "llm_kb" / project_id
    retrieval_index_path = llm_root / "retrieval_index.json"
    try:
        retrieval_index = load_json(retrieval_index_path)
    except EvidenceError as exc:
        violations.append(f"llm_kb retrieval index invalid: {exc}")
        retrieval_index = {}
    if retrieval_index:
        llm_kb["retrieval_index_sha256"] = sha256_file(
            retrieval_index_path
        )
        llm_kb["source_digest"] = str(
            retrieval_index.get("source_digest") or ""
        )
        try:
            llm_kb["as_of_chapter"] = int(
                retrieval_index.get("as_of_chapter") or 0
            )
        except (TypeError, ValueError):
            violations.append("llm_kb as_of_chapter is invalid")
        if str(retrieval_index.get("project_id") or "") != project_id:
            violations.append("llm_kb project identity mismatch")
        if llm_kb["as_of_chapter"] != target_chapter:
            violations.append(
                "llm_kb as_of_chapter="
                f"{llm_kb['as_of_chapter']}, expected={target_chapter}"
            )
        if (
            llm_kb["source_digest"]
            != expected_llm_kb_source_digest
        ):
            violations.append("llm_kb source digest mismatch")
        if retrieval_index.get("projection_version") != "llm_kb_v2":
            violations.append("llm_kb projection version mismatch")
        expected_index_files = set(EXPECTED_LLM_KB_FILES) - {
            "retrieval_index.json"
        }
        if set(retrieval_index.get("files") or []) != expected_index_files:
            violations.append("llm_kb retrieval index file set mismatch")
        vector_index = retrieval_index.get("vector_index") or {}
        llm_kb["vector_index"] = {
            key: vector_index.get(key)
            for key in (
                "backend",
                "collection",
                "section_count",
                "dims",
            )
        }
        if (
            vector_index.get("backend") != "qdrant"
            or not str(vector_index.get("collection") or "")
            or int(vector_index.get("section_count") or 0) <= 0
            or int(vector_index.get("dims") or 0) <= 0
        ):
            violations.append("llm_kb vector index metadata is incomplete")

    if not llm_root.is_dir():
        violations.append("llm_kb projection directory is missing")
        actual_files: set[str] = set()
    else:
        actual_files = {
            path.name
            for path in llm_root.iterdir()
            if path.is_file() or path.is_symlink()
        }
    if actual_files != set(EXPECTED_LLM_KB_FILES):
        violations.append(
            "llm_kb root file set mismatch: "
            f"{sorted(actual_files)}"
        )
    for file_name in sorted(EXPECTED_LLM_KB_FILES):
        path = llm_root / file_name
        if path.is_symlink() or not path.is_file():
            violations.append(
                f"llm_kb file is missing or unsafe: {file_name}"
            )
            continue
        llm_kb["file_hashes"][file_name] = sha256_file(path)
        if file_name in EXPECTED_LLM_KB_MARKDOWN_FILES:
            content = path.read_text(encoding="utf-8")
            lines = set(content.splitlines())
            if f"as_of_chapter: {target_chapter}" not in lines:
                violations.append(
                    f"llm_kb markdown target mismatch: {file_name}"
                )
            if (
                f"source_digest: {expected_llm_kb_source_digest}"
                not in lines
            ):
                violations.append(
                    f"llm_kb markdown digest mismatch: {file_name}"
                )
        elif file_name in EXPECTED_LLM_KB_JSONL_FILES:
            for line_number, raw_line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if not raw_line.strip():
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    violations.append(
                        f"llm_kb JSONL is invalid: {file_name}:{line_number}"
                    )
                    continue
                if not isinstance(record, dict):
                    violations.append(
                        f"llm_kb JSONL record is invalid: "
                        f"{file_name}:{line_number}"
                    )
                    continue
                if int(record.get("as_of_chapter") or 0) != target_chapter:
                    violations.append(
                        f"llm_kb JSONL target mismatch: "
                        f"{file_name}:{line_number}"
                    )
                if not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(record.get("source_digest") or ""),
                ):
                    violations.append(
                        f"llm_kb JSONL digest is invalid: "
                        f"{file_name}:{line_number}"
                    )
    try:
        llm_kb["vector_payload_hashes"] = (
            llm_kb_vector_payload_hashes(
                llm_root,
                project_id=project_id,
                source_digest=expected_llm_kb_source_digest,
                target_chapter=target_chapter,
            )
        )
    except Exception as exc:
        violations.append(
            f"llm_kb vector payload derivation failed: {redact_text(str(exc))}"
        )
    vector_index = retrieval_index.get("vector_index") or {}
    if (
        llm_kb["vector_payload_hashes"]
        and int(vector_index.get("section_count") or 0)
        != len(llm_kb["vector_payload_hashes"])
    ):
        violations.append("llm_kb vector section count mismatch")
    return {
        "collected_at": now(),
        "violations": violations,
        "obsidian": obsidian,
        "llm_kb": llm_kb,
    }


def collect_projection_artifacts(
    freeze_identity: dict[str, Any],
    *,
    project_id: str,
    target_chapter: int,
    expected_llm_kb_source_digest: str,
) -> dict[str, Any]:
    api_containers = [
        item
        for item in freeze_identity.get("runtime_containers") or []
        if str(item.get("compose_service") or "") == "forwin"
    ]
    if len(api_containers) != 1:
        raise EvidenceError(
            "projection evidence requires exactly one verified forwin container"
        )
    source = api_containers[0]
    source_name = str(source.get("name") or "")
    if not source_name or not source.get("container_id"):
        raise EvidenceError(
            "verified forwin container identity is incomplete"
        )
    with tempfile.TemporaryDirectory(
        prefix="forwin-l200-projections-"
    ) as temporary:
        data_root = Path(temporary)
        obsidian_parent = data_root / "world_vaults"
        llm_parent = data_root / "llm_kb"
        obsidian_parent.mkdir(parents=True)
        llm_parent.mkdir(parents=True)
        command(
            "docker",
            "container",
            "cp",
            f"{source_name}:/app/data/world_vaults/{project_id}",
            str(obsidian_parent / project_id),
        )
        command(
            "docker",
            "container",
            "cp",
            f"{source_name}:/app/data/llm_kb/{project_id}",
            str(llm_parent / project_id),
        )
        state = inspect_projection_artifact_tree(
            data_root,
            project_id=project_id,
            target_chapter=target_chapter,
            expected_llm_kb_source_digest=(
                expected_llm_kb_source_digest
            ),
        )
    state["source_container"] = {
        "name": source_name,
        "container_id": str(source.get("container_id") or ""),
        "compose_service": str(source.get("compose_service") or ""),
    }
    return state


def collect_projection_vectors(
    bindings: dict[str, Any],
    freeze_identity: dict[str, Any],
    *,
    project_id: str,
    projection_target: dict[str, Any],
    projection_artifacts: dict[str, Any],
) -> dict[str, Any]:
    qdrant = bindings.get("qdrant") or {}
    host = str(qdrant.get("host_ip") or "")
    port = int(qdrant.get("host_port") or 0)
    if not _loopback_host(host) or port <= 0:
        raise EvidenceError("verified Qdrant binding is incomplete")
    rendered_host = f"[{host}]" if ":" in host else host
    base_url = f"http://{rendered_host}:{port}"
    runtime = _service_container(freeze_identity, "forwin")
    projection_config = runtime.get("projection_config") or {}
    chapter_collection = str(
        projection_config.get("chapter_memory_collection") or ""
    )
    llm_kb_collection = str(
        projection_config.get("llm_kb_collection") or ""
    )
    artifact_collection = str(
        (
            (
                projection_artifacts.get("llm_kb") or {}
            ).get("vector_index")
            or {}
        ).get("collection")
        or ""
    )
    violations: list[str] = []
    if (
        not chapter_collection
        or not llm_kb_collection
        or artifact_collection != llm_kb_collection
    ):
        violations.append(
            "Qdrant collection configuration does not match projection artifacts"
        )
    chapter_points: dict[str, dict[str, Any]] = {}
    llm_kb_points: dict[str, dict[str, Any]] = {}
    if not violations:
        try:
            with direct_sync_client(timeout=60) as client:
                chapter_points = scroll_qdrant_points(
                    base_url,
                    collection=chapter_collection,
                    project_id=project_id,
                    client=client,
                )
                llm_kb_points = scroll_qdrant_points(
                    base_url,
                    collection=llm_kb_collection,
                    project_id=project_id,
                    client=client,
                )
        except EvidenceError as exc:
            violations.append(str(exc))
    expected_chapters = (
        projection_target.get("chapter_memory_payload_hashes") or {}
    )
    expected_llm_kb = (
        (projection_artifacts.get("llm_kb") or {}).get(
            "vector_payload_hashes"
        )
        or {}
    )
    if not violations:
        violations.extend(
            qdrant_projection_violations(
                chapter_points=chapter_points,
                llm_kb_points=llm_kb_points,
                expected_chapter_hashes=expected_chapters,
                expected_llm_kb_hashes=expected_llm_kb,
            )
        )

    def summary(
        collection: str,
        points: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "collection": collection,
            "point_count": len(points),
            "point_set_sha256": canonical_hash(
                [
                    [point_id, canonical_hash(payload)]
                    for point_id, payload in sorted(points.items())
                ]
            ),
        }

    return {
        "collected_at": now(),
        "binding": qdrant,
        "violations": violations,
        "chapter_memory": summary(chapter_collection, chapter_points),
        "llm_kb": summary(llm_kb_collection, llm_kb_points),
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _secret_key(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def redact_text(value: str) -> str:
    redacted = _CREDENTIAL_URL.sub(r"\1<redacted>@", str(value))
    redacted = _AUTH_HEADER_RE.sub(r"\1<redacted>", redacted)
    redacted = _COOKIE_HEADER_RE.sub(r"\1<redacted>", redacted)
    redacted = _AUTH_SCHEME_RE.sub(
        lambda match: f"{match.group(1)} <redacted>",
        redacted,
    )
    redacted = _SECRET_QUERY_RE.sub(r"\1<redacted>", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        redacted,
    )
    for key, secret in os.environ.items():
        if _secret_key(key) and len(secret) >= 8:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def redact_artifact(value: Any, *, key: str = "") -> Any:
    if _secret_key(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(child_key): redact_artifact(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_artifact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def fingerprint_environment(entries: Iterable[object]) -> list[str]:
    values: list[str] = []
    for raw in entries:
        key, separator, value = str(raw).partition("=")
        values.append(
            f"{key}=<redacted>"
            if separator and _secret_key(key)
            else f"{key}={value}" if separator else key
        )
    return sorted(values)


def command(*args: str) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=command_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EvidenceError(f"command failed ({' '.join(args)}): {detail}")
    return completed.stdout.strip()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvidenceError(f"required JSON is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"expected an object in {path}")
    return payload


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_json(path: Path, payload: Any) -> None:
    sanitized = redact_artifact(payload)
    atomic_write(
        path,
        json.dumps(
            sanitized,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
    )


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
    raise EvidenceError("MCP tool returned no payload")


async def call_mcp(client: Client, name: str, arguments: dict[str, Any]) -> Any:
    result = await client.call_tool(name, arguments)
    return result_payload(result)


def mcp_report_result(payload: Any, *, tool_name: str) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"result"}
        or not isinstance(payload["result"], dict)
    ):
        raise EvidenceError(f"{tool_name} returned an invalid report result envelope")
    return payload["result"]


async def call_mcp_report(
    client: Client,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return mcp_report_result(
        await call_mcp(client, name, arguments),
        tool_name=name,
    )


def image_identity(tag: str) -> dict[str, Any]:
    try:
        payload = json.loads(command("docker", "image", "inspect", tag))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"docker returned invalid JSON for {tag}") from exc
    if len(payload) != 1:
        raise EvidenceError(f"expected one Docker image for {tag}")
    item = payload[0]
    labels = (item.get("Config") or {}).get("Labels") or {}
    return {
        "tag": tag,
        "image_id": str(item.get("Id") or ""),
        "revision": str(labels.get("org.opencontainers.image.revision") or ""),
    }


def container_identity(name: str) -> dict[str, Any]:
    try:
        payload = json.loads(command("docker", "container", "inspect", name))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"docker returned invalid JSON for container {name}") from exc
    if len(payload) != 1:
        raise EvidenceError(f"expected one Docker container for {name}")
    item = payload[0]
    config = item.get("Config") or {}
    environment = {}
    for raw in config.get("Env") or []:
        key, separator, value = str(raw).partition("=")
        if separator:
            environment[key] = value
    state = item.get("State") or {}
    health = state.get("Health") or {}
    labels = config.get("Labels") or {}
    host_config = item.get("HostConfig") or {}
    mounts = item.get("Mounts") or []
    network_settings = item.get("NetworkSettings") or {}
    networks = network_settings.get("Networks") or {}
    mount_entries = sorted(
        (
            {
                "type": str(mount.get("Type") or ""),
                "source": str(mount.get("Name") or mount.get("Source") or ""),
                "destination": str(mount.get("Destination") or ""),
                "rw": bool(mount.get("RW")),
            }
            for mount in mounts
        ),
        key=lambda mount: (
            mount["destination"],
            mount["type"],
            mount["source"],
        ),
    )
    published_ports = {
        str(container_port): sorted(
            (
                {
                    "host_ip": str(binding.get("HostIp") or ""),
                    "host_port": int(binding.get("HostPort") or 0),
                }
                for binding in bindings or []
            ),
            key=lambda binding: (
                binding["host_ip"],
                binding["host_port"],
            ),
        )
        for container_port, bindings in (
            network_settings.get("Ports") or {}
        ).items()
        if bindings
    }
    configuration = {
        "image": str(config.get("Image") or ""),
        "cmd": config.get("Cmd") or [],
        "entrypoint": config.get("Entrypoint") or [],
        "environment": fingerprint_environment(config.get("Env") or []),
        "working_dir": str(config.get("WorkingDir") or ""),
        "user": str(config.get("User") or ""),
        "compose_labels": {
            key: str(value)
            for key, value in labels.items()
            if str(key).startswith("com.docker.compose.")
        },
        "binds": sorted(str(value) for value in host_config.get("Binds") or []),
        "network_mode": str(host_config.get("NetworkMode") or ""),
        "port_bindings": host_config.get("PortBindings") or {},
        "restart_policy": host_config.get("RestartPolicy") or {},
        "mounts": mount_entries,
        "networks": sorted(str(value) for value in networks),
    }
    return {
        "name": name,
        "container_id": str(item.get("Id") or ""),
        "image_id": str(item.get("Image") or ""),
        "running": bool(state.get("Running")),
        "status": str(state.get("Status") or ""),
        "health": str(health.get("Status") or ""),
        "compose_project": str(labels.get("com.docker.compose.project") or ""),
        "compose_service": str(labels.get("com.docker.compose.service") or ""),
        "published_ports": published_ports,
        "mounts": mount_entries,
        "projection_config": {
            "chapter_memory_collection": str(
                environment.get("FORWIN_QDRANT_COLLECTION")
                or "chapter_memories"
            ),
            "llm_kb_collection": str(
                environment.get("FORWIN_LLM_KB_QDRANT_COLLECTION")
                or "llm_kb_vectors"
            ),
        },
        "configuration_sha256": canonical_hash(configuration),
    }


def rc_image(rc_manifest: dict[str, Any], key: str) -> dict[str, Any]:
    item = ((rc_manifest.get("images") or {}).get(key) or {})
    if not isinstance(item, dict) or not item.get("tag") or not item.get("image_id"):
        raise EvidenceError(f"RC manifest has no complete images.{key} identity")
    return item


def load_rc_collector_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "forwin_v5_rc_collector_runtime",
        RC_COLLECTOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise EvidenceError("RC collector import failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_runtime_model_routing(
    args: argparse.Namespace,
    rc_manifest: dict[str, Any],
    runtime_image_id: str,
) -> dict[str, Any]:
    expected = (
        (rc_manifest.get("model_profiles") or {}).get(
            "effective_container_fields"
        )
        or {}
    )
    if not expected:
        raise EvidenceError(
            "RC manifest has no effective runtime model routing"
        )
    try:
        actual = load_rc_collector_module().inspect_model_environments(
            args.runtime_container,
            runtime_image_id,
            model_profile_id=str(
                (rc_manifest.get("runtime_policy") or {}).get(
                    "model_profile_id"
                )
                or ""
            ),
        )
    except Exception as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(
            f"could not inspect effective runtime model routing: {exc}"
        ) from exc
    if actual != expected:
        raise FreezeViolation(
            "model_routing",
            "runtime model routing differs from the frozen RC",
        )
    return actual


def verify_container_set(
    names: Iterable[str],
    *,
    expected_image_id: str = "",
    role: str,
    expected_services: frozenset[str],
) -> list[dict[str, Any]]:
    identities = [container_identity(name) for name in names]
    if not identities:
        raise EvidenceError(f"at least one {role} container is required")
    projects = {item["compose_project"] for item in identities}
    if "" in projects or len(projects) != 1:
        raise EvidenceError(f"{role} containers are not bound to one Compose project")
    services = {str(item.get("compose_service") or "") for item in identities}
    if services != set(expected_services) or len(identities) != len(expected_services):
        raise FreezeViolation(
            "config",
            f"{role} service set is {sorted(services)}, expected "
            f"{sorted(expected_services)}",
        )
    for item in identities:
        if expected_image_id and item["image_id"] != expected_image_id:
            raise FreezeViolation(
                "config",
                f"{role} container {item['name']} uses {item['image_id']}, "
                f"expected {expected_image_id}"
            )
        if not item["running"] or item["health"] not in {"", "healthy"}:
            raise EvidenceError(f"{role} container is not healthy: {item}")
    return identities


def verify_candidate_mount_policy(
    identities: Iterable[dict[str, Any]],
) -> None:
    shared_data_sources: set[str] = set()
    for identity in identities:
        service = str(identity.get("compose_service") or "")
        if service not in CANDIDATE_VOLUME_DESTINATIONS:
            raise FreezeViolation(
                "config", f"no candidate mount policy for service {service}"
            )
        mounts = identity.get("mounts") or []
        if any(str(mount.get("type") or "") == "bind" for mount in mounts):
            raise FreezeViolation(
                "config",
                f"host bind mounts are forbidden for candidate service {service}",
            )
        invalid_types = [
            str(mount.get("type") or "")
            for mount in mounts
            if str(mount.get("type") or "") != "volume"
        ]
        if invalid_types:
            raise FreezeViolation(
                "config",
                f"unsupported candidate mount types for {service}: "
                f"{sorted(invalid_types)}",
            )
        destinations = {
            str(mount.get("destination") or "") for mount in mounts
        }
        expected = set(CANDIDATE_VOLUME_DESTINATIONS[service])
        if destinations != expected:
            raise FreezeViolation(
                "config",
                f"candidate mount destinations for {service} are "
                f"{sorted(destinations)}, expected {sorted(expected)}",
            )
        for mount in mounts:
            if not str(mount.get("source") or "") or not mount.get("rw"):
                raise FreezeViolation(
                    "config",
                    f"candidate data volume is incomplete for {service}",
                )
            if mount.get("destination") == "/app/data":
                shared_data_sources.add(str(mount["source"]))
    if shared_data_sources and len(shared_data_sources) != 1:
        raise FreezeViolation(
            "config",
            "runtime and publisher services do not use one shared data volume",
        )


def verify_frozen(args: argparse.Namespace, rc_manifest: dict[str, Any]) -> dict[str, Any]:
    source = rc_manifest.get("source") or {}
    expected_sha = str(source.get("sha") or "")
    expected_tree = str(source.get("tree") or "")
    if not expected_sha or not expected_tree:
        raise EvidenceError("RC manifest source identity is incomplete")
    actual_sha = command("git", "rev-parse", "HEAD")
    actual_tree = command("git", "rev-parse", "HEAD^{tree}")
    if actual_sha != expected_sha or actual_tree != expected_tree:
        raise FreezeViolation(
            "code",
            f"source drift: sha/tree={actual_sha}/{actual_tree}, "
            f"expected={expected_sha}/{expected_tree}"
        )
    if command("git", "status", "--porcelain=v1", "--untracked-files=no"):
        raise FreezeViolation("code", "tracked worktree is dirty during no-hotfix run")

    runtime = rc_image(rc_manifest, "runtime")
    browser = rc_image(rc_manifest, "publisher_browser")
    for expected in (runtime, browser):
        actual = image_identity(str(expected["tag"]))
        if actual["image_id"] != expected["image_id"]:
            raise FreezeViolation(
                "code", f"image identity drift for {expected['tag']}"
            )
        if actual["revision"] != expected_sha:
            raise FreezeViolation(
                "code", f"image revision drift for {expected['tag']}"
            )

    runtime_containers = verify_container_set(
        args.runtime_container,
        expected_image_id=str(runtime["image_id"]),
        role="runtime",
        expected_services=EXPECTED_RUNTIME_SERVICES,
    )
    browser_containers = verify_container_set(
        args.browser_container,
        expected_image_id=str(browser["image_id"]),
        role="publisher-browser",
        expected_services=EXPECTED_BROWSER_SERVICES,
    )
    dependency_containers = verify_container_set(
        args.dependency_container,
        role="dependency",
        expected_services=EXPECTED_DEPENDENCY_SERVICES,
    )
    compose_projects = {
        item["compose_project"]
        for item in runtime_containers + browser_containers + dependency_containers
    }
    if len(compose_projects) != 1:
        raise EvidenceError("runtime and browser containers belong to different stacks")
    verify_candidate_mount_policy(
        runtime_containers + browser_containers + dependency_containers
    )
    model_routing = verify_runtime_model_routing(
        args,
        rc_manifest,
        str(runtime["image_id"]),
    )
    if model_routing.get("compose_project") != next(iter(compose_projects)):
        raise FreezeViolation(
            "model_routing",
            "model-routing containers belong to a different Compose project",
        )
    return {
        "checked_at": now(),
        "source_sha": actual_sha,
        "source_tree": actual_tree,
        "tracked_worktree_clean": True,
        "runtime_image": runtime,
        "publisher_browser_image": browser,
        "compose_project": next(iter(compose_projects)),
        "runtime_containers": runtime_containers,
        "publisher_browser_containers": browser_containers,
        "dependency_containers": dependency_containers,
        "model_routing": model_routing,
    }


def verify_runtime_configuration(
    run_manifest: dict[str, Any],
    current: dict[str, Any],
) -> None:
    initial = run_manifest.get("freeze_identity") or {}
    if current.get("compose_project") != initial.get("compose_project"):
        raise FreezeViolation("config", "Compose project changed during run")
    for key in (
        "runtime_containers",
        "publisher_browser_containers",
        "dependency_containers",
    ):
        expected = {
            str(item.get("name") or ""): (
                str(item.get("container_id") or ""),
                str(item.get("image_id") or ""),
                str(item.get("compose_service") or ""),
                str(item.get("configuration_sha256") or ""),
            )
            for item in initial.get(key) or []
        }
        actual = {
            str(item.get("name") or ""): (
                str(item.get("container_id") or ""),
                str(item.get("image_id") or ""),
                str(item.get("compose_service") or ""),
                str(item.get("configuration_sha256") or ""),
            )
            for item in current.get(key) or []
        }
        if actual != expected:
            raise FreezeViolation(
                "config", f"container runtime configuration changed for {key}"
            )
    if current.get("model_routing") != initial.get("model_routing"):
        raise FreezeViolation(
            "model_routing",
            "effective runtime model routing changed during run",
        )


def database_schema_identity(database_url: str) -> dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            revision = str(
                connection.execute(text("SELECT version_num FROM alembic_version"))
                .scalar_one()
            )
            queries = {
                "columns": """
                    SELECT table_name,column_name,ordinal_position,data_type,
                           udt_name,is_nullable,coalesce(column_default,'')
                    FROM information_schema.columns
                    WHERE table_schema='public'
                    ORDER BY table_name,ordinal_position
                """,
                "constraints": """
                    SELECT c.relname,con.conname,con.contype,
                           pg_get_constraintdef(con.oid,true)
                    FROM pg_constraint con
                    JOIN pg_class c ON c.oid=con.conrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public'
                    ORDER BY c.relname,con.conname
                """,
                "indexes": """
                    SELECT tablename,indexname,indexdef
                    FROM pg_indexes WHERE schemaname='public'
                    ORDER BY tablename,indexname
                """,
                "views": """
                    SELECT viewname,definition
                    FROM pg_views WHERE schemaname='public'
                    ORDER BY viewname
                """,
                "functions": """
                    SELECT p.proname,pg_get_function_identity_arguments(p.oid),
                           pg_get_functiondef(p.oid)
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname='public'
                    ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)
                """,
                "triggers": """
                    SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid,true)
                    FROM pg_trigger t
                    JOIN pg_class c ON c.oid=t.tgrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND NOT t.tgisinternal
                    ORDER BY c.relname,t.tgname
                """,
                "enum_values": """
                    SELECT t.typname,e.enumsortorder,e.enumlabel
                    FROM pg_type t
                    JOIN pg_enum e ON e.enumtypid=t.oid
                    JOIN pg_namespace n ON n.oid=t.typnamespace
                    WHERE n.nspname='public'
                    ORDER BY t.typname,e.enumsortorder
                """,
            }
            sections = {
                name: [list(row) for row in connection.execute(text(statement))]
                for name, statement in queries.items()
            }
            return {
                "alembic_revision": revision,
                "schema_sha256": canonical_hash(sections),
                "section_counts": {
                    name: len(rows) for name, rows in sections.items()
                },
            }
    finally:
        engine.dispose()


def verify_database_schema(
    run_manifest: dict[str, Any],
    database_url: str,
) -> dict[str, Any]:
    current = database_schema_identity(database_url)
    if current != run_manifest.get("live_schema_identity"):
        raise FreezeViolation("schema", "live PostgreSQL schema changed during run")
    return current


def rc_model_routing_violations(
    effective_models: dict[str, Any],
    *,
    expected_runtime_image: str,
    selected_profile_id: str,
) -> list[str]:
    violations: list[str] = []
    collector = load_rc_collector_module()
    routing_groups = {
        str(group): frozenset(str(service) for service in services)
        for group, services in collector.MODEL_ROUTING_GROUPS.items()
    }
    expected_services = frozenset().union(*routing_groups.values())
    model_services = effective_models.get("services")
    group_hashes = effective_models.get("routing_group_hashes")
    aggregate_hash = str(effective_models.get("routing_sha256") or "")
    if (
        not isinstance(model_services, dict)
        or set(model_services) != set(expected_services)
        or expected_services != EXPECTED_RUNTIME_SERVICES
        or not effective_models.get("compose_project")
        or not isinstance(group_hashes, dict)
        or set(group_hashes) != set(routing_groups)
        or not re.fullmatch(r"[0-9a-f]{64}", aggregate_hash)
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(value or ""))
            for value in (group_hashes or {}).values()
        )
    ):
        return ["RC effective model-routing role groups are incomplete"]

    aggregate_expected: dict[str, str] = {}
    for service, item in sorted(model_services.items()):
        if not isinstance(item, dict):
            violations.append(
                f"RC effective model routing is invalid: {service}"
            )
            continue
        routing = item.get("routing") or {}
        routing_hash = str(item.get("routing_sha256") or "")
        aggregate_expected[service] = routing_hash
        group = next(
            (
                name
                for name, services in routing_groups.items()
                if service in services
            ),
            "",
        )
        if (
            not isinstance(routing, dict)
            or item.get("runtime_image_id") != expected_runtime_image
            or canonical_hash(routing) != routing_hash
            or routing_hash != str(group_hashes.get(group) or "")
            or (
                routing.get("selected_profile") or {}
            ).get("id")
            != selected_profile_id
        ):
            violations.append(
                f"RC effective model routing is invalid: {service}"
            )
        if group == "passive" and (
            (routing.get("selected_profile") or {}).get(
                "api_key_configured"
            )
            or routing.get("fallback_profiles")
            or (routing.get("codex") or {}).get("enabled")
            or (routing.get("embedding") or {}).get(
                "api_key_configured"
            )
        ):
            violations.append(
                f"RC passive model routing has credentials: {service}"
            )
    if canonical_hash(aggregate_expected) != aggregate_hash:
        violations.append("RC effective model-routing aggregate hash mismatch")
    return violations


def validate_frozen_rc_manifest(
    args: argparse.Namespace,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    violations: list[str] = []
    if int(manifest.get("schema_version") or 0) < 3:
        violations.append("RC manifest schema_version is not final v3+")
    required_sections = {
        "source",
        "images",
        "baseline_schema",
        "runtime_policy",
        "model_profiles",
        "prompt_revision",
        "skill_registry_revision",
        "report_tools",
        "release_harness",
        "matrix_evidence",
        "freeze_contract",
        "release_candidate",
        "collector",
    }
    if not required_sections <= set(manifest):
        violations.append(
            f"RC manifest missing sections: {sorted(required_sections - set(manifest))}"
        )
    source = manifest.get("source") or {}
    if not source.get("sha") or not source.get("tree") or not source.get("tracked_worktree_clean"):
        violations.append("RC source identity is incomplete or dirty")
    images = manifest.get("images") or {}
    for key in (
        "runtime",
        "publisher_browser",
        "postgres",
        "qdrant",
        "minio",
    ):
        image = images.get(key) or {}
        if not image.get("tag") or not image.get("image_id"):
            violations.append(f"RC image identity is incomplete: {key}")
        if (
            key in {"runtime", "publisher_browser"}
            and image.get("revision") != source.get("sha")
        ):
            violations.append(f"RC image revision mismatch: {key}")
    baseline = manifest.get("baseline_schema") or {}
    baseline_path = ROOT / str(baseline.get("path") or "")
    if (
        not baseline_path.is_file()
        or str(baseline.get("sha256") or "") != sha256_file(baseline_path)
    ):
        violations.append("RC baseline schema hash mismatch")
    policy = manifest.get("runtime_policy") or {}
    if int(policy.get("schema_version") or 0) != 2:
        violations.append("RC RuntimePolicy schema_version is not 2")
    if policy.get("quality_profile") != args.quality_profile:
        violations.append("RC quality profile differs from selected L200 profile")
    if policy.get("gate_delegate") != args.gate_delegate:
        violations.append("RC gate delegate differs from selected L200 delegate")
    model_profiles = manifest.get("model_profiles") or {}
    effective_models = (
        model_profiles.get("effective_container_fields") or {}
    )
    violations.extend(
        rc_model_routing_violations(
            effective_models,
            expected_runtime_image=str(
                ((images.get("runtime") or {}).get("image_id") or "")
            ),
            selected_profile_id=(
                str(policy.get("model_profile_id") or "").strip()
                or "env-minimax"
            ),
        )
    )
    for key in ("model_profiles", "prompt_revision", "skill_registry_revision"):
        value = manifest.get(key) or {}
        revision = value.get("revision") if key == "model_profiles" else value
        if not isinstance(revision, dict) or not revision.get("sha256"):
            violations.append(f"RC {key} revision is incomplete")
    report_tools = manifest.get("report_tools") or {}
    schemas = report_tools.get("schema_versions") or {}
    if set(schemas) != {
        "GateLedgerReportView",
        "CostLedgerReportView",
        "RuleProvenanceReportView",
    } or any(int(value or 0) != 1 for value in schemas.values()):
        violations.append("RC report schema versions are incomplete")
    if not (report_tools.get("revision") or {}).get("sha256"):
        violations.append("RC report tool revision is incomplete")
    release_harness = manifest.get("release_harness") or {}
    if (
        not release_harness.get("sha256")
        or int(release_harness.get("file_count") or 0) <= 0
    ):
        violations.append("RC release harness revision is incomplete")
    matrix = manifest.get("matrix_evidence") or {}
    if matrix.get("result") != "pass":
        violations.append("RC matrix evidence is not a final pass")
    if int(matrix.get("code_changes_during_run") or 0) != 0:
        violations.append("RC matrix evidence reports code changes")
    if matrix.get("current_rc_source_sha") != source.get("sha"):
        violations.append("RC matrix evidence current source SHA mismatch")
    if set(matrix.get("cells") or {}) != {"L30", "L60S", "L60P", "L100"}:
        violations.append("RC matrix evidence cell identities mismatch")
    freeze_contract = manifest.get("freeze_contract") or {}
    if not freeze_contract or any(int(value or 0) != 0 for value in freeze_contract.values()):
        violations.append("RC freeze contract is incomplete or nonzero")
    collector = manifest.get("collector") or {}
    collector_path = Path(str(collector.get("path") or ""))
    if not collector_path.is_absolute():
        collector_path = ROOT / collector_path
    collector_path = collector_path.resolve()
    collector_valid = True
    if collector_path != RC_COLLECTOR_PATH:
        violations.append("RC collector path mismatch")
        collector_valid = False
    if not collector_path.is_file():
        violations.append("RC collector is missing")
        collector_valid = False
    elif sha256_file(collector_path) != collector.get("sha256"):
        violations.append("RC collector hash mismatch")
        collector_valid = False
    collector_module: Any | None = None
    if collector_valid:
        spec = importlib.util.spec_from_file_location(
            "forwin_rc_manifest_collector",
            collector_path,
        )
        if spec is None or spec.loader is None:
            violations.append("RC collector import failed")
        else:
            collector_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(collector_module)
            try:
                expected_harness = collector_module.tracked_source_revision(
                    str(source.get("sha") or ""),
                    collector_module.RELEASE_HARNESS_PATHS,
                )
            except collector_module.ManifestError as exc:
                violations.append(f"RC release harness is invalid: {exc}")
            else:
                if expected_harness != release_harness:
                    violations.append("RC release harness revision mismatch")
            matrix_path = Path(str(matrix.get("path") or ""))
            if not matrix_path.is_absolute():
                matrix_path = ROOT / matrix_path
            if (
                not matrix_path.is_file()
                or sha256_file(matrix_path) != matrix.get("sha256")
            ):
                violations.append("RC matrix final audit artifact hash mismatch")
            else:
                try:
                    revalidated_matrix = collector_module.load_matrix_manifest(
                        matrix_path,
                        str(source.get("sha") or ""),
                        require_final_audit=True,
                    )
                except collector_module.ManifestError as exc:
                    violations.append(f"RC matrix final audit is invalid: {exc}")
                else:
                    if revalidated_matrix != matrix:
                        violations.append("RC matrix evidence summary mismatch")
    release = manifest.get("release_candidate") or {}
    if release.get("status") != "frozen":
        violations.append("RC manifest is not marked frozen")
    if release.get("source_sha") != source.get("sha"):
        violations.append("RC release candidate source SHA mismatch")
    tag = str(release.get("annotated_tag") or "")
    if not tag:
        violations.append("RC manifest has no annotated tag")
    else:
        try:
            tag_ref = f"refs/tags/{tag}"
            if command("git", "cat-file", "-t", tag_ref) != "tag":
                violations.append("RC tag is not annotated")
            if command("git", "rev-parse", f"{tag_ref}^{{}}") != source.get("sha"):
                violations.append("RC tag does not point to source SHA")
            tag_object_sha = str(release.get("tag_object_sha") or "")
            if (
                not tag_object_sha
                or command("git", "rev-parse", tag_ref) != tag_object_sha
            ):
                violations.append("RC annotated tag object SHA mismatch")
        except EvidenceError as exc:
            violations.append(f"RC tag cannot be verified: {exc}")
    evidence = release.get("evidence") or {}
    candidate_manifest_hashes: set[str] = set()
    for key in (
        "v1_preflight",
        "release_gates",
        "live_recovery",
        "post_decision_smoke",
    ):
        item = evidence.get(key) or {}
        if item.get("result") != "pass" or not item.get("sha256"):
            violations.append(f"RC evidence is not passing: {key}")
            continue
        if item.get("source_sha") != source.get("sha"):
            violations.append(f"RC evidence source SHA mismatch: {key}")
        candidate_manifest_hashes.add(
            str(item.get("candidate_manifest_sha256") or "")
        )
        artifact_path = Path(str(item.get("path") or ""))
        if not artifact_path.is_absolute():
            artifact_path = ROOT / artifact_path
        if not artifact_path.is_file():
            violations.append(f"RC evidence file is missing: {key}")
            continue
        if sha256_file(artifact_path) != item.get("sha256"):
            violations.append(f"RC evidence file hash mismatch: {key}")
            continue
        try:
            artifact = load_json(artifact_path)
        except EvidenceError as exc:
            violations.append(f"RC evidence file is invalid: {key}: {exc}")
            continue
        artifact_source = str(
            artifact.get("source_sha")
            or (artifact.get("identity") or {}).get("source_sha")
            or ""
        )
        if artifact_source != source.get("sha"):
            violations.append(f"RC evidence artifact source SHA mismatch: {key}")
        artifact_passed = (
            artifact.get("release_gate_passed") is True
            if key == "release_gates"
            else artifact.get("result") == "pass"
        )
        if not artifact_passed:
            violations.append(f"RC evidence artifact is not passing: {key}")
        if collector_module is not None:
            try:
                if key == "v1_preflight":
                    collector_module.validate_v1_evidence(
                        artifact,
                        source_sha=str(source.get("sha") or ""),
                    )
                elif key == "release_gates":
                    collector_module.validate_release_gate_evidence(
                        artifact,
                        source_sha=str(source.get("sha") or ""),
                        expected_gate_identity={
                            "source_tree": source.get("tree"),
                            "runtime_image": images.get("runtime"),
                            "browser_image": images.get("publisher_browser"),
                        },
                    )
                elif key == "live_recovery":
                    collector_module.validate_recovery_evidence(
                        artifact,
                        source_sha=str(source.get("sha") or ""),
                    )
                elif key == "post_decision_smoke":
                    collector_module.validate_smoke_evidence(
                        artifact,
                        source_sha=str(source.get("sha") or ""),
                        expected_candidate_identity={
                            "source_tree": source.get("tree"),
                            "runtime_image": images.get("runtime"),
                            "browser_image": images.get("publisher_browser"),
                            "dependency_images": {
                                dependency: images.get(dependency)
                                for dependency in (
                                    "postgres",
                                    "qdrant",
                                    "minio",
                                )
                            },
                        },
                    )
            except collector_module.ManifestError as exc:
                label = {
                    "v1_preflight": "V1 preflight",
                    "release_gates": "release gate",
                    "live_recovery": "live recovery",
                    "post_decision_smoke": "post-decision smoke",
                }[key]
                violations.append(f"{label} evidence invalid: {exc}")
    if (
        len(candidate_manifest_hashes) != 1
        or "" in candidate_manifest_hashes
    ):
        violations.append(
            "RC evidence does not share one candidate manifest identity"
        )
    try:
        collected_at = datetime.fromisoformat(str(manifest.get("collected_at") or ""))
    except ValueError:
        violations.append("RC manifest collected_at is invalid")
        collected_at = datetime.min.replace(tzinfo=UTC)
    if violations:
        raise EvidenceError("invalid frozen RC manifest: " + "; ".join(violations))
    return {
        "source_sha": str(source["sha"]),
        "annotated_tag": tag,
        "collected_at": collected_at.astimezone(UTC).isoformat(),
    }


def fresh_database_state(database_url: str, project_id: str) -> dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM projects) AS projects,
                      (SELECT count(*) FROM canon_commit_records) AS canon_commits,
                      (SELECT count(*) FROM generation_tasks) AS generation_tasks,
                      (SELECT count(*) FROM outbox_events) AS outbox_events,
                      (SELECT count(*) FROM publisher_upload_jobs) AS publisher_jobs,
                      (SELECT created_at FROM projects WHERE id=:project_id) AS project_created_at
                    """
                ),
                {"project_id": project_id},
            ).mappings().one()
            return {
                key: (int(value or 0) if key != "project_created_at" else str(value or ""))
                for key, value in row.items()
            }
    finally:
        engine.dispose()


async def fetch_genesis(mcp_url: str, project_id: str) -> dict[str, Any]:
    async with direct_mcp_client(mcp_url) as client:
        payload = await call_mcp(client, "genesis_get", {"project_id": project_id})
    if not isinstance(payload, dict):
        raise EvidenceError("genesis_get returned an invalid payload")
    return payload


def assert_fresh_genesis_handoff(
    *,
    rc_identity: dict[str, Any],
    fresh_database: dict[str, Any],
    project: dict[str, Any],
    genesis: dict[str, Any],
) -> None:
    violations: list[str] = []
    for key, expected in (
        ("projects", 1),
        ("canon_commits", 0),
        ("generation_tasks", 0),
        ("outbox_events", 0),
        ("publisher_jobs", 0),
    ):
        if int(fresh_database.get(key) or 0) != expected:
            violations.append(f"fresh database {key}={fresh_database.get(key)}, expected={expected}")
    try:
        created_at = datetime.fromisoformat(str(fresh_database.get("project_created_at") or ""))
        frozen_at = datetime.fromisoformat(str(rc_identity["collected_at"]))
        if created_at.replace(tzinfo=created_at.tzinfo or UTC) <= frozen_at:
            violations.append("L200 project was not created after RC freeze")
    except ValueError:
        violations.append("L200 project creation time is invalid")
    if project.get("creation_status") != "genesis_ready" or not project.get("can_start_writing"):
        violations.append("project has not reached Genesis writing handoff")
    if genesis.get("creation_status") != "genesis_ready" or not genesis.get("can_start_writing"):
        violations.append("Genesis has not reached writing handoff")
    stages = {
        str(item.get("stage_key") or ""): item
        for item in genesis.get("stage_states") or []
    }
    if set(stages) != set(GENESIS_STAGES):
        violations.append(f"Genesis stage identities mismatch: {sorted(stages)}")
    for stage in GENESIS_STAGES:
        item = stages.get(stage) or {}
        if item.get("status") != "locked" or not item.get("locked"):
            violations.append(f"Genesis stage is not locked: {stage}")
    if violations:
        raise EvidenceError("invalid fresh L200 handoff: " + "; ".join(violations))


async def fetch_policy(api_url: str, project_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=api_url,
        timeout=60,
        trust_env=False,
        follow_redirects=False,
    ) as http:
        response = await http.get(f"/api/projects/{project_id}/policy")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("policy"), dict):
        raise EvidenceError("project policy endpoint returned an invalid payload")
    return payload


def frozen_rule_state(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: report.get(key) or []
        for key in (
            "global_code_backed_rules",
            "genre_rule_candidates",
            "project_rules",
        )
    }


def gate_report_violations(
    report: dict[str, Any],
    *,
    project_id: str,
    scope: str,
    band_id: str = "",
) -> list[str]:
    violations: list[str] = []
    required = {
        "schema_version",
        "scope",
        "project_id",
        "band_id",
        "project_count",
        "event_count",
        "checkpoint_count",
        "metrics",
    }
    if not required <= set(report):
        violations.append(f"gate report missing fields: {sorted(required - set(report))}")
    try:
        view = GateLedgerReportView.model_validate(report)
    except ValidationError as exc:
        return [f"gate report schema invalid: {exc}"]
    if view.scope != scope or view.project_id != project_id or view.band_id != band_id:
        violations.append("gate report scope identity mismatch")
    if view.project_count != 1:
        violations.append(f"gate report project_count={view.project_count}, expected=1")
    if not view.metrics:
        violations.append("gate report has no metrics")
        return violations
    gate_ids = [metric.gate_id for metric in view.metrics]
    if len(gate_ids) != len(set(gate_ids)):
        violations.append("gate report has duplicate gate metrics")
    known_opportunities = 0
    for metric in view.metrics:
        metric_payload = next(
            (
                item
                for item in report.get("metrics") or []
                if item.get("gate_id") == metric.gate_id
            ),
            {},
        )
        metric_required = {
            "gate_id",
            "opportunities",
            "evaluations",
            "fires",
            "blocks",
            "overrides",
            "post_override_incident_proxy",
            "post_pass_incident_proxy",
            "unknown_legacy_count",
        }
        if not metric_required <= set(metric_payload):
            violations.append(
                f"gate {metric.gate_id} missing fields: "
                f"{sorted(metric_required - set(metric_payload))}"
            )
        if metric.opportunities == "unknown":
            violations.append(f"gate {metric.gate_id} has unknown fresh-run denominator")
            continue
        opportunities = int(metric.opportunities)
        known_opportunities += opportunities
        if opportunities > 0 and (metric.fire_rate is None or metric.block_rate is None):
            violations.append(f"gate {metric.gate_id} omits denominator-derived rates")
    if known_opportunities <= 0:
        violations.append("gate report has no known opportunities")
    if scope == "band":
        checkpoint_metric = next(
            (metric for metric in view.metrics if metric.gate_id == "band_checkpoint"),
            None,
        )
        if (
            checkpoint_metric is None
            or checkpoint_metric.opportunities == "unknown"
            or int(checkpoint_metric.opportunities) < 1
        ):
            violations.append("band gate report has no checkpoint opportunity")
    return violations


def cost_report_violations(
    report: dict[str, Any],
    *,
    project_id: str,
    band_id: str = "",
) -> list[str]:
    violations: list[str] = []
    required = {
        "schema_version",
        "project_id",
        "band_id",
        "project_count",
        "trace_count",
        "event_count",
        "totals",
        "dimensions",
        "gate_costs",
        "manual_action_count",
        "manual_action_duration_ms",
        "unknown_manual_duration_count",
        "manual_actions",
    }
    if not required <= set(report):
        violations.append(f"cost report missing fields: {sorted(required - set(report))}")
    totals_required = {
        "attempts",
        "successes",
        "retries",
        "fallbacks",
        "input_chars",
        "output_chars",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "duration_ms",
        "provider_usage_attempts",
        "codex_usage_attempts",
        "estimated_usage_attempts",
        "missing_usage_attempts",
    }
    totals_payload = report.get("totals") or {}
    if not totals_required <= set(totals_payload):
        violations.append(
            f"cost totals missing fields: {sorted(totals_required - set(totals_payload))}"
        )
    try:
        view = CostLedgerReportView.model_validate(report)
    except ValidationError as exc:
        return [*violations, f"cost report schema invalid: {exc}"]
    if view.project_id != project_id or view.band_id != band_id:
        violations.append("cost report scope identity mismatch")
    if view.project_count != 1:
        violations.append(f"cost report project_count={view.project_count}, expected=1")
    totals = view.totals
    if view.trace_count <= 0 or totals.attempts <= 0 or totals.successes <= 0:
        violations.append("cost report has no successful LLM trace attempts")
    if totals.input_chars <= 0 or totals.output_chars <= 0 or totals.duration_ms <= 0:
        violations.append("cost report has no character/duration evidence")
    usage_attempts = (
        totals.provider_usage_attempts
        + totals.codex_usage_attempts
        + totals.estimated_usage_attempts
        + totals.missing_usage_attempts
    )
    if usage_attempts != totals.attempts:
        violations.append(
            f"cost usage accounting={usage_attempts}, attempts={totals.attempts}"
        )
    if band_id and not any(
        metric.dimension == "band" and metric.value == band_id
        for metric in view.dimensions
    ):
        violations.append("band cost report has no matching band dimension")
    if sum(item.count for item in view.manual_actions) != view.manual_action_count:
        violations.append("manual action detail count does not match total")
    return violations


def rule_report_violations(
    report: dict[str, Any],
    *,
    project_id: str,
) -> list[str]:
    violations: list[str] = []
    required = {
        "schema_version",
        "project_id",
        "project_count",
        "global_code_backed_rules",
        "genre_rule_candidates",
        "project_rules",
        "recommendations",
    }
    if not required <= set(report):
        violations.append(f"rule report missing fields: {sorted(required - set(report))}")
    try:
        view = RuleProvenanceReportView.model_validate(report)
    except ValidationError as exc:
        return [*violations, f"rule report schema invalid: {exc}"]
    if view.project_id != project_id or view.project_count != 1:
        violations.append("rule report project identity/count mismatch")
    if not view.global_code_backed_rules:
        violations.append("rule report has no global code-backed linguistic rules")
    identities = [
        (item.scope, item.rule_key)
        for item in (
            *view.global_code_backed_rules,
            *view.genre_rule_candidates,
            *view.project_rules,
        )
    ]
    if len(identities) != len(set(identities)):
        violations.append("rule report has duplicate scoped rule identities")
    if any(
        item.origin_project_id != project_id
        for item in view.project_rules
    ):
        violations.append("project rule origin_project_id mismatch")
    return violations


def report_contract_violations(
    *,
    gate: dict[str, Any],
    cost: dict[str, Any],
    rules: dict[str, Any],
    project_id: str,
    scope: str,
    band_id: str = "",
) -> list[str]:
    return [
        *gate_report_violations(
            gate, project_id=project_id, scope=scope, band_id=band_id
        ),
        *cost_report_violations(cost, project_id=project_id, band_id=band_id),
        *rule_report_violations(rules, project_id=project_id),
    ]


def band_directory_name(band: dict[str, Any]) -> str:
    identity = str(band.get("band_id") or "")
    if not identity:
        raise EvidenceError("band checkpoint has no band_id")
    start = int(band.get("chapter_start") or 0)
    end = int(band.get("chapter_end") or 0)
    return f"{start:03d}-{end:03d}-{hashlib.sha256(identity.encode()).hexdigest()[:10]}"


async def collect_completed_band_reports(
    args: argparse.Namespace,
    *,
    bands: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    completed = [
        band
        for band in bands
        if str(band.get("status") or "") in {"pass", "overridden"}
        and int(band.get("chapter_end") or 0) > 0
    ]
    entries: list[dict[str, Any]] = []
    async with direct_mcp_client(args.mcp_url) as client:
        for band in sorted(
            completed,
            key=lambda item: (
                int(item.get("chapter_start") or 0),
                int(item.get("chapter_end") or 0),
            ),
        ):
            band_id = str(band["band_id"])
            directory = output_dir / "bands" / band_directory_name(band)
            metadata_path = directory / "metadata.json"
            gate_path = directory / "gate-ledger.json"
            cost_path = directory / "cost-report.json"
            rule_path = directory / "rule-provenance.json"
            paths = (metadata_path, gate_path, cost_path, rule_path)
            if all(path.is_file() for path in paths):
                metadata = load_json(metadata_path)
                if canonical_hash(metadata.get("band") or {}) != canonical_hash(band):
                    raise FreezeViolation(
                        "config", f"frozen band metadata changed for {band_id}"
                    )
            elif any(path.exists() for path in paths):
                raise EvidenceError(f"partial band evidence exists: {directory}")
            else:
                gate = await call_mcp_report(
                    client,
                    "gate_ledger_report",
                    {
                        "scope": "band",
                        "project_id": args.project_id,
                        "band_id": band_id,
                        "format": "json",
                    },
                )
                cost = await call_mcp_report(
                    client,
                    "cost_report",
                    {
                        "project_id": args.project_id,
                        "band_id": band_id,
                        "format": "json",
                    },
                )
                rule = await call_mcp_report(
                    client,
                    "rule_provenance_report",
                    {"project_id": args.project_id, "format": "json"},
                )
                report_violations = report_contract_violations(
                    gate=gate,
                    cost=cost,
                    rules=rule,
                    project_id=args.project_id,
                    scope="band",
                    band_id=band_id,
                )
                if report_violations:
                    raise EvidenceError(
                        f"band {band_id} report contract failed: "
                        + "; ".join(report_violations)
                    )
                metadata = {
                    "schema_version": 1,
                    "collected_at": now(),
                    "project_id": args.project_id,
                    "band": band,
                    "s1_scope": "band",
                    "s3_scope": "band",
                    "s2_scope": "project_snapshot_at_band_completion",
                }
                write_json(gate_path, gate)
                write_json(cost_path, cost)
                write_json(rule_path, rule)
                write_json(metadata_path, metadata)
            entries.append(
                {
                    "band_id": band_id,
                    "chapter_start": int(band.get("chapter_start") or 0),
                    "chapter_end": int(band.get("chapter_end") or 0),
                    "status": str(band.get("status") or ""),
                    "directory": str(directory),
                    "metadata_sha256": sha256_file(metadata_path),
                    "gate_ledger_sha256": sha256_file(gate_path),
                    "cost_report_sha256": sha256_file(cost_path),
                    "rule_provenance_sha256": sha256_file(rule_path),
                }
            )
    return entries


async def collect_mcp_state(args: argparse.Namespace) -> dict[str, Any]:
    async with direct_mcp_client(args.mcp_url) as client:
        project = await call_mcp(
            client, "project_get", {"project_id": args.project_id}
        )
        chapters = await call_mcp(
            client, "chapter_list", {"project_id": args.project_id}
        )
        active = await call_mcp(
            client,
            "task_active_generation_check",
            {"project_id": args.project_id},
        )
        task_list = await call_mcp(client, "task_list", {"limit": 100})
        tasks_payload = (
            task_list.get("tasks", [])
            if isinstance(task_list, dict)
            else task_list
        )
        tasks = [
            item
            for item in tasks_payload or []
            if isinstance(item, dict)
            and str(item.get("project_id") or "") == args.project_id
        ]
        gate_ledger = await call_mcp_report(
            client,
            "gate_ledger_report",
            {"scope": "project", "project_id": args.project_id, "format": "json"},
        )
        cost_report = await call_mcp_report(
            client,
            "cost_report",
            {"project_id": args.project_id, "format": "json"},
        )
        rule_report = await call_mcp_report(
            client,
            "rule_provenance_report",
            {"project_id": args.project_id, "format": "json"},
        )
    if isinstance(chapters, dict):
        chapters = chapters.get("chapters", [])
    return {
        "project": project,
        "chapters": chapters or [],
        "active_task_check": active,
        "tasks": tasks,
        "gate_ledger": gate_ledger,
        "cost_report": cost_report,
        "rule_provenance": rule_report,
    }


def grouped_counts(connection: Any, statement: str, project_id: str) -> dict[str, int]:
    return {
        str(key or "unknown"): int(total or 0)
        for key, total in connection.execute(text(statement), {"project_id": project_id})
    }


def scalar_counts(connection: Any, statement: str, project_id: str) -> dict[str, int]:
    row = connection.execute(text(statement), {"project_id": project_id}).mappings().one()
    return {str(key): int(value or 0) for key, value in row.items()}


def collect_freeze_audit_state(
    connection: Any,
    project_id: str,
) -> dict[str, Any]:
    runtime_policy_version = int(
        connection.execute(
            text(
                "SELECT runtime_policy_version FROM projects "
                "WHERE id=:project_id"
            ),
            {"project_id": project_id},
        ).scalar_one()
        or 0
    )
    policy_events = [
        list(row)
        for row in connection.execute(
            text(
                """
                SELECT id,event_family,event_type,actor_type,actor_id,
                       summary,reason,payload_json::jsonb,created_at
                FROM decision_events
                WHERE project_id=:project_id
                  AND event_type='runtime_policy_updated'
                ORDER BY created_at,id
                """
            ),
            {"project_id": project_id},
        )
    ]
    active_rule_events = [
        list(row)
        for row in connection.execute(
            text(
                """
                SELECT id,signal_id,signal_type,chapter_number,subject_key,
                       status,payload_json::jsonb,evidence_refs_json::jsonb,
                       created_at,resolved_at
                FROM canon_quality_signals
                WHERE project_id=:project_id
                  AND signal_type IN (
                    'active_rule_registered',
                    'active_rule_status_changed'
                  )
                ORDER BY created_at,id
                """
            ),
            {"project_id": project_id},
        )
    ]
    return {
        "runtime_policy_version": runtime_policy_version,
        "runtime_policy_update_events": len(policy_events),
        "runtime_policy_update_sha256": canonical_hash(policy_events),
        "active_rule_events": len(active_rule_events),
        "active_rule_event_sha256": canonical_hash(active_rule_events),
    }


def collect_database_freeze_audit(
    database_url: str,
    project_id: str,
) -> dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return collect_freeze_audit_state(connection, project_id)
    finally:
        engine.dispose()


def collect_database_state(database_url: str, project_id: str) -> dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            canon = scalar_counts(
                connection,
                """
                SELECT
                  count(*) FILTER (WHERE c.status='committed') AS committed,
                  count(*) FILTER (WHERE c.status<>'committed') AS non_committed,
                  count(DISTINCT c.chapter_number) FILTER (WHERE c.status='committed') AS distinct_chapters,
                  coalesce(min(c.chapter_number) FILTER (WHERE c.status='committed'),0) AS first_chapter,
                  coalesce(max(c.chapter_number) FILTER (WHERE c.status='committed'),0) AS last_chapter,
                  count(*)-count(DISTINCT c.id) AS duplicate_ids,
                  count(*)-count(DISTINCT c.candidate_id) AS duplicate_candidate_refs,
                  count(*)-count(DISTINCT c.idempotency_key) AS duplicate_idempotency_keys,
                  (SELECT count(*) FROM (
                     SELECT chapter_number FROM canon_commit_records
                     WHERE project_id=:project_id GROUP BY chapter_number HAVING count(*)>1
                   ) duplicate_chapter) AS duplicate_chapters,
                  count(*) FILTER (WHERE ws.id IS NULL) AS missing_world_snapshot_refs,
                  count(*) FILTER (WHERE ms.id IS NULL) AS missing_map_snapshot_refs,
                  count(*) FILTER (WHERE ws.id IS NOT NULL AND (ws.project_id<>c.project_id OR ws.as_of_chapter<>c.chapter_number)) AS wrong_world_snapshot_identity,
                  count(*) FILTER (WHERE ms.id IS NOT NULL AND (ms.project_id<>c.project_id OR ms.as_of_chapter<>c.chapter_number)) AS wrong_map_snapshot_identity,
                  count(*) FILTER (WHERE d.id IS NULL OR d.project_id<>c.project_id OR d.chapter_number<>c.chapter_number OR d.status<>'accepted' OR d.canon_status<>'canon' OR d.canon_commit_id<>c.id OR d.idempotency_key<>c.idempotency_key) AS candidate_identity_mismatches,
                  count(*) FILTER (WHERE cp.id IS NULL OR cp.project_id<>c.project_id OR cp.chapter_number<>c.chapter_number OR cp.status<>'accepted') AS chapter_identity_mismatches
                FROM canon_commit_records c
                LEFT JOIN candidate_draft_records d ON d.id=c.candidate_id
                LEFT JOIN chapter_plans cp ON cp.id=d.chapter_plan_id
                LEFT JOIN world_snapshots ws ON ws.id=c.world_snapshot_id
                LEFT JOIN map_snapshots ms ON ms.id=c.map_snapshot_id
                WHERE c.project_id=:project_id
                """,
                project_id,
            )
            candidates = scalar_counts(
                connection,
                """
                SELECT
                  count(*) FILTER (WHERE d.status='accepted' AND d.canon_status='canon') AS accepted_canon,
                  count(*) FILTER (WHERE d.status='accepted' AND d.canon_status='canon' AND d.canon_commit_id='') AS missing_commit_id,
                  count(*) FILTER (WHERE d.status='accepted' AND d.canon_status='canon' AND (
                    c.id IS NULL OR c.status<>'committed' OR c.project_id<>d.project_id
                    OR c.chapter_number<>d.chapter_number OR c.candidate_id<>d.id
                    OR c.idempotency_key<>d.idempotency_key OR cp.id IS NULL
                    OR cp.project_id<>d.project_id OR cp.chapter_number<>d.chapter_number
                    OR cp.status<>'accepted'
                  )) AS reverse_identity_mismatches,
                  (SELECT count(*) FROM (
                    SELECT chapter_number FROM candidate_draft_records
                    WHERE project_id=:project_id AND status='accepted' AND canon_status='canon'
                    GROUP BY chapter_number HAVING count(*)>1
                  ) duplicate_chapter) AS duplicate_accepted_chapters
                FROM candidate_draft_records d
                LEFT JOIN canon_commit_records c ON c.id=d.canon_commit_id
                LEFT JOIN chapter_plans cp ON cp.id=d.chapter_plan_id
                WHERE d.project_id=:project_id
                """,
                project_id,
            )
            graph = scalar_counts(
                connection,
                """
                WITH refs AS (
                  SELECT c.id canon_commit_id, c.chapter_number, value graph_delta_id
                  FROM canon_commit_records c
                  CROSS JOIN LATERAL jsonb_array_elements_text(c.graph_delta_ids_json::jsonb) value
                  WHERE c.project_id=:project_id AND c.status='committed'
                ), delta_payloads AS (
                  SELECT
                    g.id,
                    jsonb_build_object(
                      'project_id',g.project_id,
                      'chapter_number',g.chapter_number,
                      'story_time',g.story_time,
                      'delta_type',g.delta_type,
                      'operation',g.operation,
                      'target_type',g.target_type,
                      'target_id',g.target_id,
                      'source_type',g.source_type,
                      'source_id',g.source_id,
                      'world_line_id',g.world_line_id,
                      'summary',g.summary,
                      'evidence_refs',g.evidence_refs_json::jsonb,
                      'review_verdict_id',g.review_verdict_id,
                      'allowed_for_canon',g.allowed_for_canon,
                      'metadata',g.metadata_json::jsonb,
                      'patches',coalesce((
                        SELECT jsonb_agg(
                          jsonb_build_object(
                            'project_id',p.project_id,
                            'chapter_number',p.chapter_number,
                            'patch_type',p.patch_type,
                            'target_ref',p.target_ref,
                            'op',p.op,
                            'field_path',p.field_path,
                            'old_value',p.old_value_json::jsonb,
                            'new_value',p.new_value_json::jsonb,
                            'reason',p.reason,
                            'visibility_default',p.visibility_default,
                            'metadata',p.metadata_json::jsonb - 'sequence'
                          )
                          ORDER BY
                            coalesce(
                              (p.metadata_json::jsonb->>'sequence')::int,
                              1000000000
                            ),
                            p.created_at,
                            p.id
                        )
                        FROM graph_delta_patches p
                        WHERE p.project_id=g.project_id AND p.delta_id=g.id
                      ),'[]'::jsonb)
                    ) payload
                  FROM graph_deltas g
                  WHERE g.project_id=:project_id
                ), duplicate_delta_payloads AS (
                  SELECT count(*) duplicate_count
                  FROM delta_payloads
                  GROUP BY payload
                  HAVING count(*)>1
                ), patch_payloads AS (
                  SELECT
                    p.delta_id,
                    jsonb_build_object(
                      'project_id',p.project_id,
                      'chapter_number',p.chapter_number,
                      'patch_type',p.patch_type,
                      'target_ref',p.target_ref,
                      'op',p.op,
                      'field_path',p.field_path,
                      'old_value',p.old_value_json::jsonb,
                      'new_value',p.new_value_json::jsonb,
                      'reason',p.reason,
                      'visibility_default',p.visibility_default,
                      'metadata',p.metadata_json::jsonb - 'sequence'
                    ) payload
                  FROM graph_delta_patches p
                  WHERE p.project_id=:project_id
                ), duplicate_patch_payloads AS (
                  SELECT count(*) duplicate_count
                  FROM patch_payloads
                  GROUP BY delta_id,payload
                  HAVING count(*)>1
                )
                SELECT
                  (SELECT count(*) FROM graph_deltas WHERE project_id=:project_id) AS graph_deltas,
                  count(*) AS canon_graph_refs,
                  count(*) FILTER (WHERE g.id IS NULL) AS missing_graph_delta_refs,
                  count(*) FILTER (WHERE g.id IS NOT NULL AND (g.project_id<>:project_id OR g.chapter_number<>refs.chapter_number OR NOT g.allowed_for_canon)) AS invalid_graph_delta_refs,
                  (SELECT count(*) FROM (
                    SELECT graph_delta_id FROM refs GROUP BY graph_delta_id HAVING count(*)>1
                  ) duplicate_ref) AS duplicate_graph_delta_refs,
                  (SELECT count(*) FROM graph_deltas unreferenced
                   WHERE unreferenced.project_id=:project_id
                     AND unreferenced.chapter_number>0
                     AND NOT EXISTS (
                       SELECT 1 FROM refs
                       WHERE refs.graph_delta_id=unreferenced.id
                     )) AS unreferenced_chapter_graph_deltas,
                  (SELECT count(*) FROM graph_delta_patches p
                   LEFT JOIN graph_deltas owner ON owner.id=p.delta_id
                   WHERE (p.project_id=:project_id OR owner.project_id=:project_id)
                     AND (
                       owner.id IS NULL
                       OR owner.project_id<>p.project_id
                       OR owner.chapter_number<>p.chapter_number
                     )) AS orphan_graph_delta_patches,
                  (SELECT coalesce(sum(duplicate_count-1),0)
                   FROM duplicate_delta_payloads) AS duplicate_semantic_graph_deltas,
                  (SELECT coalesce(sum(duplicate_count-1),0)
                   FROM duplicate_patch_payloads) AS duplicate_semantic_graph_delta_patches
                FROM refs LEFT JOIN graph_deltas g ON g.id=refs.graph_delta_id
                """,
                project_id,
            )
            snapshots = scalar_counts(
                connection,
                """
                SELECT
                  (SELECT count(*) FROM world_snapshots WHERE project_id=:project_id) AS world_snapshot_count,
                  (SELECT coalesce(max(as_of_chapter),0) FROM world_snapshots WHERE project_id=:project_id) AS world_snapshot_through,
                  (SELECT count(*) FROM map_snapshots WHERE project_id=:project_id) AS map_snapshot_count,
                  (SELECT coalesce(max(as_of_chapter),0) FROM map_snapshots WHERE project_id=:project_id) AS map_snapshot_through
                """,
                project_id,
            )
            entities = scalar_counts(
                connection,
                """
                SELECT
                  (SELECT count(*) FROM entities WHERE project_id=:project_id) AS entity_count,
                  (SELECT count(*) FROM (
                    SELECT kind, lower(trim(name)) identity FROM entities
                    WHERE project_id=:project_id AND is_active
                    GROUP BY kind, lower(trim(name)) HAVING count(*)>1
                  ) duplicate_entity_identity) AS duplicate_entity_identities,
                  (SELECT count(*) FROM entity_aliases WHERE project_id=:project_id) AS alias_count,
                  (SELECT count(*) FROM (
                    SELECT lower(trim(alias)) identity FROM entity_aliases
                    WHERE project_id=:project_id
                    GROUP BY lower(trim(alias)) HAVING count(*)>1
                  ) duplicate_alias_identity) AS duplicate_alias_identities,
                  (SELECT count(*) FROM entity_aliases a LEFT JOIN entities e ON e.id=a.entity_id
                    WHERE a.project_id=:project_id AND (e.id IS NULL OR e.project_id<>a.project_id)) AS orphan_aliases
                """,
                project_id,
            )
            outbox_counts = grouped_counts(
                connection,
                "SELECT status,count(*) FROM outbox_events WHERE aggregate_id=:project_id GROUP BY status ORDER BY status",
                project_id,
            )
            outbox_types = grouped_counts(
                connection,
                "SELECT event_type,count(*) FROM outbox_events WHERE aggregate_id=:project_id GROUP BY event_type ORDER BY event_type",
                project_id,
            )
            outbox = scalar_counts(
                connection,
                """
                SELECT
                  count(*) AS total,
                  count(*)-count(DISTINCT event_id) AS duplicate_event_ids,
                  count(*) FILTER (WHERE status<>'processed') AS backlog,
                  count(*) FILTER (WHERE status='failed') AS failed
                FROM outbox_events WHERE aggregate_id=:project_id
                """,
                project_id,
            )
            outbox_identity = scalar_counts(
                connection,
                """
                WITH commits AS (
                  SELECT c.id,c.idempotency_key,c.project_id,c.chapter_number,
                         c.candidate_id,d.body_hash,cp.title chapter_title
                  FROM canon_commit_records c
                  JOIN candidate_draft_records d ON d.id=c.candidate_id
                  JOIN chapter_plans cp ON cp.id=d.chapter_plan_id
                  WHERE c.project_id=:project_id AND c.status='committed'
                ), expected AS (
                  SELECT commits.*,event_type
                  FROM commits CROSS JOIN (VALUES
                    ('canon.projection.requested'),
                    ('canon.phase3.requested'),
                    ('canon.publisher.requested')
                  ) types(event_type)
                ), matched AS (
                  SELECT e.*,o.id outbox_id,o.event_id,o.aggregate_type,o.aggregate_id,
                         o.payload_json::jsonb payload
                  FROM expected e
                  LEFT JOIN outbox_events o
                    ON o.event_id=e.idempotency_key || ':' || e.event_type
                )
                SELECT
                  count(*) FILTER (WHERE outbox_id IS NULL) AS missing_expected_events,
                  count(*) FILTER (WHERE outbox_id IS NOT NULL AND (
                    aggregate_type<>'project' OR aggregate_id<>project_id
                    OR payload->>'schema_version'<>'1'
                    OR payload->>'canon_commit_id'<>id
                    OR payload->>'canon_idempotency_key'<>idempotency_key
                    OR payload->>'project_id'<>project_id
                    OR NULLIF(payload->>'chapter_number','')::int<>chapter_number
                    OR payload->>'candidate_id'<>candidate_id
                    OR (event_type='canon.publisher.requested' AND (
                      payload->>'body_sha256'<>body_hash
                      OR payload->>'chapter_title'<>chapter_title
                      OR COALESCE(NULLIF(payload->>'publish','')::boolean,true)
                    ))
                  )) AS identity_mismatches,
                  (SELECT count(*) FROM outbox_events o
                   WHERE o.aggregate_id=:project_id
                     AND o.event_type IN (
                       'canon.projection.requested',
                       'canon.phase3.requested',
                       'canon.publisher.requested'
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM expected e
                       WHERE o.event_id=e.idempotency_key || ':' || e.event_type
                     )) AS unexpected_canon_events
                FROM matched
                """,
                project_id,
            )
            outbox.update(outbox_identity)
            outbox["status_counts"] = outbox_counts
            outbox["event_type_counts"] = outbox_types

            projection_target_row = connection.execute(
                text(
                    """
                    SELECT id canon_commit_id,idempotency_key,chapter_number
                    FROM canon_commit_records
                    WHERE project_id=:project_id AND status='committed'
                    ORDER BY chapter_number DESC,created_at DESC,id DESC
                    LIMIT 1
                    """
                ),
                {"project_id": project_id},
            ).mappings().one_or_none()
            projection_chapters = [
                {
                    "chapter_number": int(row["chapter_number"]),
                    "title": str(row["title"] or ""),
                    "summary": str(row["summary"] or ""),
                    "body": str(row["body"] or ""),
                }
                for row in connection.execute(
                    text(
                        """
                        SELECT cp.chapter_number,cp.title,cd.summary,cd.body_text body
                        FROM canon_commit_records c
                        JOIN candidate_draft_records d ON d.id=c.candidate_id
                        JOIN chapter_plans cp ON cp.id=d.chapter_plan_id
                        JOIN chapter_drafts cd ON cd.id=d.candidate_draft_id
                        WHERE c.project_id=:project_id
                          AND c.status='committed'
                          AND d.status='accepted'
                          AND cp.status='accepted'
                        ORDER BY cp.chapter_number ASC
                        """
                    ),
                    {"project_id": project_id},
                ).mappings()
            ]
            projection_target = {
                "canon_commit_id": str(
                    (projection_target_row or {}).get("canon_commit_id") or ""
                ),
                "chapter_number": int(
                    (projection_target_row or {}).get("chapter_number") or 0
                ),
                "event_id": (
                    f"{projection_target_row['idempotency_key']}:"
                    "canon.projection.requested"
                    if projection_target_row
                    else ""
                ),
                "chapter_memory_source_digest": projection_source_digest(
                    projection_chapters
                ),
                "chapter_memory_payload_hashes": {
                    chapter_memory_point_id(
                        project_id,
                        int(chapter["chapter_number"]),
                    ): canonical_hash(
                        {
                            "project_id": project_id,
                            "chapter_number": int(
                                chapter["chapter_number"]
                            ),
                            "title": chapter["title"],
                            "summary": chapter["summary"],
                            "excerpt": chapter["body"][:500],
                        }
                    )
                    for chapter in projection_chapters
                },
                "llm_kb_source_digest": llm_kb_source_digest(
                    engine,
                    project_id,
                    int((projection_target_row or {}).get("chapter_number") or 0),
                ),
            }
            projection_rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT projection_kind,status,target_canon_commit_id,
                               target_chapter_number,projected_canon_commit_id,
                               projected_chapter_number,last_event_id,
                               source_digest,last_error
                        FROM projection_checkpoints WHERE project_id=:project_id
                        ORDER BY projection_kind
                        """
                    ),
                    {"project_id": project_id},
                ).mappings()
            ]
            maintenance_counts = grouped_counts(
                connection,
                "SELECT status,count(*) FROM post_canon_maintenance_runs WHERE project_id=:project_id GROUP BY status ORDER BY status",
                project_id,
            )
            maintenance_steps = grouped_counts(
                connection,
                "SELECT step_name,count(*) FROM post_canon_maintenance_runs WHERE project_id=:project_id GROUP BY step_name ORDER BY step_name",
                project_id,
            )
            maintenance = scalar_counts(
                connection,
                """
                SELECT
                  count(*) AS total,
                  count(*) FILTER (WHERE status<>'succeeded') AS backlog,
                  count(*)-count(DISTINCT idempotency_key) AS duplicate_idempotency_keys,
                  (SELECT count(*) FROM (
                    SELECT canon_commit_id,step_name FROM post_canon_maintenance_runs
                    WHERE project_id=:project_id GROUP BY canon_commit_id,step_name HAVING count(*)>1
                  ) duplicate_step) AS duplicate_canon_steps
                FROM post_canon_maintenance_runs WHERE project_id=:project_id
                """,
                project_id,
            )
            maintenance["status_counts"] = maintenance_counts
            maintenance["step_counts"] = maintenance_steps

            task_rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT id,status,current_stage,current_chapter,requested_chapters,
                               lease_owner,lease_epoch,lease_expires_at,heartbeat_at,
                               started_at,finished_at,deleted_at,error_message
                        FROM generation_tasks WHERE project_id=:project_id
                        ORDER BY created_at
                        """
                    ),
                    {"project_id": project_id},
                ).mappings()
            ]
            reclaim_events = grouped_counts(
                connection,
                """
                SELECT event_type,count(*) FROM decision_events
                WHERE project_id=:project_id AND event_type ILIKE '%reclaim%'
                GROUP BY event_type ORDER BY event_type
                """,
                project_id,
            )
            active_task_rows = sum(
                1
                for row in task_rows
                if row.get("deleted_at") is None
                and str(row.get("status") or "")
                not in TERMINAL_GENERATION_STATUSES
            )

            publisher_status = grouped_counts(
                connection,
                "SELECT status,count(*) FROM publisher_upload_jobs WHERE project_id=:project_id GROUP BY status ORDER BY status",
                project_id,
            )
            publisher = scalar_counts(
                connection,
                """
                SELECT
                  count(*) AS jobs,
                  count(*) FILTER (WHERE status NOT IN ('scheduled','succeeded','cancelled','canceled')) AS unsettled_jobs,
                  count(*) FILTER (WHERE idempotency_key<>'')
                    - count(DISTINCT idempotency_key) FILTER (WHERE idempotency_key<>'') AS duplicate_job_idempotency_keys,
                  (SELECT count(*) FROM (
                    SELECT canon_commit_id,platform_id FROM publisher_upload_jobs
                    WHERE project_id=:project_id AND canon_commit_id IS NOT NULL
                    GROUP BY canon_commit_id,platform_id HAVING count(*)>1
                  ) duplicate_canon_platform) AS duplicate_canon_platform_jobs,
                  (SELECT count(*) FROM publisher_upload_attempts a
                    JOIN publisher_upload_jobs j ON j.id=a.upload_job_id
                    WHERE j.project_id=:project_id) AS attempts,
                  (SELECT count(*) FROM publisher_upload_receipts r
                    JOIN publisher_upload_jobs j ON j.id=r.upload_job_id
                    WHERE j.project_id=:project_id) AS receipts,
                  (SELECT count(*) FROM (
                    SELECT r.receipt_key FROM publisher_upload_receipts r
                    JOIN publisher_upload_jobs j ON j.id=r.upload_job_id
                    WHERE j.project_id=:project_id GROUP BY r.receipt_key HAVING count(*)>1
                  ) duplicate_receipt_keys) AS duplicate_receipt_keys,
                  (SELECT count(*) FROM publisher_upload_jobs j
                    LEFT JOIN publisher_upload_attempts a ON a.id=j.current_attempt_id
                    WHERE j.project_id=:project_id AND j.current_attempt_id<>''
                      AND (a.id IS NULL OR a.upload_job_id<>j.id)) AS invalid_current_attempt_refs
                FROM publisher_upload_jobs WHERE project_id=:project_id
                """,
                project_id,
            )
            publisher["status_counts"] = publisher_status

            band_rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT DISTINCT ON (band_id)
                               id,band_id,arc_id,chapter_start,chapter_end,
                               boundary_chapter,status,resolved_at,updated_at
                        FROM band_checkpoints
                        WHERE project_id=:project_id AND band_id<>''
                        ORDER BY band_id,updated_at DESC,created_at DESC
                        """
                    ),
                    {"project_id": project_id},
                ).mappings()
            ]
            freeze_audit = collect_freeze_audit_state(
                connection,
                project_id,
            )

            return {
                "collected_at": now(),
                "canon": canon,
                "candidates": candidates,
                "graph": graph,
                "snapshots": snapshots,
                "entities": entities,
                "outbox": outbox,
                "projections": projection_rows,
                "projection_target": projection_target,
                "freeze_audit": freeze_audit,
                "maintenance": maintenance,
                "tasks": {
                    "rows": task_rows,
                    "reclaim_event_counts": reclaim_events,
                    "active_rows": active_task_rows,
                    "max_lease_epoch": max(
                        [int(row.get("lease_epoch") or 0) for row in task_rows] or [0]
                    ),
                },
                "publisher": publisher,
                "bands": sorted(
                    band_rows,
                    key=lambda item: (
                        int(item.get("chapter_start") or 0),
                        int(item.get("chapter_end") or 0),
                    ),
                ),
            }
    finally:
        engine.dispose()


def accepted_count(project: dict[str, Any]) -> int:
    return int(project.get("accepted_chapter_count") or 0)


def needs_review_count(project: dict[str, Any]) -> int:
    return int(project.get("needs_review_chapter_count") or 0)


def failed_chapters(project: dict[str, Any]) -> list[int]:
    control = project.get("generation_control") or {}
    return [int(item) for item in control.get("failed_chapters") or []]


def assert_project_identity(args: argparse.Namespace, project: dict[str, Any]) -> None:
    if str(project.get("id") or "") != args.project_id:
        raise EvidenceError("project_get returned the wrong project")
    target = int(project.get("target_total_chapters") or 0)
    if target != args.expected_target:
        raise EvidenceError(f"project target is {target}, expected {args.expected_target}")


def assert_policy_and_rules_frozen(
    run_manifest: dict[str, Any],
    policy: dict[str, Any],
    rule_report: dict[str, Any],
) -> dict[str, str]:
    current_policy_hash = canonical_hash(policy)
    current_rule_hash = canonical_hash(frozen_rule_state(rule_report))
    expected_policy_hash = str(run_manifest.get("policy_hash") or "")
    expected_rule_hash = str(run_manifest.get("rule_state_hash") or "")
    if current_policy_hash != expected_policy_hash:
        raise FreezeViolation(
            "threshold", "project policy changed during no-hotfix run"
        )
    if current_rule_hash != expected_rule_hash:
        raise FreezeViolation("rule_state", "rule state changed during no-hotfix run")
    return {
        "policy_hash": current_policy_hash,
        "rule_state_hash": current_rule_hash,
    }


def verify_freeze_audit(
    run_manifest: dict[str, Any],
    current: dict[str, Any],
) -> None:
    if current != run_manifest.get("freeze_audit_state"):
        raise FreezeViolation(
            "rule_state",
            "policy/rule freeze audit ledger changed during run",
        )


def manifest_path(args: argparse.Namespace) -> Path:
    return args.output_dir.resolve() / "manifest.json"


def connection_hashes(args: argparse.Namespace) -> dict[str, str]:
    database = make_url(args.database_url)
    database_target = {
        "drivername": database.drivername,
        "host": database.host or "",
        "port": int(database.port or 0),
        "database": database.database or "",
    }

    def http_target(raw: str) -> dict[str, Any]:
        url = httpx.URL(raw)
        return {
            "scheme": url.scheme,
            "host": url.host,
            "port": int(url.port or 0),
            "path": url.path.rstrip("/"),
        }

    return {
        "api_target": canonical_hash(http_target(args.api_url)),
        "mcp_target": canonical_hash(http_target(args.mcp_url)),
        "database_target": canonical_hash(database_target),
    }


def _loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(str(host or "")).is_loopback
    except ValueError:
        return False


def _service_container(
    freeze_identity: dict[str, Any],
    service: str,
) -> dict[str, Any]:
    matches = [
        item
        for key in (
            "runtime_containers",
            "publisher_browser_containers",
            "dependency_containers",
        )
        for item in freeze_identity.get(key) or []
        if str(item.get("compose_service") or "") == service
    ]
    if len(matches) != 1:
        raise FreezeViolation(
            "config",
            f"expected one verified {service} container, found {len(matches)}",
        )
    return matches[0]


def _published_endpoint(
    container: dict[str, Any],
    *,
    container_port: str,
    host: str,
    port: int,
    label: str,
) -> dict[str, Any]:
    if not _loopback_host(host):
        raise FreezeViolation(
            "config", f"{label} endpoint is not bound to a loopback host"
        )
    bindings = (
        container.get("published_ports") or {}
    ).get(container_port) or []
    matching = [
        binding
        for binding in bindings
        if _loopback_host(str(binding.get("host_ip") or ""))
        and int(binding.get("host_port") or 0) == port
    ]
    if len(matching) != 1:
        raise FreezeViolation(
            "config",
            f"{label} endpoint {host}:{port} is not the verified "
            f"{container.get('compose_service')} {container_port} binding",
        )
    binding = matching[0]
    return {
        "container_id": str(container.get("container_id") or ""),
        "container_name": str(container.get("name") or ""),
        "compose_service": str(container.get("compose_service") or ""),
        "container_port": container_port,
        "host_ip": str(binding.get("host_ip") or ""),
        "host_port": int(binding.get("host_port") or 0),
    }


def _single_loopback_published_endpoint(
    container: dict[str, Any],
    *,
    container_port: str,
    label: str,
) -> dict[str, Any]:
    bindings = [
        binding
        for binding in (
            (container.get("published_ports") or {}).get(container_port)
            or []
        )
        if _loopback_host(str(binding.get("host_ip") or ""))
        and int(binding.get("host_port") or 0) > 0
    ]
    if len(bindings) != 1:
        raise FreezeViolation(
            "config",
            f"{label} must have exactly one loopback {container_port} binding",
        )
    binding = bindings[0]
    return {
        "container_id": str(container.get("container_id") or ""),
        "container_name": str(container.get("name") or ""),
        "compose_service": str(container.get("compose_service") or ""),
        "container_port": container_port,
        "host_ip": str(binding.get("host_ip") or ""),
        "host_port": int(binding.get("host_port") or 0),
    }


def connection_bindings(
    args: argparse.Namespace,
    freeze_identity: dict[str, Any],
) -> dict[str, Any]:
    api_url = httpx.URL(args.api_url)
    if (
        api_url.scheme != "http"
        or api_url.path.rstrip("/")
        or api_url.query
        or api_url.fragment
    ):
        raise FreezeViolation(
            "config", "API URL must be a direct HTTP service root"
        )
    mcp_url = httpx.URL(args.mcp_url)
    if (
        mcp_url.scheme != "http"
        or mcp_url.path.rstrip("/") != "/mcp"
        or mcp_url.query
        or mcp_url.fragment
    ):
        raise FreezeViolation(
            "config", "MCP URL must be the direct HTTP /mcp endpoint"
        )
    database_url = make_url(args.database_url)
    if (
        not database_url.drivername.startswith("postgresql")
        or not database_url.database
    ):
        raise FreezeViolation(
            "config", "database URL must target a named PostgreSQL database"
        )
    api = _published_endpoint(
        _service_container(freeze_identity, "forwin"),
        container_port="8899/tcp",
        host=str(api_url.host or ""),
        port=int(api_url.port or 0),
        label="API",
    )
    api.update(
        {
            "scheme": api_url.scheme,
            "path": api_url.path.rstrip("/"),
        }
    )
    mcp = _published_endpoint(
        _service_container(freeze_identity, "forwin-mcp"),
        container_port="8896/tcp",
        host=str(mcp_url.host or ""),
        port=int(mcp_url.port or 0),
        label="MCP",
    )
    mcp.update(
        {
            "scheme": mcp_url.scheme,
            "path": mcp_url.path.rstrip("/"),
        }
    )
    database = _published_endpoint(
        _service_container(freeze_identity, "postgres"),
        container_port="5432/tcp",
        host=str(database_url.host or ""),
        port=int(database_url.port or 0),
        label="database",
    )
    database.update(
        {
            "drivername": database_url.drivername,
            "database": database_url.database,
        }
    )
    qdrant = _single_loopback_published_endpoint(
        _service_container(freeze_identity, "qdrant"),
        container_port="6333/tcp",
        label="Qdrant",
    )
    return {
        "compose_project": str(
            freeze_identity.get("compose_project") or ""
        ),
        "api": api,
        "mcp": mcp,
        "database": database,
        "qdrant": qdrant,
    }


def verify_connection_bindings(
    run_manifest: dict[str, Any],
    current: dict[str, Any],
) -> None:
    if current != run_manifest.get("connection_bindings"):
        raise FreezeViolation(
            "config", "API/MCP/database container bindings changed during run"
        )


def verify_run_inputs(
    args: argparse.Namespace,
    run_manifest: dict[str, Any],
) -> None:
    collector = run_manifest.get("collector") or {}
    if str(collector.get("sha256") or "") != sha256_file(Path(__file__).resolve()):
        raise FreezeViolation("code", "L200 evidence collector changed during run")
    rc_identity = run_manifest.get("rc_manifest") or {}
    if str(rc_identity.get("path") or "") != str(args.rc_manifest.resolve()):
        raise FreezeViolation("config", "RC manifest path changed during run")
    if str(rc_identity.get("sha256") or "") != sha256_file(args.rc_manifest.resolve()):
        raise FreezeViolation("config", "RC manifest content changed during run")
    if run_manifest.get("connection_hashes") != connection_hashes(args):
        raise FreezeViolation("config", "API/MCP/database target changed during run")
    if (
        run_manifest.get("quality_profile") != args.quality_profile
        or run_manifest.get("gate_delegate") != args.gate_delegate
    ):
        raise FreezeViolation("config", "selected L200 profile/delegate changed")
    expected_containers = run_manifest.get("container_names") or {}
    if sorted(args.runtime_container) != sorted(expected_containers.get("runtime") or []):
        raise FreezeViolation("config", "runtime container set changed during run")
    if sorted(args.browser_container) != sorted(expected_containers.get("browser") or []):
        raise FreezeViolation("config", "browser container set changed during run")
    if sorted(args.dependency_container) != sorted(
        expected_containers.get("dependency") or []
    ):
        raise FreezeViolation("config", "dependency container set changed during run")


def verify_band_artifact_entries(
    output_dir: Path,
    entries: list[dict[str, Any]],
) -> None:
    band_root = (output_dir / "bands").resolve()
    for entry in entries:
        directory = Path(str(entry.get("directory") or "")).resolve()
        if not directory.is_relative_to(band_root):
            raise FreezeViolation("evidence", "band evidence path escaped output root")
        for field, filename in (
            ("metadata_sha256", "metadata.json"),
            ("gate_ledger_sha256", "gate-ledger.json"),
            ("cost_report_sha256", "cost-report.json"),
            ("rule_provenance_sha256", "rule-provenance.json"),
        ):
            path = directory / filename
            if not path.is_file() or sha256_file(path) != str(entry.get(field) or ""):
                raise FreezeViolation(
                    "evidence", f"band evidence changed or disappeared: {path}"
                )


def checkpoint_chain_anchor(run_manifest: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "run": run_manifest.get("run"),
            "project_id": run_manifest.get("project_id"),
            "initialized_at": run_manifest.get("initialized_at"),
            "rc_manifest_sha256": (run_manifest.get("rc_manifest") or {}).get(
                "sha256"
            ),
        }
    )


def verify_checkpoint_artifact_entries(
    output_dir: Path,
    run_manifest: dict[str, Any],
) -> None:
    entries = list(run_manifest.get("checkpoints") or [])
    chapters = [int(item.get("chapter") or 0) for item in entries]
    if chapters != list(CHECKPOINTS[: len(entries)]):
        raise FreezeViolation("evidence", "checkpoint entries are not a schedule prefix")
    previous_chain = str(
        run_manifest.get("checkpoint_chain_anchor")
        or checkpoint_chain_anchor(run_manifest)
    )
    previous_time = datetime.fromisoformat(str(run_manifest["initialized_at"]))
    checkpoint_root = (output_dir / "checkpoints").resolve()
    for index, entry in enumerate(entries):
        chapter = int(entry.get("chapter") or 0)
        relative_path = Path(str(entry.get("path") or ""))
        expected_relative = Path("checkpoints") / f"{chapter:03d}.json"
        if relative_path != expected_relative:
            raise FreezeViolation(
                "evidence", f"checkpoint {chapter} path identity changed"
            )
        path = (output_dir / relative_path).resolve()
        if not path.is_relative_to(checkpoint_root) or not path.is_file():
            raise FreezeViolation(
                "evidence", f"checkpoint {chapter} file is missing or escaped root"
            )
        digest = sha256_file(path)
        if digest != str(entry.get("sha256") or ""):
            raise FreezeViolation("evidence", f"checkpoint {chapter} hash changed")
        payload = load_json(path)
        observed_at = str(entry.get("observed_at") or "")
        accepted = int(entry.get("accepted_at_observation") or 0)
        if (
            int(payload.get("requested_checkpoint") or 0) != chapter
            or int(payload.get("accepted_at_observation") or 0) != accepted
            or str(payload.get("observed_at") or "") != observed_at
        ):
            raise FreezeViolation(
                "evidence", f"checkpoint {chapter} manifest/payload mismatch"
            )
        next_boundary = CHECKPOINTS[index + 1] if index + 1 < len(CHECKPOINTS) else 201
        if accepted < chapter or accepted >= next_boundary:
            raise FreezeViolation(
                "evidence",
                f"checkpoint {chapter} was observed too late at accepted={accepted}",
            )
        observed_time = datetime.fromisoformat(observed_at)
        if observed_time <= previous_time:
            raise FreezeViolation(
                "evidence", f"checkpoint {chapter} timestamp is not monotonic"
            )
        chain_payload = {
            "chapter": chapter,
            "accepted_at_observation": accepted,
            "observed_at": observed_at,
            "path": relative_path.as_posix(),
            "sha256": digest,
            "previous_chain_sha256": previous_chain,
        }
        if str(entry.get("previous_chain_sha256") or "") != previous_chain:
            raise FreezeViolation(
                "evidence", f"checkpoint {chapter} previous chain changed"
            )
        expected_chain = canonical_hash(chain_payload)
        if str(entry.get("chain_sha256") or "") != expected_chain:
            raise FreezeViolation("evidence", f"checkpoint {chapter} chain changed")
        previous_chain = expected_chain
        previous_time = observed_time


def invalidate(args: argparse.Namespace, violation: FreezeViolation) -> None:
    path = manifest_path(args)
    if not path.is_file():
        return
    payload = load_json(path)
    payload["valid"] = False
    changes = payload.setdefault("observed_freeze_changes", {})
    changes[violation.category] = int(changes.get(violation.category) or 0) + 1
    if violation.category == "code":
        payload["code_changes_during_run"] = max(
            1, int(payload.get("code_changes_during_run") or 0)
        )
    payload["invalidated_at"] = now()
    payload["invalidated_category"] = violation.category
    payload["invalidated_reason"] = str(violation)
    write_json(path, payload)


async def init_run(args: argparse.Namespace) -> None:
    output = args.output_dir.resolve()
    path = output / "manifest.json"
    if path.exists():
        raise EvidenceError(f"L200 manifest already exists: {path}")
    rc_manifest = load_json(args.rc_manifest.resolve())
    rc_identity = validate_frozen_rc_manifest(args, rc_manifest)
    freeze = verify_frozen(args, rc_manifest)
    bindings = connection_bindings(args, freeze)
    live_schema = database_schema_identity(args.database_url)
    fresh_database = fresh_database_state(args.database_url, args.project_id)
    freeze_audit = collect_database_freeze_audit(
        args.database_url,
        args.project_id,
    )
    mcp = await collect_mcp_state(args)
    genesis = await fetch_genesis(args.mcp_url, args.project_id)
    project = mcp["project"]
    assert_project_identity(args, project)
    if accepted_count(project) != 0:
        raise EvidenceError("L200 must initialize before the first accepted chapter")
    if mcp["active_task_check"].get("has_active_generation_task"):
        raise EvidenceError("L200 must initialize before generation starts")
    assert_fresh_genesis_handoff(
        rc_identity=rc_identity,
        fresh_database=fresh_database,
        project=project,
        genesis=genesis,
    )
    policy = await fetch_policy(args.api_url, args.project_id)
    policy_value = policy.get("policy") or {}
    if policy_value.get("quality_profile") != args.quality_profile:
        raise EvidenceError("project quality profile differs from frozen L200 profile")
    if (policy_value.get("pause") or {}).get("gate_delegate") != args.gate_delegate:
        raise EvidenceError("project gate delegate differs from frozen L200 delegate")
    expected_model_profile = str(
        (rc_manifest.get("runtime_policy") or {}).get("model_profile_id")
        or ""
    )
    if str(policy_value.get("model_profile_id") or "") != expected_model_profile:
        raise EvidenceError(
            "project model profile differs from the frozen RC"
        )
    rule_state = frozen_rule_state(mcp["rule_provenance"])
    run_manifest = {
        "schema_version": 1,
        "run": "forwin-v5-final-l200-no-hotfix",
        "valid": True,
        "initialized_at": now(),
        "project_id": args.project_id,
        "expected_target": args.expected_target,
        "quality_profile": args.quality_profile,
        "gate_delegate": args.gate_delegate,
        "rc_identity": rc_identity,
        "rc_manifest": {
            "path": str(args.rc_manifest.resolve()),
            "sha256": sha256_file(args.rc_manifest.resolve()),
        },
        "connection_hashes": connection_hashes(args),
        "connection_bindings": bindings,
        "container_names": {
            "runtime": sorted(args.runtime_container),
            "browser": sorted(args.browser_container),
            "dependency": sorted(args.dependency_container),
        },
        "freeze_identity": freeze,
        "live_schema_identity": live_schema,
        "policy": policy,
        "policy_hash": canonical_hash(policy),
        "rule_state": rule_state,
        "rule_state_hash": canonical_hash(rule_state),
        "freeze_audit_state": freeze_audit,
        "checkpoint_schedule": list(CHECKPOINTS),
        "checkpoint_chain_anchor": "",
        "checkpoints": [],
        "band_reports": [],
        "code_changes_during_run": 0,
        "freeze_contract": {
            "code": 0,
            "prompt": 0,
            "model_routing": 0,
            "config": 0,
            "schema": 0,
            "rule_state": 0,
            "threshold": 0,
        },
        "initial_project": project,
        "initial_genesis": genesis,
        "initial_fresh_database": fresh_database,
        "collector": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
        },
    }
    run_manifest["checkpoint_chain_anchor"] = checkpoint_chain_anchor(run_manifest)
    write_json(path, run_manifest)
    print(path)


async def checkpoint(args: argparse.Namespace) -> None:
    if args.chapter not in CHECKPOINTS:
        raise EvidenceError(f"checkpoint must be one of {CHECKPOINTS}")
    path = manifest_path(args)
    run_manifest = load_json(path)
    if not run_manifest.get("valid"):
        raise EvidenceError("L200 run is already invalidated")
    verify_run_inputs(args, run_manifest)
    verify_band_artifact_entries(
        args.output_dir.resolve(), list(run_manifest.get("band_reports") or [])
    )
    verify_checkpoint_artifact_entries(args.output_dir.resolve(), run_manifest)
    existing_count = len(run_manifest.get("checkpoints") or [])
    if existing_count >= len(CHECKPOINTS):
        raise EvidenceError("all L200 checkpoints already exist")
    expected_next = CHECKPOINTS[existing_count]
    if args.chapter != expected_next:
        raise EvidenceError(
            f"next checkpoint is {expected_next}, not {args.chapter}"
        )
    rc_manifest = load_json(args.rc_manifest.resolve())
    freeze = verify_frozen(args, rc_manifest)
    bindings = connection_bindings(args, freeze)
    verify_connection_bindings(run_manifest, bindings)
    verify_runtime_configuration(run_manifest, freeze)
    live_schema = verify_database_schema(run_manifest, args.database_url)
    mcp = await collect_mcp_state(args)
    project = mcp["project"]
    assert_project_identity(args, project)
    accepted = accepted_count(project)
    if accepted < args.chapter:
        raise EvidenceError(
            f"checkpoint {args.chapter} requested at accepted={accepted}"
        )
    next_boundary = (
        CHECKPOINTS[CHECKPOINTS.index(args.chapter) + 1]
        if args.chapter != CHECKPOINTS[-1]
        else 201
    )
    if accepted >= next_boundary:
        raise FreezeViolation(
            "evidence",
            f"checkpoint {args.chapter} was missed; accepted already reached {accepted}",
        )
    policy = await fetch_policy(args.api_url, args.project_id)
    hashes = assert_policy_and_rules_frozen(
        run_manifest,
        policy,
        mcp["rule_provenance"],
    )
    database = collect_database_state(args.database_url, args.project_id)
    verify_freeze_audit(run_manifest, database["freeze_audit"])
    band_reports = await collect_completed_band_reports(
        args,
        bands=database["bands"],
        output_dir=args.output_dir.resolve(),
    )
    checkpoint_path = (
        args.output_dir.resolve() / "checkpoints" / f"{args.chapter:03d}.json"
    )
    if checkpoint_path.exists():
        raise EvidenceError(f"checkpoint already exists: {checkpoint_path}")
    payload = {
        "schema_version": 1,
        "requested_checkpoint": args.chapter,
        "observed_at": now(),
        "accepted_at_observation": accepted,
        "freeze": freeze,
        "connection_bindings": bindings,
        "live_schema_identity": live_schema,
        "freeze_hashes": hashes,
        "project": project,
        "active_task_check": mcp["active_task_check"],
        "tasks": mcp["tasks"],
        "gate_ledger": mcp["gate_ledger"],
        "cost_report": mcp["cost_report"],
        "rule_provenance": mcp["rule_provenance"],
        "database": database,
        "band_reports": band_reports,
    }
    write_json(checkpoint_path, payload)
    relative_path = Path("checkpoints") / checkpoint_path.name
    previous_chain = (
        str(run_manifest["checkpoints"][-1]["chain_sha256"])
        if run_manifest["checkpoints"]
        else str(run_manifest["checkpoint_chain_anchor"])
    )
    entry = {
        "chapter": args.chapter,
        "accepted_at_observation": accepted,
        "observed_at": payload["observed_at"],
        "path": relative_path.as_posix(),
        "sha256": sha256_file(checkpoint_path),
        "previous_chain_sha256": previous_chain,
    }
    entry["chain_sha256"] = canonical_hash(entry)
    run_manifest["checkpoints"].append(entry)
    run_manifest["band_reports"] = band_reports
    write_json(path, run_manifest)
    print(checkpoint_path)


def chapter_csv(chapters: list[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=CHAPTER_CSV_FIELDS,
        extrasaction="ignore",
    )
    writer.writeheader()
    for chapter in sorted(chapters, key=lambda item: int(item["chapter_number"])):
        writer.writerow(chapter)
    return buffer.getvalue()


def final_violations(
    args: argparse.Namespace,
    run_manifest: dict[str, Any],
    mcp: dict[str, Any],
    database: dict[str, Any],
) -> list[str]:
    violations: list[str] = []
    project = mcp["project"]
    target = args.expected_target
    chapters = mcp["chapters"]
    chapter_numbers = sorted(int(item.get("chapter_number") or 0) for item in chapters)
    accepted_numbers = sorted(
        int(item.get("chapter_number") or 0)
        for item in chapters
        if item.get("status") == "accepted"
    )
    expected_numbers = list(range(1, target + 1))
    if accepted_count(project) != target:
        violations.append(f"accepted_chapter_count={accepted_count(project)}, expected={target}")
    if needs_review_count(project):
        violations.append(f"needs_review_chapter_count={needs_review_count(project)}")
    if failed_chapters(project):
        violations.append(f"failed_chapters={failed_chapters(project)}")
    if mcp["active_task_check"].get("has_active_generation_task"):
        violations.append("active generation task remains")
    if int(database["tasks"].get("active_rows") or 0):
        violations.append(
            f"database active generation rows={database['tasks'].get('active_rows')}"
        )
    if chapter_numbers != expected_numbers or accepted_numbers != expected_numbers:
        violations.append("chapter list is not exactly accepted chapters 1..target")
    observed_checkpoints = sorted(
        int(item.get("chapter") or 0) for item in run_manifest.get("checkpoints") or []
    )
    if observed_checkpoints != list(CHECKPOINTS):
        violations.append(f"checkpoint evidence incomplete: {observed_checkpoints}")
    if int(run_manifest.get("code_changes_during_run") or 0) != 0:
        violations.append("code_changes_during_run is not zero")
    if database.get("freeze_audit") != run_manifest.get(
        "freeze_audit_state"
    ):
        violations.append("policy/rule freeze audit ledger changed")

    canon = database["canon"]
    for key, expected in (
        ("committed", target),
        ("non_committed", 0),
        ("distinct_chapters", target),
        ("first_chapter", 1),
        ("last_chapter", target),
        ("duplicate_ids", 0),
        ("duplicate_candidate_refs", 0),
        ("duplicate_idempotency_keys", 0),
        ("duplicate_chapters", 0),
        ("missing_world_snapshot_refs", 0),
        ("missing_map_snapshot_refs", 0),
        ("wrong_world_snapshot_identity", 0),
        ("wrong_map_snapshot_identity", 0),
        ("candidate_identity_mismatches", 0),
        ("chapter_identity_mismatches", 0),
    ):
        if int(canon.get(key) or 0) != expected:
            violations.append(f"canon.{key}={canon.get(key)}, expected={expected}")
    candidates = database["candidates"]
    for key, expected in (
        ("accepted_canon", target),
        ("missing_commit_id", 0),
        ("reverse_identity_mismatches", 0),
        ("duplicate_accepted_chapters", 0),
    ):
        if int(candidates.get(key) or 0) != expected:
            violations.append(f"candidates.{key}={candidates.get(key)}, expected={expected}")
    graph = database["graph"]
    for key in (
        "missing_graph_delta_refs",
        "invalid_graph_delta_refs",
        "duplicate_graph_delta_refs",
        "unreferenced_chapter_graph_deltas",
        "orphan_graph_delta_patches",
        "duplicate_semantic_graph_deltas",
        "duplicate_semantic_graph_delta_patches",
    ):
        if int(graph.get(key) or 0):
            violations.append(f"graph.{key}={graph.get(key)}")
    snapshots = database["snapshots"]
    for key in ("world_snapshot_through", "map_snapshot_through"):
        if int(snapshots.get(key) or 0) != target:
            violations.append(f"snapshots.{key}={snapshots.get(key)}, expected={target}")
    entities = database["entities"]
    for key in (
        "duplicate_entity_identities",
        "duplicate_alias_identities",
        "orphan_aliases",
    ):
        if int(entities.get(key) or 0):
            violations.append(f"entities.{key}={entities.get(key)}")

    outbox = database["outbox"]
    if int(outbox.get("total") or 0) != target * len(EXPECTED_OUTBOX_TYPES):
        violations.append(f"outbox.total={outbox.get('total')}, expected={target * 3}")
    for event_type in EXPECTED_OUTBOX_TYPES:
        actual = int((outbox.get("event_type_counts") or {}).get(event_type, 0))
        if actual != target:
            violations.append(f"outbox.{event_type}={actual}, expected={target}")
    for key in (
        "duplicate_event_ids",
        "backlog",
        "failed",
        "missing_expected_events",
        "identity_mismatches",
        "unexpected_canon_events",
    ):
        if int(outbox.get(key) or 0):
            violations.append(f"outbox.{key}={outbox.get(key)}")

    projections = {row["projection_kind"]: row for row in database["projections"]}
    projection_target = database.get("projection_target") or {}
    expected_canon_id = str(projection_target.get("canon_commit_id") or "")
    expected_event_id = str(projection_target.get("event_id") or "")
    if int(projection_target.get("chapter_number") or 0) != target:
        violations.append(
            "projection target chapter="
            f"{projection_target.get('chapter_number')}, expected={target}"
        )
    if not expected_canon_id:
        violations.append("projection target Canon identity is empty")
    if not expected_event_id:
        violations.append("projection target event identity is empty")
    if set(projections) != set(EXPECTED_PROJECTIONS):
        violations.append(f"projection kinds={sorted(projections)}, expected={list(EXPECTED_PROJECTIONS)}")
    for kind in EXPECTED_PROJECTIONS:
        row = projections.get(kind) or {}
        if row.get("status") != "healthy":
            violations.append(f"projection {kind} status={row.get('status')}")
        if int(row.get("target_chapter_number") or 0) != target:
            violations.append(f"projection {kind} target={row.get('target_chapter_number')}")
        if int(row.get("projected_chapter_number") or 0) != target:
            violations.append(f"projection {kind} projected={row.get('projected_chapter_number')}")
        if str(row.get("target_canon_commit_id") or "") != expected_canon_id:
            violations.append(
                f"projection {kind} target Canon="
                f"{row.get('target_canon_commit_id')}"
            )
        if str(row.get("projected_canon_commit_id") or "") != expected_canon_id:
            violations.append(
                f"projection {kind} projected Canon="
                f"{row.get('projected_canon_commit_id')}"
            )
        if str(row.get("last_event_id") or "") != expected_event_id:
            violations.append(f"projection {kind} event={row.get('last_event_id')}")
        source_digest = str(row.get("source_digest") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", source_digest):
            violations.append(f"projection {kind} source digest is invalid")
        if str(row.get("last_error") or "").strip():
            violations.append(f"projection {kind} last_error is not empty")
        if (
            kind == "chapter_memory"
            and source_digest
            != str(projection_target.get("chapter_memory_source_digest") or "")
        ):
            violations.append("projection chapter_memory source digest mismatch")
        if (
            kind == "llm_kb"
            and source_digest
            != str(projection_target.get("llm_kb_source_digest") or "")
        ):
            violations.append("projection llm_kb source digest mismatch")
    projection_artifacts = database.get("projection_artifacts")
    if not isinstance(projection_artifacts, dict):
        violations.append("projection artifact evidence is missing")
        projection_artifacts = {}
    for item in projection_artifacts.get("violations") or []:
        violations.append(f"projection artifact: {item}")
    for kind in ("obsidian", "llm_kb"):
        artifact = projection_artifacts.get(kind) or {}
        checkpoint = projections.get(kind) or {}
        if (
            str(artifact.get("source_digest") or "")
            != str(checkpoint.get("source_digest") or "")
        ):
            violations.append(
                f"projection {kind} artifact digest mismatch"
            )
        if int(artifact.get("as_of_chapter") or 0) != target:
            violations.append(
                f"projection {kind} artifact target="
                f"{artifact.get('as_of_chapter')}, expected={target}"
            )
    projection_vectors = database.get("projection_vectors")
    if not isinstance(projection_vectors, dict):
        violations.append("projection vector evidence is missing")
        projection_vectors = {}
    for item in projection_vectors.get("violations") or []:
        violations.append(f"projection vector: {item}")

    maintenance = database["maintenance"]
    if int(maintenance.get("total") or 0) != target * len(EXPECTED_MAINTENANCE_STEPS):
        violations.append(f"maintenance.total={maintenance.get('total')}, expected={target * 4}")
    for step in EXPECTED_MAINTENANCE_STEPS:
        actual = int((maintenance.get("step_counts") or {}).get(step, 0))
        if actual != target:
            violations.append(f"maintenance.{step}={actual}, expected={target}")
    for key in ("backlog", "duplicate_idempotency_keys", "duplicate_canon_steps"):
        if int(maintenance.get(key) or 0):
            violations.append(f"maintenance.{key}={maintenance.get(key)}")

    publisher = database["publisher"]
    for key in (
        "unsettled_jobs",
        "duplicate_job_idempotency_keys",
        "duplicate_canon_platform_jobs",
        "duplicate_receipt_keys",
        "invalid_current_attempt_refs",
    ):
        if int(publisher.get(key) or 0):
            violations.append(f"publisher.{key}={publisher.get(key)}")

    completed_bands = [
        band
        for band in database.get("bands") or []
        if str(band.get("status") or "") in {"pass", "overridden"}
    ]
    completed_bands.sort(
        key=lambda item: (
            int(item.get("chapter_start") or 0),
            int(item.get("chapter_end") or 0),
        )
    )
    expected_start = 1
    for band in completed_bands:
        start = int(band.get("chapter_start") or 0)
        end = int(band.get("chapter_end") or 0)
        if start != expected_start or end < start:
            violations.append(
                f"band coverage breaks at {band.get('band_id')}: {start}-{end}, "
                f"expected start={expected_start}"
            )
            break
        expected_start = end + 1
    if not completed_bands or expected_start != target + 1:
        violations.append(
            f"resolved band coverage ends at {expected_start - 1}, expected={target}"
        )
    report_entries = {
        str(item.get("band_id") or ""): item
        for item in run_manifest.get("band_reports") or []
    }
    expected_band_ids = {str(item.get("band_id") or "") for item in completed_bands}
    if set(report_entries) != expected_band_ids:
        violations.append(
            "band report identities do not match resolved bands: "
            f"reports={sorted(report_entries)}, resolved={sorted(expected_band_ids)}"
        )
    for band in completed_bands:
        band_id = str(band.get("band_id") or "")
        entry = report_entries.get(band_id) or {}
        directory = Path(str(entry.get("directory") or ""))
        paths = {
            "gate": directory / "gate-ledger.json",
            "cost": directory / "cost-report.json",
            "rules": directory / "rule-provenance.json",
        }
        missing = [name for name, path in paths.items() if not path.is_file()]
        if missing:
            violations.append(f"band {band_id} is missing reports: {missing}")
            continue
        violations.extend(
            f"band {band_id}: {item}"
            for item in report_contract_violations(
                gate=load_json(paths["gate"]),
                cost=load_json(paths["cost"]),
                rules=load_json(paths["rules"]),
                project_id=args.project_id,
                scope="band",
                band_id=band_id,
            )
        )

    violations.extend(
        f"project report: {item}"
        for item in report_contract_violations(
            gate=mcp["gate_ledger"],
            cost=mcp["cost_report"],
            rules=mcp["rule_provenance"],
            project_id=args.project_id,
            scope="project",
        )
    )
    return violations


def final_report_markdown(
    args: argparse.Namespace,
    run_manifest: dict[str, Any],
    mcp: dict[str, Any],
    database: dict[str, Any],
    violations: list[str],
) -> str:
    project = mcp["project"]
    lines = [
        "# ForWin v5 L200 No-Hotfix Final Report",
        "",
        f"- Result: {'PASS' if not violations else 'FAIL'}",
        f"- Project: `{args.project_id}`",
        f"- Source SHA: `{run_manifest['freeze_identity']['source_sha']}`",
        f"- Accepted: `{accepted_count(project)}/{args.expected_target}`",
        f"- Needs review: `{needs_review_count(project)}`",
        f"- Active generation task: `{bool(mcp['active_task_check'].get('has_active_generation_task'))}`",
        f"- Code changes during run: `{run_manifest.get('code_changes_during_run', 0)}`",
        f"- Canon commits: `{database['canon']['committed']}`",
        f"- Outbox backlog: `{database['outbox']['backlog']}`",
        f"- Maintenance backlog: `{database['maintenance']['backlog']}`",
        f"- Publisher unsettled jobs: `{database['publisher']['unsettled_jobs']}`",
        "",
        "## Checkpoints",
        "",
    ]
    lines.extend(
        f"- Chapter {item['chapter']}: accepted={item['accepted_at_observation']}, sha256=`{item['sha256']}`"
        for item in run_manifest.get("checkpoints") or []
    )
    lines.extend(["", "## Integrity", ""])
    if violations:
        lines.extend(f"- FAIL: {item}" for item in violations)
    else:
        lines.append("- All release invariants passed.")
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            "- `manifest.json`",
            "- `chapter-status.csv`",
            "- `gate-ledger.json`",
            "- `cost-report.json`",
            "- `rule-provenance.json`",
            "- `canon-integrity.json`",
            "- `task-recovery.json`",
            "- `projection-publisher.json`",
            "",
        ]
    )
    return "\n".join(lines)


def final_chapter_rows(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != CHAPTER_CSV_FIELDS:
                raise EvidenceError("L200 chapter CSV columns changed")
            rows = [dict(row) for row in reader]
    except FileNotFoundError as exc:
        raise EvidenceError(f"required L200 artifact is missing: {path}") from exc
    return rows


def verify_final_artifact_set(
    output: Path,
    run_manifest: dict[str, Any],
) -> dict[str, Any]:
    output = output.resolve()
    target = int(run_manifest.get("expected_target") or 0)
    project_id = str(run_manifest.get("project_id") or "")
    if (
        int(run_manifest.get("schema_version") or 0) != 1
        or run_manifest.get("run") != "forwin-v5-final-l200-no-hotfix"
        or run_manifest.get("valid") is not True
        or run_manifest.get("result") != "pass"
        or not run_manifest.get("finalized_at")
        or target != 200
        or not project_id
        or run_manifest.get("violations") != []
        or int(run_manifest.get("code_changes_during_run") or 0) != 0
    ):
        raise EvidenceError("L200 manifest is not a finalized passing run")
    for initial_key, final_key in (
        ("freeze_identity", "final_freeze_identity"),
        ("connection_bindings", "final_connection_bindings"),
        ("live_schema_identity", "final_live_schema_identity"),
    ):
        if (
            not run_manifest.get(initial_key)
            or run_manifest.get(initial_key) != run_manifest.get(final_key)
        ):
            raise EvidenceError(f"L200 final identity drift: {initial_key}")
    collector = run_manifest.get("collector") or {}
    if (
        Path(str(collector.get("path") or "")).resolve()
        != Path(__file__).resolve()
        or collector.get("sha256") != sha256_file(Path(__file__).resolve())
    ):
        raise EvidenceError("L200 collector identity mismatch")
    artifacts = run_manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(
        FINAL_ARTIFACT_NAMES
    ):
        raise EvidenceError("L200 final artifact inventory mismatch")
    for name in FINAL_ARTIFACT_NAMES:
        path = output / name
        if (
            not path.is_file()
            or sha256_file(path) != str(artifacts.get(name) or "")
        ):
            raise EvidenceError(f"L200 final artifact hash mismatch: {name}")

    chapters = final_chapter_rows(output / "chapter-status.csv")
    gate = load_json(output / "gate-ledger.json")
    cost = load_json(output / "cost-report.json")
    rules = load_json(output / "rule-provenance.json")
    canon = load_json(output / "canon-integrity.json")
    tasks = load_json(output / "task-recovery.json")
    projection = load_json(output / "projection-publisher.json")
    for label, payload in (
        ("gate ledger", gate),
        ("cost report", cost),
        ("rule provenance", rules),
        ("Canon integrity", canon),
        ("task recovery", tasks),
        ("projection/publisher", projection),
    ):
        if str(payload.get("project_id") or "") != project_id:
            raise EvidenceError(f"L200 {label} project identity mismatch")
    nested_types = (
        ("Canon", canon, "canon", dict),
        ("candidate", canon, "candidates", dict),
        ("graph", canon, "graph", dict),
        ("snapshot", canon, "snapshots", dict),
        ("entity", canon, "entities", dict),
        ("active task", tasks, "active_task_check", dict),
        ("MCP task", tasks, "mcp_tasks", list),
        ("outbox", projection, "outbox", dict),
        ("projection", projection, "projections", list),
        ("projection target", projection, "projection_target", dict),
        (
            "projection artifact",
            projection,
            "projection_artifacts",
            dict,
        ),
        ("projection vector", projection, "projection_vectors", dict),
        ("maintenance", projection, "maintenance", dict),
        ("publisher", projection, "publisher", dict),
    )
    malformed = [
        label
        for label, payload, key, expected_type in nested_types
        if not isinstance(payload.get(key), expected_type)
    ]
    if malformed or any(not isinstance(item, dict) for item in chapters):
        raise EvidenceError(
            "L200 final artifact payload is malformed: "
            + ", ".join(malformed or ["chapter row"])
        )
    try:
        chapter_numbers = [
            int(str(item.get("chapter_number") or "0")) for item in chapters
        ]
        accepted_numbers = [
            int(str(item.get("chapter_number") or "0"))
            for item in chapters
            if item.get("status") == "accepted"
        ]
    except (TypeError, ValueError) as exc:
        raise EvidenceError(
            "L200 final artifact payload is malformed: chapter number"
        ) from exc
    expected_numbers = list(range(1, target + 1))
    if (
        chapter_numbers != expected_numbers
        or accepted_numbers != expected_numbers
    ):
        raise EvidenceError(
            "L200 chapter CSV is not exactly accepted chapters 1..200"
        )
    active_check = tasks.get("active_task_check") or {}
    mcp = {
        "project": {
            "id": project_id,
            "accepted_chapter_count": len(accepted_numbers),
            "needs_review_chapter_count": sum(
                item.get("status") == "needs_review" for item in chapters
            ),
            "generation_control": {
                "failed_chapters": [
                    int(str(item.get("chapter_number") or "0"))
                    for item in chapters
                    if item.get("status") == "failed"
                ]
            },
        },
        "chapters": chapters,
        "active_task_check": active_check,
        "tasks": tasks.get("mcp_tasks") or [],
        "gate_ledger": gate,
        "cost_report": cost,
        "rule_provenance": rules,
    }
    database = {
        key: canon.get(key) or {}
        for key in ("canon", "candidates", "graph", "snapshots", "entities")
    }
    database.update(
        {
            key: projection.get(key) or {}
            for key in (
                "outbox",
                "projections",
                "projection_target",
                "projection_artifacts",
                "projection_vectors",
                "maintenance",
                "publisher",
            )
        }
    )
    database["tasks"] = {
        "active_rows": int(tasks.get("active_rows") or 0)
    }
    database["freeze_audit"] = run_manifest.get("freeze_audit_state") or {}
    database["bands"] = [
        {
            "band_id": str(item.get("band_id") or ""),
            "chapter_start": int(item.get("chapter_start") or 0),
            "chapter_end": int(item.get("chapter_end") or 0),
            "status": str(item.get("status") or ""),
        }
        for item in run_manifest.get("band_reports") or []
    ]
    namespace = argparse.Namespace(
        project_id=project_id,
        expected_target=target,
    )
    try:
        violations = final_violations(
            namespace,
            run_manifest,
            mcp,
            database,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise EvidenceError(
            "L200 final artifact payload is malformed"
        ) from exc
    if canon.get("violations"):
        violations.append("Canon integrity artifact contains violations")
    if violations:
        raise EvidenceError(
            "L200 final artifact contract failed: " + "; ".join(violations)
        )
    expected_report = final_report_markdown(
        namespace,
        run_manifest,
        mcp,
        database,
        [],
    )
    if (output / "final-report.md").read_text(
        encoding="utf-8"
    ) != expected_report:
        raise EvidenceError("L200 final report content mismatch")
    return {
        "project_id": project_id,
        "target": target,
        "accepted": len(accepted_numbers),
        "active_generation_task": bool(
            active_check.get("has_active_generation_task")
        ),
        "source_sha": str(
            (run_manifest.get("freeze_identity") or {}).get("source_sha")
            or ""
        ),
    }


def verify_finalized_output(output: Path) -> dict[str, Any]:
    output = output.resolve()
    run_manifest = load_json(output / "manifest.json")
    verify_band_artifact_entries(
        output,
        list(run_manifest.get("band_reports") or []),
    )
    verify_checkpoint_artifact_entries(output, run_manifest)
    return verify_final_artifact_set(output, run_manifest)


def assert_final_output_unsealed(
    output: Path,
    run_manifest: dict[str, Any],
) -> None:
    sealed_fields = ("finalized_at", "result", "violations", "artifacts")
    if any(field in run_manifest for field in sealed_fields):
        raise EvidenceError("L200 run is already finalized")
    existing = [
        name for name in FINAL_ARTIFACT_NAMES if (output / name).exists()
    ]
    if existing:
        raise EvidenceError(
            "L200 final output already exists: " + ", ".join(existing)
        )


async def finalize(args: argparse.Namespace) -> None:
    output = args.output_dir.resolve()
    run_manifest = load_json(output / "manifest.json")
    assert_final_output_unsealed(output, run_manifest)
    if not run_manifest.get("valid"):
        raise EvidenceError("L200 run is invalidated")
    verify_run_inputs(args, run_manifest)
    verify_band_artifact_entries(
        output, list(run_manifest.get("band_reports") or [])
    )
    verify_checkpoint_artifact_entries(output, run_manifest)
    rc_manifest = load_json(args.rc_manifest.resolve())
    freeze = verify_frozen(args, rc_manifest)
    bindings = connection_bindings(args, freeze)
    verify_connection_bindings(run_manifest, bindings)
    verify_runtime_configuration(run_manifest, freeze)
    live_schema = verify_database_schema(run_manifest, args.database_url)
    mcp = await collect_mcp_state(args)
    project = mcp["project"]
    assert_project_identity(args, project)
    policy = await fetch_policy(args.api_url, args.project_id)
    hashes = assert_policy_and_rules_frozen(
        run_manifest,
        policy,
        mcp["rule_provenance"],
    )
    database = collect_database_state(args.database_url, args.project_id)
    verify_freeze_audit(run_manifest, database["freeze_audit"])
    database["projection_artifacts"] = collect_projection_artifacts(
        freeze,
        project_id=args.project_id,
        target_chapter=args.expected_target,
        expected_llm_kb_source_digest=str(
            database["projection_target"].get(
                "llm_kb_source_digest"
            )
            or ""
        ),
    )
    database["projection_vectors"] = collect_projection_vectors(
        bindings,
        freeze,
        project_id=args.project_id,
        projection_target=database["projection_target"],
        projection_artifacts=database["projection_artifacts"],
    )
    violations = final_violations(args, run_manifest, mcp, database)

    write_json(output / "gate-ledger.json", mcp["gate_ledger"])
    write_json(output / "cost-report.json", mcp["cost_report"])
    write_json(output / "rule-provenance.json", mcp["rule_provenance"])
    atomic_write(output / "chapter-status.csv", chapter_csv(mcp["chapters"]))
    write_json(
        output / "canon-integrity.json",
        {
            "schema_version": 1,
            "project_id": args.project_id,
            "collected_at": now(),
            "canon": database["canon"],
            "candidates": database["candidates"],
            "graph": database["graph"],
            "snapshots": database["snapshots"],
            "entities": database["entities"],
            "violations": [
                item
                for item in violations
                if item.startswith(("canon.", "candidates.", "graph.", "snapshots.", "entities."))
            ],
        },
    )
    write_json(
        output / "task-recovery.json",
        {
            "schema_version": 1,
            "project_id": args.project_id,
            "collected_at": now(),
            "active_task_check": mcp["active_task_check"],
            "mcp_tasks": mcp["tasks"],
            **database["tasks"],
        },
    )
    write_json(
        output / "projection-publisher.json",
        {
            "schema_version": 1,
            "project_id": args.project_id,
            "collected_at": now(),
            "outbox": database["outbox"],
            "projections": database["projections"],
            "projection_target": database["projection_target"],
            "projection_artifacts": database["projection_artifacts"],
            "projection_vectors": database["projection_vectors"],
            "maintenance": database["maintenance"],
            "publisher": database["publisher"],
        },
    )
    atomic_write(
        output / "final-report.md",
        final_report_markdown(args, run_manifest, mcp, database, violations),
    )
    run_manifest["finalized_at"] = now()
    run_manifest["final_freeze_identity"] = freeze
    run_manifest["final_connection_bindings"] = bindings
    run_manifest["final_live_schema_identity"] = live_schema
    run_manifest["final_freeze_hashes"] = hashes
    run_manifest["result"] = "pass" if not violations else "fail"
    run_manifest["violations"] = violations
    run_manifest["artifacts"] = {
        name: sha256_file(output / name)
        for name in FINAL_ARTIFACT_NAMES
    }
    write_json(output / "manifest.json", run_manifest)
    print(output / "final-report.md")
    if violations:
        raise EvidenceError("L200 final integrity gate failed: " + "; ".join(violations))


def common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rc-manifest", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument(
        "--database-url-env",
        default="FORWIN_L200_DATABASE_URL",
        help="Environment variable containing the PostgreSQL SQLAlchemy URL.",
    )
    parser.add_argument("--runtime-container", action="append", required=True)
    parser.add_argument("--browser-container", action="append", required=True)
    parser.add_argument("--dependency-container", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-target", type=int, default=200)
    parser.add_argument(
        "--quality-profile", choices=("standard", "pulp"), required=True
    )
    parser.add_argument(
        "--gate-delegate", choices=("human", "spark"), required=True
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect strict evidence for the immutable ForWin v5 L200 gate."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init")
    common_arguments(init_parser)
    checkpoint_parser = commands.add_parser("checkpoint")
    common_arguments(checkpoint_parser)
    checkpoint_parser.add_argument("--chapter", type=int, required=True)
    final_parser = commands.add_parser("finalize")
    common_arguments(final_parser)
    verify_parser = commands.add_parser("verify-final")
    verify_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    if args.command == "verify-final":
        summary = verify_finalized_output(args.output_dir)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    if args.expected_target != 200:
        raise EvidenceError("this release gate is intentionally fixed at 200 chapters")
    args.database_url = str(os.environ.get(args.database_url_env) or "").strip()
    if not args.database_url:
        raise EvidenceError(
            f"database URL environment variable is empty: {args.database_url_env}"
        )
    if args.command == "init":
        await init_run(args)
    elif args.command == "checkpoint":
        await checkpoint(args)
    else:
        await finalize(args)


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(async_main(args))
    except Exception as exc:
        if args.command in {"checkpoint", "finalize"} and isinstance(
            exc, FreezeViolation
        ):
            invalidate(args, exc)
        if isinstance(exc, EvidenceError):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
