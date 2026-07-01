# ForWin

AI-assisted long-form Chinese web novel generation and publishing system.

ForWin is built around a FastAPI application, a CLI entrypoint, PostgreSQL-backed project state, publishing workflows, and governance / review layers for managing long-running writing projects that span hundreds of chapters.

## Engineering Summary

While the user-facing surface is a novel-generation platform, the engineering substance is a multi-stage **content governance pipeline** for LLM-produced text:

- **Rule extraction → constraint checking → quality gate → LLM reviewer → automated repair → human override**, modeled as **6-state checkpoint transitions** (`pending` / `pass` / `warn` / `fail` / `error` / `overridden`) for full auditability.
- A **rule engine** over **6 constraint families** (character availability, secret withhold, relationship preservation, thread keep-open, location availability, rule preservation) with hard / soft / hint severity levels, persisted via an active-rule store and an artifact ledger.
- An **agentic remediation loop** with a repair-scope router and loop detector that automatically routes failed reviews to specialized handlers and escalates pathological repair cycles to human override.
- An **MCP tool server** that lets LLM agents query project state, trigger reviews, and apply controlled overrides through a typed tool interface.
- Backed by **FastAPI + PostgreSQL + Qdrant + MinIO + Alembic** with a substantial automated test suite covering the governance, review, repair, and publishing paths.

## Highlights

- FastAPI web API for project management, runtime control, and publisher-facing workflows
- CLI entrypoint and MCP operator interface for local / Codex-assisted operations
- Structured persistence with SQLAlchemy-backed state
- Runtime generation pipeline with Genesis handoff, governance checks, checkpointing, and BookState canon admission
- Browser / publisher tooling powered by Playwright
- Optional vector and object-storage integrations via Qdrant and MinIO
- A substantial automated test suite covering generation flow, governance, payload handling, and publishing behavior

## Tech Stack

- Python 3.12+
- FastAPI
- SQLAlchemy
- Pydantic
- httpx
- Playwright
- Qdrant
- MinIO
- Docker Compose
- pytest / pytest-asyncio

## Repository Layout

```text
forwin/
├── api.py                    # Compatibility entrypoint for the split FastAPI app
├── api_core/                 # FastAPI app lifecycle, task, generation, and project helpers
├── api_*_routes.py           # API route groups
├── cli.py                    # CLI entrypoint
├── runtime/                  # Runtime container and service wiring
├── book_genesis_core/        # Genesis workspace and early project setup
├── genesis_handoff/          # Genesis -> chapter production handoff
├── generation/               # Generation task state and workset helpers
├── production/               # Production planner / executor path
├── book_state/               # BookState DB Canon runtime
├── map/                      # Scheme C BookMap runtime
├── reviewer/                 # Main review facade
├── canon_quality/            # Deterministic canon-quality analyzers
├── publisher_runtime/        # Browser-extension publishing runtime
├── publishers/               # Publishing platform integration layer
├── mcp/                      # ForWin MCP server
└── codex_bridge/             # Optional Codex bridge runtime

Design-docs/                  # Current architecture docs, status, and maintenance log
docs/                         # Operator docs and branch-scoped design specs
scripts/                      # Browser / publisher operational probes
tests/                        # Automated test suite
frontend/world-studio/        # React / Vite World Studio frontend
```

## Getting Started

### Prerequisites

- Python 3.12+
- Docker and Docker Compose, if you want the containerized workflow

### Local development

```bash
python -m pip install -e .[test]
docker compose up -d postgres-test qdrant
export FORWIN_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test
export FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test
export FORWIN_QDRANT_URL=http://127.0.0.1:6335
export FORWIN_ARTIFACT_BACKEND=local
export FORWIN_PUBLISHER_EXTENSION_API_KEY=
export FORWIN_PUBLISHER_SESSION_SECRET=
export FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=false
uvicorn forwin.api:app --reload --host 127.0.0.1 --port 8899
```

The Compose `postgres` service is internal-only by default. For host-side local
development, use `postgres-test` on `127.0.0.1:55432` or explicitly expose your
own PostgreSQL instance. Compose exposes Qdrant for debugging on
`127.0.0.1:6335`.

### Docker workflow

```bash
cp .env.example .env
# Edit .env before first start. Replace the MinIO and publisher placeholders, or
# disable the publisher extension by clearing its key/secret and encryption flag.
docker compose up -d --build forwin forwin-mcp
```

