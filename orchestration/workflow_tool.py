"""Thin tool-node adapter; endpoint selection and authorization stay in the gateway."""

from domain.task_execution import ExecutionFailure, TaskClaim
from domain.workflow import WorkflowNode
from domain.workflow_runtime import WorkflowAdapterDescriptor
from orchestration.platform_tool_gateway import PlatformToolGateway


class WorkflowTool:
    descriptor = WorkflowAdapterDescriptor(kind="tool", profile="enterprise-ai.workflow.tool.v1")

    def __init__(self, gateway: PlatformToolGateway):
        self.gateway = gateway

    async def __call__(self, claim: TaskClaim, node: WorkflowNode, values: dict, node_id: str) -> dict:
        if node.kind != "tool" or node.capability is None:
            raise ExecutionFailure("TOOL_CONFIGURATION_UNAVAILABLE")
        return await self.gateway.invoke(claim, node_id, node.capability, values)
