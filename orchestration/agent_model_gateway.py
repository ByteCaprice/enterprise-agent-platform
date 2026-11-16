"""Governed root Agent turns, using the shared model resolver and call ledger."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

from domain.agent_invocation import AgentInvocation
from domain.agent_turn import (
    AgentConversation,
    AgentToolDescriptor,
    AgentTurnRequest,
    AgentTurnResponse,
    AgentTurnTransport,
    strict_json,
)
from domain.capability import CapabilityDependency, CapabilityKind, SkillDefinition
from domain.capability_definitions import interpret_definition
from domain.execution_context import ExecutionDeadlineExceeded
from domain.execution_ports import AgentTurnLedger, ExecutionResources
from domain.outbound_operation import OutboundEvidence, evidence_from_exception
from domain.platform_agent import (
    AgentStepRecord,
    AgentStorageError,
    AgentToolIntent,
    AgentValidationUnavailable,
)
from domain.platform_model import CredentialResolver, ModelTransportFailure
from domain.schema_validation import JSONValidationUnavailable
from domain.task import canonical_json
from domain.task_execution import (
    ExecutionFailure,
    ExecutionResourceUnstoppable,
    TaskCancellationRequested,
    TaskLeaseLost,
)
from orchestration.execution_async import accounting_error, run_sync
from orchestration.model_policy_resolver import ModelPolicyResolver
from orchestration.platform_tool_gateway import PlatformToolGateway
from orchestration.published_capabilities import PublishedCapabilities


@dataclass(frozen=True)
class AcceptedAgentTurn:
    reply: AgentTurnResponse
    pending_steps: tuple[AgentStepRecord, ...]
    final_result: dict | None = field(repr=False)


@dataclass(frozen=True)
class ResolvedAgentSkill:
    key: str
    version: str
    content_hash: str
    instructions: str = field(repr=False)


class PlatformAgentModelGateway:
    def __init__(self, resources: ExecutionResources, policies: ModelPolicyResolver, turns: AgentTurnLedger,
                 transport: AgentTurnTransport, credentials: CredentialResolver, tools: PlatformToolGateway):
        self.resources = resources
        self.policies = policies
        self.turns = turns
        self.authorizer = resources.authorization
        self.catalog = PublishedCapabilities(resources.capabilities)
        self.transport = transport
        self.credentials = credentials
        self.tools = tools

    def resolve_agent(self, claim: AgentInvocation):
        try:
            claim.artifact.require_published(version=claim.reference.version)
            if claim.parent_node_id is None:
                if (claim.task.capability_kind != "agent" or claim.reference.key != claim.task.capability_key or
                        claim.artifact.content_hash != claim.task.content_hash):
                    raise ValueError
            elif (claim.task.capability_kind != "workflow" or
                  not self.authorizer.allowed_resource(claim, "agent:invoke", claim.reference.key)):
                raise ExecutionFailure("AGENT_ACCESS_DENIED")
            definition = interpret_definition("agent", claim.artifact).materialize()
            policy_ref = CapabilityDependency(**definition.model_policy.model_dump())
            prompt_ref = CapabilityDependency(**definition.instruction_prompt.model_dump())
            resolved = self.policies.resolve_agent_policy(claim, policy_ref, prompt_ref)
            catalog = []
            for tool in definition.tools:
                reference = CapabilityDependency(**tool.capability.model_dump())
                approved = self.tools.resolve(claim, reference)
                catalog.append(AgentToolDescriptor(alias=tool.alias, description=f"Approved tool {reference.key}@{reference.version}",
                                                   input_schema=approved.artifact.input_schema))
            skills = []
            for reference in definition.skills:
                if not self.authorizer.allowed_resource(claim, "skill:use", reference.key):
                    raise ExecutionFailure("AGENT_SKILL_ACCESS_DENIED")
                try:
                    artifact = self.catalog.resolve(claim.task.project_key, reference, CapabilityKind.SKILL)
                except ValueError:
                    raise ExecutionFailure("AGENT_SKILL_UNAVAILABLE") from None
                skill = SkillDefinition.model_validate(artifact.definition)
                skills.append(ResolvedAgentSkill(reference.key, reference.version, artifact.content_hash, skill.instructions))
            return definition, resolved, tuple(catalog), tuple(skills)
        except ExecutionFailure:
            raise
        except Exception:
            raise ExecutionFailure("AGENT_CONFIGURATION_UNAVAILABLE") from None

    async def _reject(self, claim, execution_id, step_id, code, *, reply=None, cancelled=False, evidence=None):
        evidence = reply.evidence if reply is not None else evidence or OutboundEvidence()
        if evidence.response != "not_received":
            evidence = evidence.with_acceptance(False)
        try:
            await run_sync(
                self.turns.reject_call, claim.lease, execution_id, step_id, error_code=code, cancelled=cancelled,
                evidence=evidence,
                _on_cancel_error=accounting_error("MODEL_ACCOUNTING_RESULT_UNKNOWN"),
            )
        except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
            raise
        except Exception:
            raise ExecutionFailure("MODEL_ACCOUNTING_RESULT_UNKNOWN") from None

    async def generate_turn(self, claim, execution_id, conversation: AgentConversation) -> AcceptedAgentTurn:
        definition, resolved, catalog, skills = await run_sync(self.resolve_agent, claim)
        payload = {**conversation.model_dump(mode="json"), "tools": [tool.model_dump(mode="json") for tool in catalog]}
        try:
            canonical_json(payload)
            await run_sync(self.resources.validation.validate, payload, resolved.artifact.input_schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("MODEL_VALIDATION_UNAVAILABLE") from None
        except Exception:
            raise ExecutionFailure("MODEL_INPUT_INVALID") from None
        try:
            claim.limit_timeout(resolved.policy.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        try:
            secret = await run_sync(self.credentials.resolve, claim.task.project_key, resolved.provider.credential_ref)
        except ModelTransportFailure as exc:
            raise ExecutionFailure(exc.code) from None
        except Exception:
            raise ExecutionFailure("MODEL_CREDENTIAL_UNAVAILABLE") from None
        fingerprint = hashlib.sha256(canonical_json({"input": payload, "policy_hash": resolved.artifact.content_hash,
                                                     "agent_hash": claim.artifact.content_hash}).encode()).hexdigest()
        try:
            claim.limit_timeout(resolved.policy.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        try:
            step, _ = await run_sync(self.turns.start_turn, claim.lease, execution_id, input_hash=fingerprint,
                                     _on_cancel_error=accounting_error("MODEL_ACCOUNTING_RESULT_UNKNOWN"))
        except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
            raise
        except AgentStorageError:
            raise ExecutionFailure("AGENT_TURN_NOT_AVAILABLE") from None
        except Exception:
            raise ExecutionFailure("MODEL_ACCOUNTING_RESULT_UNKNOWN") from None
        reply = None
        settlement_started = False
        try:
            try:
                timeout_seconds = claim.limit_timeout(resolved.policy.timeout_seconds)
            except ExecutionDeadlineExceeded:
                raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
            request = AgentTurnRequest(resolved.provider.base_url, resolved.policy.model, conversation, catalog, secret,
                                        timeout_seconds, resolved.policy.max_tokens, resolved.policy.temperature,
                                        request_profile=resolved.policy.request_profile)
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            async with asyncio.timeout(timeout_seconds):
                response = await self.transport.generate_turn(request)
            try:
                claim.limit_timeout(resolved.policy.timeout_seconds)
            except ExecutionDeadlineExceeded:
                reply = AgentTurnResponse.model_validate_json(response.model_dump_json())
                raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
            # Copy before awaits: transport-held nested values cannot change an accepted decision.
            reply = AgentTurnResponse.model_validate_json(response.model_dump_json())
            pending_task = asyncio.current_task()
            if pending_task is not None and pending_task.cancelling():
                raise asyncio.CancelledError
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            if reply.error_code is not None:
                raise ExecutionFailure(reply.error_code)
            current_definition, current, current_catalog, current_skills = await run_sync(self.resolve_agent, claim)
            if (current.artifact.content_hash != resolved.artifact.content_hash or current_catalog != catalog or
                    current_definition != definition or current_skills != skills):
                raise ExecutionFailure("MODEL_CONFIGURATION_CHANGED")
            assert reply.turn is not None
            turn = reply.turn
            try:
                await run_sync(self.resources.validation.validate, turn.model_dump(mode="json"),
                                        resolved.artifact.output_schema)
            except JSONValidationUnavailable:
                raise ExecutionFailure("MODEL_VALIDATION_UNAVAILABLE") from None
            final_result = None
            intents = []
            if turn.kind == "final":
                final_result = strict_json(turn.text)
                if not isinstance(final_result, dict):
                    raise ExecutionFailure("MODEL_OUTPUT_INVALID")
                canonical_json(final_result)
                try:
                    await run_sync(self.resources.validation.validate, final_result, claim.artifact.output_schema)
                except JSONValidationUnavailable:
                    raise ExecutionFailure("MODEL_VALIDATION_UNAVAILABLE") from None
            else:
                aliases = {tool.alias for tool in definition.tools}
                if any(call.alias not in aliases for call in turn.tool_calls):
                    raise ExecutionFailure("MODEL_TOOL_ALIAS_DENIED")
                intents = [AgentToolIntent(alias=call.alias, source_call_id=call.id, arguments=call.arguments)
                           for call in turn.tool_calls]
            output_hash = hashlib.sha256(canonical_json(turn.model_dump(mode="json")).encode()).hexdigest()
            try:
                claim.limit_timeout(resolved.policy.timeout_seconds)
            except ExecutionDeadlineExceeded:
                raise ExecutionFailure("MODEL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
            try:
                settlement_started = True
                steps = await run_sync(
                    self.turns.accept_turn, claim.lease, execution_id, step.id,
                    output_hash=output_hash, actual_model=reply.actual_model, usage=reply.usage,
                    http_status=reply.http_status, request_id=reply.request_id, final_result=final_result, intents=intents,
                    evidence=reply.evidence.with_acceptance(True),
                    _on_cancel_error=accounting_error("MODEL_ACCOUNTING_RESULT_UNKNOWN"),
                )
            except AgentValidationUnavailable:
                raise ExecutionFailure("MODEL_VALIDATION_UNAVAILABLE") from None
            except AgentStorageError:
                raise ExecutionFailure("MODEL_AGENT_TURN_REJECTED") from None
            except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
                raise
            except Exception:
                raise ExecutionFailure("MODEL_ACCOUNTING_RESULT_UNKNOWN") from None
        except TaskCancellationRequested:
            if settlement_started:
                raise
            await self._reject(claim, execution_id, step.id, "MODEL_CANCELLED_RESPONSE_DISCARDED",
                               reply=reply, cancelled=True)
            raise
        except (TaskLeaseLost, ExecutionResourceUnstoppable):
            raise
        except asyncio.CancelledError as exc:
            if settlement_started:
                raise
            await self._reject(claim, execution_id, step.id, "MODEL_CANCELLED_RESULT_UNKNOWN", reply=reply, cancelled=True,
                               evidence=evidence_from_exception(exc))
            raise
        except (TimeoutError, ModelTransportFailure) as exc:
            if settlement_started:
                raise ExecutionFailure("MODEL_ACCOUNTING_RESULT_UNKNOWN") from None
            if isinstance(exc, TimeoutError):
                try:
                    claim.limit_timeout(resolved.policy.timeout_seconds)
                except ExecutionDeadlineExceeded:
                    code = "MODEL_EXECUTION_DEADLINE_EXCEEDED"
                else:
                    code = "MODEL_UPSTREAM_TIMEOUT"
            else:
                code = exc.code
            await self._reject(claim, execution_id, step.id, code, reply=reply,
                               evidence=evidence_from_exception(exc))
            status = "timed_out" if code == "MODEL_EXECUTION_DEADLINE_EXCEEDED" else "failed"
            raise ExecutionFailure(code, status=status) from None
        except ExecutionFailure as exc:
            if settlement_started or exc.code.endswith("ACCOUNTING_RESULT_UNKNOWN"):
                raise
            code = exc.code if exc.code.startswith("MODEL_") else "MODEL_AGENT_ACCESS_DENIED"
            await self._reject(claim, execution_id, step.id, code, reply=reply)
            raise ExecutionFailure(code, status=exc.status) from None
        except Exception:
            if settlement_started:
                raise ExecutionFailure("MODEL_ACCOUNTING_RESULT_UNKNOWN") from None
            await self._reject(claim, execution_id, step.id, "MODEL_AGENT_TURN_INVALID", reply=reply)
            raise ExecutionFailure("MODEL_AGENT_TURN_INVALID") from None
        return AcceptedAgentTurn(reply, tuple(steps), final_result)
