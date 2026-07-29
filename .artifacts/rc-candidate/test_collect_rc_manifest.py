from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).with_name("collect_rc_manifest.py")
SPEC = importlib.util.spec_from_file_location("collect_rc_manifest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)

SOURCE_SHA = "a" * 40
SMOKE_FINALIZER_PATH = Path(__file__).with_name("finalize_smoke.py")
MATRIX_FINALIZER_PATH = Path(__file__).with_name("finalize_matrix.py")
L200_PATH = Path(__file__).with_name("l200_evidence.py")
RUN_RC_GATES_PATH = Path(__file__).with_name("run_rc_gates.py")
SMOKE_LIFECYCLE_PATH = Path(__file__).with_name("smoke_lifecycle.py")
CANDIDATE_MCP_PATH = Path(__file__).with_name("candidate_mcp_call.py")
RELEASE_SOURCE_MANIFEST_PATH = Path(__file__).with_name(
    "release-source-files.txt"
)
RC_FREEZE_RUNBOOK_PATH = Path(__file__).with_name("rc-freeze-runbook.md")
SMOKE_RUNBOOK_PATH = Path(__file__).with_name(
    "post-decision-smoke-runbook.md"
)
MATRIX_FIXTURES_PATH = Path(__file__).with_name("test_finalize_matrix.py")
RECOVERY_FIXTURES_PATH = Path(__file__).with_name("test_finalize_recovery.py")
RECOVERY_EVALUATOR_PATH = Path(__file__).with_name("recovery_evidence.py")
GENERATION_RECOVERY_RUNNER_PATH = Path(__file__).with_name(
    "generation_projection_recovery.py"
)
MINIO_RECOVERY_RUNNER_PATH = Path(__file__).with_name("minio_recovery.py")
PUBLISHER_RECOVERY_RUNNER_PATH = Path(__file__).with_name(
    "publisher_recovery.py"
)
V1_FIXTURES_PATH = Path(__file__).with_name("test_finalize_v1.py")
SOURCE_TREE = "c" * 40
RUNTIME_IMAGE = {
    "tag": "forwin-v5-runtime:test",
    "image_id": "sha256:" + "1" * 64,
    "revision": SOURCE_SHA,
}
BROWSER_IMAGE = {
    "tag": "forwin-v5-browser:test",
    "image_id": "sha256:" + "2" * 64,
    "revision": SOURCE_SHA,
}
GATE_STEPS = (
    "v1-fresh-schema",
    "v2-canon-recovery",
    "v3-spark-boundary",
    "v5-projection-publisher-recovery",
    "full-suite",
    "ruff",
    "compileall",
    "diff-check",
)
PYTEST_STEPS = set(GATE_STEPS[:5])
RECOVERY_RUNNER_BY_FAULT = {
    "generation_worker_precommit_crash": GENERATION_RECOVERY_RUNNER_PATH,
    "generation_worker_postcommit_crash": GENERATION_RECOVERY_RUNNER_PATH,
    "qdrant_unavailable": GENERATION_RECOVERY_RUNNER_PATH,
    "projection_consumer_unavailable": GENERATION_RECOVERY_RUNNER_PATH,
    "minio_pre_canon_unavailable": MINIO_RECOVERY_RUNNER_PATH,
    "minio_post_canon_unavailable": MINIO_RECOVERY_RUNNER_PATH,
    "publisher_backend_unavailable": PUBLISHER_RECOVERY_RUNNER_PATH,
    "publisher_browser_unavailable": PUBLISHER_RECOVERY_RUNNER_PATH,
    "publisher_captcha": PUBLISHER_RECOVERY_RUNNER_PATH,
    "publisher_mfa": PUBLISHER_RECOVERY_RUNNER_PATH,
    "publisher_account_risk": PUBLISHER_RECOVERY_RUNNER_PATH,
}


def test_rc_manifest_write_refuses_existing_output(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    collector.atomic_write(path, {"generation": 1})

    with pytest.raises(collector.ManifestError, match="already exists"):
        collector.atomic_write(path, {"generation": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"generation": 1}


def test_collector_environment_rejects_git_and_docker_control() -> None:
    with pytest.raises(collector.ManifestError, match="DOCKER_HOST"):
        collector.command_environment(
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "DOCKER_HOST": "tcp://production.example:2376",
            }
        )


def test_collector_run_uses_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class Completed:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(
        collector,
        "command_environment",
        lambda: {"SAFE": "1"},
    )
    monkeypatch.setattr(collector.subprocess, "run", fake_run)

    assert collector.run("git", "rev-parse", "HEAD") == "ok"
    assert captured["env"] == {"SAFE": "1"}


def test_release_source_manifest_is_exact_and_excludes_live_evidence() -> None:
    root = MODULE_PATH.parents[2]
    listed = RELEASE_SOURCE_MANIFEST_PATH.read_text(
        encoding="utf-8"
    ).splitlines()
    expected = sorted(
        path.relative_to(root).as_posix()
        for path in MODULE_PATH.parent.iterdir()
        if path.is_file()
        and path.suffix in {".md", ".py", ".txt", ".yml"}
    )

    assert listed == sorted(set(listed))
    assert listed == expected
    assert all((root / path).is_file() for path in listed)
    assert not any(
        "manifest.draft" in path or "gate-preflight" in path
        for path in listed
    )
    assert (
        RELEASE_SOURCE_MANIFEST_PATH.resolve()
        in collector.RELEASE_HARNESS_PATHS
    )


def fenced_bash_blocks(text: str, *, after: str) -> list[str]:
    section = text.split(after, maxsplit=1)[1]
    return re.findall(r"```bash\n(.*?)\n```", section, flags=re.DOTALL)


def active_shell_lines(block: str) -> list[str]:
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def assert_rc_bootstrap_contract(runbook: str) -> None:
    bootstrap = fenced_bash_blocks(
        runbook,
        after="### Fresh Candidate Bootstrap",
    )[0]
    active = active_shell_lines(bootstrap)
    compose_commands = [
        line for line in active if line.startswith("rc_compose ")
    ]
    compose_token_lines = [
        line for line in active if re.search(r"\bdocker\s+compose\b", line)
    ]

    assert compose_token_lines == [
        'docker compose -p "$RC_COMPOSE_PROJECT" \\',
    ]
    assert compose_commands == [
        "rc_compose down --volumes --remove-orphans",
        (
            "rc_compose up -d --no-build --wait --wait-timeout 120 "
            "postgres qdrant minio"
        ),
        (
            "rc_compose run --rm --no-deps forwin "
            "alembic upgrade head"
        ),
        (
            "rc_compose up -d --no-build --wait --wait-timeout 180 "
            "forwin generation-worker outbox-worker forwin-mcp "
            "publisher-worker publisher-browser"
        ),
    ]
    assert active.count(
        'RC_BOOTSTRAP_ID="$(date -u +%Y%m%dt%H%M%Sz)-$$"'
    ) == 1
    assert active.count(
        'export RC_COMPOSE_PROJECT="forwin-v5-rc-${RC_BOOTSTRAP_ID}"'
    ) == 1
    assert active.count(
        'test -z "$(docker ps -aq --filter '
        '"label=com.docker.compose.project=$RC_COMPOSE_PROJECT")"'
    ) == 1
    assert active.count(
        "for volume_suffix in forwin-data forwin-postgres "
        "forwin-qdrant forwin-minio"
    ) == 1
    assert active.count(
        'if docker volume inspect '
        '"${RC_COMPOSE_PROJECT}_${volume_suffix}" '
        ">/dev/null 2>&1; then"
    ) == 1
    assert active.count("false") == 1
    cleanup = active.index(compose_commands[0])
    trap_line = "trap rc_candidate_abort ERR INT TERM"
    trap = active.index(trap_line)
    dependencies = active.index(compose_commands[1])

    assert cleanup < trap < dependencies
    assert active.count(trap_line) == 1
    assert active.count("trap - ERR INT TERM") == 1
    assert active.count("rc_candidate_destroy || true") == 1
    assert active.count('exit "$status"') == 1
    assert "The bootstrap stack is not V1 evidence" in runbook


def test_rc_freeze_runbook_owns_fresh_candidate_bootstrap_lifecycle() -> None:
    runbook = RC_FREEZE_RUNBOOK_PATH.read_text(encoding="utf-8")
    assert_rc_bootstrap_contract(runbook)


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("rc_candidate_destroy || true", ":"),
        ('exit "$status"', ":"),
        (
            "trap rc_candidate_abort ERR INT TERM",
            (
                "trap rc_candidate_abort ERR INT TERM\n"
                "trap rc_candidate_abort ERR INT TERM"
            ),
        ),
        (
            "rc_compose run --rm --no-deps forwin alembic upgrade head",
            (
                "command docker compose up postgres-test\n"
                "rc_compose run --rm --no-deps forwin alembic upgrade head"
            ),
        ),
        (
            "rc_compose run --rm --no-deps forwin alembic upgrade head",
            (
                "if docker compose up postgres-test; then :; fi\n"
                "rc_compose run --rm --no-deps forwin alembic upgrade head"
            ),
        ),
        (
            "rc_compose run --rm --no-deps forwin alembic upgrade head",
            (
                "printf ready; docker compose up postgres-test\n"
                "rc_compose run --rm --no-deps forwin alembic upgrade head"
            ),
        ),
    ),
)
def test_rc_bootstrap_contract_rejects_shell_bypasses(
    old: str,
    new: str,
) -> None:
    runbook = RC_FREEZE_RUNBOOK_PATH.read_text(encoding="utf-8")
    assert runbook.count(old) >= 1
    mutated = runbook.replace(old, new, 1)

    with pytest.raises((AssertionError, ValueError)):
        assert_rc_bootstrap_contract(mutated)


