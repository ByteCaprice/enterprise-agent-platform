"""Pure assessment of whether a failed Workflow may be resubmitted as new work."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from domain.capability import CapabilityError, CapabilityVersionDraft
from domain.workflow import WorkflowDefinition
from domain.workflow_recovery import RecoveryReason, WorkflowRecoveryAssessment
from framework.workflow_plan import WorkflowPlanError, compile_workflow

if TYPE_CHECKING:
    from domain.task import TaskRecord


def assess_workflow_recovery(
    source: TaskRecord, artifact: CapabilityVersionDraft, artifact_hash: str
) -> WorkflowRecoveryAssessment:
    """Assess immutable source/artifact metadata; never inspects checkpoints or runtime availability."""
    reason: RecoveryReason = "eligible"
    eligible = True
    if source.capability_kind != "workflow":
        eligible, reason = False, "unsupported_source"
    elif source.status not in {"failed", "timed_out", "cancelled"}:
        eligible, reason = False, "source_not_terminal_unsuccessful"

    try:
        actual_hash = artifact.fingerprint()
    except CapabilityError:
        actual_hash = ""
    if eligible and (artifact_hash != source.content_hash or actual_hash != source.content_hash):
        eligible, reason = False, "artifact_hash_mismatch"

    if eligible:
        try:
            definition = WorkflowDefinition.model_validate(artifact.definition)
            plan = compile_workflow(definition)
            if any(node.kind != "rule" for node in plan.nodes):
                eligible, reason = False, "unsupported_node_kind"
        except WorkflowPlanError as exc:
            eligible = False
            reason = (
                "retry_policy_unsupported" if exc.code == WorkflowPlanError.RETRY_NOT_SUPPORTED else "invalid_workflow"
            )
        except (ValidationError, TypeError, ValueError, RecursionError):
            eligible, reason = False, "invalid_workflow"

    return WorkflowRecoveryAssessment(
        source_task_id=source.id,
        version=source.version,
        content_hash=source.content_hash,
        eligible=eligible,
        reason=reason,
    )
