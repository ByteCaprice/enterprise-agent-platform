"""Immutable, bounded execution plans compiled from workflow definitions."""

from __future__ import annotations

import json
from collections.abc import Set
from dataclasses import dataclass, field

from pydantic import ValidationError

from domain.task import TaskError, canonical_json
from domain.workflow import WorkflowBinding, WorkflowDefinition, WorkflowNode

WORKFLOW_PLAN_PROFILE = "enterprise-ai.workflow.plan.v1"


class WorkflowPlanError(ValueError):
    """A safe, classified workflow-plan compilation or progress failure."""

    DEFINITION_INVALID = "WORKFLOW_DEFINITION_INVALID"
    RETRY_NOT_SUPPORTED = "WORKFLOW_RETRY_NOT_SUPPORTED"
    PROGRESS_INVALID = "WORKFLOW_PROGRESS_INVALID"

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PlannedWorkflowNode:
    key: str
    kind: str
    dependencies: frozenset[str]
    referenced_outputs: frozenset[str]
    snapshot: str = field(repr=False)

    def materialize(self) -> WorkflowNode:
        """Return a new, validated model tree detached from this plan."""
        return WorkflowNode.model_validate_json(self.snapshot)


@dataclass(frozen=True)
class WorkflowExecutionPlan:
    nodes: tuple[PlannedWorkflowNode, ...]
    output_snapshot: str = field(repr=False)
    max_parallel_rules: int
    required_adapters: frozenset[str]

    def output_mapping(self) -> dict[str, WorkflowBinding]:
        """Decode a fresh typed output mapping on every call."""
        try:
            raw = json.loads(self.output_snapshot)
            return {key: WorkflowBinding.model_validate(value) for key, value in raw.items()}
        except (TypeError, ValueError, ValidationError):
            raise WorkflowPlanError(WorkflowPlanError.DEFINITION_INVALID) from None

    def next_batch(self, completed: Set[str]) -> tuple[PlannedWorkflowNode, ...]:
        keys = {node.key for node in self.nodes}
        if not isinstance(completed, Set):
            raise WorkflowPlanError(WorkflowPlanError.PROGRESS_INVALID)
        try:
            done = set(completed)
        except (TypeError, ValueError):
            raise WorkflowPlanError(WorkflowPlanError.PROGRESS_INVALID) from None
        if not done <= keys or any(not isinstance(key, str) for key in done):
            raise WorkflowPlanError(WorkflowPlanError.PROGRESS_INVALID)
        by_key = {node.key: node for node in self.nodes}
        if any(not by_key[key].dependencies <= done for key in done):
            raise WorkflowPlanError(WorkflowPlanError.PROGRESS_INVALID)
        if len(done) == len(keys):
            return ()

        ready = [node for node in self.nodes if node.key not in done and node.dependencies <= done]
        if not ready:
            raise WorkflowPlanError(WorkflowPlanError.PROGRESS_INVALID)
        if ready[0].kind == "rule":
            return tuple(node for node in ready if node.kind == "rule")[: self.max_parallel_rules]
        return (ready[0],)


def compile_workflow(definition: WorkflowDefinition) -> WorkflowExecutionPlan:
    """Validate and freeze a workflow using bounded canonical JSON snapshots."""
    try:
        source = definition.model_dump(mode="json", exclude_defaults=True)
        frozen_definition = canonical_json(source)
        cloned = WorkflowDefinition.model_validate_json(frozen_definition)
        if any(node.max_attempts != 1 for node in cloned.nodes):
            raise WorkflowPlanError(WorkflowPlanError.RETRY_NOT_SUPPORTED)
        ordered = cloned.ordered_nodes()
        node_plans: list[PlannedWorkflowNode] = []
        for node in ordered:
            bindings = list(node.input_mapping.values())
            if node.when is not None:
                bindings.append(node.when)
            references = frozenset(binding.node for binding in bindings if binding.source == "node" and binding.node is not None)
            node_plans.append(
                PlannedWorkflowNode(
                    key=node.key,
                    kind=node.kind,
                    dependencies=frozenset(node.depends_on),
                    referenced_outputs=references,
                    snapshot=canonical_json(node.model_dump(mode="json", exclude_defaults=True)),
                )
            )
        output_snapshot = canonical_json(
            {key: value.model_dump(mode="json", exclude_defaults=True) for key, value in cloned.output_mapping.items()}
        )
        return WorkflowExecutionPlan(
            nodes=tuple(node_plans),
            output_snapshot=output_snapshot,
            max_parallel_rules=cloned.max_parallel_rules,
            required_adapters=frozenset(node.kind for node in cloned.nodes if node.kind != "rule"),
        )
    except WorkflowPlanError:
        raise
    except (TaskError, ValidationError, TypeError, ValueError, RecursionError):
        raise WorkflowPlanError(WorkflowPlanError.DEFINITION_INVALID) from None
