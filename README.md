# ForWin

AI-assisted long-form Chinese web novel generation and publishing system.

ForWin is built around a FastAPI application, a CLI entrypoint, persistent project state, publishing workflows, and governance / review layers for managing long-running writing projects that span hundreds of chapters.

## Highlights

- FastAPI web API for project management, runtime control, and publisher-facing workflows
- CLI entrypoint for local operations
- Structured persistence with SQLAlchemy-backed state
- Runtime generation pipeline with governance checks and checkpointing
- Browser / publisher tooling powered by Playwright
- Optional vector and object-storage integrations via Qdrant and MinIO
- A substantial automated test suite covering generation flow, governance, payload handling, and publishing behavior

## Tech Stack

- Python 3.12
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
├── api.py                    # Main FastAPI application
├── cli.py                    # CLI entrypoint
├── api_runtime.py            # Runtime and task orchestration helpers
├── api_project_payloads.py   # Project payload builders
├── governance.py             # Governance / checkpoint logic
└── book_genesis.py           # Early-stage project setup flow

Design-docs/                  # Design notes and rollout plans
scripts/                      # Browser / publisher operational probes
tests/                        # Automated test suite
```

## Getting Started

### Prerequisites

- Python 3.12+
- Docker and Docker Compose, if you want the containerized workflow

### Local development

```bash
python -m pip install -e .[test]
export FORWIN_DATABASE_URL=postgresql+psycopg://forwin:forwin@localhost:5432/forwin
export FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test
export FORWIN_QDRANT_URL=http://localhost:6333
uvicorn forwin.api:app --reload --host 127.0.0.1 --port 8899
```

### Docker workflow

```bash
cp .env.example .env
docker compose up --build
```

By default the main web API is bound to `127.0.0.1:8899`, so it is reachable only from the server itself.
The Compose stack includes `postgres:16-alpine` for runtime state, Qdrant for vector retrieval, and `postgres-test` on `127.0.0.1:55432` for tests.
SQLite files such as `data/novel.db` are no longer supported as a runtime database.

### Personal LAN deployment

1. Copy `.env.example` to `.env`.
2. Set `FORWIN_HTTP_BIND` to your server LAN IP, for example `192.168.1.10`.
3. Set MinIO credentials and LLM API keys.
4. Optionally set `FORWIN_HTTP_BASIC_USER` and `FORWIN_HTTP_BASIC_PASSWORD`.
5. Run `docker compose up -d --build`.

Keep `FORWIN_HTTP_BIND=127.0.0.1` for local-only access. LAN access requires an explicit server LAN IP; do not use `0.0.0.0` unless you mean to listen on every network interface. Basic Auth is lightweight single-user protection for a trusted LAN, not a multi-user permission system.

### Configuration

ForWin reads configuration through `Config.from_env()`. It loads the file
pointed to by `FORWIN_ENV_FILE`, or `.env` when unset, then overlays the real
process environment. Real environment variables always win over values from the
file.

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

## Testing

```bash
pytest
```

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
