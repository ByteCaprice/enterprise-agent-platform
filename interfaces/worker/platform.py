"""Explicit Workflow and root Agent composition; no automatic executor opt-in."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from domain.agent_runtime import AgentRuntimeRegistry
from domain.execution_ports import ExecutionResources
from domain.execution_requirements import (
    AGENT_INTERPRETER_PROFILE,
    TOOL_INTERPRETER_PROFILE,
    WORKFLOW_CHECKPOINT_PROFILE,
    WORKFLOW_INTERPRETER_PROFILE,
    InstalledExecutionCapabilities,
)
from domain.platform_mcp import MCPCatalog
from domain.task_worker import WorkerServices
from domain.workflow_runtime import WorkflowNodeAdapter, WorkflowNodeRegistry
from framework.readonly_tools import ReadonlyToolRegistry, builtin_readonly_tools
from infra.agent_turn_http import AgentTurnHTTPTransport
from infra.model_credentials import EnvironmentCredentialResolver
from infra.model_http import OpenAICompatibleTransport
from infra.schema_validation import IsolatedJSONValidator, check_validation_backend
from interfaces.worker.lifecycle import observe_worker, stop_signals
from orchestration.agent_executor import AgentExecutor
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.code_workflow import CodeWorkflowExecutor
from orchestration.execution_authorization import ExecutionAuthorizer
from orchestration.mcp_tools import mcp_readonly_tools
from orchestration.model_policy_resolver import ModelPolicyResolver
from orchestration.platform_model_gateway import PlatformModelGateway
from orchestration.platform_tool_gateway import PlatformToolGateway
from orchestration.platform_worker import Executor, PlatformWorker
from orchestration.prompt_renderer import PromptRenderer
from orchestration.workflow_agent import WorkflowAgent
from orchestration.workflow_dispatcher import WorkflowDispatcher
from orchestration.workflow_executor import WorkflowExecutor
from orchestration.workflow_llm import WorkflowLLM
from orchestration.workflow_tool import WorkflowTool


async def run_workflow_worker(store, *, worker_id: str | None = None, once: bool = False,
                              gateway: PlatformModelGateway | None = None,
                              tool_gateway: PlatformToolGateway | None = None,
                              mcp_catalog: MCPCatalog | None = None,
                              hub_bundles: tuple[str, ...] = ()) -> bool | None:
    return await run_platform_worker(store, worker_id=worker_id, once=once, gateway=gateway,
                                     tool_gateway=tool_gateway, mcp_catalog=mcp_catalog, hub_bundles=hub_bundles)


async def run_platform_worker(store, *, execution_kinds: tuple[str, ...] = ("workflow",),
                              worker_id: str | None = None, once: bool = False,
                              gateway: PlatformModelGateway | None = None,
                              tool_gateway: PlatformToolGateway | None = None,
                              mcp_catalog: MCPCatalog | None = None,
                              agent_gateway: PlatformAgentModelGateway | None = None,
                              agent_runtimes: AgentRuntimeRegistry | None = None,
                              workflow_agents: bool = False,
                              hub_bundles: tuple[str, ...] = ()) -> bool | None:
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
    business = None
    if hub_bundles:
        # Only the trusted process entry imports the explicitly selected source
        # catalog. Published artifacts never supply modules or install code.
        from capability_hub.catalog import builtin_catalog

        business = builtin_catalog().select(hub_bundles)
    if agent_gateway is not None:
        if tool_gateway is not None and agent_gateway.tools is not tool_gateway:
            raise ValueError("Agent and Workflow must share their governed Tool Gateway")
        tool_gateway = agent_gateway.tools
    # Overrides are trusted process composition, not artifact-controlled plugins.
    # Require one shared read/authority scope and the same fenced ledgers without
    # giving execution consumers access to the rest of PlatformStore.
    resources = (agent_gateway.resources if agent_gateway is not None else
                 tool_gateway.resources if tool_gateway is not None else
                 gateway.policies.resources if gateway is not None else
                 ExecutionResources(store.capabilities, ExecutionAuthorizer(store.governance, store.api_key_configs),
                                    IsolatedJSONValidator()))
    if resources.capabilities is not store.capabilities:
        raise ValueError("Execution resources must read the configured capability catalog")
    if tool_gateway is not None and (tool_gateway.resources is not resources or
                                    tool_gateway.calls is not store.tool_bindings or
                                    tool_gateway.agent_calls is not store.agents):
        raise ValueError("Tool execution must share the configured resource scope and fenced ledgers")
    if agent_gateway is not None and (agent_gateway.turns is not store.agents or
                                     agent_gateway.policies.resources is not resources or
                                     agent_gateway.policies.providers is not store.models):
        raise ValueError("Agent execution must share the configured resource scope and fenced ledgers")
    if gateway is not None and (gateway.calls is not store.models or gateway.policies.resources is not resources or
                                gateway.policies.providers is not store.models):
        raise ValueError("Model execution must share the configured resource scope and fenced ledger")
    await asyncio.to_thread(check_validation_backend)
    policies = ModelPolicyResolver(resources, store.models)
    prompts = PromptRenderer(resources.capabilities)
    registry = mcp_readonly_tools(mcp_catalog) if mcp_catalog is not None else builtin_readonly_tools()
    if business is not None:
        registry = ReadonlyToolRegistry((*registry.handlers, *business.tools))
    tool_gateway = tool_gateway or PlatformToolGateway(resources, store.tool_bindings, store.agents, registry)
    if business is not None and not set(reference for tool in business.tools if (reference := tool.implementation) is not None) <= set(
        tool_gateway.registry.implementation_references,
    ):
        raise ValueError("The supplied Tool Gateway does not install the selected business implementations")
    executors: dict[str, Executor] = {}
    agent_executor = None
    workflow_executor = None
    if agents_enabled:
        if agent_runtimes is None:
            from framework.agent_runtime.pi.adapter import PiRuntime

            agent_runtimes = AgentRuntimeRegistry((PiRuntime(),))
        # Fail before claiming any Task if the selected runtime/dependencies are unavailable.
        await agent_runtimes.check_ready()
        agent_gateway = agent_gateway or PlatformAgentModelGateway(
            resources, policies, store.agents, AgentTurnHTTPTransport(), EnvironmentCredentialResolver(), tool_gateway,
        )
        agent_executor = AgentExecutor(agent_gateway, agent_runtimes, lifecycle=store.agents, prompts=prompts)
        if "agent" in execution_kinds:
            executors["agent"] = agent_executor
    if "workflow" in execution_kinds:
        gateway = gateway or PlatformModelGateway(policies, store.models, OpenAICompatibleTransport(),
                                                 EnvironmentCredentialResolver(), validator=resources.validation)
        adapters: list[WorkflowNodeAdapter] = [WorkflowLLM(gateway, prompts, resources.authorization), WorkflowTool(tool_gateway)]
        if workflow_agents:
            assert agent_executor is not None
            adapters.append(WorkflowAgent(resources, agent_executor))
        nodes = WorkflowNodeRegistry(adapters)
        declarative = WorkflowExecutor(store.workflows, resources.authorization, nodes)
        code = (CodeWorkflowExecutor(store.workflows, resources, business.workflows, nodes)
                if business is not None and business.workflows.references else None)
        workflow_executor = WorkflowDispatcher(declarative, code, tool_implementations=tool_gateway.registry.implementation_references)
        executors["workflow"] = workflow_executor
    capabilities = InstalledExecutionCapabilities.from_descriptors(
        executors,
        workflow_interpreter_profile=WORKFLOW_INTERPRETER_PROFILE if workflow_executor is not None else None,
        workflow_checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE if workflow_executor is not None else None,
        workflow_adapters=workflow_executor.adapter_descriptors if workflow_executor is not None else (),
        agent_interpreter_profile=AGENT_INTERPRETER_PROFILE if agent_executor is not None else None,
        agent_runtimes=agent_executor.runtime_descriptors if agent_executor is not None else (),
        tool_interpreter_profile=TOOL_INTERPRETER_PROFILE,
        additional_profiles=workflow_executor.additional_profiles if workflow_executor is not None else (),
        workflow_implementations=tuple({reference for executor in executors.values()
                                       for reference in getattr(executor, "workflow_implementation_references", ())}),
        tool_implementations=tuple({reference for executor in executors.values()
                                   for reference in getattr(executor, "tool_implementation_references", ())}),
    )
    services = WorkerServices(
        store.task_queue, store.task_control, store.task_completion, resources.authorization, resources.validation,
    )
    worker = PlatformWorker(services, worker_id or f"{'-'.join(execution_kinds)}-{uuid4().hex}", executors,
                            capabilities=capabilities)
    stop = asyncio.Event()
    with stop_signals(stop):
        return await observe_worker(store, worker.worker_id, "task",
            worker.run_once if once else lambda: worker.run(stop),
            executors=execution_kinds, workflow_agents=workflow_agents, capabilities=capabilities)
