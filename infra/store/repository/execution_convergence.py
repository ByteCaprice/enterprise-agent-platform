"""Atomic terminal evidence convergence shared by completion, stop and recovery."""

from sqlalchemy import text


def converge_execution_evidence(session, task_id, attempt_id, finished, reason, *, cancelled=False):
    workflow_error = reason
    reason = reason.removesuffix("_RESULT_UNKNOWN")[:60]
    scope = {"task": task_id, "attempt": attempt_id, "finished": finished,
             "error": f"AGENT_{reason}_RESULT_UNKNOWN"}
    session.execute(text("""
        UPDATE ai_agent_step SET status=CASE WHEN status='pending' THEN 'cancelled' ELSE 'failed' END,
          error_code=CASE WHEN status='pending' THEN 'AGENT_NOT_STARTED' ELSE :error END,
          finish_time=:finished, output_hash=NULL, final_result_hash=NULL, decision=NULL
        WHERE task_id=:task AND task_attempt_id=:attempt AND status IN ('pending','running')
    """), scope)
    session.execute(text("""
        UPDATE ai_agent_execution SET status=:status, error_code=:error, finish_time=:finished, output_hash=NULL
        WHERE task_id=:task AND task_attempt_id=:attempt AND status='running'
    """), {**scope, "status": "cancelled" if cancelled else "failed",
            "error": "AGENT_CANCELLED" if cancelled else scope["error"]})
    for kind in ("model", "tool"):
        session.execute(text(f"""
            UPDATE ai_platform_{kind}_call SET status='failed', error_code=:error,
                output_hash=NULL, finish_time=:finished
            WHERE task_id=:task AND task_attempt_id=:attempt AND status='running'
        """), {**scope, "error": f"{kind.upper()}_{reason}_RESULT_UNKNOWN"})
    session.execute(text("""
        UPDATE ai_workflow_node SET status=:status, error_code=:error,
            finish_time=:finished, output=NULL, output_hash=NULL
        WHERE task_id=:task AND task_attempt_id=:attempt AND status='running'
    """), {**scope, "status": "cancelled" if cancelled else "failed",
            "error": "TASK_CANCELLED_RESULT_UNKNOWN" if cancelled else workflow_error})
