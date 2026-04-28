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
uvicorn forwin.api:app --reload --host 127.0.0.1 --port 8899
```

### Docker workflow

```bash
cp .env.example .env
docker compose up --build
```

By default the main web API is bound to `127.0.0.1:8899`, so it is reachable only from the server itself.

### Personal LAN deployment

1. Copy `.env.example` to `.env`.
2. Set `FORWIN_HTTP_BIND` to your server LAN IP, for example `192.168.1.10`.
3. Set MinIO credentials and LLM API keys.
4. Optionally set `FORWIN_HTTP_BASIC_USER` and `FORWIN_HTTP_BASIC_PASSWORD`.
5. Run `docker compose up -d --build`.

Keep `FORWIN_HTTP_BIND=127.0.0.1` for local-only access. LAN access requires an explicit server LAN IP; do not use `0.0.0.0` unless you mean to listen on every network interface. Basic Auth is lightweight single-user protection for a trusted LAN, not a multi-user permission system.

## Testing

```bash
pytest
```

## Notes

- The project is optimized for Chinese-language long-form fiction workflows, but the codebase is broadly interesting as an example of stateful AI orchestration with publishing controls.
- The `Design-docs/` directory is worth reading if you want the higher-level product and architecture context behind the modules.