def shell_function(block: str, name: str) -> str:
    start = block.index(f"{name}() {{")
    end = block.index("\n}", start) + len("\n}")
    return block[start:end]


def test_rc_freeze_volume_check_triggers_cleanup_for_every_existing_volume(
    tmp_path: Path,
) -> None:
    runbook = RC_FREEZE_RUNBOOK_PATH.read_text(encoding="utf-8")
    bootstrap = fenced_bash_blocks(
        runbook,
        after="### Fresh Candidate Bootstrap",
    )[0]
    loop_start = bootstrap.index("for volume_suffix in ")
    loop_end = bootstrap.index("\ndone", loop_start) + len("\ndone")
    volume_loop = bootstrap[loop_start:loop_end]
    destroy = shell_function(bootstrap, "rc_candidate_destroy")
    abort = shell_function(bootstrap, "rc_candidate_abort")
    suffixes = (
        "forwin-data",
        "forwin-postgres",
        "forwin-qdrant",
        "forwin-minio",
    )

    for existing in suffixes:
        cleanup_log = tmp_path / f"{existing}.log"
        script = f"""
set -Eeuo pipefail
RC_COMPOSE_PROJECT=forwin-v5-rc-test
EXISTING_VOLUME={existing}
CLEANUP_LOG={json.dumps(str(cleanup_log))}
rc_compose() {{
  printf '%s\\n' "$*" >> "$CLEANUP_LOG"
}}
docker() {{
  [[ "$1" == volume && "$2" == inspect &&
     "$3" == "${{RC_COMPOSE_PROJECT}}_${{EXISTING_VOLUME}}" ]]
}}
{destroy}
{abort}
trap rc_candidate_abort ERR INT TERM
{volume_loop}
"""
        result = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, existing
        assert (
            cleanup_log.read_text(encoding="utf-8")
            == "down --volumes --remove-orphans\n"
        )

    clean_log = tmp_path / "clean.log"
    clean_script = f"""
set -Eeuo pipefail
RC_COMPOSE_PROJECT=forwin-v5-rc-test
CLEANUP_LOG={json.dumps(str(clean_log))}
rc_compose() {{
  printf '%s\\n' "$*" >> "$CLEANUP_LOG"
}}
docker() {{ return 1; }}
{destroy}
{abort}
trap rc_candidate_abort ERR INT TERM
{volume_loop}
"""
    clean = subprocess.run(
        ["bash", "-c", clean_script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert clean.returncode == 0
    assert not clean_log.exists()


def test_fresh30_runbook_destroys_bootstrap_stack_after_finalization() -> None:
    runbook = SMOKE_RUNBOOK_PATH.read_text(encoding="utf-8")
    finalizer = "uv run python .artifacts/rc-candidate/finalize_smoke.py"
    blocks = fenced_bash_blocks(runbook, after=finalizer)
    active = [
        line
        for block in blocks
        for line in active_shell_lines(block)
    ]

    assert active.count("rc_candidate_destroy") == 1
    normalized = " ".join(runbook.split())
    assert (
        "L200 must start from a different fresh Compose project and database."
        in normalized
    )


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_mcp_client_disables_env_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_mcp = load_module("candidate_mcp_transport", CANDIDATE_MCP_PATH)
    sentinel = object()
    options: dict = {}

    def fake_async_client(**kwargs):
        options.update(kwargs)
        return sentinel

    monkeypatch.setattr(candidate_mcp.httpx, "AsyncClient", fake_async_client)
    timeout = candidate_mcp.httpx.Timeout(12)

    assert (
        candidate_mcp.direct_mcp_http_client(
            headers={"X-Test": "1"},
            timeout=timeout,
            auth=None,
            follow_redirects=True,
        )
        is sentinel
    )
    assert options == {
        "headers": {"X-Test": "1"},
        "timeout": timeout,
        "auth": None,
        "trust_env": False,
        "follow_redirects": False,
    }


def shared_candidate(path: Path) -> dict:
    fixtures = load_module("collector_shared_v1_fixtures", V1_FIXTURES_PATH)
    payload = fixtures.candidate(path)
    payload["source"]["tree"] = SOURCE_TREE
    payload["images"]["runtime"] = RUNTIME_IMAGE
    payload["images"]["publisher_browser"] = BROWSER_IMAGE
    payload["runtime_policy"] = {
        "quality_profile": "standard",
        "gate_delegate": "human",
    }
    payload["release_harness"] = {
        "files": [
            {
                "path": artifact.relative_to(MODULE_PATH.parents[2]).as_posix(),
                "sha256": collector.sha256_file(artifact),
            }
            for artifact in collector.RELEASE_HARNESS_PATHS
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def smoke_stack_identity(candidate: dict) -> dict:
    images = candidate["images"]
    project = "forwin-v5-smoke"

    def containers(services: set[str], image_key: str | None) -> list[dict]:
        return [
            {
                "name": f"{project}-{service}",
                "compose_project": project,
                "compose_service": service,
                "image_id": images[image_key or service]["image_id"],
            }
            for service in sorted(services)
        ]

    return {
        "compose_project": project,
        "runtime_containers": containers(
            set(("forwin", "generation-worker", "outbox-worker", "forwin-mcp", "publisher-worker")),
            "runtime",
        ),
        "publisher_browser_containers": containers(
            {"publisher-browser"},
            "publisher_browser",
        ),
        "dependency_containers": containers(
            {"postgres", "qdrant", "minio"},
            None,
        ),
        "connection_bindings": {
            "compose_project": project,
            "api": {
                "compose_service": "forwin",
                "host_ip": "127.0.0.1",
                "host_port": 18899,
            },
            "mcp": {
                "compose_service": "forwin-mcp",
                "host_ip": "127.0.0.1",
                "host_port": 18896,
            },
            "database": {
                "compose_service": "postgres",
                "host_ip": "127.0.0.1",
                "host_port": 55434,
            },
            "qdrant": {
                "compose_service": "qdrant",
                "host_ip": "127.0.0.1",
                "host_port": 16337,
            },
        },
    }


def write_smoke_transcript(
    path: Path,
    *,
    candidate_path: Path,
    identity: dict,
    project_id: str,
) -> None:
    lifecycle = load_module("collector_smoke_lifecycle", SMOKE_LIFECYCLE_PATH)
    recorder = lifecycle.Recorder("run-1")

    def append(
        tool: str,
        *,
        transport: str = "mcp_http",
        stage_key: str = "",
        task_id: str = "",
    ) -> None:
        recorder.append(
            tool=tool,
            transport=transport,
            arguments={"project_id": project_id, "stage_key": stage_key},
            project_id=project_id,
            stage_key=stage_key,
            task_id=task_id,
            request_id=f"request-{len(recorder.operations) + 1}",
        )

    append("project_create")
    append("project_policy_update", transport="http")
    for stage in lifecycle.GENESIS_STAGES:
        append("genesis_stage_generate", stage_key=stage)
        append("genesis_stage_lock", stage_key=stage)
    append("task_active_generation_check")
    append("project_start_writing", task_id="task-1")
    payload = {
        "schema_version": 1,
        "result": "handoff_started",
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "run_id": "run-1",
        "started_at": "2026-07-22T12:00:00+00:00",
        "completed_at": "2026-07-22T12:01:00+00:00",
        "project_id": project_id,
        "handoff_task_id": "task-1",
        "target": 30,
        "candidate_manifest": identity["candidate_manifest"],
        "harness": {
            "path": str(SMOKE_LIFECYCLE_PATH),
            "sha256": collector.sha256_file(SMOKE_LIFECYCLE_PATH),
        },
        "mcp_url": "http://127.0.0.1:18896/mcp",
        "api_url": "http://127.0.0.1:18899",
        "operation_count": len(recorder.operations),
        "operation_chain_head": recorder.operations[-1]["operation_sha256"],
        "operations": recorder.operations,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_smoke_evidence(
    path: Path,
    *,
    candidate_path: Path | None = None,
) -> None:
    fixtures = load_module("collector_smoke_fixtures", MATRIX_FIXTURES_PATH)
    evidence = fixtures.evidence("L30")
    evidence["project"]["creation_status"] = "writing"
    evidence["genesis"] = {
        "project_id": evidence["project"]["id"],
        "creation_status": "writing",
        "can_start_writing": False,
        "stage_states": [
            {"stage_key": stage, "status": "locked", "locked": True}
            for stage in fixtures.matrix.l200.GENESIS_STAGES
        ],
    }
    evidence["policy"]["version"] = 1
    evidence["task_policy_snapshots"] = {
        "count": 1,
        "items": [
            {
                "task_id": "task-1",
                "status": "completed",
                "policy_version": 1,
                "payload_sha256": "a" * 64,
                "policy_snapshot_sha256": collector.canonical_hash(
                    evidence["policy"]["policy"]
                ),
            }
        ],
    }
    evidence_path = path.parent / "smoke-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    candidate_path = candidate_path or path.parent / "smoke-candidate.json"
    candidate = shared_candidate(candidate_path)
    candidate_time = datetime.fromisoformat(candidate["collected_at"])
    identity = {
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "candidate_manifest": {
            "path": str(candidate_path),
            "sha256": collector.sha256_file(candidate_path),
        },
        "images": {
            key: {
                field: image[field]
                for field in (
                    ("tag", "image_id", "revision")
                    if key in {"runtime", "publisher_browser"}
                    else ("tag", "image_id")
                )
            }
            for key, image in candidate["images"].items()
        },
    }
    identity["candidate_stack"] = smoke_stack_identity(candidate)
    transcript_path = path.parent / "smoke-lifecycle.json"
    write_smoke_transcript(
        transcript_path,
        candidate_path=candidate_path,
        identity=identity,
        project_id=evidence["project"]["id"],
    )
    smoke_finalizer = load_module(
        "collector_smoke_finalizer",
        SMOKE_FINALIZER_PATH,
    )
    report_path = path.parent / "smoke-report.md"
    report_path.write_text(
        smoke_finalizer.report(identity, evidence, []),
        encoding="utf-8",
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha": SOURCE_SHA,
                "result": "pass",
                "target": 30,
                "project_id": "project-200",
                "quality_profile": "standard",
                "gate_delegate": "human",
                "accepted": 30,
                "needs_review": 0,
                "has_active_generation_task": False,
                "code_changes_during_run": 0,
                "identity": identity,
                "fresh_project": {
                    "projects": 1,
                    "project_created_at": (
                        candidate_time + timedelta(minutes=1)
                    ).isoformat(),
                },
                "violations": [],
                "evidence": {
                    "path": str(evidence_path),
                    "sha256": collector.sha256_file(evidence_path),
                },
                "final_report": {
                    "path": str(report_path),
                    "sha256": collector.sha256_file(report_path),
                },
                "operation_transcript": {
                    "path": str(transcript_path),
                    "sha256": collector.sha256_file(transcript_path),
                },
                "auditor": {
                    "path": str(SMOKE_FINALIZER_PATH),
                    "sha256": collector.sha256_file(SMOKE_FINALIZER_PATH),
                    "matrix_helper_path": str(MATRIX_FINALIZER_PATH),
                    "matrix_helper_sha256": collector.sha256_file(
                        MATRIX_FINALIZER_PATH
                    ),
                    "database_helper_path": str(L200_PATH),
                    "database_helper_sha256": collector.sha256_file(L200_PATH),
                    "lifecycle_runner_path": str(SMOKE_LIFECYCLE_PATH),
                    "lifecycle_runner_sha256": collector.sha256_file(
                        SMOKE_LIFECYCLE_PATH
                    ),
                },
            }
        ),
        encoding="utf-8",
    )


def write_recovery_evidence(
    path: Path,
    *,
    candidate_path: Path | None = None,
) -> None:
    fixtures = load_module("collector_recovery_fixtures", RECOVERY_FIXTURES_PATH)
    fixtures.SOURCE_SHA = SOURCE_SHA
    candidate_path = candidate_path or path.parent / "candidate.json"
    if not candidate_path.is_file():
        shared_candidate(candidate_path)
    payload = fixtures.recovery_manifest(
        path.parent,
        candidate_path=candidate_path,
    )
    for kind, item in payload["faults"].items():
        report_path = Path(item["path"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        runner_path = RECOVERY_RUNNER_BY_FAULT[kind].resolve()
        report["runner"] = {
            "path": str(runner_path),
            "sha256": collector.sha256_file(runner_path),
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        item["sha256"] = collector.sha256_file(report_path)
    path.write_text(json.dumps(payload), encoding="utf-8")


def rewrite_recovery_report(
    payload: dict,
    kind: str,
    mutate,
) -> dict:
    item = payload["faults"][kind]
    report_path = Path(item["path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    mutate(report)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    item["sha256"] = collector.sha256_file(report_path)
    item["result"] = report["result"]
    return report


def expected_recovery_candidate_identity(payload: dict) -> dict:
    candidate_path = Path(payload["identity"]["rc_manifest"]["path"])
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    return {
        "source_tree": candidate["source"]["tree"],
        "runtime_image": candidate["images"]["runtime"],
        "browser_image": candidate["images"]["publisher_browser"],
        "dependency_images": {
            key: candidate["images"][key]
            for key in ("postgres", "qdrant", "minio")
        },
    }


def write_v1_evidence(
    path: Path,
    *,
    candidate_path: Path | None = None,
) -> None:
    fixtures = load_module("collector_v1_fixtures", V1_FIXTURES_PATH)
    candidate_path = candidate_path or path.parent / "v1-candidate.json"
    candidate = shared_candidate(candidate_path)
    events_path = path.parent / "v1-stack-events.jsonl"
    fixtures.event_log(events_path, candidate_path)
    payload = fixtures.v1.build_manifest(
        candidate=candidate,
        candidate_path=candidate_path,
        events_path=events_path,
    )
    report_path = path.parent / "report.md"
    fixtures.v1.atomic_write_text(
        report_path,
        fixtures.v1.final_report(payload),
    )
    payload["report"] = {
        "path": str(report_path),
        "sha256": fixtures.v1.sha256_file(report_path),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_matrix_audit(
    path: Path,
    *,
    auditor_path: Path = MATRIX_FINALIZER_PATH,
    helper_path: Path = L200_PATH,
) -> None:
    fixtures = load_module("collector_matrix_fixtures", MATRIX_FIXTURES_PATH)
    raw_path = path.parent / "matrix.json"
    raw_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha": SOURCE_SHA,
                "code_changes_during_run": 0,
                "matrix": "30/60S/60P/100",
            }
        ),
        encoding="utf-8",
    )
    cells = {}
    results = {}
    for name in ("L30", "L60S", "L60P", "L100"):
        evidence_path = path.parent / f"{name}.json"
        evidence = fixtures.evidence(name)
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        cells[name] = {
            "project_id": evidence["project"]["id"],
            "target": fixtures.matrix.EXPECTED_CELLS[name]["target"],
            "violations": [],
            "evidence_path": str(evidence_path),
            "evidence_sha256": collector.sha256_file(evidence_path),
        }
        results[name] = {
            "violations": [],
            "evidence": evidence,
        }
    report_path = path.parent / "matrix-report.md"
    report_path.write_text(
        fixtures.matrix.final_report({"source_sha": SOURCE_SHA}, results),
        encoding="utf-8",
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": fixtures.matrix.MATRIX_AUDIT_SCHEMA_VERSION,
                "result": "pass",
                "violations": [],
                "identity": {
                    "source_sha": SOURCE_SHA,
                    "code_changes_during_run": 0,
                    "candidate_stack": {
                        "spark_model": "gpt-5.6-sol",
                    },
                    "matrix_manifest": {
                        "path": str(raw_path),
                        "sha256": collector.sha256_file(raw_path),
                    },
                },
                "auditor": {
                    "path": str(auditor_path),
                    "source_path": collector.relative(MATRIX_FINALIZER_PATH),
                    "sha256": collector.sha256_file(auditor_path),
                    "database_helper_path": str(helper_path),
                    "database_helper_source_path": collector.relative(L200_PATH),
                    "database_helper_sha256": collector.sha256_file(helper_path),
                },
                "cells": cells,
                "final_report": {
                    "path": str(report_path),
                    "sha256": collector.sha256_file(report_path),
                },
            }
        ),
        encoding="utf-8",
    )


def write_evidence(
    path: Path,
    kind: str,
    *,
    candidate_path: Path | None = None,
) -> None:
    if kind == "release_gates":
        runner = load_module("collector_gate_runner", RUN_RC_GATES_PATH)
        configured = {step["name"]: step for step in runner.gate_steps()}
        candidate_path = candidate_path or path.parent / "candidate-draft.json"
        shared_candidate(candidate_path)
        steps = []
        for name in GATE_STEPS:
            configured_step = configured[name]
            command = list(configured_step["command"])
            junit_path = path.parent / f"{name}.xml"
            if name in PYTEST_STEPS:
                command.append(f"--junitxml={junit_path}")
            log_path = path.parent / f"{name}.log"
            log_path.write_text(
                "started_at=2026-07-22T12:00:00+00:00\n"
                + "command="
                + json.dumps(command, ensure_ascii=False)
                + f"\n{name}: pass\n",
                encoding="utf-8",
            )
            step = {
                "name": name,
                "kind": configured_step["kind"],
                "command": command,
                "started_at": "2026-07-22T12:00:00+00:00",
                "completed_at": "2026-07-22T12:01:00+00:00",
                "duration_seconds": 60.0,
                "passed": True,
                "exit_code": 0,
                "log": {
                    "path": str(log_path),
                    "sha256": collector.sha256_file(log_path),
                },
            }
            if name in PYTEST_STEPS:
                junit_path.write_text(
                    '<testsuites><testsuite tests="1" failures="0" '
                    'errors="0"><testcase name="pass"/></testsuite></testsuites>\n',
                    encoding="utf-8",
                )
                step["junit"] = {
                    "path": str(junit_path),
                    "exists": True,
                    "sha256": collector.sha256_file(junit_path),
                }
            steps.append(step)
        payload = {
            "schema_version": 1,
            "identity": {
                "source_sha": SOURCE_SHA,
                "source_tree": SOURCE_TREE,
                "runtime_image": RUNTIME_IMAGE,
                "browser_image": BROWSER_IMAGE,
                "rc_manifest": {
                    "path": str(candidate_path),
                    "sha256": collector.sha256_file(candidate_path),
                },
            },
            "runner": {
                "path": str(RUN_RC_GATES_PATH),
                "sha256": collector.sha256_file(RUN_RC_GATES_PATH),
            },
            "release_gate_passed": True,
            "all_steps_completed": True,
            "required_step_names": list(GATE_STEPS),
            "completed_step_names": list(GATE_STEPS),
            "steps": steps,
        }
    elif kind == "post_decision_smoke":
        write_smoke_evidence(path, candidate_path=candidate_path)
        return
    elif kind == "live_recovery":
        write_recovery_evidence(path, candidate_path=candidate_path)
        return
    elif kind == "v1_preflight":
        write_v1_evidence(path, candidate_path=candidate_path)
        return
    else:
        payload = {
            "schema_version": 1,
            "source_sha": SOURCE_SHA,
            "result": "pass",
        }
    path.write_text(json.dumps(payload), encoding="utf-8")


def final_args(tmp_path: Path) -> argparse.Namespace:
    candidate_path = tmp_path / "shared-candidate.json"
    shared_candidate(candidate_path)
    paths = {
        kind: tmp_path / f"{kind}.json"
        for kind in (
            "v1_preflight",
            "release_gates",
            "live_recovery",
            "post_decision_smoke",
        )
    }
    for kind, path in paths.items():
        write_evidence(path, kind, candidate_path=candidate_path)
    return argparse.Namespace(
        draft=False,
        rc_tag="v5.0.0-rc1",
        v1_manifest=paths["v1_preflight"],
        gate_manifest=paths["release_gates"],
        recovery_manifest=paths["live_recovery"],
        smoke_manifest=paths["post_decision_smoke"],
    )


def test_model_routing_is_resolved_for_every_runtime_role_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = (
        "forwin",
        "generation-worker",
        "outbox-worker",
        "forwin-mcp",
        "publisher-worker",
    )
    containers = [f"stack-{service}" for service in services]

    def fake_run(*command: str) -> str:
        assert command[:3] == ("docker", "container", "inspect")
        container = command[3]
        service = container.removeprefix("stack-")
        env = [
            "FORWIN_EMBEDDING_BACKEND=gateway",
            "FORWIN_EMBEDDING_BASE_URL=http://embedding:8080",
            "FORWIN_EMBEDDING_MODEL=embedding-model",
            "FORWIN_EMBEDDING_DIMS=384",
            "FORWIN_EMBEDDING_REQUIRED=true",
        ]
        if service in collector.MODEL_EXECUTION_SERVICES:
            env.extend(
                [
                    "MINIMAX_API_KEY=super-secret",
                    "MINIMAX_BASE_URL=https://model.example/v1",
                    "MINIMAX_MODEL=primary-model",
                    "KIMI_API_KEY=kimi-secret",
                    "KIMI_MODEL=fallback-model",
                    "FORWIN_CODEX_ENABLED=true",
                    "FORWIN_CODEX_DEFAULT_MODEL=codex-model",
                ]
            )
        return json.dumps(
            [
                {
                    "Id": f"{service}-id",
                    "Image": "runtime-image-id",
                    "Config": {
                        "Env": env,
                        "Labels": {
                            "com.docker.compose.project": "forwin-rc",
                            "com.docker.compose.service": service,
                        },
                    },
                    "State": {
                        "Running": True,
                        "Health": {"Status": "healthy"},
                    },
                }
            ]
        )

    monkeypatch.setattr(collector, "run", fake_run)
    resolved = collector.inspect_model_environments(
        containers,
        "runtime-image-id",
        model_profile_id="env-minimax",
    )

    assert set(resolved["services"]) == set(services)
    assert resolved["compose_project"] == "forwin-rc"
    assert all(
        item["routing"]["selected_profile"]["model"] == "primary-model"
        for service, item in resolved["services"].items()
        if service in collector.MODEL_EXECUTION_SERVICES
    )
    assert all(
        item["routing"]["codex"]["default_model"] == "codex-model"
        for service, item in resolved["services"].items()
        if service in collector.MODEL_EXECUTION_SERVICES
    )
    assert all(
        not item["routing"]["selected_profile"]["api_key_configured"]
        and not item["routing"]["fallback_profiles"]
        and not item["routing"]["codex"]["enabled"]
        for service, item in resolved["services"].items()
        if service in collector.MODEL_PASSIVE_SERVICES
    )
    assert set(resolved["routing_group_hashes"]) == {
        "model_execution",
        "passive",
    }
    assert (
        resolved["routing_group_hashes"]["model_execution"]
        != resolved["routing_group_hashes"]["passive"]
    )
    assert "super-secret" not in repr(resolved)
    assert "kimi-secret" not in repr(resolved)


def test_model_routing_rejects_incomplete_runtime_role_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        collector,
        "run",
        lambda *_command: json.dumps(
            [
                {
                    "Id": "api-id",
                    "Image": "runtime-image-id",
                    "Config": {
                        "Env": [],
                        "Labels": {
                            "com.docker.compose.project": "forwin-rc",
                            "com.docker.compose.service": "forwin",
                        },
                    },
                    "State": {"Running": True},
                }
            ]
        ),
    )

    with pytest.raises(collector.ManifestError, match="service set"):
        collector.inspect_model_environments(
            ["only-api"],
            "runtime-image-id",
            model_profile_id="",
        )


def test_passive_runtime_role_rejects_model_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = tuple(sorted(collector.EXPECTED_RUNTIME_SERVICES))

    def fake_run(*command: str) -> str:
        service = command[3].removeprefix("stack-")
        env = (
            ["MINIMAX_API_KEY=must-not-be-here"]
            if service == "forwin-mcp"
            else []
        )
        return json.dumps(
            [
                {
                    "Id": f"{service}-id",
                    "Image": "runtime-image-id",
                    "Config": {
                        "Env": env,
                        "Labels": {
                            "com.docker.compose.project": "forwin-rc",
                            "com.docker.compose.service": service,
                        },
                    },
                    "State": {"Running": True},
                }
            ]
        )

    monkeypatch.setattr(collector, "run", fake_run)

    with pytest.raises(collector.ManifestError, match="passive runtime service"):
        collector.inspect_model_environments(
            [f"stack-{service}" for service in services],
            "runtime-image-id",
            model_profile_id="",
        )


def test_model_routing_defaults_match_source_configuration() -> None:
    config_path = MODULE_PATH.parents[2] / "forwin/config.py"

    assert collector.assert_routing_defaults(config_path) == {
        key: collector.ROUTING_DEFAULTS[key]
        for key in sorted(collector.ROUTING_DEFAULTS)
    }


def test_model_routing_default_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        collector.ROUTING_DEFAULTS,
        "MINIMAX_MODEL",
        "stale-model",
    )
    config_path = MODULE_PATH.parents[2] / "forwin/config.py"

    with pytest.raises(collector.ManifestError, match="routing defaults drift"):
        collector.assert_routing_defaults(config_path)


def test_final_release_candidate_binds_annotated_tag_and_passing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = final_args(tmp_path)

    def fake_run(*command: str) -> str:
        if command == ("git", "cat-file", "-t", "refs/tags/v5.0.0-rc1"):
            return "tag"
        if command == ("git", "rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return SOURCE_SHA
        if command == ("git", "rev-parse", "refs/tags/v5.0.0-rc1"):
            return "b" * 40
        raise AssertionError(command)

    monkeypatch.setattr(collector, "run", fake_run)

    release = collector.collect_release_candidate(args, SOURCE_SHA)

    assert release["status"] == "frozen"
    assert release["annotated_tag"] == "v5.0.0-rc1"
    assert release["tag_object_sha"] == "b" * 40
    assert release["source_sha"] == SOURCE_SHA
    assert set(release["evidence"]) == {
        "v1_preflight",
        "release_gates",
        "live_recovery",
        "post_decision_smoke",
    }
    assert all(item["result"] == "pass" for item in release["evidence"].values())


def test_final_release_candidate_rejects_lightweight_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = final_args(tmp_path)
    monkeypatch.setattr(collector, "run", lambda *_args: "commit")

    with pytest.raises(collector.ManifestError, match="not annotated"):
        collector.collect_release_candidate(args, SOURCE_SHA)


def test_final_release_candidate_rejects_cross_candidate_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = final_args(tmp_path)
    gate = json.loads(args.gate_manifest.read_text(encoding="utf-8"))
    original_candidate = Path(gate["identity"]["rc_manifest"]["path"])
    swapped_candidate = tmp_path / "swapped-candidate.json"
    swapped = json.loads(original_candidate.read_text(encoding="utf-8"))
    swapped["evidence_nonce"] = "different-candidate"
    swapped_candidate.write_text(json.dumps(swapped), encoding="utf-8")
    gate["identity"]["rc_manifest"] = {
        "path": str(swapped_candidate),
        "sha256": collector.sha256_file(swapped_candidate),
    }
    args.gate_manifest.write_text(json.dumps(gate), encoding="utf-8")

    def fake_run(*command: str) -> str:
        if command == ("git", "cat-file", "-t", "refs/tags/v5.0.0-rc1"):
            return "tag"
        if command == ("git", "rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return SOURCE_SHA
        if command == ("git", "rev-parse", "refs/tags/v5.0.0-rc1"):
            return "b" * 40
        raise AssertionError(command)

    monkeypatch.setattr(collector, "run", fake_run)

    with pytest.raises(collector.ManifestError, match="share one candidate"):
        collector.collect_release_candidate(args, SOURCE_SHA)


def test_final_release_candidate_requires_all_four_evidence_files(
    tmp_path: Path,
) -> None:
    args = final_args(tmp_path)
    args.v1_manifest = None

    with pytest.raises(collector.ManifestError, match="v1-manifest"):
        collector.collect_release_candidate(args, SOURCE_SHA)


def test_draft_release_candidate_is_explicitly_not_frozen() -> None:
    args = argparse.Namespace(
        draft=True,
        rc_tag="",
        v1_manifest=None,
        gate_manifest=None,
        recovery_manifest=None,
        smoke_manifest=None,
    )

    assert collector.collect_release_candidate(args, SOURCE_SHA) == {
        "status": "draft",
        "source_sha": SOURCE_SHA,
        "annotated_tag": "",
        "tag_object_sha": "",
        "evidence": {},
    }


def test_release_gate_evidence_rejects_tampered_nested_log(tmp_path: Path) -> None:
    path = tmp_path / "release-gates.json"
    write_evidence(path, "release_gates")
    (tmp_path / "v1-fresh-schema.log").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="artifact hash mismatch"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="release_gates",
        )


def test_release_gate_evidence_rejects_substituted_runner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release-gates.json"
    write_evidence(path, "release_gates")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runner"] = {
        "path": str(MODULE_PATH),
        "sha256": collector.sha256_file(MODULE_PATH),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="runner path mismatch"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="release_gates",
        )


def test_release_gate_evidence_rejects_substituted_command_and_log(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release-gates.json"
    write_evidence(path, "release_gates")
    payload = json.loads(path.read_text(encoding="utf-8"))
    step = payload["steps"][0]
    step["command"] = ["true"]
    log_path = Path(step["log"]["path"])
    log_path.write_text(
        "started_at=2026-07-22T12:00:00+00:00\ncommand=[\"true\"]\n",
        encoding="utf-8",
    )
    step["log"]["sha256"] = collector.sha256_file(log_path)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="command mismatch"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="release_gates",
        )


def test_release_gate_evidence_rejects_failing_junit_with_updated_hash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release-gates.json"
    write_evidence(path, "release_gates")
    payload = json.loads(path.read_text(encoding="utf-8"))
    junit = payload["steps"][0]["junit"]
    junit_path = Path(junit["path"])
    junit_path.write_text(
        '<testsuites><testsuite tests="1" failures="1" errors="0">'
        '<testcase name="fail"><failure/></testcase>'
        "</testsuite></testsuites>\n",
        encoding="utf-8",
    )
    junit["sha256"] = collector.sha256_file(junit_path)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="JUnit is not passing"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="release_gates",
        )


def test_release_gate_evidence_rejects_candidate_image_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release-gates.json"
    write_evidence(path, "release_gates")
    mismatched_runtime = {
        **RUNTIME_IMAGE,
        "image_id": "sha256:" + "9" * 64,
    }

    with pytest.raises(collector.ManifestError, match="runtime image mismatch"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="release_gates",
            expected_gate_identity={
                "source_tree": SOURCE_TREE,
                "runtime_image": mismatched_runtime,
                "browser_image": BROWSER_IMAGE,
            },
        )


def test_smoke_evidence_rejects_tampered_nested_state(tmp_path: Path) -> None:
    path = tmp_path / "smoke.json"
    write_smoke_evidence(path)
    (tmp_path / "smoke-evidence.json").write_text("{}", encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="artifact hash mismatch"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="post_decision_smoke",
        )


def test_smoke_evidence_rejects_forged_report_and_updated_hash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "smoke.json"
    write_smoke_evidence(path)
    report_path = tmp_path / "smoke-report.md"
    report_path.write_text("# Forged pass\n", encoding="utf-8")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["final_report"]["sha256"] = collector.sha256_file(report_path)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="report content mismatch"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="post_decision_smoke",
        )


