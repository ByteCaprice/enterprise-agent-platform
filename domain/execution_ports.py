"""Execution-only ports. No configuration writes, sessions or store traversal.

Each method retains its adapter's atomic fenced transaction. Splitting consumers
does not split Agent-step/model accounting or introduce a second execution owner.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from domain.business_code import CodeNodeScope
from domain.capability import CapabilityReader
from domain.execution_authorization import ExecutionAuthorizationPort
from domain.outbound_operation import ModelUsage, OutboundEvidence
from domain.platform_agent import AgentExecutionRecord, AgentStepRecord, AgentToolIntent
from domain.platform_model import ModelProviderRecord
from domain.platform_tool import ToolBindingRecord
from domain.schema_validation import JSONSchemaValidator
from domain.task_execution import TaskLease
from domain.workflow import WorkflowCheckpoint


@dataclass(frozen=True)
class ExecutionResources:
    """Shared read/decision ports, never cached grants or mutable administration."""

    capabilities: CapabilityReader
    authorization: ExecutionAuthorizationPort
    validation: JSONSchemaValidator


class ModelProviderReader(Protocol):
    def provider(self, project_key: str, key: str) -> ModelProviderRecord: ...


class ModelCallLedger(Protocol):
    def start_call(self, lease: TaskLease, node_id: str, *, policy_key: str, policy_version: str,
                   policy_hash: str, requested_model: str, input_hash: str) -> str | None: ...

    def finish_call(self, lease: TaskLease, call_id: str, *, status: str, actual_model: str | None = None,
                    output_hash: str | None = None, usage: ModelUsage | None = None, http_status: int | None = None,
                    provider_request_id: str | None = None, error_code: str | None = None,
                    evidence: OutboundEvidence | None = None) -> bool: ...


class AgentCallRejection(Protocol):
    def reject_call(self, lease: TaskLease, execution_id: str, step_id: str, *, error_code: str,
                    cancelled: bool = False, actual_model: str | None = None, usage: ModelUsage | None = None,
                    http_status: int | None = None, request_id: str | None = None,
                    evidence: OutboundEvidence | None = None) -> None: ...


class AgentTurnLedger(AgentCallRejection, Protocol):
    def start_turn(self, lease: TaskLease, execution_id: str, *, input_hash: str) -> tuple[AgentStepRecord, str]: ...

    def accept_turn(self, lease: TaskLease, execution_id: str, step_id: str, *, output_hash: str,
                    actual_model: str, final_result: dict | None = None, intents: Sequence[AgentToolIntent] = (),
                    usage: ModelUsage | None = None, http_status: int | None = None, request_id: str | None = None,
                    evidence: OutboundEvidence | None = None) -> list[AgentStepRecord]: ...


class AgentToolLedger(AgentCallRejection, Protocol):
    def start_tool(self, lease: TaskLease, execution_id: str, dispatch_id: str, *,
                   arguments: dict) -> tuple[AgentStepRecord, str]: ...

    def finish_tool(self, lease: TaskLease, execution_id: str, step_id: str, *, output: dict) -> None: ...


class AgentLifecycle(Protocol):
    def begin(self, lease: TaskLease, *, runtime_profile: str, parent_node_id: str | None = None,
              input_values: dict | None = None) -> AgentExecutionRecord: ...

    def finish(self, lease: TaskLease, execution_id: str, *, result: dict) -> None: ...

    def fail(self, lease: TaskLease, execution_id: str, *, error_code: str) -> None: ...


class ToolCallLedger(Protocol):
    def binding(self, project_key: str, key: str) -> ToolBindingRecord: ...

    def start_call(self, lease: TaskLease, node_id: str, *, tool_key: str, tool_version: str, tool_hash: str,
                   binding_key: str, handler_key: str, handler_version: str, input_hash: str) -> str | None: ...

    def finish_call(self, lease: TaskLease, call_id: str, *, status: str,
                    output_hash: str | None = None, error_code: str | None = None) -> bool: ...


class WorkflowCheckpoints(Protocol):
    def begin(self, lease: TaskLease, node_key: str, input_hash: str, *,
              interpreter_profile: str, checkpoint_profile: str,
              scope: CodeNodeScope | None = None, parent_node_id: str | None = None) -> WorkflowCheckpoint | None: ...

    def finish(self, lease: TaskLease, node_id: str, *, status: str,
               output: dict | None = None, error_code: str | None = None) -> bool: ...


class ExecutionPrompts(Protocol):
    def render(self, project_key: str, key: str, version: str, variables: dict) -> dict: ...
