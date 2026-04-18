# Review Fix Log

Date: 2026-04-15

## Fixed

1. Runtime model overrides now respect saved runtime/profile values when `/api/generate` omits `base_url` or `model`.
2. Existing projects can no longer start a second generation/continue task while another generation task is still active.
3. Project deletion and bulk deletion now refuse projects with active generation/upload work instead of deleting under in-flight operations.
4. Project automation updates now support `publish_bindings`, so dual-platform bindings can be edited after project creation.
5. FastAPI lifespan now reuses preconfigured runtime objects in tests, which makes `TestClient`-based regression cases hermetic instead of reinitializing over injected state.

## Files

- `forwin/api.py`
- `forwin/api_schemas.py`
- `tests/test_phase05_regressions.py`
- `tests/test_project_publish_bindings.py`
- `tests/test_project_operation_guards.py`

## Validation

- `python3 -m pytest -q tests/test_project_operation_guards.py`
- `python3 -m pytest -q tests/test_project_publish_bindings.py`
- `python3 -m pytest -q tests/test_phase05_regressions.py`
- `python3 -m pytest -q`
- `npm test` in `browser_extension/forwin-publisher`

## Result

- `tests/test_phase05_regressions.py`: 110 passed
- Python test suite: 144 passed
- Browser extension tests: 26 passed
