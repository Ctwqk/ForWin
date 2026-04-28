# Runtime Loading v2

Scene writing must not load the full personality library. It should load only referenced skills and compress them into `active_personality_context`.

## Loading Steps

1. Read character card.
2. Read current canon state.
3. Read current scene goal.
4. Read current relationship state.
5. Load dominant personality skill.
6. Load secondary skills above the configured threshold.
7. Load social masks whose `active_when` matches scene flags.
8. Load stress modes whose triggers match current pressure triggers.
9. Load relationship patterns whose target matches active relationship targets.
10. Merge into `active_personality_context`.

## Priority

```text
canon facts
> current plot state
> scene goal
> relationship state
> active stress mode
> dominant personality skill
> secondary personality skill
> social mask
> prose style
```

## Runtime Rule

Use `prompt_compression` from each skill whenever available. Reference model labels are never runtime behavior sources.

## Active Context Shape

```yaml
active_personality_context:
  character_id: char_example
  active_skills:
    dominant:
      - trait-suspicious-survivor
    secondary:
      - trait-loyal-protector
    social_mask:
      - mask-cold-professional
    stress_mode:
      - stress-paranoid-controller
  current_behavior_bias:
    perception:
      - 先看谁隐瞒、谁受益、哪里能撤
    decision:
      - 先保退路，再承诺
    dialogue:
      - 短句、反问、要求证据
    body_language:
      - 扫出口，不背对陌生人
    relationship_behavior:
      - 对陌生人低披露，对盟友条件信任
    stress_behavior:
      - 信息缺口会激活控制信息流
  constraints:
    - Do not override canon.
    - Do not make the character omniscient.
    - Do not flatten the character into one repeated behavior.
    - Do not infer behavior from model labels.
```
