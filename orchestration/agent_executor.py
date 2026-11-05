"""Agent invocation adapter; the root claim retains scheduling and final-result authority."""

import asyncio

from domain.agent_invocation import AgentInvocation
from domain.agent_runtime import AgentRuntimeRegistry, AgentRuntimeUnstoppable
from domain.capability import CapabilityValidationUnavailable
from domain.platform_agent import AgentDefinition, AgentValidationUnavailable
from domain.task_execution import ExecutionFailure, ExecutionResourceUnstoppable, TaskClaim
from orchestration.agent_broker import AgentBroker
from orchestration.agent_model_gateway import PlatformAgentModelGateway


class AgentExecutor:
    def __init__(self, gateway: PlatformAgentModelGateway, runtimes: AgentRuntimeRegistry):
        self._gateway = gateway
        self._runtimes = runtimes

    async def __call__(self, claim: TaskClaim) -> dict:
        return await self.invoke(AgentInvocation.from_root(claim))

    async def invoke(self, invocation: AgentInvocation) -> dict:
        definition = AgentDefinition.model_validate(invocation.artifact.definition)
        invocation = invocation.with_timeout(definition.limits.timeout_seconds)
        runtime = self._runtimes.get(definition.runtime)
        if runtime is None:
            raise ExecutionFailure("AGENT_RUNTIME_UNAVAILABLE")
        broker = AgentBroker(invocation, self._gateway, runtime.descriptor)
        try:
            start = await broker.open()
            async with asyncio.timeout(start.timeout_seconds):
                completed = await runtime.adapter.run(start, broker)
                return await broker.finish(expected_text=completed.final_text)
        except (AgentValidationUnavailable, CapabilityValidationUnavailable):
            raise ExecutionFailure("AGENT_VALIDATION_UNAVAILABLE") from None
        except AgentRuntimeUnstoppable:
            raise ExecutionResourceUnstoppable("Agent runtime could not stop safely; stop this Worker") from None
        except TimeoutError:
            raise ExecutionFailure("AGENT_EXECUTION_TIMEOUT_RESULT_UNKNOWN", status="timed_out") from None