def test_recovery_evidence_rejects_tampered_fault_report(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    Path(payload["faults"]["publisher_mfa"]["path"]).write_text(
        "{}",
        encoding="utf-8",
    )

    with pytest.raises(collector.ManifestError, match="recovery evidence invalid"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


def test_recovery_evidence_rejects_wrong_family_runner(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    def swap_runner(report: dict) -> None:
        report["runner"] = {
            "path": str(MINIO_RECOVERY_RUNNER_PATH.resolve()),
            "sha256": collector.sha256_file(MINIO_RECOVERY_RUNNER_PATH),
        }

    rewrite_recovery_report(payload, "qdrant_unavailable", swap_runner)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="runner path mismatch"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


def test_recovery_runner_rejects_modified_bytes_with_copied_hash(
    tmp_path: Path,
) -> None:
    runner_path = tmp_path / "generation_projection_recovery.py"
    runner_path.write_text("original runner\n", encoding="utf-8")
    copied_hash = collector.sha256_file(runner_path)
    runner_identity = {
        "path": str(runner_path),
        "sha256": copied_hash,
    }
    release_files = {
        collector.relative(runner_path): copied_hash,
    }
    runner_path.write_text("modified runner\n", encoding="utf-8")

    with pytest.raises(
        collector.ManifestError,
        match="runner artifact hash mismatch",
    ):
        collector.validate_recovery_runner_identity(
            runner_identity,
            expected_runner=runner_path,
            release_files=release_files,
            fault_kind="generation_worker_precommit_crash",
        )


def test_recovery_evidence_rejects_runner_missing_from_candidate_source(
    tmp_path: Path,
) -> None:
    candidate_path = tmp_path / "candidate.json"
    candidate = shared_candidate(candidate_path)
    missing = GENERATION_RECOVERY_RUNNER_PATH.relative_to(
        MODULE_PATH.parents[2]
    ).as_posix()
    candidate["release_harness"]["files"] = [
        item
        for item in candidate["release_harness"]["files"]
        if item["path"] != missing
    ]
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path, candidate_path=candidate_path)

    with pytest.raises(
        collector.ManifestError,
        match="runner is not bound by candidate source",
    ):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


@pytest.mark.parametrize(
    ("identity_key", "image_key"),
    (
        ("source_tree", ""),
        ("runtime_image", "runtime"),
        ("browser_image", "publisher_browser"),
        ("dependency_images", "postgres"),
        ("dependency_images", "qdrant"),
        ("dependency_images", "minio"),
    ),
)
def test_recovery_evidence_rejects_collector_candidate_identity_mismatch(
    tmp_path: Path,
    identity_key: str,
    image_key: str,
) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = expected_recovery_candidate_identity(payload)
    if identity_key == "source_tree":
        expected["source_tree"] = "f" * 40
    elif identity_key == "dependency_images":
        expected[identity_key][image_key] = {
            **expected[identity_key][image_key],
            "image_id": "sha256:" + "f" * 64,
        }
    else:
        expected[identity_key] = {
            **expected[identity_key],
            "image_id": "sha256:" + "f" * 64,
        }

    with pytest.raises(
        collector.ManifestError,
        match="recovery candidate .* mismatch",
    ):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
            expected_candidate_identity=expected,
        )


def test_recovery_evidence_rejects_source_identity_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_sha"] = "f" * 40
    payload["identity"]["source_sha"] = "f" * 40
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="source_sha"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


def test_recovery_evidence_rejects_candidate_manifest_hash_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidate_path = Path(payload["identity"]["rc_manifest"]["path"])
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["tampered"] = True
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    with pytest.raises(
        collector.ManifestError,
        match="candidate manifest artifact hash mismatch",
    ):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


def test_recovery_evidence_rejects_endpoint_cross_binding_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    def detach_endpoint(report: dict) -> None:
        before = next(
            item for item in report["artifacts"] if item["stage"] == "before"
        )
        snapshot_path = Path(before["path"])
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["state"]["target"]["endpoint_identity"]["run_id"] = "f" * 32
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        before["sha256"] = collector.sha256_file(snapshot_path)

    rewrite_recovery_report(payload, "qdrant_unavailable", detach_endpoint)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        collector.ManifestError,
        match="endpoint identity is not event-bound",
    ):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


