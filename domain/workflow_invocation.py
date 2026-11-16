"""A nested Workflow keeps the root Task's authority and inherited budget."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace

from domain.capability import CapabilityDependency, CapabilityVersionRecord
from domain.task import canonical_json
from domain.task_execution import TaskClaim


@dataclass(frozen=True)
class WorkflowInvocation:
    root: TaskClaim = field(repr=False)
    reference: CapabilityDependency
    artifact: CapabilityVersionRecord = field(repr=False)
    input: dict = field(repr=False)
    parent_node_id: str | None = None
    delegated_workflows: tuple[CapabilityDependency, ...] = ()

    @property
    def task(self):
        return self.root.task

    @property
    def lease(self):
        return self.root.lease

    @property
    def budget(self):
        return self.root.budget

    def with_timeout(self, seconds: float) -> WorkflowInvocation:
        return replace(self, root=self.root.with_timeout(seconds))

    def limit_timeout(self, seconds: float) -> float:
        return self.root.limit_timeout(seconds)

    @classmethod
    def from_root(cls, claim: TaskClaim) -> WorkflowInvocation:
        return cls(claim, CapabilityDependency(key=claim.task.capability_key, version=claim.task.version),
                   claim.artifact, deepcopy(claim.input))

    def child(self, reference: CapabilityDependency, artifact: CapabilityVersionRecord,
              values: dict, parent_node_id: str) -> WorkflowInvocation:
        if not isinstance(values, dict):
            raise ValueError("Workflow input must be a JSON object")
        canonical_json(values)
        return WorkflowInvocation(self.root, reference, artifact, deepcopy(values), parent_node_id,
                                  (*self.delegated_workflows, reference))
