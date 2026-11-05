"""PostgreSQL persistence and fencing coverage for workflow checkpoints."""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_platform_task_execution_store import enqueue
from test_platform_task_execution_store import inbox as inbox_fixture

from domain.task import TaskNotFound
from infra.store.model import AiWorkflowNode


@pytest.fixture
def workflow_inbox(monkeypatch, pg_url):
    yield from inbox_fixture.__wrapped__(monkeypatch, pg_url)


def workflow_claim(client, store):
    task = enqueue(client, "workflow-checkpoint")
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ai_capability SET kind='workflow' WHERE id=(SELECT capability_id FROM ai_task WHERE id=:task)"
            ),
            {"task": task["id"]},
        )
        connection.execute(
            text(
                "UPDATE ai_capability_version SET definition=:definition WHERE capability_id=(SELECT capability_id FROM ai_task WHERE id=:task) AND version='1'"
            ),
            {
                "task": task["id"],
                "definition": json.dumps(
                    {
                        "nodes": [{"key": "first", "kind": "rule", "rule": {"operation": "project"}}],
                        "output_mapping": {},
                    }
                ),
            },
        )
    return task, store.tasks.claim("workflow-worker")


def test_checkpoint_fencing_hash_privacy_and_no_replay(workflow_inbox):
    client, store = workflow_inbox
    task, claim = workflow_claim(client, store)
    digest = hashlib.sha256(b"private node input").hexdigest()
    checkpoint = store.workflows.begin(claim.lease, "first", digest)
    assert checkpoint.created and checkpoint.output is None
    with pytest.raises(ValueError, match="cannot be replayed"):
        store.workflows.begin(claim.lease, "first", digest)
    from dataclasses import replace

    assert store.workflows.begin(replace(claim.lease, token="0" * 64), "first", digest) is None
    assert store.workflows.begin(replace(claim.lease, worker_id="other"), "first", digest) is None
    assert store.workflows.begin(replace(claim.lease, task_id="missing"), "first", digest) is None
    assert (
        store.workflows.finish(
            replace(claim.lease, token="0" * 64), checkpoint.record.id, status="succeeded", output={}
        )
        is False
    )
    assert store.workflows.finish(claim.lease, checkpoint.record.id, status="succeeded", output={"safe": True})
    replay = store.workflows.begin(claim.lease, "first", digest)
    assert not replay.created and replay.output == {"safe": True}
    with pytest.raises(ValueError):
        store.workflows.begin(claim.lease, "first", "0" * 64)
    with pytest.raises(ValueError):
        store.workflows.begin(claim.lease, "first", digest.upper())
    rows = store.workflows.list("payments", task["id"], allowed_keys=["*"], owner=None)
    assert [row.node_key for row in rows] == ["first"]
    assert "output" not in rows[0].model_dump()
    assert "private node input" not in rows[0].model_dump_json()
    assert claim.lease.token not in rows[0].model_dump_json()
    with Session(store.engine) as session:
        row = session.get(AiWorkflowNode, checkpoint.record.id)
        assert row.output_hash == hashlib.sha256(b'{"safe":true}').hexdigest()
        assert row.output == {"safe": True}
    assert store.tasks.complete(claim.lease, status="succeeded", result={"ok": True})


def test_running_node_blocks_success_and_failure_closes_atomically(workflow_inbox):
    client, store = workflow_inbox
    task, claim = workflow_claim(client, store)
    checkpoint = store.workflows.begin(claim.lease, "first", hashlib.sha256(b"x").hexdigest())
    with pytest.raises(ValueError, match="declared nodes"):
        store.tasks.complete(claim.lease, status="succeeded", result={"ok": True})
    assert store.tasks.complete(claim.lease, status="failed", error_code="SAFE_FAILURE")
    with Session(store.engine) as session:
        row = session.get(AiWorkflowNode, checkpoint.record.id)
        assert row.status == "failed" and row.error_code == "SAFE_FAILURE" and row.finish_time
    assert not store.workflows.finish(claim.lease, checkpoint.record.id, status="succeeded", output={})


def test_workflow_audit_failures_roll_back_node_and_task_updates(workflow_inbox, monkeypatch):
    client, store = workflow_inbox
    task, claim = workflow_claim(client, store)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store.workflows, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        store.workflows.begin(claim.lease, "first", hashlib.sha256(b"x").hexdigest())
    with Session(store.engine) as session:
        assert (
            session.scalar(text("SELECT count(*) FROM ai_workflow_node WHERE task_id=:task"), {"task": task["id"]}) == 0
        )

    monkeypatch.undo()
    checkpoint = store.workflows.begin(claim.lease, "first", hashlib.sha256(b"x").hexdigest())
    monkeypatch.setattr(store.workflows, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        store.workflows.finish(claim.lease, checkpoint.record.id, status="failed", error_code="SAFE_FAILURE")
    with Session(store.engine) as session:
        assert session.get(AiWorkflowNode, checkpoint.record.id).status == "running"


def test_expiry_closes_running_node_and_metadata_scope(workflow_inbox, monkeypatch):
    client, store = workflow_inbox
    task, claim = workflow_claim(client, store)
    checkpoint = store.workflows.begin(claim.lease, "first", hashlib.sha256(b"x").hexdigest())
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE ai_task_attempt SET lease_expires_at=start_time WHERE id=:id"),
            {"id": claim.lease.attempt_id},
        )
    assert store.tasks.recover_expired() == 1
    with Session(store.engine) as session:
        row = session.get(AiWorkflowNode, checkpoint.record.id)
        assert row.status == "failed" and row.error_code == "LEASE_EXPIRED_RESULT_UNKNOWN"
    with pytest.raises(TaskNotFound):
        store.workflows.list("other", task["id"], allowed_keys=["*"], owner=None)
