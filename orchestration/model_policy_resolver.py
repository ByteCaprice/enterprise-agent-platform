"""Shared model governance, independent of completion and Agent-turn transports."""

from dataclasses import dataclass
from typing import TypeVar

from domain.agent_invocation import AgentInvocation
from domain.capability import (
    AgentTurnPolicyDefinition,
    CapabilityDependency,
    CapabilityKind,
    CapabilityVersionRecord,
    ModelPolicyDefinition,
)
from domain.execution_ports import ExecutionResources, ModelProviderReader
from domain.platform_model import ModelProviderRecord
from domain.task_execution import ExecutionFailure, TaskClaim
from orchestration.published_capabilities import PublishedCapabilities


@dataclass(frozen=True)
class ResolvedModelPolicy:
    artifact: CapabilityVersionRecord
    policy: ModelPolicyDefinition
    provider: ModelProviderRecord


@dataclass(frozen=True)
class ResolvedAgentModelPolicy:
    artifact: CapabilityVersionRecord
    policy: AgentTurnPolicyDefinition
    provider: ModelProviderRecord


_Policy = TypeVar("_Policy", ModelPolicyDefinition, AgentTurnPolicyDefinition)


class ModelPolicyResolver:
    """Live access, pinned policies and installed Provider/catalog constraints."""

    def __init__(self, resources: ExecutionResources, providers: ModelProviderReader):
        self.resources = resources
        self.providers = providers
        self.authorizer = resources.authorization
        self.catalog = PublishedCapabilities(resources.capabilities)

    def _authorize(self, claim, policy_key, prompt=None):
        if not (self.authorizer.allowed(claim) and self.authorizer.allowed_resource(claim, "model:invoke", policy_key)):
            raise ExecutionFailure("MODEL_ACCESS_DENIED")
        if prompt is not None and not self.authorizer.allowed_resource(claim, "prompt:render", prompt.key):
            raise ExecutionFailure("MODEL_PROMPT_ACCESS_DENIED")

    def resolve_policy(self, claim: TaskClaim, reference: CapabilityDependency,
                       prompt: CapabilityDependency | None = None) -> ResolvedModelPolicy:
        artifact, policy, provider = self._resolve_policy(claim, reference, prompt, ModelPolicyDefinition)
        return ResolvedModelPolicy(artifact, policy, provider)

    def resolve_agent_policy(self, claim: TaskClaim | AgentInvocation, reference: CapabilityDependency,
                             prompt: CapabilityDependency) -> ResolvedAgentModelPolicy:
        artifact, policy, provider = self._resolve_policy(claim, reference, prompt, AgentTurnPolicyDefinition)
        return ResolvedAgentModelPolicy(artifact, policy, provider)

    def _resolve_policy(self, claim, reference, prompt, policy_type: type[_Policy]
                        ) -> tuple[CapabilityVersionRecord, _Policy, ModelProviderRecord]:
        try:
            self._authorize(claim, reference.key, prompt)
            artifact = self.catalog.resolve(claim.task.project_key, reference, CapabilityKind.MODEL_POLICY)
            policy = policy_type.model_validate(artifact.definition)
            provider = self.providers.provider(claim.task.project_key, policy.provider_key)
            if not provider.enabled:
                raise ExecutionFailure("MODEL_PROVIDER_DISABLED")
            candidates = [policy.model, *(policy.fallback_models if isinstance(policy, ModelPolicyDefinition) else [])]
            if not set(candidates) <= set(provider.models) or policy.max_tokens > provider.max_tokens:
                raise ExecutionFailure("MODEL_CATALOG_DENIED")
            if policy.credential_ref is not None and policy.credential_ref != provider.credential_ref:
                raise ExecutionFailure("MODEL_CREDENTIAL_REFERENCE_DENIED")
            return artifact, policy, provider
        except ExecutionFailure:
            raise
        except Exception:
            raise ExecutionFailure("MODEL_CONFIGURATION_UNAVAILABLE") from None
