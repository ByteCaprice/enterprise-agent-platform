"""Governed model calls for the new platform, with transport and credentials ports.

Policy selects an immutable project Provider/catalog. It cannot replace URLs,
headers or credentials. Every outbound attempt is separately accounted; explicit
fallback never turns an ambiguous timeout into an automatic paid replay.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from domain.capability import CapabilityDependency
from domain.execution_context import ExecutionDeadlineExceeded
from domain.platform_model import (
    CredentialResolver,
    ModelCallConflict,
    ModelConfigurationError,
    ModelMessages,
    ModelTransport,
    ModelTransportFailure,
    ModelTransportRequest,
    ModelTransportResponse,
)
from domain.schema_validation import JSONValidationUnavailable
from domain.task import canonical_json
from domain.task_execution import ExecutionFailure, TaskCancellationRequested, TaskClaim, TaskLeaseLost
from infra.schema_validation import validate_json
from orchestration.model_policy_resolver import ModelPolicyResolver, ResolvedModelPolicy


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Non-finite JSON value")


class PlatformModelGateway(ModelPolicyResolver):
    def __init__(self, store, transport: ModelTransport, credentials: CredentialResolver):
        super().__init__(store)
        self.transport = transport
        self.credentials = credentials

    @staticmethod
    def _output(reply: ModelTransportResponse, resolved: ResolvedModelPolicy) -> dict:
        try:
            if not isinstance(reply.content, str):
                raise ValueError("Model response content must be text")
            if resolved.policy.response_format == "text":
                output = {"text": reply.content}
            else:
                output = json.loads(reply.content, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            if not isinstance(output, dict):
                raise ValueError("Expected object output")
            canonical_json(output)
        except Exception:
            raise ExecutionFailure("MODEL_OUTPUT_INVALID") from None
        try:
            validate_json(output, resolved.artifact.output_schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("MODEL_VALIDATION_UNAVAILABLE") from None
        except Exception:
            raise ExecutionFailure("MODEL_OUTPUT_SCHEMA_INVALID") from None
        return output

    async def _finish(self, claim, call_id, *, status, reply=None, output_hash=None, error_code=None, http_status=None):
        finished = await asyncio.to_thread(
            self.store.models.finish_call, claim.lease, call_id, status=status,
            actual_model=reply.actual_model if reply is not None else None,
            output_hash=output_hash, usage=reply.usage if reply is not None else None,
            error_code=error_code, http_status=reply.http_status if reply is not None else http_status,
            provider_request_id=reply.request_id if reply is not None else None,
        )
        if not finished:
            raise TaskLeaseLost("Model call no longer owns its execution lease")

    async def complete(self, claim: TaskClaim, node_id: str, reference: CapabilityDependency,
                        messages: ModelMessages, *, prompt: CapabilityDependency | None = None) -> dict:
        resolved = await asyncio.to_thread(self.resolve_policy, claim, reference, prompt)
        try:
            payload = messages.model_dump(mode="json")
            canonical_json(payload)
            await asyncio.to_thread(validate_json, payload, resolved.artifact.input_schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("MODEL_VALIDATION_UNAVAILABLE") from None
        except Exception:
            raise ExecutionFailure("MODEL_INPUT_INVALID") from None
        fingerprint = hashlib.sha256(canonical_json({"input": payload, "policy_hash": resolved.artifact.content_hash}).encode()).hexdigest()
        candidates = [resolved.policy.model, *resolved.policy.fallback_models]
        for index, candidate in enumerate(candidates):
            try:
                claim.limit_timeout(resolved.policy.timeout_seconds)
            except ExecutionDeadlineExceeded:
                raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
            # Recheck live grants/provider disablement before each potentially paid attempt.
            current = await asyncio.to_thread(self.resolve_policy, claim, reference, prompt)
            if current.artifact.content_hash != resolved.artifact.content_hash:
                raise ExecutionFailure("MODEL_POLICY_CHANGED")
            try:
                secret = await asyncio.to_thread(self.credentials.resolve, claim.task.project_key, current.provider.credential_ref)
            except ModelTransportFailure as exc:
                raise ExecutionFailure(exc.code) from None
            except Exception:
                raise ExecutionFailure("MODEL_CREDENTIAL_UNAVAILABLE") from None
            try:
                claim.limit_timeout(current.policy.timeout_seconds)
            except ExecutionDeadlineExceeded:
                raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
            try:
                call_id = await asyncio.to_thread(
                    self.store.models.start_call, claim.lease, node_id, policy_key=reference.key,
                    policy_version=reference.version, policy_hash=resolved.artifact.content_hash,
                    requested_model=candidate, input_hash=fingerprint,
                )
            except ModelCallConflict:
                raise ExecutionFailure("MODEL_CALL_CONFLICT") from None
            except ModelConfigurationError:
                raise ExecutionFailure("MODEL_CONFIGURATION_UNAVAILABLE") from None
            except (TaskCancellationRequested, ExecutionFailure):
                raise
            except Exception:
                raise ExecutionFailure("MODEL_ACCOUNTING_UNAVAILABLE") from None
            if call_id is None:
                raise TaskLeaseLost("Model call no longer owns its execution lease")
            try:
                timeout_seconds = claim.limit_timeout(current.policy.timeout_seconds)
            except ExecutionDeadlineExceeded:
                await self._finish(claim, call_id, status="failed", error_code="MODEL_EXECUTION_DEADLINE_EXCEEDED")
                raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
            request = ModelTransportRequest(
                base_url=current.provider.base_url, model=candidate, messages=tuple(messages.messages), secret=secret,
                timeout_seconds=timeout_seconds, max_tokens=current.policy.max_tokens,
                response_format=current.policy.response_format, temperature=current.policy.temperature,
            )
            reply = None
            try:
                async with asyncio.timeout(timeout_seconds):
                    reply = await self.transport.complete(request)
                try:
                    claim.limit_timeout(current.policy.timeout_seconds)
                except ExecutionDeadlineExceeded:
                    raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
            except ExecutionFailure as exc:
                await self._finish(claim, call_id, status="failed", reply=reply, error_code=exc.code)
                raise
            except asyncio.CancelledError:
                await self._finish(claim, call_id, status="cancelled", error_code="MODEL_CANCELLED_RESULT_UNKNOWN")
                raise
            except ModelTransportFailure as exc:
                await self._finish(claim, call_id, status="failed", error_code=exc.code, http_status=exc.http_status)
                if exc.retryable and exc.code == "MODEL_UPSTREAM_UNAVAILABLE" and index + 1 < len(candidates):
                    continue
                raise ExecutionFailure(exc.code) from None
            except TimeoutError:
                try:
                    claim.limit_timeout(current.policy.timeout_seconds)
                except ExecutionDeadlineExceeded:
                    code = "MODEL_EXECUTION_DEADLINE_EXCEEDED"
                    await self._finish(claim, call_id, status="failed", error_code=code)
                    raise ExecutionFailure(code, status="timed_out") from None
                await self._finish(claim, call_id, status="failed", error_code="MODEL_UPSTREAM_TIMEOUT")
                raise ExecutionFailure("MODEL_UPSTREAM_TIMEOUT") from None
            except Exception:
                await self._finish(claim, call_id, status="failed", error_code="MODEL_TRANSPORT_FAILED")
                raise ExecutionFailure("MODEL_TRANSPORT_FAILED") from None
            try:
                claim.limit_timeout(current.policy.timeout_seconds)
                # A response does not entitle a revoked principal to receive a result.
                await asyncio.to_thread(self.resolve_policy, claim, reference, prompt)
                output = await asyncio.to_thread(self._output, reply, current)
            except asyncio.CancelledError:
                await self._finish(claim, call_id, status="cancelled", reply=reply,
                                   error_code="MODEL_CANCELLED_RESULT_UNKNOWN")
                raise
            except ExecutionFailure as exc:
                await self._finish(claim, call_id, status="failed", reply=reply, error_code=exc.code)
                raise
            except ExecutionDeadlineExceeded:
                code = "MODEL_EXECUTION_DEADLINE_EXCEEDED"
                await self._finish(claim, call_id, status="failed", reply=reply, error_code=code)
                raise ExecutionFailure(code, status="timed_out") from None
            output_hash = hashlib.sha256(canonical_json(output).encode()).hexdigest()
            try:
                claim.limit_timeout(current.policy.timeout_seconds)
            except ExecutionDeadlineExceeded:
                code = "MODEL_EXECUTION_DEADLINE_EXCEEDED"
                await self._finish(claim, call_id, status="failed", reply=reply, error_code=code)
                raise ExecutionFailure(code, status="timed_out") from None
            await self._finish(claim, call_id, status="succeeded", reply=reply, output_hash=output_hash)
            return output
        raise ExecutionFailure("MODEL_UPSTREAM_UNAVAILABLE")
