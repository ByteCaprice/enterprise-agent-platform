"""Live execution authorization, independent of Worker scheduling and HTTP tokens.

The persisted root Task defines project and submitter. Agent invocations inherit
that authority; passing an Agent key never creates a new principal or project.
Resource grants are rechecked at use and cannot be inferred from publication.
"""

from __future__ import annotations

from domain.agent_invocation import AgentInvocation
from domain.execution_authorization import AdministratorKeyReader, GovernanceExecutionReader
from domain.task_execution import TaskClaim
from domain.workflow_invocation import WorkflowInvocation


class ExecutionAuthorizer:
    def __init__(self, governance: GovernanceExecutionReader, api_keys: AdministratorKeyReader):
        self.governance = governance
        self.api_keys = api_keys

    def allowed(self, claim: TaskClaim | AgentInvocation | WorkflowInvocation) -> bool:
        return self.allowed_resource(claim, "task:submit", claim.task.capability_key) and all(
            self.allowed_resource(claim, "workflow:invoke", reference.key)
            for reference in getattr(claim, "delegated_workflows", ())
        )

    def allowed_resource(self, claim: TaskClaim | AgentInvocation | WorkflowInvocation, action: str, resource_key: str) -> bool:
        project = claim.task.project_key
        if not self.governance.get_project(project)["enabled"]:
            return False
        owner = claim.task.submitted_by
        if owner.startswith("service:"):
            identity_id = owner.removeprefix("service:")
            if not identity_id:
                return False
            resources = self.governance.execution_resources(project, identity_id, action)
            return "*" in resources or resource_key in resources
        if owner.startswith("admin:"):
            name = owner.removeprefix("admin:")
            if not name:
                return False
            # Query only the root administrator; never scan every stored Key on
            # every node, model turn or heartbeat. No bearer secret is required.
            key = self.api_keys.get(name)
            return key is not None and key.enabled and bool({"admin", "*"} & set(key.scopes))
        return False
