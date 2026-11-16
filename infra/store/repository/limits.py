"""Serialized admission limits and explicit monetary reservations, never estimated billing."""

from sqlalchemy import func, select, text

from domain.task import TaskConflict
from domain.task_execution import ExecutionFailure
from infra.store.model import AiAgentExecution, AiPlatformModelCall, AiProject, AiTask
from infra.store.model.ai_operations import AiBudgetCharge, AiBudgetCycle, AiProjectLimits


def concurrency_lock(session):
    """Serialize claim decisions and policy changes across all platform processes."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(current_database()), "
                         "hashtext(current_schema() || '/task-concurrency'))"))


def capability_running_count(session, capability_id):
    root_tasks = session.scalar(select(func.count()).select_from(AiTask).where(
        AiTask.capability_id == capability_id, AiTask.status == "running"))
    embedded = session.scalar(select(func.count()).select_from(AiAgentExecution).where(
        AiAgentExecution.agent_capability_id == capability_id, AiAgentExecution.parent_node_id.is_not(None),
        AiAgentExecution.status == "running"))
    return root_tasks, embedded


def project_lock(session, project):
    row = session.scalar(select(AiProject).where(AiProject.key == project).with_for_update())
    if row is None or not row.enabled:
        raise TaskConflict("Project is unavailable")
    return row


def admit_task(session, project):
    project_lock(session, project)
    limits = session.get(AiProjectLimits, project)
    if limits is not None and limits.max_queued is not None:
        count = session.scalar(select(func.count()).select_from(AiTask).where(AiTask.project_key == project, AiTask.status == "queued"))
        if count >= limits.max_queued:
            raise TaskConflict("Project queued-task limit reached")


def admit_claim(session, project):
    project_lock(session, project)
    limits = session.get(AiProjectLimits, project)
    if limits is None or limits.max_running is None:
        return True
    count = session.scalar(select(func.count()).select_from(AiTask).where(AiTask.project_key == project, AiTask.status == "running"))
    return count < limits.max_running


def budget_used(session, project, budget):
    return session.scalar(select(func.coalesce(func.sum(func.coalesce(AiBudgetCharge.settled_microunits,
        AiBudgetCharge.reserved_microunits)), 0)).where(AiBudgetCharge.project_key == project, AiBudgetCharge.budget_key == budget))


def _locked_model_call(session, call_id):
    """Use the caller's fenced transaction and serialize project budget changes."""
    session.flush()
    call = session.get(AiPlatformModelCall, call_id)
    if call is None:
        raise ValueError("Model accounting requires an existing call")
    # Identity/project revocation must not prevent settlement of an existing charge.
    session.scalar(select(AiProject.key).where(AiProject.key == call.project_key).with_for_update())
    return call


def reserve_model_call_budget(session, call_id):
    """Reserve declared money for an admitted call, without audit or journal effects."""
    call = _locked_model_call(session, call_id)
    limits = session.get(AiProjectLimits, call.project_key)
    if limits is None or limits.active_budget_key is None:
        return
    budget = session.get(AiBudgetCycle, (call.project_key, limits.active_budget_key))
    now = session.scalar(select(func.clock_timestamp()))
    if budget is None or not budget.starts_at <= now < budget.ends_at:
        raise ExecutionFailure("MODEL_BUDGET_UNAVAILABLE")
    rate = next((r for r in budget.rates if r["provider_key"] == call.provider_key and r["model"] == call.requested_model), None)
    if rate is None:
        raise ExecutionFailure("MODEL_BUDGET_PRICE_UNAVAILABLE")
    if budget_used(session, call.project_key, budget.key) + budget.reserve_per_call_microunits > budget.limit_microunits:
        raise ExecutionFailure("MODEL_BUDGET_EXHAUSTED")
    session.add(AiBudgetCharge(call_id=call.id, project_key=call.project_key, budget_key=budget.key,
                               task_id=call.task_id, reserved_microunits=budget.reserve_per_call_microunits,
                               settled_microunits=None, state="reserved"))


def settle_model_call_budget(session, call_id):
    """Settle retained Usage even after cancellation/revocation; never invent zero usage."""
    call = _locked_model_call(session, call_id)
    charge = session.get(AiBudgetCharge, call_id)
    if charge is not None and charge.state == "reserved":
        budget = session.get(AiBudgetCycle, (call.project_key, charge.budget_key))
        rate = next((r for r in budget.rates if r["provider_key"] == call.provider_key and r["model"] == call.requested_model), None)
        if rate is not None and call.prompt_tokens is not None and call.completion_tokens is not None:
            numerator = call.prompt_tokens * rate["input_microunits_per_million"] + call.completion_tokens * rate["output_microunits_per_million"]
            amount = (numerator + 999999) // 1000000
            if amount <= 9007199254740991:
                charge.settled_microunits, charge.state = amount, "known"
            else:
                # Preserve the real call Usage even if an extreme price/Usage
                # product cannot be represented safely by the financial API.
                charge.state = "unknown"
        else:
            charge.state = "unknown"  # retains reservation; never invent zero usage


def close_task_budget_reservations(session, task_id):
    """Close unsettled calls as unknown on terminal Task transition, not callback delivery."""
    for charge in session.scalars(select(AiBudgetCharge).where(
        AiBudgetCharge.task_id == task_id, AiBudgetCharge.state == "reserved",
    )):
        charge.state = "unknown"
