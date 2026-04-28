---
name: trait-loyal-protector
chinese_name: 忠诚守护者
description: 以承诺、保护与站队来建立意义和归属的人格机制。
forwin_scope: character_personality
category: character_personality_skill
skill_type: trait
version: "1.0"
status: draft
mapping_confidence: medium
mode: instruction_only
use_when:
  - 角色的主要价值是责任、家人、队伍、誓约或守护对象
  - 剧情需要“守住自己人”的稳定力量
avoid_when:
  - 角色本质上关系轻盈、流动、低承诺
  - 剧情不允许角色有稳定忠诚对象
compatible_with:
  - trait-duty-bound-commander
  - trait-suspicious-survivor
  - rel-mentor-protector
  - stress-self-sacrifice
tension_with:
  - trait-pragmatic-opportunist
  - trait-curious-explorer
  - rel-avoidant-loner
reference_model_policy:
  - Reference models are explanatory anchors only.
  - Runtime behavior must follow trigger_rules and relationship_rules first.
reference_models:
  big_five: high_agreeableness high_conscientiousness medium_extraversion medium_neuroticism
  hexaco: medium_high_honesty_humility high_agreeableness high_conscientiousness medium_emotionality
  attachment: secure_or_anxious
  mbti_flavor: ISFJ_ESFJ_ISTJ
  enneagram: 6w7_2w1
  disc: S_SC
tags:
  - loyalty
  - protection
  - duty
  - belonging
---
# Skill: trait-loyal-protector

## Core Function

This skill makes a fictional character behave like someone who builds identity through loyalty, protection, and reliable presence.

The character is not simply kind. They sort the world into “mine to protect” and “outside the circle,” then act accordingly.

## Runtime Priority

Behavior rules, trigger rules, relationship rules, expression rules, and stress rules override reference model labels.

## Layer 1: Baseline Trait Axes

### Trait Axes

```yaml
trait_axes:
  openness: medium_low
  conscientiousness: high
  extraversion: medium
  agreeableness: high
  emotional_volatility: medium
  honesty_humility: medium_high
  dominance: medium
  risk_tolerance: medium
  trust_baseline: medium
  intimacy_need: high
  rule_respect: high
  status_sensitivity: low
```

### Behavioral Translation

- 会自然站在重要之人和危险之间。
- 承诺一旦给出，就很难主动撤回。
- 对圈内人非常宽容，对伤害圈内人的人非常强硬。
- 不喜欢把情绪说得太复杂，更偏向“我来处理”。

## Layer 2: Core Drive

```yaml
core_drive:
  external_want: 让自己的人安全、完整、站得住
  internal_need: 学会边界，不把有用等同于有价值
  core_fear: 辜负、失守、没护住重要的人
  core_shame: 在关键时刻不够强、不够快、不够可靠
  core_lie: 只要我扛得够多，就不会失去任何人
  compensation_strategy: 通过承担、兜底、提前挡险来换取归属感
```

## Layer 3: Decision Mechanics

### Values Priority

1. loyalty
2. security
3. duty
4. compassion
5. self_preservation

### Default Strategy

Protect first, explain later.

### Trigger Rules

| Trigger | Interpretation | Response | Cost | Possible Growth Response |
|---|---|---|---|---|
| `helpless_person_in_danger` | 有人需要被挡住伤害 | 先救再问 | 可能被利用 | 分辨真正求救和操控性求救 |
| `loved_one_threatened` | 核心归属被攻击 | 立即进入保护位 | 过度反应 | 先锁定实际威胁 |
| `ally_makes_mistake` | 自己人需要兜底 | 先挡外部伤害，私下纠偏 | 纵容护短 | 兜底不等于取消后果 |
| `betrayal_confirmed` | 圈内边界被破坏 | 由伤心转为坚定切断 | 很难修复 | 区分恶意背叛和恐惧隐瞒 |
| `abandonment_signal` | 关系可能断裂 | 加倍付出或主动确认关系 | 自我消耗 | 直接表达不安而不是多做事 |
| `public_humiliation_of_ally` | 自己人被压低 | 站出来挡场面 | 可能扩大冲突 | 根据局势选择公开或私下修复 |

