"""Observational outbound audit effects, never paid-retry authority.

Callers own the fenced transaction and persist authoritative evidence on the call.
"""

from domain import LogEvent
from infra.store.model.ai_run_log import AiRunLog


def record_outbound_evidence(session, project_key, task_id, call_id, actor, evidence):
    """Append safe outcome audit without reading or authorizing call state."""
    session.add(AiRunLog.from_domain(LogEvent(
        trace_id=f"task:{task_id}", component="model_audit", event_type="model_outbound_outcome",
        message="model outbound outcome",
        data={"project_key": project_key, "task_id": task_id, "call_id": call_id, "actor": actor,
              "outcome": evidence.model_dump(mode="json"), "usage_certainty": evidence.usage_certainty},
    )))
