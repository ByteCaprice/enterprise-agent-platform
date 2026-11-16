"""Small mock-provider integration group; never calls a real model endpoint."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture
from worker_support import model_gateway

from domain.capability import CapabilityDependency, CapabilitySpec, CapabilityVersionDraft
from domain.capability_definitions import WORKFLOW_CHECKPOINT_PROFILE, WORKFLOW_INTERPRETER_PROFILE
from domain.platform_model import ModelMessage, ModelMessages, ModelTransportFailure, ModelTransportResponse, ModelUsage
from domain.task_execution import ExecutionFailure
from infra.model_credentials import EnvironmentCredentialResolver, credential_variable_name
from infra.schema_validation import IsolatedJSONValidator
from interfaces.worker.platform import run_workflow_worker
from orchestration.capability_service import CapabilityService

inbox = inbox_fixture
PROVIDERS = "/v2/projects/payments/model-providers"


class MockModel:
    def __init__(self, *, failure=None, after=None):
        self.requests = []
        self.failure = failure
        self.after = after

    async def complete(self, request):
        self.requests.append(request)
        if self.after:
            await self.after()
        if self.failure and (len(self.requests) == 1 or self.failure.code != "MODEL_UPSTREAM_UNAVAILABLE"):
            raise self.failure
        return ModelTransportResponse(content='{"answer":"synthetic-approved"}', actual_model="catalog/snapshot",
                                      usage=ModelUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6),
                                      request_id="synthetic-request")


def literal(value):
    return {"source": "literal", "value": value}


def node_value(node, pointer):
    return {"source": "node", "node": node, "pointer": pointer}


def setup_model(client, store, monkeypatch, *, fallback=False, prompt=True):
    response = client.post(PROVIDERS, headers=ADMIN, json={
        "key": "mock", "name": "Mock catalog", "base_url": "https://mock-provider.example.invalid/v1",
        "credential_ref": "mock-key", "models": ["catalog/primary", "catalog/fallback"], "max_tokens": 256,
    })
    assert response.status_code == 201
    monkeypatch.setenv(credential_variable_name("payments", "mock-key"), "synthetic-provider-secret")
    service = CapabilityService(store.capabilities, IsolatedJSONValidator())

    def publish(key, kind, definition, **kwargs):
        service.create("payments", CapabilitySpec(key=key, kind=kind, name=key), "task-admin")
        service.create_draft("payments", key, CapabilityVersionDraft(version="1", definition=definition, **kwargs), "task-admin")
        service.publish("payments", key, "1", "task-admin")

    output_schema = {"type": "object", "properties": {"answer": {"type": "string"}},
                     "required": ["answer"], "additionalProperties": False}
    publish("policy", "model_policy", {"provider_key": "mock", "model": "catalog/primary", "max_tokens": 128,
                                      "fallback_models": ["catalog/fallback"] if fallback else []},
            input_schema={"type": "object", "required": ["messages"]}, output_schema=output_schema)
    dependencies = [{"key": "policy", "version": "1"}]
    llm = {"key": "llm", "kind": "llm", "capability": {"key": "policy", "version": "1"},
           "depends_on": ["prepare"]}
    if prompt:
        publish("review-prompt", "prompt", {"template": "Review ${name}"},
                input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
        llm["prompt"] = {"key": "review-prompt", "version": "1"}
        llm["input_mapping"] = {"name": node_value("prepare", "/name")}
        dependencies.append({"key": "review-prompt", "version": "1"})
    else:
        llm["input_mapping"] = {"messages": literal([{"role": "user", "content": "Synthetic request"}])}
    publish("model-flow", "workflow", {"nodes": [
        {"key": "prepare", "kind": "rule", "rule": {"operation": "project"},
         "input_mapping": {"name": {"source": "input", "pointer": "/name"}}},
        llm,
        {"key": "finish", "kind": "rule", "rule": {"operation": "project"}, "depends_on": ["llm"],
         "input_mapping": {"answer": node_value("llm", "/answer")}},
    ], "output_mapping": {"answer": node_value("finish", "/answer")}},
            dependencies=dependencies, output_schema=output_schema)
    return response.json()


def grant(client, identity, action, resource):
    response = client.post(f"/v2/projects/payments/service-identities/{identity['id']}/grants", headers=ADMIN,
                           json={"action": action, "resource_key": resource})
    assert response.status_code == 201
    return response.json()


def execute(store, transport):
    gateway = model_gateway(store, transport, EnvironmentCredentialResolver())
    return asyncio.run(run_workflow_worker(store, once=True, gateway=gateway))


def test_prompt_policy_llm_rules_result_and_safe_usage_are_one_real_pipeline(inbox, monkeypatch):
    client, store = inbox
    setup_model(client, store, monkeypatch)
    identity, _, headers = service_identity(store)
    grant(client, identity, "model:invoke", "policy")
    grant(client, identity, "prompt:render", "review-prompt")
    target = submit(client, headers=headers, capability_key="model-flow").headers["Location"]
    transport = MockModel()
    assert execute(store, transport)
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.model == "catalog/primary" and request.messages[0].content == "Review Synthetic customer"
    assert request.secret.value == "synthetic-provider-secret"
    assert client.get(target + "/result", headers=headers).json()["result"] == {"answer": "synthetic-approved"}
    assert [row["status"] for row in client.get(target + "/nodes", headers=headers).json()] == ["succeeded"] * 3
    response = client.get(target + "/model-calls", headers=headers)
    assert response.headers["Cache-Control"] == "no-store"
    call = response.json()[0]
    assert call["status"] == "succeeded" and call["actual_model"] == "catalog/snapshot" and call["total_tokens"] == 6
    assert call["prompt_key"] == "review-prompt" and call["prompt_hash"]
    assert not {"input", "output", "messages", "secret", "token", "base_url"} & call.keys()
    assert "Synthetic customer" not in response.text and "synthetic-provider-secret" not in response.text
    _, _, other = service_identity(store, key="another-system")
    assert client.get(target + "/model-calls", headers=other).status_code == 404
    assert client.get(PROVIDERS, headers=headers).status_code == 403
    assert client.patch(PROVIDERS + "/mock", headers=ADMIN, json={"enabled": False}).status_code == 200


@pytest.mark.parametrize("missing", ["model", "prompt", "credential", "provider"])
def test_missing_authority_or_configuration_never_sends_a_model_request(inbox, monkeypatch, missing):
    client, store = inbox
    setup_model(client, store, monkeypatch)
    identity, _, headers = service_identity(store)
    if missing != "model":
        grant(client, identity, "model:invoke", "policy")
    if missing != "prompt":
        grant(client, identity, "prompt:render", "review-prompt")
    if missing == "credential":
        monkeypatch.delenv(credential_variable_name("payments", "mock-key"))
    if missing == "provider":
        assert client.patch(PROVIDERS + "/mock", headers=ADMIN, json={"enabled": False}).status_code == 200
    target = submit(client, headers=headers, capability_key="model-flow").headers["Location"]
    transport = MockModel()
    assert execute(store, transport) and not transport.requests
    result = client.get(target + "/result", headers=ADMIN).json()
    expected = {"model": "MODEL_ACCESS_DENIED", "prompt": "MODEL_PROMPT_ACCESS_DENIED",
                "credential": "MODEL_CREDENTIAL_UNAVAILABLE", "provider": "MODEL_PROVIDER_DISABLED"}
    assert result["status"] == "failed" and result["error_code"] == expected[missing]
    assert client.get(target + "/model-calls", headers=ADMIN).json() == []


@pytest.mark.parametrize("code,retryable,count,status", [
    ("MODEL_UPSTREAM_UNAVAILABLE", True, 2, "succeeded"),
    ("MODEL_UPSTREAM_TIMEOUT", True, 1, "failed"),
])
def test_fallback_records_each_candidate_but_never_replays_timeout(inbox, monkeypatch, code, retryable, count, status):
    client, store = inbox
    setup_model(client, store, monkeypatch, fallback=True, prompt=False)
    target = submit(client, capability_key="model-flow").headers["Location"]
    from domain.outbound_operation import OutboundEvidence

    evidence = (OutboundEvidence(dispatch="dispatched", response="received", acceptance="rejected", http_status=429,
                                 retry_permission="safe_response") if code == "MODEL_UPSTREAM_UNAVAILABLE" else
                OutboundEvidence())
    transport = MockModel(failure=ModelTransportFailure(code, evidence=evidence, retryable=retryable))
    assert execute(store, transport)
    assert len(transport.requests) == count
    calls = client.get(target + "/model-calls", headers=ADMIN).json()
    assert len(calls) == count and calls[0]["error_code"] == code
    assert client.get(target + "/result", headers=ADMIN).json()["status"] == status


def test_revoking_model_grant_while_reply_returns_prevents_delivery(inbox, monkeypatch):
    client, store = inbox
    setup_model(client, store, monkeypatch, prompt=False)
    identity, _, headers = service_identity(store)
    permission = grant(client, identity, "model:invoke", "policy")

    async def revoke():
        await asyncio.to_thread(store.governance.delete_grant, "payments", identity["id"], permission["id"], "task-admin")

    target = submit(client, headers=headers, capability_key="model-flow").headers["Location"]
    transport = MockModel(after=revoke)
    assert execute(store, transport)
    assert len(transport.requests) == 1
    result = client.get(target + "/result", headers=headers).json()
    assert result["status"] == "failed" and result["result"] is None and result["error_code"] == "MODEL_ACCESS_DENIED"
    assert client.get(target + "/model-calls", headers=headers).json()[0]["error_code"] == "MODEL_ACCESS_DENIED"


def test_call_cannot_be_replayed_and_database_terminal_nulls_are_rejected(inbox, monkeypatch):
    client, store = inbox
    setup_model(client, store, monkeypatch, prompt=False)
    submit(client, capability_key="model-flow")
    from worker_support import worker_capabilities

    claim = store.task_queue.claim("model-ledger-worker", capabilities=worker_capabilities("workflow"), lease_seconds=60)
    checkpoint = store.workflows.begin(claim.lease, "llm", "a" * 64,
                                       interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                       checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    transport = MockModel()
    gateway = model_gateway(store, transport, EnvironmentCredentialResolver())
    messages = ModelMessages(messages=[ModelMessage(role="user", content="Synthetic request")])
    reference = CapabilityDependency(key="policy", version="1")
    assert asyncio.run(gateway.complete(claim, checkpoint.record.id, reference, messages)) == {"answer": "synthetic-approved"}
    with pytest.raises(ExecutionFailure, match="MODEL_CALL_CONFLICT"):
        asyncio.run(gateway.complete(claim, checkpoint.record.id, reference, messages))
    assert len(transport.requests) == 1
    with store.engine.begin() as connection:
        for mutation in ("output_hash=NULL", "status='failed', output_hash=NULL, error_code=NULL",
                         "finish_time=start_time - interval '1 second'"):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text(f"UPDATE ai_platform_model_call SET {mutation}"))


def test_provider_api_rejects_secrets_endpoint_mutation_and_cross_project_lookup(inbox, monkeypatch):
    client, store = inbox
    setup_model(client, store, monkeypatch)
    assert client.get(PROVIDERS + "/mock", headers=ADMIN).status_code == 200
    assert client.get("/v2/projects/other/model-providers/mock", headers=ADMIN).status_code == 404
    for payload in ({"base_url": "https://new.example.com"}, {"api_key": "forbidden"}, {"enabled": "false"}):
        response = client.patch(PROVIDERS + "/mock", headers=ADMIN, json=payload)
        assert response.status_code == 422 and "forbidden" not in response.text


def test_late_model_response_cannot_finish_after_lease_loss(inbox, monkeypatch):
    client, store = inbox
    setup_model(client, store, monkeypatch, prompt=False)
    accepted = submit(client, capability_key="model-flow")
    task_id = accepted.json()["id"]

    def expire():
        with store.engine.begin() as connection:
            connection.execute(text("UPDATE ai_task_attempt SET lease_expires_at=start_time WHERE task_id=:id"),
                               {"id": task_id})

    async def lose_lease():
        await asyncio.to_thread(expire)

    transport = MockModel(after=lose_lease)
    assert execute(store, transport) and len(transport.requests) == 1
    assert client.get(accepted.headers["Location"], headers=ADMIN).json()["status"] == "running"
    assert store.task_queue.recover_expired() == 1
    result = client.get(accepted.headers["Location"] + "/result", headers=ADMIN).json()
    assert result["status"] == "failed" and result["result"] is None
    call = client.get(accepted.headers["Location"] + "/model-calls", headers=ADMIN).json()[0]
    assert call["status"] == "failed" and call["error_code"] == "MODEL_LEASE_EXPIRED_RESULT_UNKNOWN"
    assert call["output_hash"] is None
