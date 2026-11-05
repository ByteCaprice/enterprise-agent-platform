"""Thin Workflow LLM adapter; all model I/O goes through the governed gateway."""

from __future__ import annotations

import asyncio

from domain.platform_model import ModelMessage, ModelMessages
from domain.task_execution import ExecutionFailure, TaskClaim
from domain.workflow import WorkflowNode
from domain.workflow_runtime import WorkflowAdapterDescriptor
from orchestration.execution_authorization import ExecutionAuthorizer
from orchestration.platform_model_gateway import PlatformModelGateway
from orchestration.prompt_renderer import PromptRenderer


class WorkflowLLM:
    descriptor = WorkflowAdapterDescriptor(kind="llm", profile="enterprise-ai.workflow.llm.v1")

    def __init__(self, store, gateway: PlatformModelGateway):
        self.gateway = gateway
        self.prompts = PromptRenderer(store.capabilities)
        self.authorizer = ExecutionAuthorizer(store)

    def _messages(self, claim: TaskClaim, node: WorkflowNode, values: dict) -> ModelMessages:
        if node.prompt is None:
            try:
                return ModelMessages.model_validate(values)
            except ValueError:
                raise ExecutionFailure("MODEL_INPUT_INVALID") from None
        if not (self.authorizer.allowed(claim) and
                self.authorizer.allowed_resource(claim, "prompt:render", node.prompt.key)):
            raise ExecutionFailure("MODEL_PROMPT_ACCESS_DENIED")
        try:
            rendered = self.prompts.render(claim.task.project_key, node.prompt.key, node.prompt.version, values)
            return ModelMessages(messages=[ModelMessage(role="user", content=rendered["text"])])
        except ValueError:
            raise ExecutionFailure("MODEL_PROMPT_INVALID") from None

    async def __call__(self, claim: TaskClaim, node: WorkflowNode, values: dict, node_id: str) -> dict:
        if node.kind != "llm" or node.capability is None:
            raise ExecutionFailure("MODEL_CONFIGURATION_UNAVAILABLE")
        messages = await asyncio.to_thread(self._messages, claim, node, values)
        return await self.gateway.complete(claim, node_id, node.capability, messages, prompt=node.prompt)
