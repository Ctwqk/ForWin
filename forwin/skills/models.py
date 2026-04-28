from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillCapability:
    mode: str = "instruction_only"
    instruction_only: bool = True


@dataclass(frozen=True)
class SkillManifest:
    name: str
    version: str
    description: str
    forwin_scope: str
    stage_keys: tuple[str, ...] = ()
    task_families: tuple[str, ...] = ()
    mode: str = "instruction_only"
    body: str = ""
    path: str = ""
    skill_hash: str = ""
    group: str = ""
    metadata: dict[str, object] = field(default_factory=dict)
    capability: SkillCapability = field(default_factory=SkillCapability)


@dataclass(frozen=True)
class SkillSelection:
    manifest: SkillManifest
    activation_reason: str


@dataclass(frozen=True)
class SkillLayer:
    content: str
    skill_id: str
    skill_version: str
    skill_hash: str
    path: str
    activation_reason: str
    mode: str
    role: str = "system"
    kind: str = "skill"

    def message_payload(self) -> dict[str, str]:
        return {
            "role": self.role,
            "content": self.content,
        }

    def trace_payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "role": self.role,
            "content": self.content,
            "skill_id": self.skill_id,
            "skill_version": self.skill_version,
            "skill_hash": self.skill_hash,
            "path": self.path,
            "activation_reason": self.activation_reason,
            "mode": self.mode,
        }
