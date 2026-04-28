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
uvicorn forwin.api:app --reload --host 0.0.0.0 --port 8899
```

### Docker workflow

```bash
docker compose up --build
```

By default the main web API is exposed on `http://localhost:8899`.

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

## Testing

```bash
pytest
```

## Notes

- The project is optimized for Chinese-language long-form fiction workflows, but the codebase is broadly interesting as an example of stateful AI orchestration with publishing controls.
- The `Design-docs/` directory is worth reading if you want the higher-level product and architecture context behind the modules.
