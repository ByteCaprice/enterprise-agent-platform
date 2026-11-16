"""Offline checks for explicit persistence effect ownership; never connect a DB."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from domain.platform_model import ModelUsage
from domain.task_event import TERMINAL_TASK_EVENTS
from domain.task_execution import ExecutionFailure, TaskCancellationRequested
from infra.store.model import AiPlatformModelCall, AiRunLog, AiTask, AiTaskEvent
from infra.store.model.ai_operations import AiBudgetCharge, AiBudgetCycle, AiCallbackEndpoint, AiProjectLimits
from infra.store.repository import agents, callbacks, limits, models, task_control, task_events, tools
from infra.store.repository import execution_effects as effects

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class SessionDouble:
    """No transaction APIs: helpers must use exactly the supplied session."""

    def __init__(self):
        self.added = []
        self.operations = []

    def add(self, row):
        self.added.append(row)
        self.operations.append(("add", row))

    def flush(self):
        self.operations.append(("flush",))


@pytest.mark.parametrize("event", sorted(TERMINAL_TASK_EVENTS))
def test_terminal_task_transition_composes_effects_in_order_on_same_session(monkeypatch, event):
    session = SessionDouble()
    task = SimpleNamespace(id="task", project_key="project")

    def append(actual, task_id, event_type):
        assert actual is session
        assert (task_id, event_type) == (task.id, event)
        assert len(session.added) == 1  # audit already staged
        session.operations.append(("journal",))
        return 17

    def close(actual, task_id):
        assert actual is session and task_id == task.id
        session.operations.append(("close",))

    def enqueue(actual, task_id, sequence, event_type):
        assert actual is session
        assert (task_id, sequence, event_type) == (task.id, 17, event)
        session.operations.append(("enqueue",))

    monkeypatch.setattr(effects, "append_task_event", append)
    monkeypatch.setattr(effects, "close_task_budget_reservations", close)
    monkeypatch.setattr(effects, "enqueue_terminal_callbacks", enqueue)
    assert effects.record_task_transition(session, task, "worker", event) == 17
    assert [op[0] for op in session.operations] == ["add", "journal", "close", "enqueue"]
    audit = session.added[0]
    assert isinstance(audit, AiRunLog)
    assert (audit.trace_id, audit.component, audit.event_type) == ("task:task", "task_audit", event)
    assert audit.message == event.replace("_", " ")
    assert audit.data == {"actor": "worker", "project_key": "project", "task_id": "task"}


@pytest.mark.parametrize("event", ["task_submitted", "task_started", "task_cancel_requested", "task_cancel_observed"])
def test_nonterminal_task_transition_has_no_accounting_or_delivery(monkeypatch, event):
    session = SessionDouble()
    monkeypatch.setattr(effects, "append_task_event", lambda actual, *_: 3 if actual is session else None)

    def forbidden(*args):
        pytest.fail("Nonterminal event triggered terminal effects")

    monkeypatch.setattr(effects, "close_task_budget_reservations", forbidden)
    monkeypatch.setattr(effects, "enqueue_terminal_callbacks", forbidden)
    assert effects.record_task_transition(session, SimpleNamespace(id="task", project_key="project"), "actor", event) == 3


def test_append_task_event_only_allocates_and_appends_even_for_terminal_event(monkeypatch):
    session = SessionDouble()
    session.execute = lambda *_args, **_kwargs: SimpleNamespace(
        one=lambda: SimpleNamespace(project_key="project", event_sequence=8),
    )
    scalar_results = iter(["attempt", NOW])
    session.scalar = lambda *_: next(scalar_results)

    def forbidden(*args):
        pytest.fail("Journal append must not own accounting or delivery")

    monkeypatch.setattr(callbacks, "enqueue_terminal_callbacks", forbidden)
    monkeypatch.setattr(limits, "close_task_budget_reservations", forbidden)
    assert task_events.append_task_event(session, "task", "task_succeeded") == 8
    assert session.operations[0] == ("flush",)
    assert len(session.added) == 1
    event = session.added[0]
    assert isinstance(event, AiTaskEvent)
    assert (event.task_id, event.project_key, event.sequence, event.task_attempt_id) == ("task", "project", 8, "attempt")
    assert (event.entity_kind, event.entity_id, event.event_type, event.occurred_at) == ("task", "task", "task_succeeded", NOW)


def test_callback_enqueue_owns_delivery_only_and_retains_payload():
    session = SessionDouble()
    task = SimpleNamespace(id="task", project_key="project", status="cancelled")
    session.get = lambda entity, key: task if (entity, key) == (AiTask, "task") else pytest.fail("Unexpected lookup")
    session.scalar = lambda *_: NOW

    def endpoints(query):
        assert query.column_descriptions[0]["entity"] is AiCallbackEndpoint
        return [SimpleNamespace(key="one"), SimpleNamespace(key="two")]

    session.scalars = endpoints
    callbacks.enqueue_terminal_callbacks(session, task.id, 4, "task_cancelled")
    assert len(session.added) == 2
    assert {row.endpoint_key for row in session.added} == {"one", "two"}
    for row in session.added:
        assert (row.state, row.attempts, row.next_attempt_at) == ("pending", 0, NOW)
        assert row.payload == {"delivery_id": row.id, "project_key": "project", "task_id": "task",
                               "sequence": 4, "event_type": "task_cancelled", "status": "cancelled"}


def test_callback_enqueue_ignores_nonterminal_without_touching_session():
    callbacks.enqueue_terminal_callbacks(object(), "task", 1, "task_cancel_requested")


def test_terminal_budget_closure_is_independent_of_callback_endpoints():
    session = SessionDouble()
    charges = [SimpleNamespace(state="reserved", reserved_microunits=25, settled_microunits=None) for _ in range(2)]

    def reserved(query):
        assert query.column_descriptions[0]["entity"] is AiBudgetCharge
        assert query.compile().params == {"task_id_1": "task", "state_1": "reserved"}
        return charges

    session.scalars = reserved
    limits.close_task_budget_reservations(session, "task")
    assert [(row.state, row.reserved_microunits, row.settled_microunits) for row in charges] == [
        ("unknown", 25, None), ("unknown", 25, None),
    ]
    assert not session.added


@pytest.mark.parametrize("event", ["model_provider_created", "model_provider_enabled", "model_provider_disabled"])
def test_provider_audit_is_audit_only(event):
    session = SessionDouble()
    effects.record_model_audit(session, "project", None, None, "admin", event)
    assert [op[0] for op in session.operations] == ["add"]
    audit = session.added[0]
    assert (audit.trace_id, audit.component, audit.event_type) == ("task:model-provider", "model_audit", event)
    assert audit.data == {"project_key": "project", "task_id": None, "call_id": None, "actor": "admin"}


@pytest.mark.parametrize("event", ["model_call_started", "model_call_succeeded", "model_call_failed", "model_call_cancelled"])
def test_model_call_effects_explicitly_compose_accounting_audit_and_journal(monkeypatch, event):
    session = SessionDouble()

    def account(actual, call_id):
        assert actual is session and call_id == "call"
        session.operations.append(("reserve" if event == "model_call_started" else "settle",))

    def append(actual, task_id, event_type, **kwargs):
        assert actual is session
        assert (task_id, event_type, kwargs) == ("task", event, {"entity_kind": "model_call", "entity_id": "call"})
        session.operations.append(("journal",))
        return 9

    monkeypatch.setattr(effects, "reserve_model_call_budget", account)
    monkeypatch.setattr(effects, "settle_model_call_budget", account)
    monkeypatch.setattr(effects, "append_task_event", append)
    args = (session, "project", "task", "call", "worker")
    if event == "model_call_started":
        assert effects.record_model_call_started(*args) == 9
    else:
        assert effects.record_model_call_settled(*args, event) == 9
    assert [op[0] for op in session.operations] == ["reserve" if event == "model_call_started" else "settle", "add", "journal"]
    assert session.added[0].data == {"project_key": "project", "task_id": "task", "call_id": "call", "actor": "worker"}


def test_model_settlement_rejects_start_event_before_any_effect():
    with pytest.raises(ValueError, match="settlement"):
        effects.record_model_call_settled(object(), "project", "task", "call", "actor", "model_call_started")


def test_tool_call_effects_audit_and_journal_without_accounting(monkeypatch):
    session = SessionDouble()

    def append(actual, task_id, event, **kwargs):
        assert actual is session
        assert (task_id, event, kwargs) == ("task", "tool_call_started", {"entity_kind": "tool_call", "entity_id": "call"})
        return 5

    monkeypatch.setattr(effects, "append_task_event", append)
    assert effects.record_tool_call_transition(session, "project", "worker", "tool_call_started",
                                               task_id="task", call_id="call", binding_key="binding") == 5
    assert session.added[0].data == {"project_key": "project", "actor": "worker", "task_id": "task",
                                      "call_id": "call", "binding_key": "binding"}


def accounting_session(*, prompt_tokens=3, completion_tokens=2, rate=1_000_000, state="reserved"):
    session = SessionDouble()
    call = SimpleNamespace(id="call", project_key="project", task_id="task", provider_key="provider", requested_model="model",
                           prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    charge = SimpleNamespace(budget_key="budget", state=state, reserved_microunits=50, settled_microunits=None)
    budget = SimpleNamespace(key="budget", starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=1),
                             reserve_per_call_microunits=50, limit_microunits=100,
                             rates=[{"provider_key": "provider", "model": "model", "input_microunits_per_million": rate,
                                     "output_microunits_per_million": rate}])
    rows = {(AiPlatformModelCall, "call"): call, (AiBudgetCharge, "call"): charge,
            (AiBudgetCycle, ("project", "budget")): budget,
            (AiProjectLimits, "project"): SimpleNamespace(active_budget_key="budget")}
    session.get = lambda entity, key: rows[(entity, key)]
    # A missing/revoked project must not block settlement; no enabled-project lookup.
    results = iter([None, NOW])
    session.scalar = lambda *_: next(results)
    return session, charge, budget


def test_model_reservation_uses_declared_budget_without_journal_or_audit(monkeypatch):
    session, _, _ = accounting_session()
    monkeypatch.setattr(limits, "budget_used", lambda actual, project, budget: 50 if actual is session else None)
    limits.reserve_model_call_budget(session, "call")
    charge = session.added[0]
    assert isinstance(charge, AiBudgetCharge)
    assert (charge.call_id, charge.task_id, charge.state, charge.reserved_microunits, charge.settled_microunits) == (
        "call", "task", "reserved", 50, None,
    )


def test_exhausted_reservation_propagates_without_audit_or_journal(monkeypatch):
    session, _, _ = accounting_session()
    monkeypatch.setattr(limits, "budget_used", lambda *_: 51)
    monkeypatch.setattr(effects, "reserve_model_call_budget", limits.reserve_model_call_budget)
    with pytest.raises(ExecutionFailure, match="MODEL_BUDGET_EXHAUSTED"):
        effects.record_model_call_started(session, "project", "task", "call", "worker")
    assert not session.added


@pytest.mark.parametrize(("prompt", "completion", "rate", "state", "amount"), [
    (3, 2, 1_000_000, "known", 5),
    (1, 0, 1, "known", 1),  # round up, never truncate a real charge to zero
    (0, 0, 1_000_000, "known", 0),  # explicit zero Usage is different from unknown
    (None, 2, 1_000_000, "unknown", None),
    (3, None, 1_000_000, "unknown", None),
    (9007199254740991, 1, 1_000_000, "unknown", None),
])
def test_model_settlement_preserves_usage_and_unknown_reservations(prompt, completion, rate, state, amount):
    session, charge, _ = accounting_session(prompt_tokens=prompt, completion_tokens=completion, rate=rate)
    limits.settle_model_call_budget(session, "call")
    assert (charge.state, charge.settled_microunits, charge.reserved_microunits) == (state, amount, 50)
    assert not session.added


@pytest.mark.parametrize("state", ["known", "unknown"])
def test_already_closed_model_charge_is_not_settled_again(state):
    session, charge, _ = accounting_session(state=state)
    limits.settle_model_call_budget(session, "call")
    assert charge.state == state
    assert charge.settled_microunits is None


class CallerTransaction:
    def __init__(self, session):
        self.session = session
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def begin(self):
        try:
            yield self.session
        except Exception:
            self.session.added.clear()
            self.rolled_back = True
            raise
        else:
            self.committed = True


def test_delivery_failure_propagates_to_callers_transaction(monkeypatch):
    session = SessionDouble()
    owner = CallerTransaction(session)
    monkeypatch.setattr(effects, "append_task_event", lambda *_: 11)
    monkeypatch.setattr(effects, "close_task_budget_reservations", lambda *_: None)

    def fail(*args):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(effects, "enqueue_terminal_callbacks", fail)
    with pytest.raises(RuntimeError, match="outbox unavailable"), owner.begin() as actual:
        effects.record_task_transition(actual, SimpleNamespace(id="task", project_key="project"), "worker", "task_succeeded")
    assert owner.rolled_back and not owner.committed
    assert not session.added


@pytest.mark.parametrize("repository", [models, tools])
@pytest.mark.parametrize("fence", ["initial", "final"])
def test_stale_call_finish_has_no_effects(monkeypatch, repository, fence):
    session = SessionDouble()
    cls = repository.ModelRepository if repository is models else repository.ToolRepository
    repo = object.__new__(cls)
    repo._sf = CallerTransaction(session)
    task = SimpleNamespace(id="task", project_key="project", cancel_request_id=None)
    scope = None if fence == "initial" else (task, SimpleNamespace(id="attempt"))
    monkeypatch.setattr(repository, "active_execution", lambda *_args, **_kwargs: scope)
    session.execute = lambda *_args, **_kwargs: SimpleNamespace(scalar_one_or_none=lambda: None)
    lease = SimpleNamespace(worker_id="worker", token="token")
    assert repo.finish_call(lease, "call", status="failed", error_code="MODEL_FAILED" if repository is models else "TOOL_FAILED") is False
    assert not session.operations


@pytest.mark.parametrize("repository", [models, tools])
def test_cancelled_response_settles_before_signalling_discard(monkeypatch, repository):
    session = SessionDouble()
    session.execute = lambda *_args, **_kwargs: SimpleNamespace(scalar_one_or_none=lambda: "call")
    cls = repository.ModelRepository if repository is models else repository.ToolRepository
    repo = object.__new__(cls)
    repo._sf = CallerTransaction(session)
    task = SimpleNamespace(id="task", project_key="project", cancel_request_id="cancel")
    monkeypatch.setattr(repository, "active_execution", lambda *_args, **_kwargs: (task, SimpleNamespace(id="attempt")))
    recorded = []
    if repository is models:
        monkeypatch.setattr(models, "record_model_call_settled", lambda *args: recorded.append(args))
        kwargs = {"actual_model": "model", "usage": ModelUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5)}
    else:
        monkeypatch.setattr(tools, "record_tool_call_transition", lambda *args, **kwargs: recorded.append(args))
        kwargs = {}
    lease = SimpleNamespace(worker_id="worker", token="token")
    with pytest.raises(TaskCancellationRequested):
        repo.finish_call(lease, "call", status="succeeded", output_hash="a" * 64, **kwargs)
    assert repo._sf.committed  # discard is signalled only after charge/evidence commit
    assert recorded[0][0] is session
    assert ("model_call_cancelled" if repository is models else "tool_call_cancelled") in recorded[0]


def test_agent_model_rejection_retains_usage_and_uses_public_settlement(monkeypatch):
    session = SessionDouble()
    call = SimpleNamespace()
    session.scalar = lambda query: NOW if str(query).startswith("SELECT clock_timestamp") else call
    repo = object.__new__(agents.AgentRepository)
    repo._sf = CallerTransaction(session)
    task = SimpleNamespace(id="task", project_key="project")
    execution = SimpleNamespace(id="execution")
    step = SimpleNamespace(id="step", status="running", kind="model_turn")
    monkeypatch.setattr(repo, "_evidence_scope", lambda *_args, **_kwargs: (task, execution))
    monkeypatch.setattr(repo, "_steps", lambda *_: [step])
    recorded = []
    call.id = "call"
    monkeypatch.setattr(agents, "record_model_call_settled", lambda *args: recorded.append(args))
    monkeypatch.setattr(agents, "fence_execution_write", lambda *_args, **_kwargs: None)
    repo.reject_call(SimpleNamespace(worker_id="worker"), "execution", "step", error_code="MODEL_REJECTED",
                     cancelled=True, usage=ModelUsage(prompt_tokens=7, completion_tokens=4, total_tokens=11))
    assert (call.status, call.prompt_tokens, call.completion_tokens, call.total_tokens) == ("cancelled", 7, 4, 11)
    assert recorded == [(session, "project", "task", "call", "worker", "model_call_cancelled")]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_task_control_records_intent_then_only_queued_terminal_transition(monkeypatch, status):
    session = SessionDouble()
    task = SimpleNamespace(id="task", project_key="project", status=status, cancel_request_id=None, create_time=NOW)
    capability = SimpleNamespace(key="capability")
    session.execute = lambda *_: SimpleNamespace(one_or_none=lambda: (task, capability))
    session.scalar = lambda *_: NOW
    repo = object.__new__(task_control.TaskControlRepository)
    repo._sf = CallerTransaction(session)
    monkeypatch.setattr(repo, "_receipt", lambda *_: task)
    recorded = []
    monkeypatch.setattr(task_control, "record_task_transition", lambda *args: recorded.append(args))
    assert repo.request_cancel("project", "task", "actor", allowed_keys=["capability"], owner="owner") is task
    assert recorded[0] == (session, task, "actor", "task_cancel_requested")
    if status == "queued":
        assert task.status == "cancelled" and task.finish_time == NOW
        assert recorded[1] == (session, task, "actor", "task_cancelled")
    else:
        assert task.status == "running" and len(recorded) == 1
    # An idempotent cancellation replay never generates another terminal callback.
    repo.request_cancel("project", "task", "actor", allowed_keys=["capability"], owner="owner")
    assert len(recorded) == (2 if status == "queued" else 1)


@pytest.mark.parametrize("confirmed", [True, False])
def test_cancellation_converges_before_public_terminal_effects(monkeypatch, confirmed):
    session = SessionDouble()
    session.scalar = lambda *_: NOW
    session.execute = lambda *_: SimpleNamespace(scalar_one_or_none=lambda: "attempt")
    task = SimpleNamespace(id="task", project_key="project", status="running", cancel_request_id="cancel", cancel_requested_at=NOW)
    attempt = SimpleNamespace(id="attempt", start_time=NOW, cancel_observed_at=NOW)
    repo = object.__new__(task_control.TaskControlRepository)
    repo._sf = CallerTransaction(session)
    monkeypatch.setattr(task_control, "active_execution", lambda *_args, **_kwargs: (task, attempt))
    observed = []

    def converge(actual, task_id, attempt_id, finished, reason, *, cancelled):
        assert actual is session and (task_id, attempt_id, finished) == ("task", "attempt", NOW)
        assert cancelled is confirmed
        observed.append("converge")

    def transition(actual, actual_task, actor, event):
        assert actual is session and actual_task is task and actor == "worker"
        assert task.status == ("cancelled" if confirmed else "failed")
        assert event == ("task_cancelled" if confirmed else "task_cancel_unconfirmed")
        observed.append("terminal")

    monkeypatch.setattr(task_control, "converge_execution_evidence", converge)
    monkeypatch.setattr(task_control, "record_task_transition", transition)
    lease = SimpleNamespace(task_id="task", attempt_id="attempt", worker_id="worker", token="token")
    assert repo._resolve(lease, confirmed=confirmed)
    assert observed == ["converge", "terminal"]
