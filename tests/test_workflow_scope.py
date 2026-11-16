"""Small offline checks for pinned activity/frame evidence; no database or IO."""

import hashlib
import json
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from domain.business_code import (
    CODE_WORKFLOW_CHECKPOINT_PROFILE,
    CODE_WORKFLOW_INTERPRETER_PROFILE,
    CodeActivity,
    CodeNodeScope,
    CodeReference,
    CodeWorkflowDefinition,
)
from domain.capability import CapabilityDependency
from domain.workflow import WorkflowNodeRecord
from infra.store.model import AiWorkflowNode
from infra.store.repository import workflow as checkpoints
from infra.store.repository import workflow_scope as scopes

HASH = "a" * 64
INVOCATION = hashlib.sha256(b"customer-1").hexdigest()


def definition(*activities):
    return CodeWorkflowDefinition(
        definition_version="workflow.code.v1",
        implementation=CodeReference(key="reviewed", version="1", digest=HASH),
        activities=activities or (CodeActivity(key="compute", kind="step"),),
    )


def node(id, scope, *, parent=None, kind="result", key=None):
    return AiWorkflowNode(
        id=id, task_id="task", task_attempt_id="attempt", kind=kind, status="running",
        parent_node_id=parent, scope=asdict(scope), node_key=key or scopes.result_node_key(parent),
        input_hash=HASH, start_time=datetime.now(UTC),
    )


@pytest.fixture
def tree(monkeypatch):
    task = SimpleNamespace(id="task", capability_id="root-cap", version="1", content_hash=HASH)
    definitions = {"root": definition()}
    rows = {}

    def artifact(session, task, scope):
        current = definitions[scope.workflow_key]
        dependencies = [activity.capability for activity in current.activities if activity.capability is not None]
        dependencies.extend(activity.prompt for activity in current.activities if activity.prompt is not None)
        return (SimpleNamespace(id=scope.workflow_key + "-cap"),
                SimpleNamespace(dependencies=dependencies), current)

    def parent(session, task, child):
        found = rows.get(child.parent_node_id)
        if found is None or found.status != "running":
            raise ValueError("parent is not running")
        return found

    monkeypatch.setattr(scopes, "_artifact", artifact)
    monkeypatch.setattr(scopes, "_parent", parent)
    root = node("root-frame", CodeNodeScope("root", "1", HASH))
    rows[root.id] = root
    return task, definitions, rows, root


def test_deterministic_keys_use_exact_framed_recipe():
    expected = hashlib.sha256(f"frame\0activity\0{INVOCATION}".encode()).hexdigest()
    assert scopes.activity_node_key("frame", "activity", INVOCATION) == "call." + expected
    assert len(scopes.activity_node_key("frame", "activity", INVOCATION)) == 69
    assert scopes.result_node_key(None) == "workflow-result"
    assert scopes.result_node_key("parent") == "result." + hashlib.sha256(b"parent").hexdigest()


def test_scope_metadata_roundtrips_without_payload():
    scope = CodeNodeScope("root", "1", HASH, "compute", INVOCATION)
    row = node("activity", scope, parent="frame", kind="step")
    record = row.to_record()
    assert record.scope == scope
    assert record.parent_node_id == "frame"
    assert WorkflowNodeRecord.model_validate(record.model_dump()).scope == scope
    assert "output" not in record.model_dump()


def test_legacy_record_defaults_are_optional():
    record = WorkflowNodeRecord(id="node", task_id="task", task_attempt_id="attempt", node_key="rule",
        kind="rule", status="running", input_hash=HASH, start_time=datetime.now(UTC))
    assert record.parent_node_id is None and record.scope is None


def test_root_must_match_task_and_deterministic_result_key(tree):
    task, _, _, root = tree
    assert scopes.validate_code_node(None, task, root)[0] is None
    task.content_hash = "b" * 64
    with pytest.raises(ValueError, match="Root frame"):
        scopes.validate_code_node(None, task, root)
    task.content_hash = HASH
    root.node_key = "not-the-result"
    with pytest.raises(ValueError, match="deterministic result"):
        scopes.validate_code_node(None, task, root)


def test_activity_uses_its_declared_scope_not_receipt_key(tree):
    task, _, _, root = tree
    scope = CodeNodeScope("root", "1", HASH, "compute", INVOCATION)
    activity = node("call", scope, parent=root.id, kind="step",
                    key=scopes.activity_node_key(root.id, "compute", INVOCATION))
    declared = scopes.declared_workflow_activity(None, task, activity)
    assert declared.definition.key == "compute"
    assert declared.reference is None
    activity.node_key = "compute"
    with pytest.raises(ValueError, match="deterministic invocation"):
        scopes.validate_code_node(None, task, activity)


