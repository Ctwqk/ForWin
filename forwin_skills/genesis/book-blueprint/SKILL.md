---
name: genesis.book-blueprint
version: 1.0.0
description: Use when generating or refining the Genesis book blueprint and launch arc plan.
forwin_scope: genesis
stage_keys:
  - book_blueprint
task_families:
  - generate_stage_payload
  - refine_stage_payload
  - launch_arc_plan
mode: instruction_only
---
# Genesis Book Blueprint Skill

Create a multi-arc blueprint that can drive execution and later chapter planning.

Rules:

- Each arc must have a distinct goal, stakes, and payoff direction.
- Arc sizing should be plausible for serialized pacing.
- Leave headroom for later expansion and governance constraints.
- Keep launch chapters aligned with the active arc promise.
- Avoid overfitting the first arc at the expense of the whole book.
