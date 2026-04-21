---
name: genesis.bootstrap
version: 1.0.0
description: Use when generating or refining the Genesis bootstrap stage.
forwin_scope: genesis
stage_keys:
  - bootstrap
task_families:
  - generate_stage_payload
  - refine_stage_payload
mode: instruction_only
---
# Genesis Bootstrap Skill

Prepare execution defaults from the Genesis root pack.

Rules:

- Reflect root readiness honestly.
- Keep governance defaults explicit.
- Do not silently mark the book ready if upstream stages remain unstable.
- Prefer conservative start policies over speculative ones.
