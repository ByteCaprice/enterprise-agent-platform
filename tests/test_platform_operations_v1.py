"""Two bounded operational smokes, synthetic data and injected transports only."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from test_platform_models import MockModel, execute, setup_model
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture

from domain.capability import CapabilitySpec, CapabilityVersionDraft
from domain.platform_model import ModelTransportResponse
from infra.model_credentials import credential_variable_name
from interfaces.worker.callbacks import run_callback_worker
from interfaces.worker.platform import run_workflow_worker
from orchestration.capability_service import CapabilityService

inbox = inbox_fixture
ROOT = "/v2/projects/payments"


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def test_operations_quality_memory_callback_recovery_and_oidc(inbox, monkeypatch):
    client, store = inbox
    assert not any(path.startswith("/v1/") for path in client.app.openapi()["paths"])
    service = CapabilityService(store.capabilities)
    service.create("payments", CapabilitySpec(key="pure", kind="workflow", name="Pure"), "task-admin")
    service.create_draft("payments", "pure", CapabilityVersionDraft(version="1", definition={
        "max_parallel_rules": 2,
        "nodes": [{"key": key, "kind": "rule", "rule": {"operation": "project"},
                   "input_mapping": {"name": {"source": "input", "pointer": "/name"}}} for key in ("a", "b")],
        "output_mapping": {"name": {"source": "node", "node": "a", "pointer": "/name"}},
    }), "task-admin")
    service.publish("payments", "pure", "1", "task-admin")
    identity, _, headers = service_identity(store, actions=("task:read", "task:submit", "task:cancel",
        "memory:read", "memory:write", "quality:run", "quality:review"))
    checked(client.put(ROOT + "/operations/limits", headers=ADMIN, json={"max_queued": 0}))
    assert submit(client, headers=headers, capability_key="pure").status_code == 409
    checked(client.put(ROOT + "/operations/limits", headers=ADMIN, json={"max_queued": 10, "max_running": 0}))
    target = submit(client, headers=headers, capability_key="pure").headers["Location"]
    assert not asyncio.run(run_workflow_worker(store, once=True))
    checked(client.post(target + "/cancel", headers=headers))
    recovered = checked(client.post(target + "/recover", headers=headers, json={"request_key": "recover-1"}), 202)
    assert recovered["origin_task_id"] == target.split("/")[-1]
    assert checked(client.post(target + "/recover", headers=headers, json={"request_key": "recover-1"}))["id"] == recovered["id"]
    checked(client.put(ROOT + "/operations/limits", headers=ADMIN, json={"max_queued": 10, "max_running": 1}))
    endpoint = {"key": "fixture", "url": "https://callback.example.invalid/notify", "credential_ref": "callback-signing",
                "max_attempts": 2, "retry_delay_seconds": 1, "timeout_seconds": 3}
    checked(client.post(ROOT + "/operations/callbacks", headers=ADMIN, json=endpoint), 201)
    assert asyncio.run(run_workflow_worker(store, once=True))
    recovered_path = ROOT + "/tasks/" + recovered["id"]
    assert checked(client.get(recovered_path + "/result", headers=headers))["result"] == {"name": "Synthetic customer"}
    assert len(checked(client.get(recovered_path + "/nodes", headers=headers))) == 2
    observed = checked(client.get(recovered_path + "/deliveries", headers=headers))
    assert len(observed["items"]) == 1 and observed["items"][0]["state"] == "pending"
    assert not {"lease_token", "lease_expires_at", "payload", "url", "credential_ref"} & observed["items"][0].keys()
    assert checked(client.get(recovered_path + "/requests", headers=headers))["items"][0]["operation"] == "recover"
    _, _, outsider = service_identity(store, key="outsider")
    assert client.get(recovered_path + "/deliveries", headers=outsider).status_code == 404
    assert client.get(recovered_path.replace("/payments/", "/other/") + "/deliveries", headers=headers).status_code == 403
    memory = {"capability_key": "pure", "subject_key": "synthetic-customer", "fact_key": "preference",
              "expected_revision": 0, "value": {"language": "zh"}, "source_task_id": recovered["id"]}
    checked(client.post(ROOT + "/memory", headers=headers, json=memory), 201)
    assert client.post(ROOT + "/memory", headers=headers, json=memory).status_code == 409
    assert checked(client.get(ROOT + "/memory/pure/synthetic-customer", headers=headers))[0]["revision"] == 1
    checked(client.post(ROOT + "/memory", headers=headers, json=memory | {
        "expected_revision": 1, "state": "deleted", "value": None}), 201)
    assert checked(client.get(ROOT + "/memory/pure/synthetic-customer", headers=headers)) == [
        {"fact_key": "preference", "revision": 2, "state": "deleted"}]
    checked(client.post(ROOT + "/quality/datasets", headers=ADMIN, json={"key": "synthetic", "version": "1", "cases": [
        {"key": "one", "input": {"name": "Dataset customer"}, "expected": {"name": "Dataset customer"}, "matcher": "exact"}]}), 201)
    run_spec = {"request_key": "run-1", "dataset_key": "synthetic", "dataset_version": "1",
                "capability_key": "pure", "capability_version": "1"}
    run = checked(client.post(ROOT + "/quality/runs", headers=headers, json=run_spec), 202)
    assert checked(client.post(ROOT + "/quality/runs", headers=headers, json=run_spec), 202)["id"] == run["id"]
    assert asyncio.run(run_workflow_worker(store, once=True))
    scored = checked(client.post(ROOT + f"/quality/runs/{run['id']}/score", headers=headers))
    assert scored["state"] == "completed" and scored["scores"][0]["passed"] is True
    checked(client.post(recovered_path + "/reviews", headers=headers, json={"verdict": "approved"}), 201)
    assert len(checked(client.get(recovered_path + "/reviews", headers=headers))) == 1

    monkeypatch.setenv(credential_variable_name("payments", "callback-signing"), "synthetic-signing-secret")

    class Callback:
        async def send(self, delivery, endpoint, secret):
            assert "Dataset customer" not in json.dumps(delivery["payload"])
            assert secret.value == "synthetic-signing-secret"
            return 204

    assert asyncio.run(run_callback_worker(store, once=True, transport=Callback()))
    assert "delivered" in {row["state"] for row in checked(client.get(ROOT + "/operations/deliveries", headers=ADMIN))}
    assert checked(client.get(recovered_path + "/deliveries", headers=headers))["items"][0]["state"] == "delivered"
    assert checked(client.get(recovered_path + "/result", headers=headers))["status"] == "succeeded"

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {"kid": "fixture", "alg": "RS256"}
    checked(client.put("/v2/oidc/providers", headers=ADMIN, json={"key": "fixture", "issuer": "https://issuer.example.invalid",
        "audience": "enterprise-ai-test", "jwks": {"keys": [public]}}))
    binding = {"provider_key": "fixture", "subject": "synthetic-subject", "identity_id": identity["id"]}
    checked(client.post(ROOT + "/oidc/bindings", headers=ADMIN, json=binding), 201)
    token = jwt.encode({"iss": "https://issuer.example.invalid", "aud": "enterprise-ai-test", "sub": "synthetic-subject",
        "iat": int(time.time()), "exp": int(time.time()) + 300, "roles": ["admin"]}, private, algorithm="RS256", headers={"kid": "fixture"})
    bearer = {"Authorization": "Bearer " + token}
    assert checked(client.get("/v2/session", headers=bearer))["is_admin"] is False
    checked(client.get(recovered_path, headers=bearer))
    assert client.get(ROOT + "/operations/limits", headers=bearer).status_code == 403
    checked(client.post(ROOT + "/oidc/unbind", headers=ADMIN, json=binding))
    assert client.get("/v2/session", headers=bearer).status_code == 401


def test_budget_reservation_settlement_and_exhaustion(inbox, monkeypatch):
    client, store = inbox
    setup_model(client, store, monkeypatch)
    now = datetime.now(UTC)
    checked(client.post(ROOT + "/operations/budgets", headers=ADMIN, json={
        "key": "synthetic", "currency": "USD", "limit_microunits": 6, "reserve_per_call_microunits": 6,
        "starts_at": (now - timedelta(minutes=1)).isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat(),
        "rates": [{"provider_key": "mock", "model": "catalog/primary", "input_microunits_per_million": 1000000,
                   "output_microunits_per_million": 1000000}]}), 201)
    checked(client.put(ROOT + "/operations/limits", headers=ADMIN, json={"active_budget_key": "synthetic"}))
    first = submit(client, key="budget-one", capability_key="model-flow").headers["Location"]
    transport = MockModel()
    assert execute(store, transport)
    assert checked(client.get(first + "/result", headers=ADMIN))["status"] == "succeeded"
    charge = checked(client.get(ROOT + "/operations/charges", headers=ADMIN))[0]
    assert charge["state"] == "known" and charge["settled_microunits"] == 6
    second = submit(client, key="budget-two", capability_key="model-flow").headers["Location"]
    assert execute(store, transport)
    assert checked(client.get(second + "/result", headers=ADMIN))["error_code"] == "MODEL_BUDGET_EXHAUSTED"
    assert len(transport.requests) == 1
    assert client.post(second + "/recover", headers=ADMIN, json={"request_key": "unsafe"}).status_code == 409
    # Missing Usage is not zero cost; a terminal unknown charge keeps the reservation.
    cycle = checked(client.get(ROOT + "/operations/budgets", headers=ADMIN))[0]
    cycle = {key: value for key, value in cycle.items() if key not in {"project_key", "used_or_reserved_microunits"}}
    checked(client.post(ROOT + "/operations/budgets", headers=ADMIN, json=cycle | {"key": "unknown"}), 201)
    checked(client.put(ROOT + "/operations/limits", headers=ADMIN, json={"active_budget_key": "unknown"}))

    class UnknownUsage:
        async def complete(self, request):
            return ModelTransportResponse(content='{"answer":"synthetic-approved"}', actual_model="catalog/primary")

    submit(client, key="budget-unknown", capability_key="model-flow")
    assert execute(store, UnknownUsage())
    unknown = next(row for row in checked(client.get(ROOT + "/operations/charges", headers=ADMIN)) if row["budget_key"] == "unknown")
    assert unknown["state"] == "unknown" and unknown["settled_microunits"] is None
    assert next(row for row in checked(client.get(ROOT + "/operations/budgets", headers=ADMIN)) if row["key"] == "unknown")["used_or_reserved_microunits"] == 6
    reconciled = checked(client.post(ROOT + f"/operations/charges/{unknown['call_id']}/reconcile", headers=ADMIN,
        json={"amount_microunits": 5, "reference": "synthetic-invoice-check"}))
    assert reconciled["state"] == "reconciled" and reconciled["settled_microunits"] == 5
