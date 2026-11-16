"""Two root evidence smokes, with synthetic persisted artifacts, no Agent runtime/IO."""

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_platform_migrations import config_for
from test_platform_models import setup_model
from test_platform_tasks import ADMIN, submit
from test_platform_tasks import inbox as inbox_fixture
from test_platform_tools import setup_tool

from domain.capability import CapabilitySpec, CapabilityVersionDraft
from domain.platform_agent import AgentDefinition, AgentStorageConflict, AgentToolIntent
from domain.platform_model import ModelUsage
from domain.task_execution import TaskLeaseLost
from infra.schema_validation import IsolatedJSONValidator
from orchestration.capability_service import CapabilityService

inbox = inbox_fixture
ARGS = {"data": {"name": "Synthetic name", "private": "synthetic-private"}, "fields": ["name"]}
RESULT = {"name": "Synthetic name"}


def root_fixture(client, store, monkeypatch, *, begin=True, headers=None, claim_task=True, skill=None):
    setup_tool(client, store)
    setup_model(client, store, monkeypatch, prompt=False)
    service = CapabilityService(store.capabilities, IsolatedJSONValidator())
    service.create("payments", CapabilitySpec(key="instructions", kind="prompt", name="Instructions"), "task-admin")
    service.create_draft("payments", "instructions", CapabilityVersionDraft(
        version="1", definition={"template": "Synthetic review instructions"},
    ), "task-admin")
    service.publish("payments", "instructions", "1", "task-admin")
    service.create("payments", CapabilitySpec(key="agent-policy", kind="model_policy", name="Agent turns"), "task-admin")
    service.create_draft("payments", "agent-policy", CapabilityVersionDraft(
        version="1", definition={"interaction": "agent_turn", "provider_key": "mock", "model": "catalog/primary", "max_tokens": 128},
    ), "task-admin")
    service.publish("payments", "agent-policy", "1", "task-admin")
    if skill is not None:
        service.create("payments", CapabilitySpec(key="review-guide", kind="skill", name="Review guide"), "task-admin")
        service.create_draft("payments", "review-guide", CapabilityVersionDraft(
            version="1", definition={"instructions": skill},
        ), "task-admin")
        service.publish("payments", "review-guide", "1", "task-admin")
    definition = AgentDefinition.model_validate({
        "definition_version": "agent.v1", "runtime": "pi",
        "instruction_prompt": {"key": "instructions", "version": "1"},
        "model_policy": {"key": "agent-policy", "version": "1"},
        "tools": [{"alias": "lookup", "capability": {"key": "select-fields", "version": "1"}}],
        "skills": [{"key": "review-guide", "version": "1"}] if skill is not None else [],
        "limits": {"max_turns": 3, "max_tool_calls": 2, "timeout_seconds": 60},
    })
    service.create("payments", CapabilitySpec(key="root-agent", kind="agent", name="Root Agent"), "task-admin")
    draft = CapabilityVersionDraft(version="1", definition=definition.model_dump(mode="json"),
        dependencies=[ref.model_dump() for ref in definition.dependency_refs()],
        output_schema={"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"], "additionalProperties": False})
    service.create_draft("payments", "root-agent", draft, "task-admin")
    service.publish("payments", "root-agent", "1", "task-admin")
    response = submit(client, capability_key="root-agent", headers=headers)
    assert response.status_code == 202
    if not claim_task:
        return None, None, response.headers["Location"]
    from worker_support import worker_capabilities

    claim = store.task_queue.claim("agent-storage", capabilities=worker_capabilities("agent"), lease_seconds=60)
    execution = store.agents.begin(claim.lease, runtime_profile="enterprise-ai.pi.v1") if begin else None
    return claim, execution, response.headers["Location"]