@pytest.mark.parametrize("mutation", ["terminal_parent", "wrong_kind", "unknown_activity", "no_parent"])
def test_activity_rejects_invalid_parent_or_declaration(tree, mutation):
    task, _, _, root = tree
    scope = CodeNodeScope("root", "1", HASH, "compute", INVOCATION)
    activity = node("call", scope, parent=root.id, kind="step",
                    key=scopes.activity_node_key(root.id, "compute", INVOCATION))
    if mutation == "terminal_parent":
        root.status = "succeeded"
    elif mutation == "wrong_kind":
        activity.kind = "agent"
    elif mutation == "unknown_activity":
        activity.scope["activity_key"] = "unknown"
    else:
        activity.parent_node_id = None
    with pytest.raises(ValueError):
        scopes.validate_code_node(None, task, activity)


def test_nested_frame_must_match_parent_pinned_reference(tree):
    task, definitions, rows, root = tree
    definitions["root"] = definition(CodeActivity(key="nested", kind="workflow",
        capability=CapabilityDependency(key="child", version="1")))
    definitions["child"] = definition()
    activity_scope = CodeNodeScope("root", "1", HASH, "nested", INVOCATION)
    activity = node("workflow-call", activity_scope, parent=root.id, kind="workflow",
                    key=scopes.activity_node_key(root.id, "nested", INVOCATION))
    rows[activity.id] = activity
    child = node("child-frame", CodeNodeScope("child", "1", HASH), parent=activity.id)
    assert scopes.validate_code_node(None, task, child)[0] is None
    child.scope["version"] = "2"
    with pytest.raises(ValueError, match="declared pinned Workflow"):
        scopes.validate_code_node(None, task, child)


def test_scope_rejects_partial_activity_identity_and_unknown_keys():
    with pytest.raises(ValueError):
        scopes.code_node_scope({"workflow_key": "root", "version": "1", "content_hash": HASH,
                                "activity_key": "compute"})
    with pytest.raises(TypeError):
        scopes.code_node_scope({"workflow_key": "root", "version": "1", "content_hash": HASH,
                                "private": "not permitted"})


def test_storage_allows_once_for_two_activities_but_rejects_same_activity_replay(tree, monkeypatch):
    task, definitions, rows, root = tree
    task.origin_task_id = None
    definitions["root"] = definition(CodeActivity(key="first", kind="step"), CodeActivity(key="second", kind="step"))
    session = SimpleNamespace(scalars=lambda query: list(rows.values()), flush=lambda: None,
                              get=lambda model, id: rows[id])

    def execute(query, params=None):
        if params is None:
            return SimpleNamespace(scalar_one_or_none=lambda: None)
        scope = scopes.code_node_scope(json.loads(params["scope"]))
        row = node(params["node_id"], scope, parent=params["parent_node_id"], kind=params["kind"],
                   key=params["node_key"])
        rows[row.id] = row
        return SimpleNamespace(scalar_one_or_none=lambda: row.id)

    session.execute = execute
    repository = checkpoints.WorkflowRepository.__new__(checkpoints.WorkflowRepository)
    repository._sf = SimpleNamespace(begin=lambda: nullcontext(session))
    monkeypatch.setattr(repository, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(checkpoints, "active_execution", lambda *args: (task, SimpleNamespace(id="attempt")))
    monkeypatch.setattr(checkpoints, "_artifact", scopes._artifact)
    lease = SimpleNamespace(task_id=task.id, attempt_id="attempt", worker_id="worker", token="token")
    invocation_hash = hashlib.sha256(b"once").hexdigest()

    def begin(activity):
        return repository.begin(lease, scopes.activity_node_key(root.id, activity, invocation_hash), HASH,
            interpreter_profile=CODE_WORKFLOW_INTERPRETER_PROFILE, checkpoint_profile=CODE_WORKFLOW_CHECKPOINT_PROFILE,
            scope=CodeNodeScope("root", "1", HASH, activity, invocation_hash), parent_node_id=root.id)

    assert begin("first").created
    assert begin("second").created
    with pytest.raises(ValueError, match="cannot be replayed"):
        begin("first")


def test_storage_step_cannot_finish_unavailable_even_with_continue_and_safe_code(tree, monkeypatch):
    task, definitions, _, root = tree
    definitions["root"] = definition(CodeActivity(key="compute", kind="step", on_unavailable="continue"))
    scope = CodeNodeScope("root", "1", HASH, "compute", INVOCATION)
    activity = node("step", scope, parent=root.id, kind="step",
                    key=scopes.activity_node_key(root.id, "compute", INVOCATION))
    session = SimpleNamespace(scalar=lambda query: activity)
    repository = checkpoints.WorkflowRepository.__new__(checkpoints.WorkflowRepository)
    repository._sf = SimpleNamespace(begin=lambda: nullcontext(session))
    monkeypatch.setattr(checkpoints, "active_execution", lambda *args: (task, SimpleNamespace(id="attempt")))
    with pytest.raises(ValueError, match="does not permit classified unavailability"):
        repository.finish(None, activity.id, status="unavailable", error_code="MODEL_UPSTREAM_UNAVAILABLE")
    assert activity.status == "running"
