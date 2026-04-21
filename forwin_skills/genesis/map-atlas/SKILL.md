---
name: genesis.map-atlas
version: 1.0.0
description: Use when generating or refining the Genesis map stage.
forwin_scope: genesis
stage_keys:
  - map
task_families:
  - generate_stage_payload
  - refine_stage_payload
mode: instruction_only
---
# Genesis Map Atlas Skill

Build a map layer that can anchor long-running location logic.

Rules:

- Keep topology readable and reusable.
- Make travel cost and regional separation explicit.
- Leave room for runtime regional drafts and SubWorld additions.
- Keep region and node naming consistent with culture profiles.
- Do not silently override world-bible constraints.
