---
name: genesis.world_seed
version: 1.0.0
description: Use when generating or refining the Genesis world seed stage.
forwin_scope: genesis
stage_keys:
  - world_seed
task_families:
  - generate_stage_payload
  - refine_stage_payload
mode: instruction_only
---
# Genesis World Seed Skill

Expand the locked Genesis blueprint into the state of the world at the opening moment.

Rules:

- Give every story-engine character and faction exactly one actor entry; never invent entities or locations.
- Give every major actor one to three agendas with concrete means and constraints, so the world can move without the protagonist.
- Place characters at map nodes that fit their role, and give them travel modes the map can support.
- Every hidden or secret fact needs at least one knower, or is explicitly marked as known by nobody.
- Take duration units only from the book's own calendar units and lexicon time aliases.
- Seed initial knowledge through the seeded channel; misbeliefs and suspicions are allowed.
- Keep clocks to deadlines that already exist at the opening, and reveal candidates as suggestions, not hard bindings to arcs.