By default the main web API is bound to `127.0.0.1:8899`, so it is reachable only from the server itself.
The Compose stack includes `postgres:16-alpine` for runtime state, Qdrant for vector retrieval, MinIO for object storage, `forwin-mcp` on `127.0.0.1:8896`, and `postgres-test` on `127.0.0.1:55432` for tests.
SQLite files such as `data/novel.db` are no longer supported as a runtime database.

The checked-in `.env.example` intentionally contains placeholder credentials.
For a quick local-only run without the publisher extension, set:

```env
FORWIN_PUBLISHER_EXTENSION_API_KEY=
FORWIN_PUBLISHER_SESSION_SECRET=
FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=false
```

For publisher-browser work, keep the extension enabled but replace
`FORWIN_PUBLISHER_EXTENSION_API_KEY` and `FORWIN_PUBLISHER_SESSION_SECRET` with
long random values before starting the backend.

Routine production login continuity uses backend-synced browser sessions. The
shared `forwin-publisher-browser-swarm` profile is the production publisher
browser profile for both Fanqie and Qidian. On startup, the publisher browser
qualifies the extension profile, restores the latest backend-synced sessions
into the same browser context, opens `/publishers`, and waits for extension
heartbeat. Verify the routine path with:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

If the verifier reports `publisher_login_required`, complete the login inside
the production publisher browser profile and rerun the same command. Do not
send QR codes to Discord for routine production login expiry.

Shared production Swarm keeps Discord publisher login webhooks disabled. The
legacy `FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK`,
`FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL`, and
`FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE` settings are ignored by runtime
config and must not be used to route scan-login state to Discord. Do not put
Discord webhook env on browser or worker services. The publisher extension's
login QR notification setting is disabled by default; while disabled, the
extension must not capture a QR image or call
`/api/publishers/extension/login-qr`. A stale profile value of
`loginQrNotificationsEnabled=true` is not enough to re-enable QR forwarding.
Ordinary publisher heartbeat checks must only report `login-required`.

### Current production deployment

Current production is a distributed Docker Swarm deployment, not the local
single-host compose layout described above.

- Source-of-truth code: GitHub repository `Ctwqk/ForWin`, branch `master`.
  Use a fresh local clone or isolated worktree for code changes.
- Production deploy target:
  `10.0.0.126:/Users/magi1/ForWin-swarm`.
- Current baseline Swarm services for this repository:
  `forwin-app-swarm`, `forwin-generation-worker-swarm`,
  `forwin-mcp-swarm`, `forwin-publisher-worker-swarm`,
  `forwin-outbox-worker-swarm`, and
  `forwin-publisher-browser-swarm`.
- User-facing URL: `http://10.0.0.126:8899`.
- MCP/API helper port: `10.0.0.126:8896`.
- Production deploys are picked up by the 150 GitHub sync job after changes are
  pushed to GitHub. The ForWin deploy builds both `forwin-forwin:deploy-<commit>`
  and `forwin-publisher-browser:deploy-<commit>`, then updates the app, worker,
  MCP, outbox, publisher-worker, and publisher-browser Swarm services together.

The 126 directory is a deployment output with `.deploy-sync-project` and
`.deploy-sync-source-commit` markers. Do not create a long-lived Codex coding
project there unless the task is explicitly about inspecting the deployed copy.
The former LAN development host at `10.0.0.246` is retired/unavailable; clone
from GitHub instead of using either 246 or the deploy output as the source
workspace.

Production data stores are centralized on `10.0.0.150`. This repository still
contains database models, migrations, storage code, and local compose profiles,
but production ForWin should connect to the 150-hosted Postgres/Qdrant/MinIO
services instead of starting app-local stateful containers on 126.

The API process should handle UI/API/read/enqueue work. Generation execution is
owned by the durable generation worker, publisher backend jobs by the publisher
worker, and browser automation by the publisher-browser process when production
publishing is enabled. See
`docs/operations/forwin-production-processes.md` for operator checks and role
details.

Production publisher login and service baseline can be checked without
publishing content:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

The command emits one redacted JSON object. `status=ok` means service health,
MCP health, publisher browser state, Discord-login-alert policy, and
Fanqie/Qidian page/API login evidence all agree. `status=degraded` with
`publisher_login_required` means the operator must log in to the named platform
in the shared production publisher browser profile, then rerun the command. The
command must not publish content or send Discord login messages.

### Personal LAN deployment

1. Copy `.env.example` to `.env`.
2. Set `FORWIN_HTTP_BIND` to your server LAN IP, for example `192.168.1.10`.
3. Set MinIO credentials and LLM API keys.
4. Optionally set `FORWIN_HTTP_BASIC_USER` and `FORWIN_HTTP_BASIC_PASSWORD`.
5. Run `docker compose up -d --build forwin forwin-mcp`.

