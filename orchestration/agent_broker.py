"""Python-owned canonical conversation and dispatch state; not an Agent Loop.

An installed runtime adapter drives these ports. No implicit model/tool requests, retries,
resume or Task completion occur here. Any failed operation poisons this broker.
"""

import asyncio
from copy import deepcopy

from domain.agent_invocation import AgentInvocation
from domain.agent_runtime import AgentRuntimeDescriptor, AgentRuntimeStart
from domain.agent_turn import (
    AgentAssistantMessage,
    AgentConversation,
    AgentMessage,
    AgentTextMessage,
    AgentToolMessage,
    RuntimeToolCall,
    RuntimeTurn,
)
from domain.capability import CapabilityDependency
from domain.capability_definitions import interpret_definition
from domain.execution_context import ExecutionDeadlineExceeded
from domain.execution_ports import AgentLifecycle, ExecutionPrompts
from domain.platform_agent import AgentCapacityUnavailable
from domain.task import canonical_json
from domain.task_execution import (
    ExecutionFailure,
    ExecutionResourceUnstoppable,
    TaskCancellationRequested,
    TaskLeaseLost,
)
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.execution_async import accounting_error, run_sync


class AgentBroker:
    def __init__(self, invocation: AgentInvocation, gateway: PlatformAgentModelGateway,
                 runtime: AgentRuntimeDescriptor, *, lifecycle: AgentLifecycle, prompts: ExecutionPrompts):
        self._claim = invocation
        self._gateway = gateway
        self._runtime = runtime
        self._lifecycle = lifecycle
        self._prompts = prompts
        self._messages: list[AgentMessage] = []
        self._pending = []
        self._final = None
        self._final_text = None
        self._phase = "new"
        self._lock = asyncio.Lock()
        self._execution_id = None
        self._deadline = None
        self._terminal_started = False

    async def open(self) -> AgentRuntimeStart:
        async with self._lock:
            if self._phase != "new":
                self._phase = "failed"
                raise ExecutionFailure("AGENT_BROKER_ALREADY_STARTED")
            self._phase = "failed"
            try:
                declared = interpret_definition("agent", self._claim.artifact).materialize()
            except ValueError:
                raise ExecutionFailure("AGENT_CONFIGURATION_UNAVAILABLE") from None
            # Apply the Agent budget before any live resource resolution. The
            # resolver still verifies identity, publication and the content hash.
            self._claim = self._claim.with_timeout(declared.limits.timeout_seconds)
            try:
                timeout_seconds = self._claim.limit_timeout(declared.limits.timeout_seconds)
                self._deadline = asyncio.get_running_loop().time() + timeout_seconds
                async with asyncio.timeout_at(self._deadline):
                    definition, _, catalog, skills = await run_sync(self._gateway.resolve_agent, self._claim)
            except TimeoutError:
                raise ExecutionFailure("AGENT_START_TIMEOUT", status="timed_out") from None
            if definition.runtime != self._runtime.key:
                raise ExecutionFailure("AGENT_RUNTIME_MISMATCH")
            try:
                async with asyncio.timeout_at(self._deadline):
                    rendered = await run_sync(
                        self._prompts.render,
                        self._claim.task.project_key, definition.instruction_prompt.key,
                        definition.instruction_prompt.version, {},
                    )
            except TimeoutError:
                raise ExecutionFailure("AGENT_START_TIMEOUT", status="timed_out") from None
            instructions = rendered["text"]
            if skills:
                guidance = [{"key": skill.key, "version": skill.version, "content_hash": skill.content_hash,
                             "instructions": skill.instructions} for skill in skills]
                instructions += "\n\nVersioned Skill guidance (not tool authorization):\n" + canonical_json(guidance)
            if len(instructions) > 262144:
                raise ExecutionFailure("AGENT_INSTRUCTIONS_TOO_LARGE")
            messages: list[AgentMessage] = [AgentTextMessage(role="system", content=instructions),
                                           AgentTextMessage(role="user", content=canonical_json(self._claim.input))]
            AgentConversation(messages=tuple(messages))
            try:
                async with asyncio.timeout_at(self._deadline):
                    while True:
                        try:
                            execution = await run_sync(self._lifecycle.begin, self._claim.lease,
                                parent_node_id=self._claim.parent_node_id, input_values=self._claim.input,
                                runtime_profile=self._runtime.profile,
                                _on_cancel_result=self._remember_execution,
                                _on_cancel_error=accounting_error("AGENT_ACCOUNTING_RESULT_UNKNOWN"))
                            self._remember_execution(execution)
                            break
                        except AgentCapacityUnavailable:
                            await asyncio.sleep(0.25)
                        except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
                            raise
                        except Exception:
                            raise ExecutionFailure("AGENT_ACCOUNTING_RESULT_UNKNOWN") from None
            except TimeoutError:
                raise ExecutionFailure("AGENT_START_TIMEOUT", status="timed_out") from None
            if (execution.agent_key, execution.agent_version, execution.agent_hash) != (
                self._claim.reference.key, self._claim.reference.version, self._claim.artifact.content_hash,
            ):
                raise ExecutionFailure("AGENT_INVOCATION_SNAPSHOT_MISMATCH")
            self._definition = definition
            self._execution_id = execution.id
            self._messages = messages
            remaining = self._deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self._phase = "failed"
                raise ExecutionFailure("AGENT_START_TIMEOUT", status="timed_out")
            try:
                remaining = min(remaining, self._claim.limit_timeout(definition.limits.timeout_seconds))
            except ExecutionDeadlineExceeded:
                self._phase = "failed"
                raise ExecutionFailure("AGENT_START_TIMEOUT", status="timed_out") from None
            start = AgentRuntimeStart(instructions=instructions, input_text=messages[1].content,
                                      tools=catalog, max_turns=definition.limits.max_turns,
                                      max_tool_calls=definition.limits.max_tool_calls, timeout_seconds=remaining)
            self._phase = "model"
            return start

    def _remember_execution(self, execution):
        self._execution_id = execution.id

    def _require(self, phase):
        if self._phase != phase:
            self._phase = "failed"
            raise ExecutionFailure("AGENT_PROTOCOL_STATE_INVALID")

    async def request_model(self) -> RuntimeTurn:
        async with self._lock:
            self._require("model")
            self._phase = "failed"
            assert self._execution_id is not None
            async with asyncio.timeout_at(self._deadline):
                conversation = AgentConversation(messages=tuple(self._messages))
                accepted = await self._gateway.generate_turn(self._claim, self._execution_id, conversation)
                turn = accepted.reply.turn
                assert turn is not None
                if turn.kind == "final":
                    self._final = deepcopy(accepted.final_result)
                    self._final_text = turn.text
                    self._phase = "final"
                    return RuntimeTurn(kind="final", text=turn.text)
                self._messages.append(AgentAssistantMessage(content=turn.text, tool_calls=turn.tool_calls))
                aliases = {tool.alias: CapabilityDependency(**tool.capability.model_dump()) for tool in self._definition.tools}
                pending = []
                presented = []
                for step, call in zip(accepted.pending_steps, turn.tool_calls, strict=True):
                    if step.dispatch_id is None:
                        raise ExecutionFailure("AGENT_DISPATCH_ACCOUNTING_INVALID")
                    pending.append((step.dispatch_id, call.id, aliases[call.alias], deepcopy(call.arguments)))
                    presented.append(RuntimeToolCall(dispatch_id=step.dispatch_id, alias=call.alias,
                                                     arguments=deepcopy(call.arguments)))
                self._pending = pending
                result = RuntimeTurn(kind="tools", text=turn.text, tool_calls=tuple(presented))
                self._phase = "tools"
                return result

    async def dispatch_tool(self, dispatch_id: str) -> dict:
        async with self._lock:
            self._require("tools")
            self._phase = "failed"
            assert self._execution_id is not None
            if not self._pending or self._pending[0][0] != dispatch_id:
                raise ExecutionFailure("AGENT_TOOL_DISPATCH_INVALID")
            handle, source_id, reference, arguments = self._pending[0]
            async with asyncio.timeout_at(self._deadline):
                output = await self._gateway.tools.invoke_agent(self._claim, self._execution_id, handle, reference, arguments)
                self._messages.append(AgentToolMessage(tool_call_id=source_id, content=canonical_json(output)))
                self._pending.pop(0)
                self._phase = "tools" if self._pending else "model"
                return deepcopy(output)

    async def finish(self, *, expected_text: str | None = None) -> dict:
        async with self._lock:
            self._require("final")
            self._phase = "failed"
            assert self._final is not None
            if expected_text is not None and expected_text != self._final_text:
                raise ExecutionFailure("AGENT_RUNTIME_FINAL_MISMATCH")
            async with asyncio.timeout_at(self._deadline):
                # A final answer is not authorization to deliver after revocation.
                await run_sync(self._gateway.resolve_agent, self._claim)
                self._terminal_started = True
                try:
                    await run_sync(self._lifecycle.finish, self._claim.lease,
                                   self._execution_id, result=self._final,
                                   _on_cancel_error=accounting_error("AGENT_ACCOUNTING_RESULT_UNKNOWN"))
                except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
                    raise
                except Exception:
                    raise ExecutionFailure("AGENT_ACCOUNTING_RESULT_UNKNOWN") from None
            self._phase = "closed"
            return deepcopy(self._final)

    async def fail(self, error_code: str) -> None:
        """Close a safely failed child, never compensate an uncertain terminal write."""
        async with self._lock:
            if self._execution_id is None:
                return
            if self._terminal_started:
                raise ExecutionFailure("AGENT_ACCOUNTING_RESULT_UNKNOWN")
            self._terminal_started = True
            self._phase = "failed"
            try:
                await run_sync(self._lifecycle.fail, self._claim.lease, self._execution_id, error_code=error_code,
                               _on_cancel_error=accounting_error("AGENT_ACCOUNTING_RESULT_UNKNOWN"))
            except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
                raise
            except Exception:
                raise ExecutionFailure("AGENT_ACCOUNTING_RESULT_UNKNOWN") from None
