"""Persistence for project providers and fenced platform model-call evidence."""

from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from domain import utc_now
from domain.capability import ModelPolicyDefinition
from domain.outbound_operation import OutboundEvidence
from domain.platform_model import (
    ModelCallConflict,
    ModelConfigurationError,
    ModelProviderConflict,
    ModelProviderNotFound,
    ModelProviderRecord,
    ModelUsage,
)
from domain.task import TaskNotFound
from domain.task_execution import TaskCancellationRequested
from infra.store.model import (
    AiCapability,
    AiCapabilityVersion,
    AiProject,
    AiTask,
    AiWorkflowNode,
)
from infra.store.model.ai_model_provider import AiPlatformModelProvider
from infra.store.model.ai_platform_model_call import AiPlatformModelCall
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution import ExecutionWriteMode, active_execution
from infra.store.repository.execution_effects import (
    record_model_audit,
    record_model_call_settled,
    record_model_call_started,
)
from infra.store.repository.outcome_effects import record_outbound_evidence
from infra.store.repository.workflow_scope import declared_workflow_activity

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^MODEL_[A-Z0-9_]{1,90}$")


def _permits_fallback(call):
    """Only coherent authoritative call evidence can permit another paid attempt."""
    if call.status != "failed" or call.error_code != "MODEL_UPSTREAM_UNAVAILABLE" or call.http_status != 429:
        return False
    try:
        evidence = OutboundEvidence.model_validate(getattr(call, "outbound_evidence", None))
    except ValueError:
        return False
    return (evidence.retry_permission == "safe_response" and evidence.http_status == call.http_status and
            evidence.actual_model == call.actual_model and evidence.request_id == call.provider_request_id and
            all(getattr(call, key) == value for key, value in evidence.usage.model_dump().items()))


