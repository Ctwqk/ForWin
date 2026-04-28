---
name: trait-ambitious-climber
chinese_name: 野心攀登者
description: 以成就、上升与被看见来确认自我价值的人格机制。
forwin_scope: character_personality
category: character_personality_skill
skill_type: trait
version: "1.0"
status: draft
mapping_confidence: medium
mode: instruction_only
use_when:
  - 角色生命力来自目标、排名、竞争、升迁或夺位
  - 剧情需要向上爬、证明自己、争夺资源或跨越阶层
avoid_when:
  - 角色核心价值是慢生活、低比较、去竞争
  - 作品需要该角色长期保持低欲望、低成就焦虑
compatible_with:
  - trait-duty-bound-commander
  - trait-shame-driven-overachiever
  - mask-arrogant-elite
  - rel-rival-respect
tension_with:
  - trait-sincere-innocent
  - trait-quiet-observer
  - rel-dependent-pleaser
reference_model_policy:
  - Reference models are explanatory anchors only.
  - Runtime behavior must follow trigger_rules and relationship_rules first.
reference_models:
  big_five: high_conscientiousness high_extraversion medium_low_agreeableness
  hexaco: low_honesty_humility high_conscientiousness medium_agreeableness medium_extraversion
  attachment: anxious_or_earned_secure
  mbti_flavor: ENTJ_ESTJ_ENFJ
  enneagram: 3w4_8w7
  disc: D_Di
tags:
  - ambition
  - status
  - achievement
  - rivalry
---
# Skill: trait-ambitious-climber

## Core Function

This skill makes a fictional character behave like someone who uses achievement, upward movement, and recognition to confirm self-worth.

The character is not simply greedy. They are driven by a need to prove they are not ordinary, disposable, or beneath notice.

## Runtime Priority

Behavior rules, trigger rules, relationship rules, expression rules, and stress rules override reference model labels.

## Layer 1: Baseline Trait Axes

### Trait Axes

```yaml
trait_axes:
  openness: medium
  conscientiousness: high
  extraversion: high
  agreeableness: medium_low
  emotional_volatility: medium
  honesty_humility: low
  dominance: high
  risk_tolerance: medium_high
  trust_baseline: medium_low
  intimacy_need: medium
  rule_respect: conditional
  status_sensitivity: very_high
```

### Behavioral Translation

- 会快速判断一个场合的权力阶梯、资源位置和上升通道。
- 讨厌被低估，尤其讨厌被当成“可替代”。
- 面对机会时会主动争取，不太等别人分配。
- 会把失败当成身份威胁，而不只是结果不佳。

## Layer 2: Core Drive

```yaml
core_drive:
  external_want: 进入更高层级并被公认为强者
  internal_need: 把价值从排名和掌声里拆出来
  core_fear: 平庸、被看低、努力却不被承认
  core_shame: 自己其实不够特别，不配站在高处
  core_lie: 只有赢、只有往上，才配被尊重和爱
  compensation_strategy: 用成果、位置、名声、胜利覆盖羞耻
```

## Layer 3: Decision Mechanics

### Values Priority

1. achievement
2. power
3. dignity
4. recognition
5. intimacy

### Default Strategy

Turn every situation into a ladder, contest, or leverage point.

### Trigger Rules

| Trigger | Interpretation | Response | Cost | Possible Growth Response |
|---|---|---|---|---|
| `rival_comparison` | 地位正在被衡量 | 迅速进入竞争状态 | 难以放松关系 | 承认对手强，不必立刻压过 |
| `public_humiliation` | 自身价值被公开压低 | 立刻修复形象或寻找翻盘窗口 | 容易过度报复 | 先评估实质损害再反应 |
| `failure` | 我可能不够好 | 强迫性复盘，加倍投入 | 消耗自己和身边人 | 区分失败和自我价值 |
| `unexpected_success` | 上升通道打开 | 扩张目标，抬高标准 | 永远不满足 | 庆祝阶段性成果 |
| `superior_is_incompetent` | 上位者不配其位 | 失去敬意，绕过或夺权 | 破坏秩序 | 判断是否先修复系统 |
| `low_status_treatment` | 被放在低位 | 用表现、话术或资源反击 | 显得功利 | 接受短期低位，不等于认同低价值 |

