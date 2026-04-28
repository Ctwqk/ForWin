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
uvicorn forwin.api:app --reload --host 0.0.0.0 --port 8899
```

### Docker workflow

```bash
docker compose up --build
```

By default the main web API is exposed on `http://localhost:8899`.

The application now requires PostgreSQL for its main persistence store. SQLite
files such as `data/novel.db` are no longer supported as a runtime database.

## Testing

```bash
export FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@localhost:5432/forwin_test
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
- The `Design-docs/` directory is worth reading if you want the higher-level product and architecture context behind the modules.
