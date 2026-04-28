# ForWin Character Personality Skill Library v2

This repository stores reusable personality behavior mechanisms for fictional characters.

These skills are not author personas, writing-agent styles, clinical diagnoses, single character cards, or one-to-one conversions from MBTI, Enneagram, DISC, or any other model. A skill describes how a character type tends to perceive, decide, speak, move, attach, withdraw, fail under pressure, recover, grow, and deteriorate.

Runtime principle:

```text
Character card + canon + current plot state + relationship state
> active stress mode
> dominant trait
> secondary traits
> social mask
> prose style
```

Writer and reviewer prompts must receive compressed `active_personality_context`, not the full skill library.

## Structure

- `docs/`: rules and authoring policy.
- `catalog/`: global skill catalog and taxonomies.
- `schema/`: machine-readable contracts for skill metadata, loadouts, and active context.
- `templates/`: generic and skill-type-specific authoring templates.
- `skills/`: runnable `SKILL.md` files.
- `examples/`: loadout and prompt examples.
- `tests/`: human-readable consistency tests.

## Skill Types

- `trait`: baseline personality mechanism. Uses the full six-layer model.
- `social_mask`: outward presentation layer. It hides something but cannot erase dominant traits.
- `stress_mode`: temporary trigger-dependent deformation under pressure.
- `relationship_pattern`: trust, boundary, conflict, and repair logic.
- `archetype`: narrative function only. It cannot decide dialogue or behavior by itself.

## v2 Rules

- `reference_models` are explanatory anchors only.
- Behavior must come from trigger rules, relationship rules, expression rules, stress rules, and prompt compression.
- Every skill must include body language and prompt compression.
- Growth changes the expression of a mechanism; it does not delete the mechanism.
- Canon facts always outrank personality skills.
