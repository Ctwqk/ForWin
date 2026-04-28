---
name: stress-paranoid-controller
chinese_name: 偏执控制模式
description: 在高不确定和高背叛感下，以控制信息、人和流程来降低恐慌的压力反应。
forwin_scope: character_personality
category: character_personality_skill
skill_type: stress_mode
version: "1.0"
status: draft
mapping_confidence: medium
mode: instruction_only
use_when:
  - 角色遭遇失控、信息断裂、背叛疑云、资源稀缺或重要之人受威胁
  - 需要把原本的谨慎、保护、责任、野心放大成控制欲
avoid_when:
  - 没有明确压力触发却长期常驻
  - 想把它当成角色常态人格
compatible_with:
  - trait-suspicious-survivor
  - trait-loyal-protector
  - trait-duty-bound-commander
  - trait-ambitious-climber
tension_with:
  - rel-avoidant-loner
  - trait-social-butterfly
  - mask-playful-fool
reference_model_policy:
  - Reference models are explanatory anchors only.
  - Stress mode is temporary and trigger-dependent.
  - It must not replace the character's dominant trait.
reference_models:
  enneagram: 6_8_stress
  disc: C_D
  attachment: fearful
  hexaco: low_agreeableness high_emotionality
tags:
  - stress_response
  - control
  - paranoia
  - information_lockdown
---
# Skill: stress-paranoid-controller

## Core Function

This skill makes a fictional character under pressure attempt to reduce fear by controlling information, movement, decisions, and people.

This is not a healthy leadership style. It is a stress response with relational costs.

## Runtime Priority

Behavior rules, trigger rules, relationship rules, expression rules, and stress rules override reference model labels. This stress mode is temporary and trigger-dependent.

## Stress Mode Specifics

```yaml
stress_mode_specific:
  activation_threshold: medium_high
  trigger_families:
    - information_gap
    - betrayal_suspected
    - loss_of_control
    - resource_scarcity
    - loved_one_threatened
    - subordinate_disobeys
  action_bias: 盘问、锁边界、切权限、要报告、收流程
  relational_cost: 盟友感到不被信任，爱人感到被监控，下属感到窒息
  recovery_key: 透明信息、可验证承诺、阶段性移交控制权
```

## Layer 3: Decision Mechanics

### Trigger Rules

| Trigger | Interpretation | Response | Cost | Possible Growth Response |
|---|---|---|---|---|
| `information_gap` | 有人可能在隐瞒关键事实 | 先补监控，再追责 | 破坏信任气氛 | 要求明确报告节点，而非全面监控 |
| `betrayal_suspected` | 可信圈被污染 | 迅速缩小权限和知情范围 | 误伤清白盟友 | 分层核验而不是一刀切 |
| `subordinate_disobeys` | 控制链断裂 | 把自由改成流程和审批 | 打击主动性 | 追责具体后果，不否定所有自主权 |
| `resource_scarcity` | 不控制就会崩盘 | 囤积、排位、优先级硬化 | 变得冷酷 | 公开规则和理由 |
| `authority_pressure` | 上方可能压垮自己 | 表面服从，背后建立平行控制链 | 增加政治风险 | 争取正式授权或公开边界 |
| `loved_one_threatened` | 保护对象可能失去 | 限制其行动、隐瞒危险 | 亲密关系窒息 | 共同制定风险方案 |

## Layer 4: Relationship Pattern

### With strangers

默认不可信，先设门槛、权限和观察期。

### With allies

不再平等协作，而是开始微观管理。容易把“我在保护你”变成“你必须按我说的做”。

### With superiors

表面配合，暗中建立备用路径和证据链。

### With subordinates

要求汇报、限制权限、强化流程。

### With loved ones

保护升级成限制与监视。会把对方的自主视为风险。

### With enemies

假设最坏意图，抢先卡位，减少所有可被利用的漏洞。

### With betrayers

几乎不再允许自然修复，只接受长期可审计行动。

## Layer 5: Expression

### Dialogue Behavior

- 盘问式、短促、结论快于证据。
- 喜欢问“谁知道？”“什么时候？”“为什么没报？”
- 命令多于解释。
- 不接受模糊回答。

### Body Language

- 站位更居中或更靠近门口。
- 手指敲桌、翻看记录、反复确认物件位置。
- 眼神扫描变频繁。
- 听到不完整回答时身体前倾。
- 对亲近之人的接近可能变成拦截。

### Affection Style

“为了你好，所以我替你决定。”这是该 stress_mode 最危险的亲密表达。

### Anger Style

怒气被转化成命令、权限切割、流程收紧。

### Lie Style

会用“暂时不告诉你是为了安全”来合理化隐瞒。

### Silence Style

沉默通常是在重新排序风险和权限。

### Humor Style

几乎消失。

## Layer 6: Stress and Arc

### Stress Triggers

- information_gap
- betrayal_suspected
- loss_of_control
- resource_scarcity
- loved_one_threatened
- subordinate_disobeys

### Mild Pressure

清单化，反复确认，要求更多信息。

### Medium Pressure

监控增加，权限收紧，开始测试忠诚。

### Extreme Pressure

威权化、关系窒息、误伤盟友，甚至把自己人也当成变量处理。

### Breakdown Signals

- 反复问同一问题
- 不允许别人解释完
- 突然重写所有流程
- 把“不知道”当成罪
- 用安全理由取消他人自主

### Recovery Conditions

- 信息透明
- 可验证承诺
- 风险被拆成阶段
- 角色看到别人也能承担责任
- 有人明确指出控制造成的关系成本

### Healthy Growth

- 用流程防错替代对人防错。
- 把控制权分阶段交回。
- 允许别人知道风险并共同承担。

### Negative Arc

- 安全感完全建立在控制别人上。
- 保护关系变成监控关系。
- 最终所有人都开始对他隐瞒，因为说真话成本太高。

### Relapse Trigger

- 刚恢复信任后再次出现信息缺口。
- 重要之人擅自行动。
- 下属善意隐瞒导致严重后果。

## Scene Uses

- 信任危机
- 指挥体系崩坏
- 保护与控制的冲突
- 盟友窒息感
- 危机后关系修复

## Do Not

- Do not write this as the correct leadership method.
- Do not skip relational cost.
- Do not keep it always on without trigger.
- Do not let “for your safety” automatically justify control.
- Do not infer behavior directly from reference model labels.

## Prompt Compression

```yaml
prompt_compression:
  one_line_summary: 一失控就想收权，一不确定就想盘问。
  trigger_bias:
    - 信息缺口、背叛疑云、失控感会激活该模式
  decision_bias:
    - 先控制流程和权限，再处理情绪
  dialogue_bias:
    - 盘问、命令、要求报告，不接受模糊回答
  body_language_bias:
    - 扫描环境、确认记录、前倾逼问、拦截动作增多
  relationship_bias:
    - 盟友感到被不信任，爱人感到被监控，下属感到窒息
  recovery_bias:
    - 透明信息、可验证承诺、分阶段交还控制权
```
