"""PostgreSQL persistence and fencing coverage for workflow checkpoints."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_platform_task_execution_store import enqueue
from test_platform_task_execution_store import inbox as inbox_fixture

from domain.capability_definitions import WORKFLOW_CHECKPOINT_PROFILE, WORKFLOW_INTERPRETER_PROFILE
from domain.task import TaskNotFound
from infra.store.model import AiWorkflowNode


@pytest.fixture
def workflow_inbox(monkeypatch, pg_url):
    yield from inbox_fixture.__wrapped__(monkeypatch, pg_url)


def workflow_claim(client, store):
    task = enqueue(client, "workflow-checkpoint", store=store)
    from worker_support import worker_capabilities

    return task, store.task_queue.claim("workflow-worker", capabilities=worker_capabilities("workflow"))


def test_checkpoint_fencing_hash_privacy_and_no_replay(workflow_inbox):
    client, store = workflow_inbox
    task, claim = workflow_claim(client, store)
    digest = hashlib.sha256(b"private node input").hexdigest()
    checkpoint = store.workflows.begin(claim.lease, "first", digest,
                                       interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                       checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    assert checkpoint.created and checkpoint.output is None
    with pytest.raises(ValueError, match="cannot be replayed"):
        store.workflows.begin(claim.lease, "first", digest,
                              interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                              checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    from dataclasses import replace

    assert store.workflows.begin(replace(claim.lease, token="0" * 64), "first", digest,
                                 interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                 checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE) is None
    assert store.workflows.begin(replace(claim.lease, worker_id="other"), "first", digest,
                                 interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                 checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE) is None
    assert store.workflows.begin(replace(claim.lease, task_id="missing"), "first", digest,
                                 interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                 checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE) is None
    assert (
        store.workflows.finish(
            replace(claim.lease, token="0" * 64), checkpoint.record.id, status="succeeded", output={}
        )
        is False
    )
    assert store.workflows.finish(claim.lease, checkpoint.record.id, status="succeeded", output={"safe": True})
    replay = store.workflows.begin(claim.lease, "first", digest,
                                  interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                  checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    assert not replay.created and replay.output == {"safe": True}
    with pytest.raises(ValueError):
        store.workflows.begin(claim.lease, "first", "0" * 64,
                              interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                              checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    with pytest.raises(ValueError):
        store.workflows.begin(claim.lease, "first", digest.upper(),
                              interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                              checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    rows = store.workflows.list("payments", task["id"], allowed_keys=["*"], owner=None)
    assert [row.node_key for row in rows] == ["first"]
    assert "output" not in rows[0].model_dump()
    assert "private node input" not in rows[0].model_dump_json()
    assert claim.lease.token not in rows[0].model_dump_json()
    with Session(store.engine) as session:
        row = session.get(AiWorkflowNode, checkpoint.record.id)
        assert row.output_hash == hashlib.sha256(b'{"safe":true}').hexdigest()
        assert row.output == {"safe": True}
    assert store.task_completion.complete(claim.lease, status="succeeded", result={"ok": True})


def test_running_node_blocks_success_and_failure_closes_atomically(workflow_inbox):
    client, store = workflow_inbox
    task, claim = workflow_claim(client, store)
    checkpoint = store.workflows.begin(claim.lease, "first", hashlib.sha256(b"x").hexdigest(),
                                       interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                       checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    with pytest.raises(ValueError, match="declared nodes"):
        store.task_completion.complete(claim.lease, status="succeeded", result={"ok": True})
    assert store.task_completion.complete(claim.lease, status="failed", error_code="SAFE_FAILURE")
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
        store.workflows.begin(claim.lease, "first", hashlib.sha256(b"x").hexdigest(),
                              interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                              checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    with Session(store.engine) as session:
        assert (
            session.scalar(text("SELECT count(*) FROM ai_workflow_node WHERE task_id=:task"), {"task": task["id"]}) == 0
        )

    monkeypatch.undo()
    checkpoint = store.workflows.begin(claim.lease, "first", hashlib.sha256(b"x").hexdigest(),
                                       interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                       checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    monkeypatch.setattr(store.workflows, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        store.workflows.finish(claim.lease, checkpoint.record.id, status="failed", error_code="SAFE_FAILURE")
    with Session(store.engine) as session:
        assert session.get(AiWorkflowNode, checkpoint.record.id).status == "running"


def test_expiry_closes_running_node_and_metadata_scope(workflow_inbox, monkeypatch):
    client, store = workflow_inbox
    task, claim = workflow_claim(client, store)
    checkpoint = store.workflows.begin(claim.lease, "first", hashlib.sha256(b"x").hexdigest(),
                                       interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                       checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE ai_task_attempt SET lease_expires_at=start_time WHERE id=:id"),
            {"id": claim.lease.attempt_id},
        )
    assert store.task_queue.recover_expired() == 1
    with Session(store.engine) as session:
        row = session.get(AiWorkflowNode, checkpoint.record.id)
        assert row.status == "failed" and row.error_code == "LEASE_EXPIRED_RESULT_UNKNOWN"
    with pytest.raises(TaskNotFound):
        store.workflows.list("other", task["id"], allowed_keys=["*"], owner=None)
