"""ForWin-only Swarm cutover; loaded checkout owns code, image owns runtime.

Run under deploy-github-sync's flock. Credentials stay in protected snapshots and
short-lived env files. No automatic rollback crosses the migration boundary.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
from uuid import uuid4

ROLES = (
    "app",
    "mcp",
    "generation-worker",
    "publisher-worker",
    "outbox-worker",
    "publisher-browser",
)
SERVICES = tuple(f"forwin-{role}-swarm" for role in ROLES)
STOP_ORDER = tuple(
    f"forwin-{role}-swarm"
    for role in (
        "publisher-browser",
        "publisher-worker",
        "generation-worker",
        "outbox-worker",
        "mcp",
        "app",
    )
)
TERMINAL = {"complete", "shutdown", "failed", "rejected", "remove"}
HOST = "10.0.0.126"


def execute(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{argv[0]} outcome is unknown (command timed out)") from exc
    if result.returncode:
        # Docker errors/argv can contain infrastructure credentials. Keep public
        # failures at the operation boundary, never echo complete command output.
        raise RuntimeError(f"{argv[0]} operation failed (exit {result.returncode})")
    return result.stdout


def environment(values):
    return dict(item.split("=", 1) for item in values or [] if "=" in item)


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".forwin-release-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Release:
    def __init__(
        self,
        *,
        revision,
        app_image,
        browser_image,
        state_dir,
        command=execute,
        poll_interval=2,
        timeout=600,
    ):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError("invalid full source revision")
        self.revision = revision
        self.app_image, self.browser_image = app_image, browser_image
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "forwin-release.json"
        self.command, self.poll_interval, self.timeout = command, poll_interval, timeout
        self.state = {}

    def remote(self, *arguments):
        # Inspection, health checks and isolated import probes only; retry never
        # repeats a migration, service mutation or external publication action.
        command = "/opt/homebrew/bin/docker " + shlex.join(arguments)
        try:
            return self.command(["ssh", "-o", "BatchMode=yes", HOST, command])
        except RuntimeError:
            command = (
                "env LIMA_HOME=/Users/magi1/.colima/_lima "
                "/opt/homebrew/bin/limactl shell colima-swarmbridged sh -c "
                + shlex.quote("docker " + shlex.join(arguments))
            )
            return self.command(["ssh", "-o", "BatchMode=yes", HOST, command])

    def specs(self):
        return self.command(["docker", "service", "inspect", *SERVICES])

    def tasks(self, service):
        ids = self.command(
            ["docker", "service", "ps", "--no-trunc", "--format", "{{.ID}}", service]
        ).split()
        return json.loads(self.command(["docker", "inspect", *ids])) if ids else []

    def wait(self, predicate, message):
        deadline = time.monotonic() + self.timeout
        while True:
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(message)
            time.sleep(self.poll_interval)

    def phase(self, phase):
        self.state["phase"] = phase
        self.state["updated_at"] = time.time()
        write_private(self.state_path, self.state)

    def verify_imports(self, image, *, browser):
        code = (
            "import sys,sqlalchemy,psycopg,uvicorn,fastmcp,forwin.migrations; "
            "assert sys.executable == '/app/.venv/bin/python'"
        )
        if browser:
            code += "; import playwright.sync_api,shutil; assert shutil.which('chromium')"
        self.remote(
            "run", "--rm", "--network", "none", "--read-only", "--no-healthcheck",
            "--entrypoint", "/app/.venv/bin/python", image, "-c", code,
        )

    def verify_images(self):
        found = {}
        for role, image in (("app", self.app_image), ("browser", self.browser_image)):
            rows = json.loads(self.remote("image", "inspect", image))
            if len(rows) != 1:
                raise RuntimeError("image identity is ambiguous")
            row = rows[0]
            config = row.get("Config", {})
            env = environment(config.get("Env"))
            if (
                config.get("Labels", {}).get("org.opencontainers.image.revision")
                != self.revision
                or env.get("FORWIN_SOURCE_REVISION") != self.revision
            ):
                raise RuntimeError("image source revision mismatch")
            if not env.get("PATH", "").startswith("/app/.venv/bin:"):
                raise RuntimeError("image does not select locked Python environment")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("Id", "")):
                raise RuntimeError("image has no immutable identity")
            self.verify_imports(row["Id"], browser=role == "browser")
            found[role] = row["Id"]
        return found

    def quiescence(self, specs):
        path = self.state_dir / "forwin-quiescence.json"
        try:
            evidence = json.loads(path.read_text())
            valid = (
                path.stat().st_mode & 0o077 == 0
                and evidence["source_revision"] == self.revision
                and time.time() < float(evidence["expires_at"]) <= time.time() + 3600
                and all(
                    evidence.get(key) is True
                    for key in (
                        "generation_paused",
                        "publisher_idle",
                        "admission_closed",
                        "automation_paused",
                    )
                )
                and isinstance(evidence.get("evidence"), str)
                and bool(evidence["evidence"].strip())
                and evidence["service_versions"]
                == {name: row["Version"]["Index"] for name, row in specs.items()}
            )
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise RuntimeError(
                "valid quiescence evidence is required before stopping services"
            )
        return evidence

    def stop_all(self):
        # Browser stops while the API can still persist its last receipt/journal.
        failed = []
        for service in STOP_ORDER:
            try:
                self.command(
                    ["docker", "service", "scale", "--detach=true", f"{service}=0"]
                )
                self.wait(
                    lambda: all(
                        t["Status"]["State"] in TERMINAL for t in self.tasks(service)
                    ),
                    f"{service} did not stop",
                )
            except Exception:
                failed.append(service)
        if failed:
            raise RuntimeError("could not confirm stop for " + ", ".join(failed))
        self.assert_stopped()

    def assert_stopped(self):
        rows = json.loads(self.specs())
        if any(row["Spec"]["Mode"]["Replicated"]["Replicas"] != 0 for row in rows):
            raise RuntimeError("maintenance requires all six replicas at zero")
        if any(
            task["Status"]["State"] not in TERMINAL
            for service in SERVICES
            for task in self.tasks(service)
        ):
            raise RuntimeError(
                "maintenance requires all previous runtime tasks stopped"
            )

    def maintenance(self, phase):
        self.assert_stopped()
        name = f"forwin-maint-{self.state['release_id']}-{phase}"
        template = self.state["specs"]["forwin-app-swarm"]["Spec"]["TaskTemplate"]
        env = environment(template["ContainerSpec"].get("Env"))
        env.pop("PATH", None)
        env.pop("FORWIN_SOURCE_REVISION", None)
        args = [
            "docker",
            "service",
            "create",
            "--detach=true",
            "--name",
            name,
            "--no-resolve-image",
            "--restart-condition",
            "none",
            "--no-healthcheck",
            "--replicas",
            "1",
            "--constraint",
            f"node.id == {self.state['node_id']}",
        ]
        for network in template["Networks"]:
            args += ["--network", network["Target"]]
        args += [
            "--mount",
            f"type=volume,source={self.state['data_volume']},target=/app/data",
        ]
        fd, filename = tempfile.mkstemp(dir=self.state_dir, prefix=".forwin-maint-env-")
        created = False
        try:
            with os.fdopen(fd, "w") as stream:
                for key, value in env.items():
                    if "\n" in value or "\r" in value:
                        raise RuntimeError(
                            "maintenance environment cannot be represented safely"
                        )
                    stream.write(f"{key}={value}\n")
            args += [
                "--env-file",
                filename,
                self.state["images"]["app"],
                "/app/.venv/bin/python",
                "scripts/forwin_deploy_maintenance.py",
                "--phase",
                phase,
                "--release-id",
                self.state["release_id"],
                "--source-revision",
                self.revision,
            ]
            self.command(args)
            created = True

            def completed():
                tasks = self.tasks(name)
                if len(tasks) != 1:
                    if tasks:
                        raise RuntimeError("maintenance must execute exactly once")
                    return False
                status = tasks[0]["Status"]
                if status["State"] in TERMINAL:
                    if (
                        status["State"] != "complete"
                        or status.get("ContainerStatus", {}).get("ExitCode") != 0
                    ):
                        raise RuntimeError(
                            f"maintenance {phase} did not complete successfully"
                        )
                    return True
                return False

            self.wait(completed, f"maintenance {phase} result is unknown")
            output = self.command(["docker", "service", "logs", "--raw", name])
            proofs = [
                json.loads(line[len("FORWIN_MAINT_RESULT ") :])
                for line in output.splitlines()
                if line.startswith("FORWIN_MAINT_RESULT ")
            ]
            if (
                len(proofs) != 1
                or proofs[0].get("phase") != phase
                or proofs[0].get("source_revision") != self.revision
            ):
                raise RuntimeError(f"maintenance {phase} proof is missing or ambiguous")
            self.state.setdefault("maintenance", {})[phase] = proofs[0]
            write_private(self.state_path, self.state)
        finally:
            os.unlink(filename)
            # Keep unsuccessful tasks available for restricted operator inspection.
        if created:
            self.command(["docker", "service", "rm", name])

    def verify_runtime(self, service):
        desired = [t for t in self.tasks(service) if t.get("DesiredState") == "running"]
        if len(desired) != 1 or desired[0]["Status"]["State"] != "running":
            return False
        task = desired[0]
        if task["NodeID"] != self.state["node_id"]:
            raise RuntimeError("runtime scheduled on an unverified image node")
        container = task["Status"]["ContainerStatus"]["ContainerID"]
        actual = json.loads(self.remote("container", "inspect", container))[0]
        role = "browser" if service.endswith("publisher-browser-swarm") else "app"
        if actual["Image"] != self.state["images"][role]:
            raise RuntimeError("running image differs from verified source image")
        actual_env = environment(actual["Config"].get("Env"))
        if (
            actual_env.get("FORWIN_SOURCE_REVISION") != self.revision
            or actual["Config"]
            .get("Labels", {})
            .get("org.opencontainers.image.revision")
            != self.revision
        ):
            raise RuntimeError(
                "running container source identity differs from its image"
            )
        if (
            not environment(actual["Config"].get("Env"))
            .get("PATH", "")
            .startswith("/app/.venv/bin:")
        ):
            raise RuntimeError("running service overrides locked Python PATH")
        code = (
            "import os,sys,sqlalchemy,psycopg,forwin.migrations; "
            "assert sys.executable == '/app/.venv/bin/python'; "
            f"assert os.environ['FORWIN_SOURCE_REVISION'] == {self.revision!r}"
        )
        if service in ("forwin-app-swarm", "forwin-mcp-swarm"):
            port = 8899 if service == "forwin-app-swarm" else 8896
            code += f"; import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:{port}/health',timeout=5).status == 200"
        try:
            self.remote("exec", container, "/app/.venv/bin/python", "-c", code)
            if role == "browser":
                self.remote(
                    "exec",
                    container,
                    "/app/.venv/bin/python",
                    "scripts/check_publisher_browser_heartbeat.py",
                    "--wait-seconds",
                    "0",
                )
        except RuntimeError:
            return False
        return True

    def run(self):
        if self.state_path.exists():
            previous = json.loads(self.state_path.read_text())
            if previous.get("phase") != "deployed":
                raise RuntimeError(
                    "unfinished ForWin release requires operator reconciliation; roles remain stopped"
                )
            release_id = previous.get("release_id", "")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", release_id):
                raise RuntimeError("previous release evidence has invalid identity")
            archive = self.state_dir / "forwin-releases"
            archive.mkdir(mode=0o700, exist_ok=True)
            if archive.stat().st_mode & 0o077:
                raise RuntimeError("release evidence directory must be private")
            write_private(archive / (release_id + ".json"), previous)
        images = self.verify_images()
        rows = json.loads(self.specs())
        specs = {row["Spec"]["Name"]: row for row in rows}
        if set(specs) != set(SERVICES):
            raise RuntimeError("expected exactly six ForWin services")
        evidence = self.quiescence(specs)
        nodes, volumes = set(), set()
        for name, row in specs.items():
            if row["Spec"]["Mode"].get("Replicated", {}).get("Replicas") != 1:
                raise RuntimeError(
                    "initial ForWin cutover requires the audited single-replica topology"
                )
            container = row["Spec"]["TaskTemplate"]["ContainerSpec"]
            if container.get("Labels", {}).get(
                "org.opencontainers.image.revision"
            ) not in (None, self.revision):
                raise RuntimeError("service overrides the image source label")
            mounts = [
                m
                for m in container.get("Mounts", [])
                if m.get("Target") == "/app/data" and m.get("Type") == "volume"
            ]
            if len(mounts) != 1:
                raise RuntimeError("audited persistent data volume is required")
            volumes.add(mounts[0]["Source"])
            active = [t for t in self.tasks(name) if t.get("DesiredState") == "running"]
            if len(active) != 1 or active[0]["Status"]["State"] != "running":
                raise RuntimeError(
                    "old role must be running and operator-quiesced before cutover"
                )
            nodes.add(active[0]["NodeID"])
        if len(nodes) != 1 or len(volumes) != 1:
            raise RuntimeError(
                "ForWin topology differs from audited shared-node deployment"
            )
        if self.remote("info", "--format", "{{.Swarm.NodeID}}").strip() not in nodes:
            raise RuntimeError("image build daemon does not match the service node")
        self.state = {
            "release_id": self.revision[:12] + "-" + uuid4().hex[:8],
            "source_revision": self.revision,
            "images": images,
            "specs": specs,
            "node_id": nodes.pop(),
            "data_volume": volumes.pop(),
            "quiescence": evidence,
        }
        self.phase("built")
        try:
            self.stop_all()
            self.phase("quiesced")
            # Recheck tags before a maintenance task can select them.
            if self.verify_images() != images:
                raise RuntimeError("image tag changed after validation")
            self.maintenance("backup")
            self.phase("backed_up")
            self.phase("migration_started")
            self.maintenance("migrate")
            self.phase("migrated")
            self.maintenance("verify")
            self.assert_stopped()
            for service in SERVICES:
                image = (
                    images["browser"]
                    if service.endswith("publisher-browser-swarm")
                    else images["app"]
                )
                self.command(
                    [
                        "docker",
                        "service",
                        "update",
                        "--detach=true",
                        "--no-resolve-image",
                        "--env-rm",
                        "PATH",
                        "--env-rm",
                        "FORWIN_SOURCE_REVISION",
                        "--image",
                        image,
                        service,
                    ]
                )
            for service in SERVICES:
                self.command(
                    ["docker", "service", "scale", "--detach=true", f"{service}=1"]
                )
                self.wait(
                    lambda: self.verify_runtime(service),
                    f"{service} runtime verification failed",
                )
            self.phase("verified")
            self.phase("deployed")
        except Exception as exc:
            self.state["error"] = str(exc)
            state_error = False
            try:
                write_private(self.state_path, self.state)
            except Exception:
                state_error = True
            try:
                self.stop_all()
            except Exception:
                self.state["stop_error"] = (
                    "could not confirm every role stopped; inspect Swarm before recovery"
                )
            try:
                write_private(self.state_path, self.state)
            except Exception:
                state_error = True
            secondary = []
            if state_error:
                secondary.append("release state write failed; retain existing evidence")
            if self.state.get("stop_error"):
                secondary.append(self.state["stop_error"])
            if secondary:
                raise RuntimeError(str(exc) + "; " + "; ".join(secondary)) from exc
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--app-image", required=True)
    parser.add_argument("--browser-image", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    release = Release(
        revision=args.revision,
        app_image=args.app_image,
        browser_image=args.browser_image,
        state_dir=args.state_dir,
    )
    try:
        release.verify_images() if args.verify_only else release.run()
    except Exception as exc:
        print(f"ForWin release stopped: {exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
