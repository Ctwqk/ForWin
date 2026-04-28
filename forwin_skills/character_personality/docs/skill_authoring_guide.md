# Skill Authoring Guide v2

Write skills as reusable behavior mechanisms, not research essays.

## Allowed Content

A reusable personality skill may describe:

- typical perception filters
- trigger rules
- value priority
- relationship behavior by target
- dialogue and expression
- body language and silence
- lie and affection style
- pressure deformation
- recovery and growth
- negative arc
- prompt compression

## Forbidden Runtime Content

Do not put these in reusable skills:

```yaml
character_id: forbidden
character_name: forbidden
current_goal: forbidden
canon_state: forbidden
scene_context: forbidden
current_relationship_score: forbidden
current_relationship_state: forbidden
current_arc_position: forbidden
current_chapter_goal: forbidden
active_personality_context: forbidden
```

Those belong to character cards, canon state, relationship state, scene context, or loaders.

## Type Boundaries

- `trait`: full six-layer mechanism.
- `social_mask`: outward presentation, what it hides, when it cracks.
- `stress_mode`: temporary trigger-dependent deformation with relational cost and recovery.
- `relationship_pattern`: trust, boundary, dependency, jealousy, conflict, reconciliation.
- `archetype`: narrative function only.

## Required Runtime Sections

Every `SKILL.md` should include:

- `Runtime Priority`
- `Layer 3: Decision Mechanics`
- `Layer 4: Relationship Pattern`
- `Layer 5: Expression`
- `Layer 6: Stress and Arc`
- `Do Not`
- `Prompt Compression`

`Body Language` and `Prompt Compression` are required because they are directly useful to writer prompts and reviewer checks.
