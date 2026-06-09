# ForWin Image Role Split Design

## Decision

Split the browser runtime from the default ForWin runtime with Dockerfile
targets, not separate repositories or separate dependency manifests.

The default final target remains the API/generation/MCP runtime and does not
install Chromium, Xvfb, or Playwright browser binaries. The optional
`publisher-browser` Compose service builds the explicit
`publisher-browser-runtime` target, which owns Chromium/Xvfb/browser-profile
runtime dependencies.

## Scope

This phase implements:

- A slim default `forwin-runtime` Dockerfile target.
- A browser-heavy `publisher-browser-runtime` Dockerfile target.
- Compose wiring so only `publisher-browser` uses the browser target.
- Tests that prevent browser packages from drifting back into the default
  runtime.

## Non-Goals

- Do not split the Python package dependency set yet.
- Do not remove publisher extension source from the API image; the API still
  serves downloadable extension packages.
- Do not split MCP into a separate image until a measured deploy/startup benefit
  justifies it.
- Do not alter production data-store placement.

## MCP Image Decision

MCP should reuse the default slim `forwin-runtime` for now. It needs the Python
package and API connectivity, but not Chromium/Xvfb. A dedicated MCP image would
add build and deployment surface without removing meaningful browser weight
after the default runtime is slimmed.

## Acceptance

- `docker-compose.yml` keeps API, generation worker, and MCP on `build: .`.
- `publisher-browser` uses `build.target: publisher-browser-runtime`.
- Dockerfile has `publisher-browser-runtime` and `forwin-runtime` targets.
- Chromium, Xvfb, Xauth, and `python -m playwright install --with-deps chromium`
  appear only in the browser target section.
- `docker compose config` is valid.
