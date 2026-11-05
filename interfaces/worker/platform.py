"""Explicit Workflow and root Agent composition; no automatic executor opt-in."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from domain.agent_runtime import AgentRuntimeRegistry
from domain.platform_mcp import MCPCatalog
from domain.workflow_runtime import WorkflowNodeAdapter, WorkflowNodeRegistry
from framework.readonly_tools import builtin_readonly_tools
from infra.agent_turn_http import AgentTurnHTTPTransport
from infra.model_credentials import EnvironmentCredentialResolver
from infra.model_http import OpenAICompatibleTransport
from infra.schema_validation import check_validation_backend
from interfaces.worker.lifecycle import observe_worker, stop_signals
from orchestration.agent_executor import AgentExecutor
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.mcp_tools import mcp_readonly_tools
from orchestration.platform_model_gateway import PlatformModelGateway
from orchestration.platform_tool_gateway import PlatformToolGateway
from orchestration.platform_worker import Executor, PlatformWorker
from orchestration.workflow_agent import WorkflowAgent
from orchestration.workflow_executor import WorkflowExecutor
from orchestration.workflow_llm import WorkflowLLM
from orchestration.workflow_tool import WorkflowTool


async def run_workflow_worker(store, *, worker_id: str | None = None, once: bool = False,
                              gateway: PlatformModelGateway | None = None,
                              tool_gateway: PlatformToolGateway | None = None,
                              mcp_catalog: MCPCatalog | None = None) -> bool | None:
    return await run_platform_worker(store, worker_id=worker_id, once=once, gateway=gateway,
                                     tool_gateway=tool_gateway, mcp_catalog=mcp_catalog)


async def run_platform_worker(store, *, execution_kinds: tuple[str, ...] = ("workflow",),
                              worker_id: str | None = None, once: bool = False,
                              gateway: PlatformModelGateway | None = None,
                              tool_gateway: PlatformToolGateway | None = None,
                              mcp_catalog: MCPCatalog | None = None,
                              agent_gateway: PlatformAgentModelGateway | None = None,
                              agent_runtimes: AgentRuntimeRegistry | None = None,
                              workflow_agents: bool = False) -> bool | None:
    if (not execution_kinds or len(set(execution_kinds)) != len(execution_kinds) or
            not set(execution_kinds) <= {"workflow", "agent"}):
        raise ValueError("Select explicit Workflow/Agent executors")
    if workflow_agents and "workflow" not in execution_kinds:
        raise ValueError("Workflow Agent nodes require the Workflow role")
    agents_enabled = "agent" in execution_kinds or workflow_agents
    if not agents_enabled and (agent_gateway is not None or agent_runtimes is not None):
        raise ValueError("Agent dependencies require explicit Agent executor selection")
    if mcp_catalog is not None and (tool_gateway is not None or agent_gateway is not None):
        raise ValueError("Pass an explicit Tool Gateway or MCP deployment catalog, not both")
    if agent_gateway is not None:
        if agent_gateway.store is not store or (tool_gateway is not None and agent_gateway.tools is not tool_gateway):
            raise ValueError("Agent and Workflow must share their Store and governed Tool Gateway")
        tool_gateway = agent_gateway.tools
    await asyncio.to_thread(check_validation_backend)
    registry = mcp_readonly_tools(mcp_catalog) if mcp_catalog is not None else builtin_readonly_tools()
    tool_gateway = tool_gateway or PlatformToolGateway(store, registry)
    executors: dict[str, Executor] = {}
    agent_executor = None
    if agents_enabled:
        if agent_runtimes is None:
            from framework.agent_runtime.pi.adapter import PiRuntime

            agent_runtimes = AgentRuntimeRegistry((PiRuntime(),))
        # Fail before claiming any Task if the selected runtime/dependencies are unavailable.
        await agent_runtimes.check_ready()
        agent_gateway = agent_gateway or PlatformAgentModelGateway(
            store, AgentTurnHTTPTransport(), EnvironmentCredentialResolver(), tool_gateway,
        )
        agent_executor = AgentExecutor(agent_gateway, agent_runtimes)
        if "agent" in execution_kinds:
            executors["agent"] = agent_executor
    if "workflow" in execution_kinds:
        gateway = gateway or PlatformModelGateway(store, OpenAICompatibleTransport(), EnvironmentCredentialResolver())
        adapters: list[WorkflowNodeAdapter] = [WorkflowLLM(store, gateway), WorkflowTool(tool_gateway)]
        if workflow_agents:
            assert agent_executor is not None
            adapters.append(WorkflowAgent(store, agent_executor))
        executors["workflow"] = WorkflowExecutor(store, WorkflowNodeRegistry(adapters))
    worker = PlatformWorker(store, worker_id or f"{'-'.join(execution_kinds)}-{uuid4().hex}", executors,
                            workflow_agents=workflow_agents)
    stop = asyncio.Event()
    with stop_signals(stop):
        return await observe_worker(store, worker.worker_id, "task",
            worker.run_once if once else lambda: worker.run(stop),
            executors=execution_kinds, workflow_agents=workflow_agents)
