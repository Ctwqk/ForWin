---
name: mask-cold-professional
chinese_name: 冷静专业面具
description: 通过克制、程序感和能力展示来降低暴露面与情绪读取。
forwin_scope: character_personality
category: character_personality_skill
skill_type: social_mask
version: "1.0"
status: draft
mapping_confidence: medium
mode: instruction_only
use_when:
  - 高权力距离组织
  - 谈判、审讯、汇报、危机处理、公众场合
  - 角色需要压住私情，以能力和流程出现
avoid_when:
  - 关系戏依赖高透明度和高温度
  - 角色设定本就热烈外放且不做遮掩
compatible_with:
  - trait-stoic-repressor
  - trait-suspicious-survivor
  - trait-duty-bound-commander
  - trait-ambitious-climber
tension_with:
  - mask-playful-fool
  - trait-social-butterfly
reference_model_policy:
  - Reference models are explanatory anchors only.
  - Social mask controls outward presentation only.
  - It must not erase the dominant trait.
reference_models:
  mbti_flavor: TJ
  enneagram: 1_3_5
  disc: C_DC
tags:
  - professional
  - restrained
  - emotional_armor
  - public_mask
---
# Skill: mask-cold-professional

## Core Function

This skill makes a fictional character appear calm, competent, formal, and emotionally unreadable in public or high-stakes contexts.

It is a mask, not the character's whole self. It hides exposure by turning attention toward competence, procedure, and role performance.

## Runtime Priority

Behavior rules, trigger rules, relationship rules, expression rules, and stress rules override reference model labels. This social mask controls outward presentation only.

## Mask Profile

```yaml
mask_profile:
  outward_image: 冷静、可靠、边界清楚、少废话
  hidden_function: 让别人先尊重其能力，再放弃追问其内心
  what_it_hides:
    - 焦虑
    - 脆弱
    - 私情
    - 野心
    - 依恋
  activation_contexts:
    - public_scene
    - superior_present
    - dangerous_negotiation
    - interrogation
    - formal_report
  crack_signals:
    - 语速更快
    - 用词更尖
    - 不必要地纠正细节
    - 对私人问题过度礼貌
    - 表情仍稳，但手部动作增加
  what_leaks_through:
    - 过度控制
    - 过度负责
    - 对重要之人的特殊例外
```

## Layer 3: Decision Mechanics

### Trigger Rules

| Trigger | Interpretation | Response | Cost | Possible Growth Response |
|---|---|---|---|---|
| `failure` | 专业可信度受损 | 加倍流程化和自我纠错 | 过度僵硬 | 承认失误并修复，而不是只恢复形象 |
| `intimacy_offer` | 私人边界被靠近 | 改谈工作、流程或事实 | 让对方感到被拒绝 | 明确说“我需要一点时间” |
| `public_humiliation` | 角色位置被压低 | 强化正式性与程序性 | 可能显得冷酷 | 只回应事实，不用面具压死真实需求 |
| `praise` | 自我暴露风险增加 | 轻描淡写或转移功劳 | 难以接受善意 | 简短接受，不立刻撤退 |
| `secret_exposed` | 内在被迫公开 | 收缩表情，重建叙事控制 | 容易二次伤人 | 承认部分真实，不必全靠程序防御 |

## Layer 4: Relationship Pattern

### With strangers

礼貌但冷，先以角色身份出现，而不是以私人自我出现。

### With allies

任务第一。帮助通过行动、流程、资源，而不是情绪安抚。

### With superiors

更正式、更克制，倾向给结论、方案和风险，不暴露恐惧。

### With subordinates

清楚、稳定、要求明确。私情少，但会用安排保护人。

### With loved ones

容易用解决问题代替示弱。私下仍可能保持过度正式，直到信任足够。

### With enemies

借规章、证据、专业可信度压制对方。

### With betrayers

维持程序化边界，减少私人情绪暴露，用证据和流程处理。

## Layer 5: Expression

### Dialogue Behavior

- 短句、精准、书面感、低情绪泄漏。
- 喜欢给结论、编号、流程。
- 被追问情绪时改谈事实。
- 不轻易用昵称或软称呼。

### Body Language

- 坐姿端正。
- 表情收束。
- 手部动作少。
- 被问到私人问题时整理袖口、文件或视线转向流程物。
- 面具裂开时不是大喊，而是更冷、更硬、更正式。

### Affection Style

私下可能通过替对方整理资源、解决问题、挡掉麻烦来表达。

### Anger Style

愤怒会被压成冷淡、挑错、程序化追责。

### Lie Style

用专业边界回避：“这与当前议题无关。”

### Silence Style

沉默是一种边界控制，用来阻止话题进入私人领域。

### Humor Style

低频，干，甚至没有。幽默不应成为该面具的主要工具。

## Layer 6: Stress and Arc

### Stress Triggers

- failure
- intimacy_offer
- public_humiliation
- secret_exposed
- superior_present

### Mild Pressure

更端正、更少闲聊、更依赖流程。

### Medium Pressure

机械化、挑错、拒绝帮助。

### Extreme Pressure

面具裂开成僵硬冷酷，或出现短瞬失态后迅速收回。

### Breakdown Signals

- 不必要地纠正细节
- 对私人问题过度礼貌
- 手部动作增加

### Recovery Conditions

- 恢复角色边界
- 允许有限真实表达
- 私人问题不被继续公开逼问

### Healthy Growth

- 能在不失专业的前提下暴露有限真实感受。

### Negative Arc

- 把所有关系都流程化，最终情感隔离。

### Relapse Trigger

- 私情被公开利用。
- 专业失误被当众放大。

## Scene Uses

- 初见误判
- 公私分裂
- 职业身份与私人情感冲突
- 面具裂开的瞬间
- 权力场中的低情绪表达

## Do Not

- Do not make professionalism equal inhumanity.
- Do not let the mask erase the dominant trait.
- Do not use this mask in every private scene unless character-specific override says so.
- Do not make all cold-professional characters speak identically.
- Do not infer behavior directly from reference model labels.

## Prompt Compression

```yaml
prompt_compression:
  one_line_summary: 以专业性做护甲，用程序感屏蔽情绪读取。
  outward_bias:
    - 冷静、正式、边界清楚
  hidden_bias:
    - 焦虑、脆弱或私情被流程盖住
  dialogue_bias:
    - 给结论、讲流程、避开私人问题
  body_language_bias:
    - 坐姿端正、手部动作少、被追问时整理文件或袖口
  crack_bias:
    - 压力越大，越礼貌、越硬、越纠正细节
```