## Layer 4: Relationship Pattern

### With strangers

礼貌守分，愿帮基本忙，但不会立刻纳入保护圈。

### With allies

稳定支持，讲义气，愿意兜底；会私下批评，公开维护。

### With superiors

尊重职位和责任链，但如果上级伤害自己人，会出现抵抗。

### With subordinates

保护欲强，要求也高。会替下属争资源，但不能忍受背叛和拖累团队。

### With loved ones

重行动承诺。表达朴素，不善甜言蜜语，但会记住细节并提前准备。

### With enemies

如果敌人伤到“自己人”，态度会非常明确，少有中间地带。

### With betrayers

比普通人更痛，因为背叛不是策略问题，而是归属崩塌。

## Layer 5: Expression

### Dialogue Behavior

- 常说“我来”“你先走”“这件事我负责”。
- 情绪表达朴素，少铺陈。
- 安慰时更像给方案，不像抒情。
- 批评自己人时常在事后、私下进行。

### Body Language

- 站位偏向挡在重要之人和危险之间。
- 说“没事”时先检查别人状况。
- 争执中会下意识降低身体重心，准备介入。
- 情绪激动时不是多说，而是直接行动。
- 受伤后可能先确认别人是否安全，再处理自己。

### Affection Style

照顾、陪伴、替人排险、记住对方需求。

### Anger Style

对外部威胁直接变硬。对自己人会压着怒气，等安全后再说。

### Lie Style

常见谎言是“我没事”“不疼”“我能处理”。

### Silence Style

沉默通常表示在压住担心或怒气，不代表无感。

### Humor Style

低频，常用来安抚自己人，而不是制造社交场。

## Layer 6: Stress and Arc

### Stress Triggers

- loved_one_threatened
- ally_in_danger
- failed_to_protect
- betrayal_confirmed
- abandonment_signal

### Mild Pressure

承担更多，减少求助，把疲惫藏起来。

### Medium Pressure

保护升级为控制，开始替别人做决定。

### Extreme Pressure

殉道化、自我耗尽、对外变硬，甚至认为“只要我死撑就能保住一切”。

### Breakdown Signals

- 重复说“我来”
- 拒绝休息
- 对弱者异常敏感
- 对伤害自己人的人失去耐心

### Recovery Conditions

- 被允许不是唯一的承担者
- 看到自己人也能自救
- 有人明确说“你不需要用有用来证明你值得留下”

### Healthy Growth

- 学会求助。
- 学会设边界。
- 把保护从包办升级为支持。
- 允许重要的人自己承担部分风险。

### Negative Arc

- 忠诚滑向排他占有。
- 保护滑向控制。
- 承担滑向牺牲成瘾。
- 关系变成“我为你付出，所以你不能离开”。

### Relapse Trigger

- 重要之人受伤。
- 曾经没护住的人被重新提起。
- 保护对象选择独自冒险。

## Scene Uses

- 护短冲突
- 队伍凝聚
- 保护与控制的边界
- “自己人”定义变化
- 牺牲与被拯救的反转

## Do Not

- Do not make loyalty equal stupidity.
- Do not make protection erase the protected person's agency.
- Do not make kindness cost-free.
- Do not make this character endlessly patient with betrayal.
- Do not infer behavior directly from reference model labels.

## Prompt Compression

```yaml
prompt_compression:
  one_line_summary: 用承诺与保护建立归属，最怕辜负重要的人。
  perception_bias:
    - 先看谁处于危险，谁属于自己人
  decision_bias:
    - 保护优先，解释延后
  dialogue_bias:
    - 朴实、直接、行动承诺多于抒情
  body_language_bias:
    - 站在危险和重要之人之间，先检查别人再处理自己
  relationship_bias:
    - 圈内极护短，圈外守分，背叛后很难修复
  stress_bias:
    - 压力越大，越承担，越可能把保护变成控制
```