Keep `FORWIN_HTTP_BIND=127.0.0.1` for local-only access. LAN access requires an explicit server LAN IP; do not use `0.0.0.0` unless you mean to listen on every network interface. Basic Auth is lightweight single-user protection for a trusted LAN, not a multi-user permission system.

### Configuration

ForWin reads configuration through `Config.from_env()`. It loads the file
pointed to by `FORWIN_ENV_FILE`, or `.env` when unset, then overlays the real
process environment. Real environment variables always win over values from the
file.

- `FORWIN_QUALITY_PROFILE`: `standard`, `pulp`, or `premium`. Defaults to
  `standard`. `pulp` applies low-cost defaults unless the same field is
  explicitly configured by env.
- `FORWIN_TROPE_TEMPLATE_PATH`: optional JSON or markdown trope template
  library path. Markdown libraries use the section format in
  `Design-docs/trope_library_pulp_v1.md`.

Docker Compose uses `.env` as the default env file. Copy `.env.example` to
`.env`, set server paths, storage settings, and any API keys, then start the
stack. If you use a different file, set `FORWIN_ENV_FILE` for the services and
pass the same file to Compose interpolation when needed, for example
`docker compose --env-file ./prod.env up --build`.

LLM API keys can be configured either in `.env` or through runtime settings.
For personal-server deployments, `.env` is usually the right place for stable
server credentials because it is loaded at process start and can be managed with
the rest of the deployment configuration.

The publisher browser extension uses `FORWIN_PUBLISHER_EXTENSION_API_KEY` when
calling extension-only backend APIs. Set `FORWIN_PUBLISHER_SESSION_SECRET` to a
long random value so ForWin encrypts stored publishing-platform cookies. If this
secret is lost, encrypted sessions cannot be recovered and the publishing
platform must be logged in again. Protect `.env` backups that contain this
secret; without it, old encrypted cookies cannot be decrypted.

For shared production Swarm, keep Discord login alerts disabled. Routine
production login continuity uses backend-synced browser sessions, not QR
delivery. If encrypted publisher sessions cannot be recovered because the
session secret changed or expired, log in again inside the production publisher
browser profile and rerun `python scripts/check_production_publisher_baseline.py`
with the production arguments shown above.

### Codex / MCP operator

When operating a running ForWin backend from Codex, use the repo-local ForWin
MCP server for project, Genesis, task, chapter, and WorldModel workflows. Do not
inspect raw database rows for those workflows when an equivalent MCP tool is
available.

```bash
docker compose up -d forwin forwin-mcp
python3 scripts/check_codex_operator_ready.py
```

See `docs/codex-forwin-mcp.md` and `AGENTS.md` for the operator workflow and
safe generation-task rules.

## Testing

```bash
docker compose up -d postgres-test qdrant
python -m pytest -q
```

Browser tests require Playwright browsers:

```bash
npm --prefix frontend/world-studio ci
npm --prefix frontend/world-studio run build
python -m playwright install chromium
python -m pytest tests/browser -q
```

Live backend / live LLM browser tests are opt-in through the `FORWIN_E2E_*`
environment flags in `tests/browser/test_real_backend_e2e.py`.

## Backup and restore

Run before upgrades or large refactors:

```bash
python scripts/backup_forwin_data.py --output-dir backups --zip
python scripts/verify_forwin_backup.py backups/forwin-backup-YYYYmmdd-HHMMSS
```

For full restore verification, provide an empty PostgreSQL database:

```bash
python scripts/verify_forwin_backup.py backups/forwin-backup-YYYYmmdd-HHMMSS \
  --restore-database-url postgresql+psycopg://forwin:forwin@localhost:5432/forwin_restore_check
```

Backups include a `pg_dump` custom-format database dump, runtime settings, local artifacts when `FORWIN_ARTIFACT_BACKEND=local`, a manifest with SHA256 checksums, and `.env.keys.txt` with environment key names only. `.env` values are not copied to avoid leaking LLM API keys. When using MinIO or Qdrant, also back up the Docker volumes `forwin-minio` and `forwin-qdrant`.

## Notes

- The project is optimized for Chinese-language long-form fiction workflows, but the codebase is broadly interesting as an example of stateful AI orchestration with publishing controls.
- Start with `Design-docs/CURRENT_ARCHITECTURE.md` for the current architecture contract and `Design-docs/DESIGN_STATUS.md` for design document status. Older V2/V3/V4 side-by-side plans remain useful history, but BookState DB Canon and Scheme C BookMap are the current source-of-truth baseline.
