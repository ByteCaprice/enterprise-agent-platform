"""Pure completion policy and controlled-session adapter checks; no DB or IO."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from domain.capability import CapabilityConflict, CapabilityDependency, CapabilityVersionDraft, CapabilityVersionRecord
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable
from domain.task import TaskValidationUnavailable
from domain.task_completion import (
    AgentCompletionEvidence,
    CompletionCommand,
    CompletionEvidence,
    CompletionTarget,
    NodeCompletionEvidence,
    require_completion_evidence,
)
from domain.task_execution import CompletionOutcome, TaskLease


def artifact_for(kind="agent", *, definition=None, dependencies=()):
    draft = CapabilityVersionDraft(version="1", definition=definition or (
        {"definition_version": "agent.v1", "runtime": "pi",
         "instruction_prompt": {"key": "instructions", "version": "1"},
         "model_policy": {"key": "policy", "version": "1"},
         "limits": {"max_turns": 1, "max_tool_calls": 0, "timeout_seconds": 60.0}} if kind == "agent" else
        {"nodes": [{"key": "project", "kind": "rule", "rule": {"operation": "project"}}]}
    ), dependencies=list(dependencies))
    artifact = CapabilityVersionRecord(
        **draft.model_dump(), id="artifact", capability_id="capability", status="published",
        content_hash=draft.fingerprint(), created_by="admin", create_time=datetime.now(UTC),
    )
    return CompletionTarget("capability", kind, "1", artifact.content_hash), artifact


def root_for(target, command, **changes):
    return replace(AgentCompletionEvidence(
        target.capability_id, "review", target.version, target.content_hash, command.output_hash, None,
    ), **changes)


def test_command_detaches_nested_result_and_returns_fresh_copies():
    source = {"nested": [{"value": "private"}], "ok": True}
    command = CompletionCommand.prepare(status="succeeded", result=source)
    digest, encoded = command.output_hash, command.result_json
    source["nested"][0]["value"] = "changed"
    command.result["nested"].append("also changed")
    assert command.result == {"nested": [{"value": "private"}], "ok": True}
    assert (command.output_hash, command.result_json) == (digest, encoded)
    assert "private" not in repr(command)
    assert CompletionCommand.prepare(status="succeeded", result={"ok": True, "nested": [{"value": "private"}]}).output_hash == digest
    with pytest.raises(FrozenInstanceError):
        command.status = "failed"


@pytest.mark.parametrize("kwargs", [
    {"status": "running"}, {"status": "cancelled"}, {"status": "unknown"},
    {"status": "succeeded"}, {"status": "succeeded", "result": []},
    {"status": "succeeded", "result": {}, "error_code": "SAFE_ERROR"},
    {"status": "failed", "result": {}}, {"status": "timed_out", "result": {}},
    *({"status": "failed", "error_code": code} for code in ("", "private detail", "lower", "1ERROR", "A" * 101, 7)),
    *({"status": "succeeded", "result": {"value": value}} for value in (float("nan"), float("inf"), object(), "\x00")),
])
def test_command_rejects_invalid_status_result_and_error(kwargs):
    with pytest.raises((ValueError, TypeError)):
        CompletionCommand.prepare(**kwargs)


@pytest.mark.parametrize("encoded", ['[]', '{"b": 1}', '{"b":1,"a":2}', '{"a":NaN}', 'null'])
def test_direct_command_cannot_bypass_canonical_object_validation(encoded):
    with pytest.raises(ValueError):
        CompletionCommand("succeeded", encoded)


@pytest.mark.parametrize("status,code", [("failed", "TASK_FAILED"), ("timed_out", "TASK_TIMED_OUT")])
def test_failure_defaults_and_explicit_safe_codes(status, code):
    command = CompletionCommand.prepare(status=status)
    assert command.error_code == code
    assert command.result is command.output_hash is None
    assert CompletionCommand.prepare(status=status, error_code="A" + "_" * 99).error_code == "A" + "_" * 99


def test_agent_success_requires_matching_completed_root_without_mutation():
    target, artifact = artifact_for()
    command = CompletionCommand.prepare(status="succeeded", result={"ok": True})
    evidence = CompletionEvidence(agents=(root_for(target, command),))
    before = artifact.model_dump()
    assert require_completion_evidence(target, artifact, command, evidence) is None
    assert artifact.model_dump() == before
    assert evidence.agents == (root_for(target, command),)
    assert command.result == {"ok": True}


@pytest.mark.parametrize("case", ["missing", "duplicate", "child-only", "capability_id", "version", "content_hash", "output_hash"])
def test_agent_rejects_missing_duplicate_or_mismatched_root(case):
    target, artifact = artifact_for()
    command = CompletionCommand.prepare(status="succeeded", result={})
    root = root_for(target, command)
    agents = () if case == "missing" else (root, root) if case == "duplicate" else (
        replace(root, **({"parent_node_id": "node"} if case == "child-only" else {case: "wrong"})),
    )
    with pytest.raises(ValueError, match="root execution"):
        require_completion_evidence(target, artifact, command, CompletionEvidence(agents=agents))


@pytest.mark.parametrize("flag,message", [
    ("running_model_call", "running external"), ("running_tool_call", "running external"),
    ("unfinished_agent", "unfinished Agent"), ("unfinished_agent_step", "unfinished Agent"),
])
@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_live_or_unfinished_evidence_blocks_success(kind, flag, message):
    target, artifact = artifact_for(kind)
    with pytest.raises(ValueError, match=message):
        require_completion_evidence(target, artifact, CompletionCommand.prepare(status="succeeded", result={}),
                                    CompletionEvidence(**{flag: True}))


@pytest.mark.parametrize("status", ["failed", "timed_out"])
def test_failure_needs_no_success_evidence_or_artifact_validation(status):
    target, artifact = artifact_for("unsupported")
    artifact.status = "draft"
    assert require_completion_evidence(target, artifact, CompletionCommand.prepare(status=status), CompletionEvidence(
        running_model_call=True, running_tool_call=True, unfinished_agent=True, unfinished_agent_step=True,
    )) is None


@pytest.mark.parametrize("mutation", ["status", "capability_id", "version", "content_hash", "definition", "output_schema", "dependencies"])
def test_success_rejects_unpublished_unpinned_and_tampered_artifact(mutation):
    target, artifact = artifact_for()
    command = CompletionCommand.prepare(status="succeeded", result={})
    if mutation in {"definition", "output_schema"}:
        getattr(artifact, mutation)["tampered"] = True
    elif mutation == "dependencies":
        artifact.dependencies.append(CapabilityDependency(key="unexpected", version="1"))
    else:
        setattr(artifact, mutation, "draft" if mutation == "status" else "wrong")
    with pytest.raises((CapabilityConflict, ValueError)):
        require_completion_evidence(target, artifact, command, CompletionEvidence(agents=(root_for(target, command),)))


@pytest.mark.parametrize("kind,definition,message", [
    ("agent", {"name": "legacy"}, "Unsupported Agent"),
    ("tool", None, "Unsupported Task"),
])
def test_unsupported_completion_contracts_fail_closed(kind, definition, message):
    target, artifact = artifact_for(kind, definition=definition)
    with pytest.raises(ValueError, match=message):
        require_completion_evidence(target, artifact, CompletionCommand.prepare(status="succeeded", result={}), CompletionEvidence())


@pytest.mark.parametrize("status", ["succeeded", "skipped"])
def test_workflow_accepts_exact_declared_nodes_in_any_ledger_order(status):
    target, artifact = artifact_for("workflow", definition={"nodes": [
        {"key": key, "kind": "rule", "rule": {"operation": "project"}} for key in ("first", "second")
    ]})
    nodes = tuple(NodeCompletionEvidence(key, key, "rule", status, None) for key in ("second", "first"))
    assert require_completion_evidence(target, artifact, CompletionCommand.prepare(status="succeeded", result={}),
                                       CompletionEvidence(nodes=nodes)) is None


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate", "wrong-key", "wrong-kind", "running", "failed", "cancelled"])
def test_workflow_requires_exact_completed_declared_nodes(case):
    target, artifact = artifact_for("workflow")
    node = NodeCompletionEvidence("node", "project", "rule", "succeeded", "a" * 64)
    nodes = {
        "missing": (), "extra": (node, replace(node, id="extra", key="extra")), "duplicate": (node, node),
        "wrong-key": (replace(node, key="wrong"),), "wrong-kind": (replace(node, kind="tool"),),
    }.get(case, (replace(node, status=case),))
    with pytest.raises(ValueError, match="exactly its completed declared nodes"):
        require_completion_evidence(target, artifact, CompletionCommand.prepare(status="succeeded", result={}), CompletionEvidence(nodes=nodes))


@pytest.mark.parametrize("case", ["match", "missing", "duplicate", "key", "version", "output_hash", "parent_node_id", "skipped"])
def test_workflow_agent_child_must_match_declared_reference_and_node_output(case):
    target, artifact = artifact_for("workflow", definition={"nodes": [
        {"key": "review", "kind": "agent", "capability": {"key": "child-agent", "version": "2"}},
    ]}, dependencies=(CapabilityDependency(key="child-agent", version="2"),))
    node = NodeCompletionEvidence("node", "review", "agent", "skipped" if case == "skipped" else "succeeded", "a" * 64)
    child = AgentCompletionEvidence("child-capability", "child-agent", "2", "b" * 64, node.output_hash, node.id)
    agents = () if case in {"missing", "skipped"} else (child, child) if case == "duplicate" else (
        replace(child, **{case: "wrong"}) if case in {"key", "version", "output_hash", "parent_node_id"} else child,
    )
    command = CompletionCommand.prepare(status="succeeded", result={})
    if case in {"match", "skipped"}:
        assert require_completion_evidence(target, artifact, command, CompletionEvidence(nodes=(node,), agents=agents)) is None
    else:
        with pytest.raises(ValueError, match="completed.*execution"):
            require_completion_evidence(target, artifact, command, CompletionEvidence(nodes=(node,), agents=agents))


@pytest.mark.parametrize("case", ["duplicate-key", "cycle", "unknown-dependency"])
def test_workflow_revalidates_graph_before_accepting_ledger(case):
    first = {"key": "project", "kind": "rule", "rule": {"operation": "project"}}
    if case == "duplicate-key":
        nodes = [first, first.copy()]
    else:
        first["depends_on"] = ["project" if case == "cycle" else "unknown"]
        nodes = [first]
    target, artifact = artifact_for("workflow", definition={"nodes": nodes})
    with pytest.raises(ValueError, match="unique|cycle or unknown"):
        require_completion_evidence(target, artifact, CompletionCommand.prepare(status="succeeded", result={}),
                                    CompletionEvidence(nodes=(NodeCompletionEvidence("node", "project", "rule", "succeeded", None),)))


def test_rejected_policy_call_has_no_mutation_or_completion_side_effects():
    target, artifact = artifact_for()
    command = CompletionCommand.prepare(status="succeeded", result={"private": [1, 2]})
    evidence = CompletionEvidence(unfinished_agent=True, agents=(root_for(target, command),))
    before = (artifact.model_dump(), command.result_json, evidence)
    with pytest.raises(ValueError, match="unfinished Agent"):
        require_completion_evidence(target, artifact, command, evidence)
    assert (artifact.model_dump(), command.result_json, evidence) == before
    assert command.result == {"private": [1, 2]}


def test_absent_task_result_binds_as_sql_null_not_json_null():
    from sqlalchemy.dialects.postgresql.psycopg2 import PGDialect_psycopg2

    from infra.store.model import AiTask

    dialect = PGDialect_psycopg2()
    bind = AiTask.__table__.c.result.type.dialect_impl(dialect).bind_processor(dialect)
    assert bind(None) is None
    assert bind({}) == "{}"


@pytest.fixture
def adapter(monkeypatch):
    from infra.store.repository import task_completion as module

    target, artifact = artifact_for()
    task = SimpleNamespace(id="task", capability_id=target.capability_id, version="1", content_hash=target.content_hash,
                           cancel_request_id=None, status="running", result=None, finish_time=None, error_code=None)
    attempt = SimpleNamespace(id="attempt", status="running", finish_time=None, error_code=None)
    lease = TaskLease(task_id="task", attempt_id="attempt", worker_id="worker", token="a" * 64)
    state = SimpleNamespace(events=[], open=set(), scope=(task, attempt), artifact=artifact, fence=datetime.now(UTC))

    class Session:
        def __init__(self, kind):
            self.kind = kind

        def execute(self, statement, parameters):
            state.events.append(("fence", str(statement), parameters))
            return SimpleNamespace(scalar_one_or_none=lambda: state.fence)

        def flush(self):
            state.events.append("flush")

    class Factory:
        kw = {"bind": None}

        @contextmanager
        def scope(self, kind):
            session = Session(kind)
            state.open.add(kind)
            state.events.append(kind + "-open")
            try:
                yield session
            finally:
                state.open.remove(kind)
                state.events.append(kind + "-closed")

        def __call__(self):
            return self.scope("read")

        def begin(self):
            return self.scope("write")

    repository = module.TaskCompletionRepository(Factory())

    def active(session, actual_lease, *, lock=True, mode):
        assert actual_lease is lease
        assert mode == module.ExecutionWriteMode.CONTROL
        assert lock == (session.kind == "write")
        state.events.append(session.kind + "-active")
        return state.scope

    def read_artifact(session, actual_task):
        assert actual_task is task
        state.artifact.require_published(capability_id=task.capability_id, version=task.version, content_hash=task.content_hash)
        state.events.append(session.kind + "-artifact")
        return SimpleNamespace(kind="agent", key="review"), state.artifact

    command = CompletionCommand.prepare(status="succeeded", result={"ok": True})
    state.evidence = CompletionEvidence(agents=(root_for(target, command),))
    monkeypatch.setattr(module, "active_execution", active)
    monkeypatch.setattr(repository, "_artifact", read_artifact)
    monkeypatch.setattr(repository, "_evidence", lambda *_: state.evidence)
    monkeypatch.setattr(module, "record_task_transition", lambda *args: state.events.append(("transition", args)))
    monkeypatch.setattr(module, "converge_execution_evidence", lambda *args: state.events.append(("converge", args)))

    def validate(result, schema):
        assert not state.open, "Schema validation must not hold a read connection or write transaction"
        state.events.append("validate")

    monkeypatch.setattr(module, "validate_json", validate)
    return SimpleNamespace(module=module, repository=repository, lease=lease, task=task, attempt=attempt, state=state)


def test_adapter_closes_read_before_validation_then_rechecks_and_commits_detached_bytes(adapter, monkeypatch):
    source = {"ok": True}

    def validate(result, schema):
        assert not adapter.state.open
        assert result == source
        source["ok"] = False
        result["ok"] = "validator mutation"
        adapter.state.events.append("validate")

    monkeypatch.setattr(adapter.module, "validate_json", validate)
    assert adapter.repository.complete(adapter.lease, status="succeeded", result=source) == CompletionOutcome.COMPLETED
    assert adapter.task.result == {"ok": True} and source == {"ok": False}
    assert adapter.task.status == adapter.attempt.status == "succeeded"
    assert adapter.task.finish_time == adapter.attempt.finish_time == adapter.state.fence
    assert adapter.state.events[:7] == ["read-open", "read-active", "read-artifact", "read-closed", "validate", "write-open", "write-active"]
    fence = next(event for event in adapter.state.events if isinstance(event, tuple) and event[0] == "fence")
    assert all(fragment in fence[1] for fragment in ("lease_expires_at > clock_timestamp()", "cancel_request_id IS NULL", "worker_id=:worker", "token=:token", "task_id=:task"))
    assert fence[2]["token"] == adapter.lease.token
    transition = next(event for event in adapter.state.events if isinstance(event, tuple) and event[0] == "transition")
    assert transition[1][1:] == (adapter.task, "worker", "task_succeeded")
    assert adapter.state.events[-2:] == ["flush", "write-closed"]


@pytest.mark.parametrize("phase", ["read", "write"])
@pytest.mark.parametrize("closed,outcome", [("lease", CompletionOutcome.LEASE_LOST), ("cancel", CompletionOutcome.CANCELLATION_PENDING)])
def test_adapter_rechecks_lease_and_cancellation_before_any_write(adapter, monkeypatch, phase, closed, outcome):
    def close():
        if closed == "lease":
            adapter.state.scope = None
        else:
            adapter.task.cancel_request_id = "cancel"

    if phase == "read":
        close()
    else:
        def validate(*_):
            assert not adapter.state.open
            close()
        monkeypatch.setattr(adapter.module, "validate_json", validate)
    assert adapter.repository.complete(adapter.lease, status="succeeded", result={"ok": True}) == outcome
    assert adapter.task.status == adapter.attempt.status == "running"
    assert adapter.task.result is None
    assert not any(isinstance(event, tuple) for event in adapter.state.events)
    if phase == "read":
        assert "write-open" not in adapter.state.events


@pytest.mark.parametrize("change", ["artifact", "capability_id", "evidence", "fence"])
def test_adapter_rejects_changes_after_expensive_validation(adapter, monkeypatch, change):
    def validate(*_):
        assert not adapter.state.open
        if change == "artifact":
            definition = {**adapter.state.artifact.definition, "limits": {
                "max_turns": 2, "max_tool_calls": 0, "timeout_seconds": 60.0,
            }}
            _, replacement = artifact_for(definition=definition)
            adapter.state.artifact = replacement
            adapter.task.content_hash = replacement.content_hash
        elif change == "capability_id":
            adapter.state.artifact = adapter.state.artifact.model_copy(update={"capability_id": "replacement"})
            adapter.task.capability_id = "replacement"
        elif change == "evidence":
            adapter.state.evidence = CompletionEvidence(running_model_call=True)
        else:
            adapter.state.fence = None

    monkeypatch.setattr(adapter.module, "validate_json", validate)
    if change == "fence":
        assert adapter.repository.complete(adapter.lease, status="succeeded", result={"ok": True}) == CompletionOutcome.LEASE_LOST
    else:
        with pytest.raises(ValueError, match="artifact changed" if change in {"artifact", "capability_id"} else "running external"):
            adapter.repository.complete(adapter.lease, status="succeeded", result={"ok": True})
    assert adapter.task.status == adapter.attempt.status == "running"
    assert adapter.task.result is None
    assert not any(isinstance(event, tuple) and event[0] in {"transition", "converge"} for event in adapter.state.events)
    assert "flush" not in adapter.state.events


@pytest.mark.parametrize("error", [JSONValidationUnavailable("VALIDATION_CAPACITY"), JSONValidationError("JSON_INVALID")])
def test_adapter_validation_failure_never_opens_write_transaction(adapter, monkeypatch, error):
    def validate(*_):
        assert not adapter.state.open
        raise error

    monkeypatch.setattr(adapter.module, "validate_json", validate)
    with pytest.raises(TaskValidationUnavailable if isinstance(error, JSONValidationUnavailable) else JSONValidationError) as caught:
        adapter.repository.complete(adapter.lease, status="succeeded", result={"ok": True})
    assert caught.value.code == error.code
    assert "write-open" not in adapter.state.events
    assert adapter.task.result is None


@pytest.mark.parametrize("status", ["failed", "timed_out"])
def test_adapter_failure_skips_schema_and_success_evidence_but_converges_before_transition(adapter, monkeypatch, status):
    def forbidden(*_):
        pytest.fail("Failure completion must not validate success evidence")

    monkeypatch.setattr(adapter.module, "validate_json", forbidden)
    monkeypatch.setattr(adapter.repository, "_evidence", forbidden)
    monkeypatch.setattr(adapter.repository, "_artifact", forbidden)
    assert adapter.repository.complete(adapter.lease, status=status) == CompletionOutcome.COMPLETED
    assert "read-open" not in adapter.state.events
    effects = [event[0] for event in adapter.state.events if isinstance(event, tuple)]
    assert effects == ["fence", "converge", "transition"]
    assert adapter.task.status == adapter.attempt.status == status
    assert adapter.task.error_code == ("TASK_FAILED" if status == "failed" else "TASK_TIMED_OUT")
    assert adapter.task.result is None


@pytest.mark.parametrize("kwargs", [
    {"status": "running"}, {"status": "succeeded"},
    {"status": "succeeded", "result": {}, "error_code": "SAFE_ERROR"},
    {"status": "failed", "result": {}}, {"status": "failed", "error_code": "private error"},
])
def test_adapter_rejects_invalid_command_without_opening_any_session(adapter, kwargs):
    with pytest.raises(ValueError):
        adapter.repository.complete(adapter.lease, **kwargs)
    assert adapter.state.events == []
    assert adapter.task.status == adapter.attempt.status == "running"


def test_adapter_extracts_detached_evidence_with_task_and_attempt_scoping():
    from infra.store.repository.task_completion import TaskCompletionRepository

    statements = []
    row = SimpleNamespace(agent_capability_id="child", agent_key="review", agent_version="2",
                          agent_hash="a" * 64, output_hash="b" * 64, parent_node_id="node",
                          status="succeeded", error_code=None)
    facts = {
        "ai_platform_model_call": "running-model", "ai_platform_tool_call": None,
        "ai_agent_execution": "unfinished-agent", "ai_agent_step": None,
    }

    class Session:
        def execute(self, statement):
            statements.append(statement)
            assert statement.compile().params == {"task_attempt_id_1": "attempt"}
            return SimpleNamespace(all=lambda: [SimpleNamespace(
                id="node", node_key="review", kind="agent", status="succeeded", output_hash="b" * 64,
                parent_node_id=None, scope=None, error_code=None,
            )])

        def scalars(self, statement):
            statements.append(statement)
            assert statement.compile().params == {"task_id_1": "task", "task_attempt_id_1": "attempt"}
            return SimpleNamespace(all=lambda: [row])

        def scalar(self, statement):
            statements.append(statement)
            parameters = statement.compile().params
            table = statement.get_final_froms()[0].name
            assert parameters["task_id_1"] == "task"
            if table in {"ai_agent_execution", "ai_agent_step"}:
                assert parameters["status_1"] in ("succeeded", "running", ["pending", "running"])
            else:
                assert parameters["status_1"] == "running"
            assert parameters["param_1"] == 1
            if parameters["status_1"] != "succeeded":
                return "running-model" if table == "ai_platform_model_call" else None
            return facts[table]

    evidence = TaskCompletionRepository._evidence(Session(), SimpleNamespace(id="task"), SimpleNamespace(id="attempt"))
    assert evidence == CompletionEvidence(
        running_model_call=True, unfinished_agent=True,
        nodes=(NodeCompletionEvidence("node", "review", "agent", "succeeded", "b" * 64),),
        agents=(AgentCompletionEvidence("child", "review", "2", "a" * 64, "b" * 64, "node"),),
    )
    row.output_hash = "changed-after-read"
    assert evidence.agents[0].output_hash == "b" * 64
    assert len(statements) == 8
    for statement in statements[2:]:
        table = statement.get_final_froms()[0].name
        if table in {"ai_agent_execution", "ai_agent_step"}:
            if statement.compile().params["status_1"] == "succeeded":
                assert "status !=" in str(statement)
            else:
                assert "status =" in str(statement) or "status IN" in str(statement)