## Layer 4: Relationship Pattern

### With strangers

先判断对方资源、位置、可合作性和可能阻力。

### With allies

愿合作，但默认讲效率、交换、目标一致。欣赏强者，不耐烦拖累者。

### With superiors

尊重有能力的上位者。若上位者无能，会迅速产生取代或绕过的念头。

### With subordinates

能给机会，也要求结果。容易把人放进绩效框架里评估。

### With loved ones

希望对方理解其野心。常把爱表达成“我会变强，让你不后悔站在我这边”。

### With enemies

重视高质量对手。比起随机碾压，更喜欢有观众、有规则、有意义地赢。

### With betrayers

会把背叛视为对自己判断力和地位的双重羞辱。

## Layer 5: Expression

### Dialogue Behavior

- 简洁、目标导向，像在推进议程。
- 常用“结果”“位置”“机会”“代价”“凭什么”。
- 赞美别人时多赞能力和成绩。
- 道歉不易，但会用资源补偿。

### Body Language

- 在场合中自然靠近权力中心或视野高位。
- 被质疑时站姿更直，眼神更稳定。
- 失败后会更快整理仪表，试图恢复可控形象。
- 遇到强对手时注意力明显集中，语速变稳。
- 被轻视时微表情会先冷一下，然后进入表演状态。

### Affection Style

把资源、机会、位置、未来规划带给对方，认为这是最有重量的表达。

### Anger Style

被低估时最容易动怒。愤怒常转化为更强的表现欲和反击计划。

### Lie Style

更常做形象管理、包装动机、隐藏失败，而不是无意义撒谎。

### Silence Style

沉默多出现在失败后复盘，或等待对方先暴露底牌。

### Humor Style

带竞争感、锋芒感，可能夹带比较。

## Layer 6: Stress and Arc

### Stress Triggers

- public_humiliation
- rival_outperforms
- failure
- low_status_treatment
- ignored_contribution

### Mild Pressure

更高效、更挑剔、更少耐心。

### Medium Pressure

把关系工具化，开始只看绩效和可用性。

### Extreme Pressure

冷酷夺位、甩锅、形象管理优先，甚至用人代替信任人。

### Breakdown Signals

- 反复提结果
- 无法听进去安慰
- 把所有谈话拉回胜负
- 对“平凡也可以”这类话反感

### Recovery Conditions

- 被承认真实努力，而不是只看结果
- 有人见过其失败仍不离开
- 目标被拆成可承受阶段

### Healthy Growth

- 仍然追求上升，但不再用排名证明自身价值。
- 能区分战略合作和把人当台阶。
- 能承认失败带来的羞耻，而不是立刻用下一场胜利覆盖。

### Negative Arc

- 卓越滑向功利。
- 野心滑向工具化他人。
- 自尊滑向地位成瘾。
- 爱与认可全部变成可量化胜负。

### Relapse Trigger

- 被曾经看不起自己的人再次评价。
- 重要胜利被夺走或被无视。
- 爱人或盟友拿自己和竞争者比较。

## Scene Uses

- 阶层上升线
- 宿敌竞争线
- 权力夺取线
- 成就与亲密冲突
- 胜利后空虚感

## Do Not

- Do not make ambition automatically evil.
- Do not make competence cost-free.
- Do not make this character unable to love.
- Do not flatten all dialogue into business language.
- Do not infer behavior directly from reference model labels.

## Prompt Compression

```yaml
prompt_compression:
  one_line_summary: 用上升与胜出确认自我价值，最怕平庸和被看低。
  perception_bias:
    - 先看权力阶梯、资源位置、谁在评价谁
  decision_bias:
    - 把局面转化为机会、较量或杠杆
  dialogue_bias:
    - 结论导向，重结果、代价、位置和证明
  body_language_bias:
    - 靠近权力中心，被质疑时更端正，失败后整理形象
  relationship_bias:
    - 欣赏强者，容易绩效化关系，爱里也带未来规划
  stress_bias:
    - 压力越大，越工具化关系，越需要赢回自我价值
```
