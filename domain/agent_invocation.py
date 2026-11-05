"""An Agent invocation is subordinate to a real root claim, never a synthetic Task."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace

from domain.capability import CapabilityDependency, CapabilityVersionRecord
from domain.task_execution import TaskClaim


@dataclass(frozen=True)
class AgentInvocation:
    root: TaskClaim = field(repr=False)
    reference: CapabilityDependency
    artifact: CapabilityVersionRecord = field(repr=False)
    input: dict = field(repr=False)
    parent_node_id: str | None = None

    @property
    def task(self):
        return self.root.task

    @property
    def lease(self):
        return self.root.lease

    @property
    def budget(self):
        return self.root.budget

    def with_timeout(self, seconds: float) -> AgentInvocation:
        return replace(self, root=self.root.with_timeout(seconds))

    def limit_timeout(self, seconds: float) -> float:
        return self.root.limit_timeout(seconds)

    @classmethod
    def from_root(cls, claim: TaskClaim):
        return cls(root=claim, reference=CapabilityDependency(key=claim.task.capability_key, version=claim.task.version),
                   artifact=claim.artifact, input=deepcopy(claim.input))
