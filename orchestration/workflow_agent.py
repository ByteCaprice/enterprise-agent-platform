"""A real Workflow Agent node, with explicit delegation and no nested Task/lease."""

from copy import deepcopy

from domain.agent_invocation import AgentInvocation
from domain.capability import CapabilityError, CapabilityKind
from domain.capability_definitions import interpret_definition
from domain.execution_ports import ExecutionResources
from domain.task import canonical_json
from domain.task_execution import ExecutionFailure, TaskClaim
from domain.workflow import WorkflowNode
from domain.workflow_runtime import WorkflowAdapterDescriptor
from orchestration.agent_executor import AgentExecutor
from orchestration.execution_async import run_sync
from orchestration.published_capabilities import PublishedCapabilities


class WorkflowAgent:
    descriptor = WorkflowAdapterDescriptor(kind="agent", profile="enterprise-ai.workflow.agent.v1")

    def __init__(self, resources: ExecutionResources, executor: AgentExecutor):
        self.executor = executor
        self.authorizer = resources.authorization
        self.catalog = PublishedCapabilities(resources.capabilities)
        self.validation = resources.validation

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
        interpret_definition("agent", artifact)
        canonical_json(values)
        self.validation.validate(values, artifact.input_schema)
        return AgentInvocation(root=claim, reference=node.capability, artifact=artifact,
                               input=deepcopy(values), parent_node_id=node_id)

    async def __call__(self, claim: TaskClaim, node: WorkflowNode, values: dict, node_id: str) -> dict:
        invocation = await run_sync(self.resolve, claim, node, values, node_id)
        return await self.executor.invoke(invocation)
