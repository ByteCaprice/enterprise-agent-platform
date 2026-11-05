"""A real Workflow Agent node, with explicit delegation and no nested Task/lease."""

import asyncio
from copy import deepcopy

from domain.agent_invocation import AgentInvocation
from domain.capability import CapabilityError, CapabilityKind
from domain.platform_agent import AgentDefinition
from domain.task import canonical_json
from domain.task_execution import ExecutionFailure, TaskClaim
from domain.workflow import WorkflowNode
from domain.workflow_runtime import WorkflowAdapterDescriptor
from infra.schema_validation import validate_json
from orchestration.agent_executor import AgentExecutor
from orchestration.execution_authorization import ExecutionAuthorizer
from orchestration.published_capabilities import PublishedCapabilities


class WorkflowAgent:
    descriptor = WorkflowAdapterDescriptor(kind="agent", profile="enterprise-ai.workflow.agent.v1")

    def __init__(self, store, executor: AgentExecutor):
        self.executor = executor
        self.authorizer = ExecutionAuthorizer(store)
        self.catalog = PublishedCapabilities(store.capabilities)

    def resolve(self, claim: TaskClaim, node: WorkflowNode, values: dict, node_id: str) -> AgentInvocation:
        if node.kind != "agent" or node.capability is None or claim.task.capability_kind != "workflow":
            raise ExecutionFailure("AGENT_NODE_INVALID")
        if not (self.authorizer.allowed(claim) and
                self.authorizer.allowed_resource(claim, "agent:invoke", node.capability.key)):
            raise ExecutionFailure("AGENT_ACCESS_DENIED")
        try:
            artifact = self.catalog.resolve(claim.task.project_key, node.capability, CapabilityKind.AGENT)
        except CapabilityError:
            raise ExecutionFailure("AGENT_CONFIGURATION_UNAVAILABLE") from None
        AgentDefinition.model_validate(artifact.definition)
        canonical_json(values)
        validate_json(values, artifact.input_schema)
        return AgentInvocation(root=claim, reference=node.capability, artifact=artifact,
                               input=deepcopy(values), parent_node_id=node_id)

    async def __call__(self, claim: TaskClaim, node: WorkflowNode, values: dict, node_id: str) -> dict:
        invocation = await asyncio.to_thread(self.resolve, claim, node, values, node_id)
        return await self.executor.invoke(invocation)
