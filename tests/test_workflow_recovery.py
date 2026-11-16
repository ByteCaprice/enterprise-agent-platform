import hashlib
import json
from datetime import UTC, datetime

from domain.capability import CapabilityDependency, CapabilityVersionDraft
from domain.task import TaskRecord, TaskStatus
from framework.workflow_recovery import assess_workflow_recovery


def _draft(definition, *, dependencies=()):
    return CapabilityVersionDraft(version="1", definition=definition, dependencies=list(dependencies))


def _source(draft, *, kind="workflow", status=TaskStatus.FAILED):
    data = draft.model_dump(mode="json", include=set(CapabilityVersionDraft.model_fields))
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    return TaskRecord(
        id="task-1",
        project_key="project",
        capability_key="flow",
        capability_kind=kind,
        version="1",
        content_hash=digest,
        status=status,
        submitted_by="owner",
        create_time=datetime.now(UTC),
        finish_time=datetime.now(UTC),
    ), digest


def test_pure_rule_workflow_is_assessed_as_new_task_only():
    draft = _draft({"nodes": [{"key": "check", "kind": "rule", "rule": {"operation": "project"}}]})
    source, digest = _source(draft)

    result = assess_workflow_recovery(source, draft, digest)

    assert result.eligible and result.reason == "eligible"
    assert result.mode == "new_task" and result.requires_runtime_validation is True
    assert "input" not in result.model_dump() and "mapping" not in result.model_dump()


def test_agent_workflow_or_hash_mismatch_is_closed():
    draft = _draft(
        {"nodes": [{"key": "agent", "kind": "agent", "capability": {"key": "agent", "version": "1"}}]},
        dependencies=(CapabilityDependency(key="agent", version="1"),),
    )
    source, digest = _source(draft)

    unsupported = assess_workflow_recovery(source, draft, digest)
    mismatched = assess_workflow_recovery(source, draft, "0" * 64)

    assert not unsupported.eligible and unsupported.reason == "unsupported_node_kind"
    assert not mismatched.eligible and mismatched.reason == "artifact_hash_mismatch"
