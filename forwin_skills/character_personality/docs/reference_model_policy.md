# Reference Model Policy

`reference_models` are explanatory anchors only. They are not behavior engines.

Runtime priority:

```text
trigger_rules
> relationship_rules
> expression
> stress_and_arc
> do_not
> prompt_compression
> reference_models
```

Do not infer behavior directly from MBTI, Enneagram, DISC, Socionics, Big Five, HEXACO, attachment style, or archetype labels. If a reference model label conflicts with explicit behavior rules, ignore the label.

## Recommended Use

| Model | Useful Field | Boundary |
|---|---|---|
| Big Five / OCEAN | `trait_axes` | Baseline tendency only |
| HEXACO | honesty, humility, manipulation, status | Moral and power anchors only |
| Enneagram | `core_drive`, fear, shame, negative arc | Motivation flavor, not hard measurement |
| DISC | speech style, conflict style | External communication only |
| VIA / CliftonStrengths | healthy growth | Strength vocabulary only |
| MBTI / Socionics | information flavor | Never direct behavior control |
| Attachment | relationship dimensions | Trust, intimacy, boundary, repair |
| Jung / Archetype | `archetype_specific` | Narrative function only |

## Required Policy Text

```yaml
reference_model_policy:
  - Reference models are explanatory anchors only.
  - Runtime generation must follow behavior rules first.
  - Do not infer new behavior from MBTI, Enneagram, DISC, Socionics, or archetype labels directly.
  - If a reference model conflicts with trigger_rules or relationship_rules, ignore the reference model.
  - Do not use clinical diagnosis labels as primary skill names.
```
