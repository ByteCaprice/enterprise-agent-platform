"""Describe a published Workflow without publication commands or runtime probes."""

from domain.business_code import CodeWorkflowDefinition
from domain.capability import CapabilityDependency, CapabilityError, CapabilityKind, CapabilityReader
from domain.capability_definitions import interpret_definition
from domain.code_workflow_plan import CodeWorkflowPlanRecord
from domain.workflow import WorkflowPlanRecord
from framework.workflow_plan import WORKFLOW_PLAN_PROFILE, WorkflowPlanError, compile_workflow
from orchestration.published_capabilities import PublishedCapabilities


class WorkflowPlanQuery:
    def __init__(self, source: CapabilityReader):
        self.catalog = PublishedCapabilities(source)

    def describe(self, project_key: str, key: str, version: str) -> WorkflowPlanRecord | CodeWorkflowPlanRecord:
        artifact = self.catalog.resolve(project_key, CapabilityDependency(key=key, version=version), CapabilityKind.WORKFLOW)
        try:
            contract = interpret_definition(CapabilityKind.WORKFLOW, artifact)
            definition = contract.materialize()
            if isinstance(definition, CodeWorkflowDefinition):
                return CodeWorkflowPlanRecord(
                    project_key=project_key, capability_key=key, version=artifact.version, content_hash=artifact.content_hash,
                    implementation=definition.implementation, activities=definition.activities,
                    max_parallelism=definition.max_parallelism,
                    required_adapters=sorted({activity.kind for activity in definition.activities
                                              if activity.kind in {"agent", "tool", "llm"}}),
                )
            plan = compile_workflow(definition)
        except WorkflowPlanError as exc:
            raise CapabilityError(exc.code) from None
        nodes = []
        for planned in plan.nodes:
            node = planned.materialize()
            nodes.append({
                "key": node.key, "kind": node.kind, "depends_on": sorted(planned.dependencies),
                "capability": node.capability, "prompt": node.prompt,
                "timeout_seconds": node.timeout_seconds, "max_attempts": node.max_attempts,
                "conditional": node.when is not None,
                "rule_operation": node.rule.operation if node.rule is not None else None,
            })
        return WorkflowPlanRecord.model_validate({
            "project_key": project_key, "capability_key": key, "version": artifact.version,
            "content_hash": artifact.content_hash, "compiler_profile": WORKFLOW_PLAN_PROFILE,
            "interpreter_profile": plan.interpreter_profile, "checkpoint_profile": plan.checkpoint_profile,
            "required_adapters": sorted(plan.required_adapters), "max_parallel_rules": plan.max_parallel_rules,
            "nodes": nodes,
        })
