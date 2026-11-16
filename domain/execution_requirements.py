"""Closed execution semantics and an immutable installed ABI manifest.

Artifact data selects versioned semantics, never an import path or the profile of
whatever happens to be installed. Profiles are ABI contracts, not build IDs:
custom implementations must attest the exact required ABI in their descriptors.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import MappingProxyType

from domain.agent_runtime import AgentRuntimeDescriptor
from domain.business_code import (
    CODE_WORKFLOW_CHECKPOINT_PROFILE,
    CODE_WORKFLOW_INTERPRETER_PROFILE,
    CodeReference,
    CodeWorkflowDefinition,
)
from domain.capability import CapabilityKind, CapabilityVersionDraft, CapabilityVersionRecord
from domain.capability_definitions import (
    WORKFLOW_CHECKPOINT_PROFILE,
    WORKFLOW_INTERPRETER_PROFILE,
    interpret_definition,
)
from domain.platform_agent import AgentDefinition
from domain.workflow import WorkflowDefinition
from domain.workflow_runtime import WorkflowAdapterDescriptor

# V1 definitions have a closed interpretation. Adding another runtime key or DSL
# version requires assigning its semantics here, not accepting a matching name.
AGENT_INTERPRETER_PROFILE = "enterprise-ai.agent.interpreter.v1"
TOOL_INTERPRETER_PROFILE = "enterprise-ai.tool.interpreter.v1"
WORKFLOW_ADAPTER_PROFILES = MappingProxyType({
    "llm": "enterprise-ai.workflow.llm.v1",
    "tool": "enterprise-ai.workflow.tool.v1",
    "agent": "enterprise-ai.workflow.agent.v1",
})
AGENT_RUNTIME_PROFILES = MappingProxyType({"pi": "enterprise-ai.pi.v1"})


@dataclass(frozen=True, order=True)
class ExecutionProfile:
    component: str
    key: str
    profile: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value or len(value) > 200
               for value in (self.component, self.key, self.profile)):
            raise ValueError("Invalid execution profile")


_CLOSED_PROFILES = frozenset({
    ExecutionProfile("interpreter", "workflow", WORKFLOW_INTERPRETER_PROFILE),
    ExecutionProfile("checkpoint", "workflow", WORKFLOW_CHECKPOINT_PROFILE),
    ExecutionProfile("interpreter", "workflow_code", CODE_WORKFLOW_INTERPRETER_PROFILE),
    ExecutionProfile("checkpoint", "workflow_code", CODE_WORKFLOW_CHECKPOINT_PROFILE),
    ExecutionProfile("interpreter", "agent", AGENT_INTERPRETER_PROFILE),
    ExecutionProfile("interpreter", "tool", TOOL_INTERPRETER_PROFILE),
    *(ExecutionProfile("workflow_adapter", key, value) for key, value in WORKFLOW_ADAPTER_PROFILES.items()),
    *(ExecutionProfile("agent_runtime", key, value) for key, value in AGENT_RUNTIME_PROFILES.items()),
})


def _implementation_references(values: Iterable[CodeReference]) -> frozenset[CodeReference]:
    references: dict[tuple[str, str], CodeReference] = {}
    for value in values:
        if not isinstance(value, CodeReference):
            raise ValueError("Code implementations require explicit CodeReference values")
        reference = CodeReference.model_validate(value.model_dump())
        identity = (reference.key, reference.version)
        if identity in references and references[identity] != reference:
            raise ValueError("Conflicting code implementation digests for one key/version")
        references[identity] = reference
    return frozenset(references.values())


@dataclass(frozen=True)
class ExecutionRequirements:
    kind: str
    profiles: frozenset[ExecutionProfile]
    workflow_implementations: frozenset[CodeReference] = frozenset()
    tool_implementations: frozenset[CodeReference] = frozenset()

    def __post_init__(self):
        profiles = frozenset(self.profiles)
        if self.kind not in {"agent", "workflow"} or not profiles <= _CLOSED_PROFILES:
            raise ValueError("Execution requirements must use closed versioned semantics")
        object.__setattr__(self, "profiles", profiles)
        workflow_refs = _implementation_references(self.workflow_implementations)
        tool_refs = _implementation_references(self.tool_implementations)
        _implementation_references(workflow_refs | tool_refs)
        object.__setattr__(self, "workflow_implementations", workflow_refs)
        object.__setattr__(self, "tool_implementations", tool_refs)

    @property
    def implementations(self) -> frozenset[CodeReference]:
        return self.workflow_implementations | self.tool_implementations


@dataclass(frozen=True, init=False)
class InstalledExecutionCapabilities:
    """A sealed manifest, composed from actual installed descriptors at startup.

    Bare executor functions must supply this explicit declaration too. A tool
    interpreter profile describes the governed read-only Tool contract, NOT the
    availability/authorization of a mutable binding or a concrete handler. Those
    remain live gateway checks; Workflow Tool adapters have a separate profile.
    Implementation pins retain their explicit Workflow or Tool execution role.
    """

    executor_kinds: frozenset[str]
    profiles: frozenset[ExecutionProfile]
    workflow_implementations: frozenset[CodeReference] = frozenset()
    tool_implementations: frozenset[CodeReference] = frozenset()

    def __init__(self):
        raise TypeError("Compose installed capabilities with from_descriptors")

    @classmethod
    def from_descriptors(
        cls, executor_kinds: Iterable[str], *,
        workflow_interpreter_profile: str | None = None,
        workflow_checkpoint_profile: str | None = None,
        agent_interpreter_profile: str | None = None,
        tool_interpreter_profile: str | None = None,
        workflow_adapters: Iterable[WorkflowAdapterDescriptor] = (),
        agent_runtimes: Iterable[AgentRuntimeDescriptor] = (),
        additional_profiles: Iterable[ExecutionProfile] = (),
        workflow_implementations: Iterable[CodeReference] = (),
        tool_implementations: Iterable[CodeReference] = (),
    ) -> InstalledExecutionCapabilities:
        kinds = frozenset(executor_kinds)
        if not kinds or not kinds <= {"workflow", "agent"}:
            raise ValueError("Install explicit Agent/Workflow executors")
        profiles = set(additional_profiles)
        if not profiles <= _CLOSED_PROFILES:
            raise ValueError("Additional execution profiles must use closed versioned semantics")
        workflow_refs = _implementation_references(workflow_implementations)
        tool_refs = _implementation_references(tool_implementations)
        _implementation_references(workflow_refs | tool_refs)
        for component, key, value in (
            ("interpreter", "workflow", workflow_interpreter_profile),
            ("checkpoint", "workflow", workflow_checkpoint_profile),
            ("interpreter", "agent", agent_interpreter_profile),
            ("interpreter", "tool", tool_interpreter_profile),
        ):
            if value is not None:
                profiles.add(ExecutionProfile(component, key, value))
        # Revalidate values, including fixed cancellation/retry/event semantics,
        # rather than accepting an already-constructed model without validation.
        adapters = tuple(WorkflowAdapterDescriptor.model_validate(item.model_dump()) for item in workflow_adapters)
        runtimes = tuple(AgentRuntimeDescriptor.model_validate(item.model_dump()) for item in agent_runtimes)
        if len({item.kind for item in adapters}) != len(adapters) or len({item.key for item in runtimes}) != len(runtimes):
            raise ValueError("Duplicate installed execution descriptors")
        if (("workflow" in kinds) != (workflow_interpreter_profile is not None and workflow_checkpoint_profile is not None) or
                ("workflow" not in kinds and (adapters or workflow_interpreter_profile or workflow_checkpoint_profile))):
            raise ValueError("Workflow executors require interpreter/checkpoint descriptors")
        if ("agent" in kinds or runtimes) and agent_interpreter_profile is None:
            raise ValueError("Agent runtimes require an explicit Agent interpreter profile")
        profiles.update(ExecutionProfile("workflow_adapter", item.kind, item.profile) for item in adapters)
        profiles.update(ExecutionProfile("agent_runtime", item.key, item.profile) for item in runtimes)
        manifest = object.__new__(cls)
        object.__setattr__(manifest, "executor_kinds", kinds)
        object.__setattr__(manifest, "profiles", frozenset(profiles))
        object.__setattr__(manifest, "workflow_implementations", workflow_refs)
        object.__setattr__(manifest, "tool_implementations", tool_refs)
        return manifest

    @property
    def implementations(self) -> frozenset[CodeReference]:
        return self.workflow_implementations | self.tool_implementations

    def supports(self, requirements: ExecutionRequirements) -> bool:
        return (requirements.kind in self.executor_kinds and requirements.profiles <= self.profiles and
                requirements.workflow_implementations <= self.workflow_implementations and
                requirements.tool_implementations <= self.tool_implementations)


@dataclass(frozen=True)
class ExecutionArtifact:
    """Identity plus artifact supplied by a project-scoped, pinned reader."""

    capability_id: str
    key: str
    kind: str
    artifact: CapabilityVersionRecord


class ExecutionRequirementsUnavailable(ValueError):
    """Missing, corrupt, oversized or semantically unsupported pinned closure.

    Do not claim or consume an Attempt. The scheduler must continue past this
    candidate; repair or installation can make queued work eligible later.
    """


def required_execution_profiles(
    root: ExecutionArtifact, *, version: str, content_hash: str,
    resolve: Callable[[str, str], ExecutionArtifact | None],
) -> ExecutionRequirements:
    """Pure bounded structural checking; no schema backend, imports or network.

    Only semantically referenced pins participate, including the entire relevant
    embedded-Agent closure. Unused declared dependencies do not install adapters.
    Each visited identity, publication state and content fingerprint is verified.
    The caller must supply locked stored pins immediately before claim mutation.
    """
    try:
        if root.kind not in {"agent", "workflow"}:
            raise ExecutionRequirementsUnavailable("Not an executable root")
        root.artifact.require_published(capability_id=root.capability_id, version=version, content_hash=content_hash)
        profiles: set[ExecutionProfile] = set()
        workflow_implementations: set[CodeReference] = set()
        tool_implementations: set[CodeReference] = set()
        visited: dict[tuple[str, str], tuple[str, str]] = {}
        pending = [(root, None)]
        total_bytes = 0
        while pending:
            source, expected = pending.pop()
            artifact = source.artifact
            identity = (source.key, artifact.version)
            artifact.require_published(capability_id=source.capability_id)
            if expected is not None and source.kind != expected.kind:
                raise ExecutionRequirementsUnavailable("Pinned dependency kind changed")
            contract = interpret_definition(source.kind, artifact)
            if expected is not None and (
                (expected.role == "completion" and contract.profile != "enterprise-ai.model_policy.completion.v1") or
                (expected.role == "agent_turn" and contract.profile != "enterprise-ai.model_policy.agent-turn.v1") or
                (expected.role == "code_workflow" and contract.profile != CODE_WORKFLOW_INTERPRETER_PROFILE)
            ):
                raise ExecutionRequirementsUnavailable("Pinned model policy semantics changed")
            if identity in visited:
                if visited[identity] != (source.capability_id, artifact.content_hash):
                    raise ExecutionRequirementsUnavailable("Inconsistent pinned identity")
                continue
            visited[identity] = (source.capability_id, artifact.content_hash)
            # Match publication fingerprints, not the task-input 1 MB limit.
            # require_published above also verifies finite JSON and valid text.
            data = artifact.model_dump(mode="json", include=set(CapabilityVersionDraft.model_fields))
            total_bytes += len(json.dumps(
                data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            ).encode("utf-8"))
            if len(visited) > 1024 or total_bytes > 8_000_000:
                raise ExecutionRequirementsUnavailable("Pinned execution closure exceeds inspection bounds")
            definition = contract.materialize()
            if isinstance(definition, CodeWorkflowDefinition):
                profiles.add(ExecutionProfile("interpreter", "workflow_code", contract.profile))
                profiles.add(ExecutionProfile("checkpoint", "workflow_code", CODE_WORKFLOW_CHECKPOINT_PROFILE))
                profiles.update(ExecutionProfile("workflow_adapter", node.kind, WORKFLOW_ADAPTER_PROFILES[node.kind])
                                for node in definition.activities if node.kind in WORKFLOW_ADAPTER_PROFILES)
                workflow_implementations.add(definition.implementation)
            elif isinstance(definition, WorkflowDefinition):
                profiles.add(ExecutionProfile("interpreter", "workflow", contract.profile))
                profiles.add(ExecutionProfile("checkpoint", "workflow", WORKFLOW_CHECKPOINT_PROFILE))
                profiles.update(ExecutionProfile("workflow_adapter", node.kind, WORKFLOW_ADAPTER_PROFILES[node.kind])
                                for node in definition.nodes if node.kind != "rule")
            elif isinstance(definition, AgentDefinition):
                profiles.add(ExecutionProfile("interpreter", "agent", contract.profile))
                runtime_profile = AGENT_RUNTIME_PROFILES.get(definition.runtime)
                if runtime_profile is None:
                    raise ExecutionRequirementsUnavailable("Runtime key has no closed semantic profile")
                profiles.add(ExecutionProfile("agent_runtime", definition.runtime, runtime_profile))
            elif contract.kind == CapabilityKind.TOOL:
                profiles.add(ExecutionProfile("interpreter", "tool", contract.profile))
                if definition.implementation is not None:
                    tool_implementations.add(definition.implementation)
            for reference in contract.references:
                target = resolve(reference.key, reference.version)
                if target is None or (target.key, target.artifact.version) != (reference.key, reference.version):
                    raise ExecutionRequirementsUnavailable("Pinned dependency is unavailable")
                pending.append((target, reference))
        return ExecutionRequirements(
            root.kind, frozenset(profiles), workflow_implementations=frozenset(workflow_implementations),
            tool_implementations=frozenset(tool_implementations),
        )
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, ExecutionRequirementsUnavailable):
            raise
        raise ExecutionRequirementsUnavailable("Pinned execution requirements are unavailable") from None