class ModelRepository(BaseRepository):
    def check_ready(self):
        with self._read() as session:
            session.execute(select(AiPlatformModelProvider).limit(0))
            session.execute(select(AiPlatformModelCall).limit(0))

    @staticmethod
    def _record(row):
        return ModelProviderRecord.model_validate({name: getattr(row, name) for name in ModelProviderRecord.model_fields})

    def create_provider(self, project_key, spec, actor):
        with self._sf.begin() as session:
            project = session.execute(select(AiProject).where(AiProject.key == project_key).with_for_update()).scalar_one_or_none()
            if project is None or not project.enabled:
                raise ModelProviderNotFound("Project is unavailable")
            if session.get(AiPlatformModelProvider, (project_key, spec.key)) is not None:
                raise ModelProviderConflict("Provider key already exists")
            row = AiPlatformModelProvider(
                project_key=project_key, key=spec.key, name=spec.name, base_url=spec.base_url,
                credential_ref=spec.credential_ref, models=list(spec.models), max_tokens=spec.max_tokens,
                enabled=True, created_by=actor, create_time=utc_now(),
            )
            session.add(row)
            record_model_audit(session, project_key, None, None, actor, "model_provider_created")
            try:
                session.flush()
            except IntegrityError as exc:
                raise ModelProviderConflict("Provider key already exists") from exc
            return self._record(row)

    def list_providers(self, project_key, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Invalid limit")
        with self._read() as session:
            rows = session.scalars(select(AiPlatformModelProvider).where(
                AiPlatformModelProvider.project_key == project_key
            ).order_by(AiPlatformModelProvider.key).limit(limit)).all()
            return [self._record(row) for row in rows]

    def provider(self, project_key, key):
        with self._read() as session:
            row = session.get(AiPlatformModelProvider, (project_key, key))
            if row is None:
                raise ModelProviderNotFound("Provider not found")
            return self._record(row)

    def set_provider_enabled(self, project_key, key, enabled, actor):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        with self._sf.begin() as session:
            row = session.execute(select(AiPlatformModelProvider).where(
                AiPlatformModelProvider.project_key == project_key, AiPlatformModelProvider.key == key
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                raise ModelProviderNotFound("Provider not found")
            row.enabled = enabled
            record_model_audit(session, project_key, None, None, actor, "model_provider_enabled" if enabled else "model_provider_disabled")
            session.flush()
            return self._record(row)

    @staticmethod
    def _lease(session, lease, *, lock=True):
        return active_execution(session, lease, lock=lock)

    def start_call(self, lease, node_id, *, policy_key, policy_version, policy_hash, requested_model, input_hash):
        if not all(isinstance(value, str) and _HASH.fullmatch(value) for value in (policy_hash, input_hash)):
            raise ModelConfigurationError("Model call configuration is invalid")
        with self._sf.begin() as session:
            scope = self._lease(session, lease)
            if scope is None:
                return None
            task, attempt = scope
            node = session.execute(select(AiWorkflowNode).where(
                AiWorkflowNode.id == node_id, AiWorkflowNode.task_id == task.id,
                AiWorkflowNode.task_attempt_id == attempt.id,
            ).with_for_update()).scalar_one_or_none()
            if node is None or node.kind != "llm" or node.status != "running":
                raise ModelConfigurationError("Model call configuration is invalid")
            try:
                definition_node, _, reference = declared_workflow_activity(session, task, node)
            except ValueError:
                raise ModelConfigurationError("Model call configuration is invalid") from None
            if (definition_node.kind != "llm" or reference is None or
                    (reference.key, reference.version) != (policy_key, policy_version)):
                raise ModelConfigurationError("Model call configuration is invalid")
            policy = session.execute(select(AiCapability, AiCapabilityVersion).join(
                AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id
            ).where(AiCapability.project_key == task.project_key, AiCapability.key == policy_key,
                    AiCapability.kind == "model_policy", AiCapabilityVersion.version == policy_version,
                    AiCapabilityVersion.status == "published", AiCapabilityVersion.content_hash == policy_hash)).one_or_none()
            if policy is None:
                raise ModelConfigurationError("Model call configuration is invalid")
            cap, artifact = policy
            try:
                definition = ModelPolicyDefinition.model_validate(artifact.definition)
            except Exception as exc:
                raise ModelConfigurationError("Model call configuration is invalid") from exc
            provider = session.get(AiPlatformModelProvider, (task.project_key, definition.provider_key))
            candidates = [definition.model, *definition.fallback_models]
            if (provider is None or not provider.enabled or requested_model not in candidates
                    or requested_model not in provider.models or definition.max_tokens > provider.max_tokens
                    or (definition.credential_ref is not None and definition.credential_ref != provider.credential_ref)):
                raise ModelConfigurationError("Model call configuration is unavailable")
            previous = session.scalars(select(AiPlatformModelCall).where(
                AiPlatformModelCall.node_id == node_id
            ).order_by(AiPlatformModelCall.start_time, AiPlatformModelCall.id).with_for_update()).all()
            if any(row.status == "running" for row in previous):
                raise ModelCallConflict("A model call is already running")
            if (any(not _permits_fallback(row) for row in previous)
                    or any(row.input_hash != input_hash or row.policy_hash != policy_hash for row in previous)
                    or [row.requested_model for row in previous] != candidates[:len(previous)]
                    or len(previous) >= len(candidates) or requested_model != candidates[len(previous)]):
                raise ModelCallConflict("Model call cannot be replayed")
            prompt_key = prompt_version = prompt_hash = None
            prompt = definition_node.prompt.model_dump() if definition_node.prompt is not None else None
            if prompt is not None:
                if not isinstance(prompt, dict) or not isinstance(prompt.get("key"), str) or not isinstance(prompt.get("version"), str):
                    raise ModelConfigurationError("Model call configuration is invalid")
                found = session.execute(select(AiCapability, AiCapabilityVersion).join(
                    AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id
                ).where(AiCapability.project_key == task.project_key, AiCapability.key == prompt["key"],
                        AiCapability.kind == "prompt", AiCapabilityVersion.version == prompt["version"],
                        AiCapabilityVersion.status == "published")).one_or_none()
                if found is None:
                    raise ModelConfigurationError("Model call configuration is unavailable")
                prompt_key, prompt_version = prompt["key"], prompt["version"]
                prompt_hash = found[1].content_hash
            call_id = uuid4().hex
            try:
                return self._insert_call(session, lease, node_id, task, attempt, cap, provider,
                                         policy_key, policy_version, policy_hash, requested_model, input_hash,
                                         prompt_key, prompt_version, prompt_hash, call_id)
            except IntegrityError:
                raise ModelCallConflict("Model call conflicts with existing evidence") from None

    def _insert_call(self, session, lease, node_id, task, attempt, cap, provider, policy_key, policy_version,
                     policy_hash, requested_model, input_hash, prompt_key, prompt_version, prompt_hash, call_id):
        inserted = session.execute(text("""
                INSERT INTO ai_platform_model_call
                (id, project_key, task_id, task_attempt_id, node_id, policy_capability_id, policy_key,
                 policy_version, policy_hash, provider_key, requested_model, prompt_key, prompt_version,
                 prompt_hash, status, input_hash, start_time)
                SELECT :id, t.project_key, t.id, a.id, n.id, :policy_id, :policy_key, :policy_version,
                       :policy_hash, :provider_key, :model, :prompt_key, :prompt_version, :prompt_hash,
                       'running', :input_hash, clock_timestamp()
                FROM ai_task t JOIN ai_task_attempt a ON a.task_id=t.id
                JOIN ai_workflow_node n ON n.task_id=t.id AND n.task_attempt_id=a.id
                WHERE t.id=:task AND t.status='running' AND t.cancel_request_id IS NULL AND a.id=:attempt AND a.worker_id=:worker
                  AND a.token=:token AND a.status='running' AND a.lease_expires_at > clock_timestamp()
                  AND n.id=:node AND n.status='running'
                RETURNING id
            """), {"id": call_id, "policy_id": cap.id, "policy_key": policy_key,
                "policy_version": policy_version, "policy_hash": policy_hash, "provider_key": provider.key,
                "model": requested_model, "prompt_key": prompt_key, "prompt_version": prompt_version,
                "prompt_hash": prompt_hash, "input_hash": input_hash, "task": task.id,
                "attempt": attempt.id, "worker": lease.worker_id, "token": lease.token, "node": node_id}).scalar_one_or_none()
        if inserted is None:
            return None
        record_model_call_started(session, task.project_key, task.id, call_id, lease.worker_id)
        return call_id

    def finish_call(self, lease, call_id, *, status, actual_model=None, output_hash=None, usage=None,
                    error_code=None, http_status=None, provider_request_id=None, evidence=None):
        if evidence is not None:
            evidence = OutboundEvidence.model_validate(evidence)
            actual_model, usage = evidence.actual_model, evidence.usage
            http_status, provider_request_id = evidence.http_status, evidence.request_id
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Invalid model call status")
        if status == "succeeded":
            if not isinstance(actual_model, str) or not isinstance(output_hash, str) or not _HASH.fullmatch(output_hash) or error_code is not None:
                raise ValueError("Invalid successful model call metadata")
        elif output_hash is not None or not isinstance(error_code, str) or not _SAFE_CODE.fullmatch(error_code):
            raise ValueError("Invalid terminal model call metadata")
        if http_status is not None and (isinstance(http_status, bool) or not isinstance(http_status, int) or not 100 <= http_status <= 599):
            raise ValueError("Invalid HTTP status")
        if provider_request_id is not None and (not isinstance(provider_request_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,512}", provider_request_id) is None):
            raise ValueError("Invalid provider request id")
        if usage is not None and not isinstance(usage, ModelUsage):
            raise ValueError("Invalid model usage")
        if actual_model is not None and (not isinstance(actual_model, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,199}", actual_model) is None):
            raise ValueError("Invalid actual model")
        data = usage.model_dump() if usage is not None else {}
        with self._sf.begin() as session:
            scope = active_execution(session, lease, mode=ExecutionWriteMode.SETTLE)
            if scope is None:
                return False
            task, attempt = scope
            discarded = task.cancel_request_id is not None and status == "succeeded"
            if discarded:
                status, output_hash, error_code = "cancelled", None, "MODEL_CANCELLED_RESPONSE_DISCARDED"
                if evidence is not None:
                    evidence = evidence.with_acceptance(False)
            # Conditional update is the final lease and running-node fence.
            row = session.execute(text("""
                UPDATE ai_platform_model_call c SET status=:status, actual_model=:actual,
                    output_hash=:output_hash, prompt_tokens=:prompt_tokens, completion_tokens=:completion_tokens,
                    total_tokens=:total_tokens, http_status=:http_status, provider_request_id=:request_id,
                    error_code=:error, outbound_evidence=CAST(:outbound_evidence AS jsonb), finish_time=clock_timestamp()
                WHERE c.id=:call AND c.task_id=:task AND c.task_attempt_id=:attempt AND c.status='running'
                  AND EXISTS (SELECT 1 FROM ai_task t JOIN ai_task_attempt a ON a.task_id=t.id
                    JOIN ai_workflow_node n ON n.task_id=t.id AND n.task_attempt_id=a.id
                    WHERE t.id=c.task_id AND t.status='running' AND a.id=c.task_attempt_id
                    AND a.id=:attempt AND a.worker_id=:worker AND a.token=:token
                    AND a.status='running' AND a.lease_expires_at > clock_timestamp()
                    AND n.id=c.node_id AND n.status='running') RETURNING c.id
            """), {"status": status, "actual": actual_model, "output_hash": output_hash,
                "prompt_tokens": data.get("prompt_tokens"), "completion_tokens": data.get("completion_tokens"),
                "total_tokens": data.get("total_tokens"), "http_status": http_status,
                "request_id": provider_request_id, "error": error_code, "call": call_id,
                "outbound_evidence": evidence.model_dump_json() if evidence is not None else None,
                "task": task.id, "attempt": attempt.id, "worker": lease.worker_id, "token": lease.token}).scalar_one_or_none()
            if row is None:
                return False
            if evidence is not None:
                record_outbound_evidence(session, task.project_key, task.id, call_id, lease.worker_id, evidence)
            record_model_call_settled(session, task.project_key, task.id, call_id, lease.worker_id, "model_call_" + status)
        if discarded:
            raise TaskCancellationRequested("Model response settled but not accepted")
        return True

    def calls(self, project_key, task_id, *, allowed_keys, owner):
        from infra.store.repository.task import TaskRepository

        with self._read() as session:
            visible = TaskRepository._visible(project_key, allowed_keys, owner).where(AiTask.id == task_id)
            if session.execute(visible.with_only_columns(AiTask.id)).scalar_one_or_none() is None:
                raise TaskNotFound("Task not found")
            rows = session.execute(select(AiPlatformModelCall).where(
                AiPlatformModelCall.project_key == project_key, AiPlatformModelCall.task_id == task_id
            ).order_by(AiPlatformModelCall.start_time, AiPlatformModelCall.id).limit(500)).scalars().all()
            return [row.to_record() for row in rows]
