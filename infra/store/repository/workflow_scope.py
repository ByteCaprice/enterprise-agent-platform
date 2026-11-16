"""Pinned Workflow frame/activity lookup, under the caller's Task lock.

No execution, authorization, JSON Schema subprocess, or external IO occurs here.
The helper is shared by all subordinate evidence admission adapters.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

from sqlalchemy import select

from domain.business_code import (
    CODE_WORKFLOW_RESULT_KEY,
    MAX_CODE_FRAME_DEPTH,
    CodeNodeScope,
    CodeWorkflowDefinition,
)
from domain.capability_definitions import interpret_definition
from infra.store.model import AiCapability, AiCapabilityVersion, AiWorkflowNode


def code_node_scope(value) -> CodeNodeScope:
    if isinstance(value, CodeNodeScope):
        return value
    if not isinstance(value, dict):
        raise ValueError("Code Workflow scope must be an object")
    return CodeNodeScope(**value)


def activity_node_key(parent_id: str, activity_key: str, invocation_hash: str) -> str:
    return "call." + hashlib.sha256(f"{parent_id}\0{activity_key}\0{invocation_hash}".encode()).hexdigest()


def result_node_key(parent_id: str | None) -> str:
    return (CODE_WORKFLOW_RESULT_KEY if parent_id is None else
            "result." + hashlib.sha256(parent_id.encode()).hexdigest())


def _identity(scope):
    return scope.workflow_key, scope.version, scope.content_hash


def _artifact(session, task, scope=None):
    query = select(AiCapability, AiCapabilityVersion).join(
        AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id,
    ).where(AiCapability.project_key == task.project_key, AiCapability.kind == "workflow")
    if scope is None:
        query = query.where(AiCapability.id == task.capability_id, AiCapabilityVersion.version == task.version)
    else:
        query = query.where(AiCapability.key == scope.workflow_key, AiCapabilityVersion.version == scope.version)
    found = session.execute(query).one_or_none()
    if found is None:
        raise ValueError("Pinned Workflow artifact is unavailable")
    capability, row = found
    artifact = row.to_domain()
    artifact.require_published(
        capability_id=capability.id, version=task.version if scope is None else scope.version,
        content_hash=task.content_hash if scope is None else scope.content_hash,
    )
    return capability, artifact, interpret_definition("workflow", artifact).materialize()


class DeclaredWorkflowActivity(NamedTuple):
    definition: object
    frame_artifact: object
    reference: object


def _parent(session, task, node):
    parent = session.scalar(select(AiWorkflowNode).where(
        AiWorkflowNode.id == node.parent_node_id, AiWorkflowNode.task_id == task.id,
        AiWorkflowNode.task_attempt_id == node.task_attempt_id,
    ))
    if parent is None or parent.status != "running":
        raise ValueError("Code Workflow parent must be running in the same Attempt")
    return parent


def validate_code_node(session, task, node, *, _ancestors=()):
    """Validate a proposed or durable node and its full pinned ancestry.

    Returns (declared activity or None for a frame, frame artifact, definition).
    A frame's depth counts Workflow frames, not the alternating activity rows.
    """
    if node.id in _ancestors or len(_ancestors) >= MAX_CODE_FRAME_DEPTH * 2:
        raise ValueError("Code Workflow ancestry exceeds its bound")
    scope = code_node_scope(node.scope)
    capability, artifact, definition = _artifact(session, task, scope)
    if not isinstance(definition, CodeWorkflowDefinition):
        raise ValueError("Code scope requires a code Workflow artifact")
    ancestors = (*_ancestors, node.id)
    if scope.activity_key is None:
        if node.kind != "result" or node.node_key != result_node_key(node.parent_node_id):
            raise ValueError("Code Workflow frame requires its deterministic result key")
        if node.parent_node_id is None:
            if (capability.id, scope.version, scope.content_hash) != (
                task.capability_id, task.version, task.content_hash,
            ):
                raise ValueError("Root frame does not match the Task artifact")
        else:
            parent = _parent(session, task, node)
            declared, _, _ = validate_code_node(session, task, parent, _ancestors=ancestors)
            if (parent.kind != "workflow" or declared is None or declared.capability is None or
                    (declared.capability.key, declared.capability.version) != (scope.workflow_key, scope.version)):
                raise ValueError("Nested frame does not match its declared pinned Workflow")
        return None, artifact, definition
    if node.parent_node_id is None:
        raise ValueError("Code activities require a result frame parent")
    parent = _parent(session, task, node)
    parent_scope = code_node_scope(parent.scope)
    if parent.kind != "result" or parent_scope.activity_key is not None or _identity(parent_scope) != _identity(scope):
        raise ValueError("Code activity does not belong to its parent frame")
    validate_code_node(session, task, parent, _ancestors=ancestors)
    declared = next((item for item in definition.activities if item.key == scope.activity_key), None)
    if declared is None or node.kind != declared.kind or node.node_key != activity_node_key(
        parent.id, scope.activity_key, scope.invocation_hash,
    ):
        raise ValueError("Code activity does not match its declared deterministic invocation")
    if declared.capability is not None and declared.capability.model_dump() not in [
        dependency.model_dump() for dependency in artifact.dependencies
    ]:
        raise ValueError("Code activity resource must be a pinned dependency")
    if declared.prompt is not None and declared.prompt.model_dump() not in [
        dependency.model_dump() for dependency in artifact.dependencies
    ]:
        raise ValueError("Code activity Prompt must be a pinned dependency")
    return declared, artifact, definition


def declared_workflow_activity(session, task, node):
    """Return (validated declaration, pinned frame artifact, capability ref).

    Code uses the durable scope and parent tree; DSL continues to use its root
    definition/node key and must not carry code-only scope or parent metadata.
    """
    if node.scope is not None:
        declared, artifact, _ = validate_code_node(session, task, node)
        if declared is None:
            raise ValueError("A result frame is not a declared resource activity")
    else:
        if node.parent_node_id is not None:
            raise ValueError("Legacy Workflow nodes cannot have a code parent")
        _, artifact, definition = _artifact(session, task)
        if isinstance(definition, CodeWorkflowDefinition):
            raise ValueError("Code Workflow activities require a scope")
        declared = next((item for item in definition.nodes if item.key == node.node_key), None)
        if declared is None or declared.kind != node.kind:
            raise ValueError("Workflow node does not match its declaration")
        if declared.capability is not None and declared.capability.model_dump() not in [
            dependency.model_dump() for dependency in artifact.dependencies
        ]:
            raise ValueError("Workflow resource must be a pinned dependency")
    return DeclaredWorkflowActivity(declared, artifact, declared.capability)