def test_recovery_evidence_rejects_cross_fault_docker_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    fixtures = load_module(
        "collector_docker_identity_fixtures",
        RECOVERY_FIXTURES_PATH,
    )

    def swap_docker_daemon(report: dict) -> None:
        events = fixtures.recovery.load_verified_events(
            Path(report["event_log"]["path"])
        )
        for event in events:
            event["identity"]["docker"]["daemon_id"] = "daemon-2"
        fixtures.reseal_event_log(report, events)

    rewrite_recovery_report(
        payload,
        "publisher_browser_unavailable",
        swap_docker_daemon,
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        collector.ManifestError,
        match="Docker identity mismatch",
    ):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


@pytest.mark.parametrize("result", ("setup_blocked", "fail"))
def test_recovery_evidence_rejects_nonpassing_fault_report(
    tmp_path: Path,
    result: str,
) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rewrite_recovery_report(
        payload,
        "publisher_mfa",
        lambda report: report.update(result=result),
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="expected=pass"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


def test_recovery_evidence_rejects_swapped_semantic_evaluator(
    tmp_path: Path,
) -> None:
    assert RECOVERY_EVALUATOR_PATH.resolve() in collector.RELEASE_HARNESS_PATHS
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    swapped = tmp_path / "recovery_evidence.py"
    swapped.write_text("# replacement evaluator\n", encoding="utf-8")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evaluator"] = {
        "path": str(swapped),
        "sha256": collector.sha256_file(swapped),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        collector.ManifestError,
        match="semantic evaluator identity mismatch",
    ):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="live_recovery",
        )


