"""Agent invocation adapter; the root claim retains scheduling and final-result authority."""

import asyncio

from domain.agent_invocation import AgentInvocation
from domain.agent_runtime import AgentRuntimeRegistry, AgentRuntimeUnstoppable
from domain.business_code import is_activity_unavailable
from domain.capability import CapabilityValidationUnavailable
from domain.capability_definitions import interpret_definition
from domain.execution_ports import AgentLifecycle, ExecutionPrompts
from domain.execution_requirements import AGENT_INTERPRETER_PROFILE, AGENT_RUNTIME_PROFILES
from domain.platform_agent import AgentValidationUnavailable
from domain.task_execution import ExecutionFailure, ExecutionResourceUnstoppable, TaskClaim
from orchestration.agent_broker import AgentBroker
from orchestration.agent_model_gateway import PlatformAgentModelGateway


class AgentExecutor:
    def __init__(self, gateway: PlatformAgentModelGateway, runtimes: AgentRuntimeRegistry, *,
                 lifecycle: AgentLifecycle, prompts: ExecutionPrompts):
        self._gateway = gateway
        self._runtimes = runtimes
        self._lifecycle = lifecycle
        self._prompts = prompts

    @property
    def runtime_descriptors(self):
        return self._runtimes.descriptors

    @property
    def interpreter_profile(self):
        return AGENT_INTERPRETER_PROFILE

    @property
    def implementation_references(self):
        return self._gateway.tools.registry.implementation_references

    @property
    def tool_implementation_references(self):
        return self._gateway.tools.registry.implementation_references

    async def __call__(self, claim: TaskClaim) -> dict:
        return await self.invoke(AgentInvocation.from_root(claim))

    async def invoke(self, invocation: AgentInvocation) -> dict:
        definition = interpret_definition("agent", invocation.artifact).materialize()
        invocation = invocation.with_timeout(definition.limits.timeout_seconds)
        runtime = self._runtimes.get(definition.runtime)
        if runtime is None:
            raise ExecutionFailure("AGENT_RUNTIME_UNAVAILABLE")
        if runtime.descriptor.profile != AGENT_RUNTIME_PROFILES.get(definition.runtime):
            raise ExecutionFailure("AGENT_RUNTIME_INCOMPATIBLE")
        broker = AgentBroker(invocation, self._gateway, runtime.descriptor,
                             lifecycle=self._lifecycle, prompts=self._prompts)
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
        except asyncio.CancelledError:
            # Gateways have drained and settled their owned calls before runtime
            # cancellation returns. Close this child too, without converging Task.
            await broker.fail("AGENT_EXECUTION_CANCELLED")
            raise
        except ExecutionFailure as exc:
            if is_activity_unavailable(exc.code):
                await broker.fail(exc.code)
            raise