def test_atomic_model_intents_tool_evidence_and_final_task_without_fake_workflow_nodes(inbox, monkeypatch):
    client, store = inbox
    claim, execution, target = root_fixture(client, store, monkeypatch)
    step, _ = store.agents.start_turn(claim.lease, execution.id, input_hash="a" * 64)
    intent = AgentToolIntent(alias="lookup", source_call_id="provider-call", arguments=ARGS)
    real_audit = store.agents._audit

    def fail_accept(*_args, **_kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(store.agents, "_audit", fail_accept)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        store.agents.accept_turn(claim.lease, execution.id, step.id, output_hash="b" * 64,
                                 actual_model="catalog/primary", intents=[intent])
    with store.engine.connect() as connection:
        assert connection.scalar(text("SELECT status FROM ai_platform_model_call")) == "running"
        assert connection.scalar(text("SELECT count(*) FROM ai_agent_step WHERE kind='tool_call'")) == 0
    monkeypatch.setattr(store.agents, "_audit", real_audit)
    pending = store.agents.accept_turn(claim.lease, execution.id, step.id, output_hash="b" * 64,
                                      actual_model="catalog/primary", intents=[intent], usage=ModelUsage(total_tokens=7))
    with pytest.raises(AgentStorageConflict):
        store.agents.start_turn(claim.lease, execution.id, input_hash="c" * 64)
    tool, _ = store.agents.start_tool(claim.lease, execution.id, pending[0].dispatch_id, arguments=ARGS)
    with pytest.raises(AgentStorageConflict):
        store.agents.start_tool(claim.lease, execution.id, pending[0].dispatch_id, arguments=ARGS)
    store.agents.finish_tool(claim.lease, execution.id, tool.id, output=RESULT)
    final, _ = store.agents.start_turn(claim.lease, execution.id, input_hash="c" * 64)
    store.agents.accept_turn(claim.lease, execution.id, final.id, output_hash="d" * 64,
                             actual_model="catalog/primary", final_result=RESULT)
    with pytest.raises(ValueError, match="unfinished Agent"):
        store.task_completion.complete(claim.lease, status="succeeded", result=RESULT)
    store.agents.finish(claim.lease, execution.id, result=RESULT)
    assert store.task_completion.complete(claim.lease, status="succeeded", result=RESULT)
    assert client.get(target + "/result", headers=ADMIN).json()["result"] == RESULT
    calls = client.get(target + "/tool-calls", headers=ADMIN)
    assert calls.json()[0]["owner"] == {"kind": "agent_step", "agent_execution_id": execution.id, "step_id": tool.id}
    assert calls.json()[0]["node_id"] is None and "synthetic-private" not in calls.text
    assert client.get(target + "/model-calls", headers=ADMIN).json()[0]["total_tokens"] == 7
    with store.engine.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM ai_workflow_node")) == 0
        for mutation in ("agent_step_kind=NULL", "agent_step_kind='model_turn'", "agent_execution_id=NULL"):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text(f"UPDATE ai_platform_tool_call SET {mutation}"))
    with pytest.raises(RuntimeError, match="cannot be discarded"):
        command.downgrade(config_for(store.engine.url.render_as_string(hide_password=False)), "base")


def test_expiry_distinguishes_unstarted_intents_from_running_calls_and_fences_late_results(inbox, monkeypatch):
    client, store = inbox
    claim, execution, _ = root_fixture(client, store, monkeypatch)
    step, _ = store.agents.start_turn(claim.lease, execution.id, input_hash="a" * 64)
    pending = store.agents.accept_turn(claim.lease, execution.id, step.id, output_hash="b" * 64,
        actual_model="catalog/primary", intents=[AgentToolIntent(alias="lookup", source_call_id=str(i), arguments=ARGS) for i in range(2)])
    tool, _ = store.agents.start_tool(claim.lease, execution.id, pending[0].dispatch_id, arguments=ARGS)
    with store.engine.begin() as connection:
        connection.execute(text("UPDATE ai_task_attempt SET lease_expires_at=start_time WHERE id=:id"), {"id": claim.lease.attempt_id})
    with pytest.raises(TaskLeaseLost):
        store.agents.finish_tool(claim.lease, execution.id, tool.id, output=RESULT)
    assert store.task_queue.recover_expired() == 1
    with store.engine.connect() as connection:
        rows = connection.execute(text("SELECT status,error_code FROM ai_agent_step WHERE kind='tool_call' ORDER BY slot")).all()
        assert rows[0].status == "failed" and rows[0].error_code.endswith("RESULT_UNKNOWN")
        assert rows[1] == ("cancelled", "AGENT_NOT_STARTED")
        assert connection.scalar(text("SELECT status FROM ai_agent_execution")) == "failed"
        assert connection.scalar(text("SELECT output_hash FROM ai_platform_tool_call")) is None
