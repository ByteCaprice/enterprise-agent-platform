"""Pure Task completion rules and the narrow persistence port used by Workers.

The database adapter supplies facts under the live execution fence. This module
decides whether those facts support success; it never opens a transaction, calls
a validator process, creates delivery work, or interprets an ORM object.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from domain.business_code import (
    CODE_WORKFLOW_RESULT_KEY,
    MAX_CODE_FRAME_DEPTH,
    MAX_CODE_NODES,
    CodeNodeScope,
    CodeWorkflowDefinition,
    is_activity_unavailable,
)
from domain.capability import CapabilityVersionRecord
from domain.capability_definitions import interpret_definition
from domain.task import canonical_json
from domain.task_execution import CompletionOutcome, TaskLease


@dataclass(frozen=True)
class CompletionCommand:
    """Detached result bytes prevent mutation between validation and commit."""

    status: str
    result_json: str | None = field(default=None, repr=False)
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "timed_out"}:
            raise ValueError("Invalid terminal status")
        if self.status == "succeeded":
            if self.result_json is None or self.error_code is not None:
                raise ValueError("Successful tasks require a result and no error code")
            result = json.loads(self.result_json)
            if not isinstance(result, dict) or canonical_json(result) != self.result_json:
                raise ValueError("Successful result must be a canonical JSON object")
        elif self.result_json is not None:
            raise ValueError("Failure result is not accepted")
        elif self.error_code is None:
            object.__setattr__(self, "error_code", "TASK_TIMED_OUT" if self.status == "timed_out" else "TASK_FAILED")
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", self.error_code) is None
        ):
            raise ValueError("Invalid error_code")

    @classmethod
    def prepare(cls, *, status: str, result: dict | None = None, error_code: str | None = None) -> CompletionCommand:
        if status == "succeeded" and not isinstance(result, dict):
            raise ValueError("Successful result must be an object")
        return cls(status, canonical_json(result) if result is not None else None, error_code)

    @property
    def result(self) -> dict | None:
        return json.loads(self.result_json) if self.result_json is not None else None

    @property
    def output_hash(self) -> str | None:
        return hashlib.sha256(self.result_json.encode()).hexdigest() if self.result_json is not None else None


@dataclass(frozen=True)
class CompletionTarget:
    capability_id: str
    capability_kind: str
    version: str
    content_hash: str
    capability_key: str | None = None


@dataclass(frozen=True)
class NodeCompletionEvidence:
    id: str
    key: str
    kind: str
    status: str
    output_hash: str | None
    parent_node_id: str | None = None
    scope: CodeNodeScope | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class AgentCompletionEvidence:
    capability_id: str
    key: str
    version: str
    content_hash: str
    output_hash: str | None
    parent_node_id: str | None
    status: str = "succeeded"
    error_code: str | None = None


@dataclass(frozen=True)
class CompletionEvidence:
    running_model_call: bool = False
    running_tool_call: bool = False
    unfinished_agent: bool = False
    unfinished_agent_step: bool = False
    nodes: tuple[NodeCompletionEvidence, ...] = ()
    agents: tuple[AgentCompletionEvidence, ...] = ()
    running_agent: bool = False
    running_agent_step: bool = False
    workflow_artifacts: tuple[tuple[str, CapabilityVersionRecord], ...] = ()


def _require_code_completion(
    target: CompletionTarget, artifact: CapabilityVersionRecord,
    command: CompletionCommand, evidence: CompletionEvidence,
) -> None:
    """Validate dynamic receipts against published frames, not reconstructed DAGs."""
    if not evidence.nodes or len(evidence.nodes) > MAX_CODE_NODES:
        raise ValueError("Code Workflow evidence exceeds node bounds")
    nodes = {node.id: node for node in evidence.nodes}
    if len(nodes) != len(evidence.nodes):
        raise ValueError("Code Workflow node identities must be unique")
    roots = [node for node in evidence.nodes if node.parent_node_id is None]
    if len(roots) != 1:
        raise ValueError("Code Workflow requires exactly one root result frame")
    root = roots[0]
    if not isinstance(root.scope, CodeNodeScope):
        raise ValueError("Code Workflow requires scoped result evidence")
    root_key = target.capability_key if target.capability_key is not None else root.scope.workflow_key
    if (root.scope.workflow_key, root.scope.version, root.scope.content_hash, root.status, root.output_hash) != (
        root_key, target.version, target.content_hash, "succeeded", command.output_hash,
    ):
        raise ValueError("Code Workflow root result must match its published Task and output")

    artifacts = {}
    definitions = {}
    for key, published in ((root_key, artifact), *evidence.workflow_artifacts):
        published.require_published()
        identity = (key, published.version, published.content_hash)
        if identity in artifacts and artifacts[identity].capability_id != published.capability_id:
            raise ValueError("Conflicting workflow artifact identities")
        if any(existing[:2] == identity[:2] and existing != identity for existing in artifacts):
            raise ValueError("Conflicting workflow artifact hashes")
        definition = interpret_definition("workflow", published).materialize()
        if not isinstance(definition, CodeWorkflowDefinition):
            raise ValueError("Nested Workflow frames require published code Workflow artifacts")
        artifacts[identity] = published
        definitions[identity] = definition

    descendants: dict[str, list[NodeCompletionEvidence]] = {}
    for node in evidence.nodes:
        scope = node.scope
        if not isinstance(scope, CodeNodeScope):
            raise ValueError("Code Workflow nodes require a scope")
        # Revalidate detached scope values rather than trusting constructed DTOs.
        CodeNodeScope(scope.workflow_key, scope.version, scope.content_hash, scope.activity_key, scope.invocation_hash)
        if node.parent_node_id is not None:
            if node.parent_node_id not in nodes:
                raise ValueError("Code Workflow contains an orphan node")
            descendants.setdefault(node.parent_node_id, []).append(node)

    agent_children: dict[str, list[AgentCompletionEvidence]] = {}
    for agent in evidence.agents:
        if agent.parent_node_id not in nodes or nodes[agent.parent_node_id].kind != "agent":
            raise ValueError("Code Workflow cannot contain orphan or extra Agent executions")
        agent_children.setdefault(agent.parent_node_id, []).append(agent)

    def identity(scope: CodeNodeScope) -> tuple[str, str, str]:
        return scope.workflow_key, scope.version, scope.content_hash

    def safe_failure(status: str, code: str | None) -> bool:
        return ((status in {"failed", "timed_out"} and is_activity_unavailable(code)) or
                (status == "cancelled" and code == "CODE_ACTIVITY_GROUP_CANCELLED"))

    visited: set[str] = set()

    def frame(result: NodeCompletionEvidence, depth: int) -> None:
        if depth > MAX_CODE_FRAME_DEPTH or result.id in visited:
            raise ValueError("Code Workflow frame depth or cycle is invalid")
        visited.add(result.id)
        scope = result.scope
        expected_result_key = (CODE_WORKFLOW_RESULT_KEY if result.parent_node_id is None else
                               "result." + hashlib.sha256(result.parent_node_id.encode("utf-8")).hexdigest())
        if (result.key != expected_result_key or result.kind != "result" or
                scope.activity_key is not None or identity(scope) not in definitions):
            raise ValueError("Code Workflow result requires its actual published frame")
        successful = result.status == "succeeded"
        if successful:
            if result.error_code is not None or result.output_hash is None:
                raise ValueError("Successful frame must carry only its output hash")
        elif not safe_failure(result.status, result.error_code) or result.output_hash is not None:
            raise ValueError("Failed child frame cannot hide control or infrastructure failures")
        definition = definitions[identity(scope)]
        declared = {activity.key: activity for activity in definition.activities}
        counts: dict[str, int] = {}
        covered: set[str] = set()
        invocations: set[tuple[str, str]] = set()
        for node in descendants.get(result.id, ()):
            if node.id in visited:
                raise ValueError("Code Workflow contains a cycle")
            visited.add(node.id)
            node_scope = node.scope
            if (identity(node_scope) != identity(scope) or node_scope.activity_key not in declared or
                    node_scope.invocation_hash is None):
                raise ValueError("Activity must belong to its parent published frame")
            activity = declared[node_scope.activity_key]
            if node.kind != activity.kind:
                raise ValueError("Activity evidence kind differs from its declaration")
            invocation_identity = (activity.key, node_scope.invocation_hash)
            if invocation_identity in invocations:
                raise ValueError("Activity invocation hashes must be unique per activity within a frame")
            invocations.add(invocation_identity)
            expected_call_key = "call." + hashlib.sha256(
                (result.id + "\0" + activity.key + "\0" + node_scope.invocation_hash).encode("utf-8"),
            ).hexdigest()
            if node.key != expected_call_key:
                raise ValueError("Activity receipt key must match its parent frame and invocation identity")
            counts[activity.key] = counts.get(activity.key, 0) + 1
            if counts[activity.key] > activity.max_calls:
                raise ValueError("Activity exceeds its declared max_calls")
            if node.status == "succeeded":
                if node.error_code is not None or node.output_hash is None:
                    raise ValueError("Successful activity requires its output and no error")
            elif node.status == "skipped":
                if activity.required or node.output_hash is not None or node.error_code is not None:
                    raise ValueError("Only optional activities may be skipped")
            elif node.status == "unavailable":
                if (activity.kind == "step" or activity.on_unavailable != "continue" or not is_activity_unavailable(node.error_code) or
                        node.output_hash is not None):
                    raise ValueError("Activity unavailable requires explicitly permitted operational failure")
            elif successful or not safe_failure(node.status, node.error_code) or node.output_hash is not None:
                raise ValueError("Activity must be terminal without hidden control failures")
            if node.status != "skipped":
                covered.add(activity.key)
            children = descendants.get(node.id, ())
            if activity.kind == "workflow":
                if (len(children) > 1 or (node.status == "succeeded" and len(children) != 1) or
                        (node.status == "skipped" and children)):
                    raise ValueError("Workflow activity requires exactly its matching child frame")
                if children:
                    child = children[0]
                    reference = activity.capability
                    if ((child.scope.workflow_key, child.scope.version) != (reference.key, reference.version) or
                            (node.status == "succeeded" and (child.status != "succeeded" or child.output_hash != node.output_hash)) or
                            (node.status != "succeeded" and (child.status == "succeeded" or child.error_code != node.error_code))):
                        raise ValueError("Workflow activity and child frame disagree")
                    frame(child, depth + 1)
            elif children:
                raise ValueError("Only Workflow activities can have child frames")
            if activity.kind == "agent":
                agents = agent_children.get(node.id, ())
                if (len(agents) > 1 or (node.status == "succeeded" and len(agents) != 1) or
                        (node.status == "skipped" and agents)):
                    raise ValueError("Agent activity requires its matching terminal child")
                for agent in agents:
                    reference = activity.capability
                    if (agent.key, agent.version) != (reference.key, reference.version):
                        raise ValueError("Agent activity child differs from its pinned reference")
                    if node.status == "succeeded":
                        if (agent.status, agent.output_hash, agent.error_code) != ("succeeded", node.output_hash, None):
                            raise ValueError("Agent activity must match its completed child output")
                    elif node.status == "cancelled":
                        # A failed frame may cancel a sibling after its Agent has
                        # already completed or safely closed its own execution.
                        completed = (agent.status == "succeeded" and agent.output_hash is not None and
                                     agent.error_code is None)
                        cancelled = (agent.status == "failed" and agent.output_hash is None and
                                     agent.error_code == "AGENT_EXECUTION_CANCELLED")
                        settled = (agent.status == "failed" and agent.output_hash is None and
                                   is_activity_unavailable(agent.error_code.removeprefix("AGENT_")
                                                           if isinstance(agent.error_code, str) else None))
                        if not (completed or cancelled or settled):
                            raise ValueError("Cancelled Agent activity requires safely terminal child evidence")
                    else:
                        agent_error = agent.error_code
                        # Agent storage prefixes model/tool failures for its closed
                        # error namespace. Normalize only that single known wrapper;
                        # authorization/control errors never acquire operational meaning.
                        if isinstance(agent_error, str) and agent_error.startswith(("AGENT_MODEL_", "AGENT_TOOL_")):
                            original = agent_error.removeprefix("AGENT_")
                            if is_activity_unavailable(original):
                                agent_error = original
                        if (not safe_failure(agent.status, agent_error) or
                                agent_error != node.error_code or agent.output_hash is not None):
                            raise ValueError("Unavailable Agent requires its safely failed terminal child")
        if successful and any(activity.required and activity.key not in covered for activity in definition.activities):
            raise ValueError("Successful frame requires every required activity")

    frame(root, 1)
    if len(visited) != len(nodes):
        raise ValueError("Code Workflow contains orphan or cyclic descendant evidence")


def require_completion_evidence(
    target: CompletionTarget,
    artifact: CapabilityVersionRecord,
    command: CompletionCommand,
    evidence: CompletionEvidence,
) -> None:
    """Shared, storage-independent success policy; schema matching is a port."""
    if command.status != "succeeded":
        return
    artifact.require_published(
        capability_id=target.capability_id, version=target.version, content_hash=target.content_hash,
    )
    if evidence.running_model_call or evidence.running_tool_call:
        raise ValueError("Task cannot succeed with a running external call")
    if evidence.running_agent or evidence.running_agent_step:
        raise ValueError("Task cannot succeed with running Agent evidence")
    code_workflow = target.capability_kind == "workflow" and artifact.definition.get("definition_version") == "workflow.code.v1"
    if not code_workflow and (evidence.unfinished_agent or evidence.unfinished_agent_step):
        raise ValueError("Task cannot succeed with unfinished Agent evidence")
    if target.capability_kind == "agent":
        if artifact.definition.get("definition_version") != "agent.v1":
            raise ValueError("Unsupported Agent completion contract")
        roots = [agent for agent in evidence.agents if agent.parent_node_id is None]
        if len(roots) != 1:
            raise ValueError("Agent Task requires exactly one completed root execution")
        root = roots[0]
        if root.status != "succeeded" or root.error_code is not None:
            raise ValueError("Agent Task requires a successfully completed root execution")
        if (root.capability_id, root.version, root.content_hash, root.output_hash) != (
            target.capability_id, target.version, target.content_hash, command.output_hash,
        ):
            raise ValueError("Agent Task result must match its completed root execution")
    elif target.capability_kind == "workflow":
        definition = interpret_definition("workflow", artifact).materialize()
        if isinstance(definition, CodeWorkflowDefinition):
            _require_code_completion(target, artifact, command, evidence)
            return
        else:
            declared = {node.key: node for node in definition.nodes}
            completed = {node.key: node.kind for node in evidence.nodes if node.status in {"succeeded", "skipped"}}
            if completed != {key: node.kind for key, node in declared.items()} or len(evidence.nodes) != len(declared):
                raise ValueError("Workflow task requires exactly its completed declared nodes")
        for node in evidence.nodes:
            if node.kind != "agent" or node.status != "succeeded":
                continue
            children = [agent for agent in evidence.agents if agent.parent_node_id == node.id]
            reference = declared[node.key].capability
            if len(children) != 1 or reference is None:
                raise ValueError("Workflow Agent node requires its completed child execution")
            child = children[0]
            if (child.key, child.version, child.output_hash, child.status, child.error_code) != (
                reference.key, reference.version, node.output_hash, "succeeded", None,
            ):
                raise ValueError("Workflow Agent node requires its matching completed execution")
    else:
        raise ValueError("Unsupported Task completion contract")


class TaskCompletionPort(Protocol):
    """One authoritative fenced commit, not permission to retry execution.

    TaskValidationUnavailable guarantees that no terminal write committed. Only
    that classified failure permits the caller to attempt failure settlement
    under the same live lease; an unknown commit outcome must never be retried.
    """

    def complete(
        self, lease: TaskLease, *, status: str, result: dict | None = None, error_code: str | None = None,
    ) -> CompletionOutcome: ...
