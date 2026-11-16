"""Explicit execution-plane composition for tests using a store or store double."""

from domain.agent_runtime import AgentRuntimeDescriptor
from domain.execution_ports import ExecutionResources
from domain.execution_requirements import (
    AGENT_INTERPRETER_PROFILE,
    AGENT_RUNTIME_PROFILES,
    TOOL_INTERPRETER_PROFILE,
    WORKFLOW_ADAPTER_PROFILES,
    WORKFLOW_CHECKPOINT_PROFILE,
    WORKFLOW_INTERPRETER_PROFILE,
    InstalledExecutionCapabilities,
)
from domain.task_worker import WorkerServices
from domain.workflow_runtime import WorkflowAdapterDescriptor
from infra.schema_validation import IsolatedJSONValidator
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.execution_authorization import ExecutionAuthorizer
from orchestration.model_policy_resolver import ModelPolicyResolver
from orchestration.platform_model_gateway import PlatformModelGateway
from orchestration.platform_tool_gateway import PlatformToolGateway
from orchestration.workflow_executor import WorkflowExecutor


def execution_resources(store):
    return ExecutionResources(
        store.capabilities, ExecutionAuthorizer(store.governance, store.api_key_configs), IsolatedJSONValidator(),
    )


def model_gateway(store, transport, credentials):
    resources = execution_resources(store)
    return PlatformModelGateway(
        ModelPolicyResolver(resources, store.models), store.models, transport, credentials,
        validator=resources.validation,
    )


def tool_gateway(store, registry):
    return PlatformToolGateway(execution_resources(store), store.tool_bindings, store.agents, registry)


def agent_gateway(store, transport, credentials, tools):
    resources = tools.resources
    return PlatformAgentModelGateway(
        resources, ModelPolicyResolver(resources, store.models), store.agents, transport, credentials, tools,
    )


def workflow_executor(store, adapters=None):
    return WorkflowExecutor(
        store.workflows, ExecutionAuthorizer(store.governance, store.api_key_configs), adapters,
    )


def worker_services(store):
    return WorkerServices(
        store.task_queue, store.task_control, store.task_completion,
        ExecutionAuthorizer(store.governance, store.api_key_configs), IsolatedJSONValidator(),
    )


def worker_capabilities(*kinds, workflow_adapters=("llm", "tool", "agent")):
    """Explicit synthetic execution contract for direct lifecycle/queue tests.

    Test executors intentionally replace real runtimes and gateways. Production
    composition must use its actual installed descriptors, not this fixture.
    """
    kinds = kinds or ("agent", "workflow")
    return InstalledExecutionCapabilities.from_descriptors(
        kinds,
        workflow_interpreter_profile=WORKFLOW_INTERPRETER_PROFILE if "workflow" in kinds else None,
        workflow_checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE if "workflow" in kinds else None,
        workflow_adapters=tuple(WorkflowAdapterDescriptor(kind=kind, profile=WORKFLOW_ADAPTER_PROFILES[kind])
                                for kind in workflow_adapters) if "workflow" in kinds else (),
        agent_interpreter_profile=AGENT_INTERPRETER_PROFILE,
        agent_runtimes=(AgentRuntimeDescriptor(key="pi", profile=AGENT_RUNTIME_PROFILES["pi"]),),
        tool_interpreter_profile=TOOL_INTERPRETER_PROFILE,
    )
