from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.orm import Session

from forwin.models.project import Project
from forwin.runtime.policy import RuntimePolicy


class ProjectPolicyMissing(RuntimeError):
    pass


class ProjectPolicyVersionConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectPolicyRecord:
    policy: RuntimePolicy
    version: int


class ProjectPolicyStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load(self, project: Project) -> ProjectPolicyRecord:
        if not project.runtime_policy_json or project.runtime_policy_version < 1:
            raise ProjectPolicyMissing(project.id)
        return ProjectPolicyRecord(
            policy=RuntimePolicy.model_validate_json(project.runtime_policy_json),
            version=int(project.runtime_policy_version),
        )

    def initialize(
        self, project: Project, policy: RuntimePolicy
    ) -> ProjectPolicyRecord:
        self.session.add(project)
        self.session.flush()
        return self._write(project, policy, expected_version=0, next_version=1)

    def save(
        self,
        project: Project,
        policy: RuntimePolicy,
        *,
        expected_version: int,
    ) -> ProjectPolicyRecord:
        return self._write(
            project,
            policy,
            expected_version=int(expected_version),
            next_version=int(expected_version) + 1,
        )

    def _write(
        self,
        project: Project,
        policy: RuntimePolicy,
        *,
        expected_version: int,
        next_version: int,
    ) -> ProjectPolicyRecord:
        payload = policy.model_dump_json()
        result = self.session.execute(
            update(Project)
            .where(
                Project.id == project.id,
                Project.runtime_policy_version == expected_version,
            )
            .values(
                runtime_policy_json=payload,
                runtime_policy_version=next_version,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise ProjectPolicyVersionConflict(project.id)
        project.runtime_policy_json = payload
        project.runtime_policy_version = next_version
        self.session.flush()
        return ProjectPolicyRecord(policy=policy, version=next_version)