def test_recovery_evidence_binds_swapped_evaluator_to_candidate_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "recovery.json"
    write_recovery_evidence(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    finalizer = load_module(
        "collector_source_bound_recovery_finalizer",
        RECOVERY_EVALUATOR_PATH.with_name("finalize_recovery.py"),
    )
    swapped = RECOVERY_EVALUATOR_PATH.with_name(
        "test_recovery_evidence.py"
    ).resolve()
    swapped_identity = {
        "path": str(swapped),
        "sha256": collector.sha256_file(swapped),
    }
    monkeypatch.setattr(finalizer, "EVALUATOR_PATH", swapped)
    payload["evaluator"] = swapped_identity
    for item in payload["faults"].values():
        report_path = Path(item["path"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["evaluator"] = swapped_identity
        report_path.write_text(json.dumps(report), encoding="utf-8")
        item["sha256"] = collector.sha256_file(report_path)
        item["evaluator"] = swapped_identity

    class NoopLoader:
        @staticmethod
        def exec_module(module) -> None:
            return None

    monkeypatch.setattr(
        collector.importlib.util,
        "spec_from_file_location",
        lambda *args, **kwargs: SimpleNamespace(loader=NoopLoader()),
    )
    monkeypatch.setattr(
        collector.importlib.util,
        "module_from_spec",
        lambda spec: finalizer,
    )

    with pytest.raises(
        collector.ManifestError,
        match="semantic evaluator is not bound by candidate source",
    ):
        collector.validate_recovery_evidence(payload, source_sha=SOURCE_SHA)


def test_v1_evidence_rejects_tampered_event_log(tmp_path: Path) -> None:
    path = tmp_path / "v1.json"
    write_v1_evidence(path)
    (tmp_path / "v1-stack-events.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="V1 evidence invalid"):
        collector.load_release_evidence(
            path,
            source_sha=SOURCE_SHA,
            kind="v1_preflight",
        )


def test_final_rc_rejects_running_matrix_manifest(tmp_path: Path) -> None:
    path = tmp_path / "matrix-running.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha": SOURCE_SHA,
                "code_changes_during_run": 0,
                "cells": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(collector.ManifestError, match="final audit"):
        collector.load_matrix_manifest(
            path,
            SOURCE_SHA,
            require_final_audit=True,
        )


def test_final_rc_accepts_revalidated_matrix_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(audit_path)

    def fake_run(*command: str) -> str:
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            return ""
        if command[:4] == ("git", "diff", "--name-status", "--no-renames"):
            return "M\tforwin/publisher_runtime/covers.py"
        if command[:2] == ("git", "rev-parse"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(collector, "run", fake_run)

    result = collector.load_matrix_manifest(
        audit_path,
        "b" * 40,
        require_final_audit=True,
    )

    assert result["result"] == "pass"
    assert result["source_sha"] == SOURCE_SHA
    assert result["current_rc_source_sha"] == "b" * 40
    assert result["predecessor_delta"]["mode"] == "bounded_successor"


def test_final_rc_rejects_outer_matrix_cell_project_mismatch(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(audit_path)
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    payload["cells"]["L100"]["project_id"] = "forged-project-id"
    audit_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        collector.ManifestError,
        match="matrix final audit cell project mismatch: L100",
    ):
        collector.load_matrix_manifest(
            audit_path,
            SOURCE_SHA,
            require_final_audit=True,
        )


def test_final_rc_rejects_matrix_without_live_spark_evidence(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(audit_path)
    matrix_fixtures = load_module(
        "collector_matrix_empty_spark_fixtures",
        MATRIX_FIXTURES_PATH,
    )
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    for name in ("L60S", "L100"):
        evidence_path = Path(payload["cells"][name]["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        matrix_fixtures.replace_spark_evidence(
            evidence,
            matrix_fixtures.empty_spark_evidence(),
        )
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        payload["cells"][name]["evidence_sha256"] = collector.sha256_file(
            evidence_path
        )
    audit_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="live Spark delegation"):
        collector.load_matrix_manifest(
            audit_path,
            SOURCE_SHA,
            require_final_audit=True,
        )


def test_final_rc_rejects_rehashed_balanced_spark_counts_without_chains(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(audit_path)
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    evidence_path = Path(payload["cells"]["L100"]["evidence_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["operational"]["spark"] = {
        "gate_delegation_requested": 1,
        "gate_delegation_decided": 1,
        "gate_delegation_approved": 1,
        "gate_delegation_failed": 0,
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    payload["cells"]["L100"]["evidence_sha256"] = collector.sha256_file(
        evidence_path
    )
    audit_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        collector.ManifestError,
        match="cell evidence fails revalidation: L100",
    ):
        collector.load_matrix_manifest(
            audit_path,
            SOURCE_SHA,
            require_final_audit=True,
        )


def test_final_rc_accepts_matrix_tools_executed_from_frozen_worktree(
    tmp_path: Path,
) -> None:
    frozen_harness = tmp_path / "frozen/.artifacts/rc-candidate"
    frozen_harness.mkdir(parents=True)
    frozen_auditor = frozen_harness / "finalize_matrix.py"
    frozen_helper = frozen_harness / "l200_evidence.py"
    frozen_auditor.write_bytes(MATRIX_FINALIZER_PATH.read_bytes())
    frozen_helper.write_bytes(L200_PATH.read_bytes())
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(
        audit_path,
        auditor_path=frozen_auditor,
        helper_path=frozen_helper,
    )

    result = collector.load_matrix_manifest(
        audit_path,
        SOURCE_SHA,
        require_final_audit=True,
    )

    assert result["result"] == "pass"
    assert result["source_sha"] == SOURCE_SHA


def test_final_rc_accepts_actual_matrix_artifact_source_paths(
    tmp_path: Path,
) -> None:
    frozen_harness = tmp_path / "frozen/.artifacts/v4-matrix-candidate"
    frozen_harness.mkdir(parents=True)
    frozen_auditor = frozen_harness / "finalize_matrix.py"
    frozen_helper = frozen_harness / "l200_evidence.py"
    frozen_auditor.write_bytes(MATRIX_FINALIZER_PATH.read_bytes())
    frozen_helper.write_bytes(L200_PATH.read_bytes())
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(
        audit_path,
        auditor_path=frozen_auditor,
        helper_path=frozen_helper,
    )
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    payload["auditor"]["source_path"] = (
        ".artifacts/v4-matrix-candidate/finalize_matrix.py"
    )
    payload["auditor"]["database_helper_source_path"] = (
        ".artifacts/v4-matrix-candidate/l200_evidence.py"
    )
    audit_path.write_text(json.dumps(payload), encoding="utf-8")

    result = collector.load_matrix_manifest(
        audit_path,
        SOURCE_SHA,
        require_final_audit=True,
    )

    assert result["result"] == "pass"


@pytest.mark.parametrize(
    "source_path",
    (
        "/tmp/finalize_matrix.py",
        "../finalize_matrix.py",
        ".artifacts/copied/finalize_matrix.py",
        ".artifacts/v4-matrix-candidate/not_the_finalizer.py",
    ),
)
def test_final_rc_rejects_matrix_source_path_not_bound_to_execution_copy(
    tmp_path: Path,
    source_path: str,
) -> None:
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(audit_path)
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    payload["auditor"]["source_path"] = source_path
    audit_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        collector.ManifestError,
        match="matrix final audit tool identity mismatch: path",
    ):
        collector.load_matrix_manifest(
            audit_path,
            SOURCE_SHA,
            require_final_audit=True,
        )


def test_final_rc_rejects_legacy_matrix_audit_schema(tmp_path: Path) -> None:
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(audit_path)
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    audit_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="schema version"):
        collector.load_matrix_manifest(
            audit_path,
            SOURCE_SHA,
            require_final_audit=True,
        )


def test_matrix_successor_rejects_unapproved_writer_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*command: str) -> str:
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            return ""
        if command[:4] == ("git", "diff", "--name-status", "--no-renames"):
            return "M\tforwin/writer/generation.py"
        raise AssertionError(command)

    monkeypatch.setattr(collector, "run", fake_run)

    with pytest.raises(collector.ManifestError, match="unapproved path"):
        collector.matrix_successor_delta(SOURCE_SHA, "b" * 40)


def test_matrix_successor_rejects_unapproved_test_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*command: str) -> str:
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            return ""
        if command[:4] == ("git", "diff", "--name-status", "--no-renames"):
            return "M\ttests/test_writer_generation.py"
        raise AssertionError(command)

    monkeypatch.setattr(collector, "run", fake_run)

    with pytest.raises(collector.ManifestError, match="unapproved path"):
        collector.matrix_successor_delta(SOURCE_SHA, "b" * 40)


def test_matrix_successor_accepts_explicit_recovery_schema_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*command: str) -> str:
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            return ""
        if command[:4] == ("git", "diff", "--name-status", "--no-renames"):
            return (
                "M\tforwin/models/base.py\n"
                "M\ttests/test_v5_recovery_schema.py"
            )
        if command[:2] == ("git", "rev-parse"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(collector, "run", fake_run)

    result = collector.matrix_successor_delta(SOURCE_SHA, "b" * 40)

    assert result["mode"] == "bounded_successor"
    assert [item["path"] for item in result["changes"]] == [
        "forwin/models/base.py",
        "tests/test_v5_recovery_schema.py",
    ]


def test_final_rc_rejects_forged_matrix_report_and_updated_hash(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "matrix-audit.json"
    write_matrix_audit(audit_path)
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    report_path = Path(payload["final_report"]["path"])
    report_path.write_text("# Forged PASS\n", encoding="utf-8")
    payload["final_report"]["sha256"] = collector.sha256_file(report_path)
    audit_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(collector.ManifestError, match="report content mismatch"):
        collector.load_matrix_manifest(
            audit_path,
            SOURCE_SHA,
            require_final_audit=True,
        )
