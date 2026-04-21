---
name: reviewer.repair-plan
version: 1.0.0
description: Use when producing repair guidance after a failed or risky chapter review.
forwin_scope: reviewer
stage_keys:
  - chapter_review
task_families:
  - review_chapter
mode: instruction_only
---
# Reviewer Repair Plan Skill

Translate review findings into concrete repair guidance.

Rules:

- State what must be fixed first.
- State what must be preserved during repair.
- Prefer scene-level repair when possible.
- Do not override hard gates or governance truth.
