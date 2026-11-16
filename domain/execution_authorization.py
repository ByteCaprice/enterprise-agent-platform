"""Narrow ports for live authority inherited from the root execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from domain.agent_invocation import AgentInvocation
from domain.api_key_config import ApiKeyConfig
from domain.task_execution import TaskClaim
from domain.workflow_invocation import WorkflowInvocation


class ExecutionAuthorizationPort(Protocol):
    def allowed(self, claim: TaskClaim | AgentInvocation | WorkflowInvocation) -> bool: ...

    def allowed_resource(self, claim: TaskClaim | AgentInvocation | WorkflowInvocation, action: str, resource_key: str) -> bool: ...


class GovernanceExecutionReader(Protocol):
    """Read current project state and active service-identity resource grants."""

    def get_project(self, project_key: str) -> Mapping[str, Any]: ...

    def execution_resources(self, project_key: str, identity_id: str, action: str) -> set[str]: ...


class AdministratorKeyReader(Protocol):
    """Look up only the named root administrator, without enumerating keys."""

    def get(self, name: str) -> ApiKeyConfig | None: ...
