"""Small readiness smokes, isolated schemas and no external supplier traffic."""

import asyncio
import json

import pytest
from sqlalchemy import text
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture
from test_platform_tools import accept, grant, setup_tool
from worker_support import worker_capabilities

from domain.execution_requirements import AGENT_INTERPRETER_PROFILE, InstalledExecutionCapabilities
from infra.deployment_policy import validate_deployment
from interfaces.worker.platform import run_workflow_worker

inbox = inbox_fixture
ROOT = "/v2/projects/payments"


def test_health_pagination_errors_and_local_readonly_scenario(inbox, monkeypatch, caplog):
    client, store = inbox
    _, _, headers = service_identity(store)
    assert client.get(ROOT + "/operations/health", headers=headers).status_code == 403
    task = submit(client)
    assert task.status_code == 202
    response = client.get(ROOT + "/operations/health", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert {"code": "WORKER_MISSING", "roles": ["agent"]} in response.json()["alerts"]
    task_id = task.json()["id"]
    assert response.json()["queue_readiness"]["issues"] == [{"task_id": task_id, "reason": "no_compatible_worker"}]
    instance = store.worker_health.start("fixture-agent", "task", ("agent",))
    legacy = client.get(ROOT + "/operations/health", headers=ADMIN).json()
    assert not any(a["code"] == "WORKER_MISSING" for a in legacy["alerts"])
    assert legacy["queue_readiness"]["issues"] == [{"task_id": task_id, "reason": "worker_coverage_unknown"}]
    store.worker_health.heartbeat(instance, "stopped")
    incompatible = InstalledExecutionCapabilities.from_descriptors(("agent",), agent_interpreter_profile=AGENT_INTERPRETER_PROFILE)
    store.worker_health.start("missing-runtime", "task", ("agent",), capabilities=incompatible)
    health = client.get(ROOT + "/operations/health", headers=ADMIN).json()
    assert {"code": "QUEUED_TASK_NO_COMPATIBLE_WORKER", "count": 1} in health["alerts"]
    assert not any(a["code"] == "WORKER_MISSING" for a in health["alerts"])
    instance = store.worker_health.start("fixture-agent", "task", ("agent",), capabilities=worker_capabilities("agent"))
    health = client.get(ROOT + "/operations/health", headers=ADMIN).json()
    assert health["queue_readiness"]["compatible"] == 1 and not health["queue_readiness"]["issues"]
    assert store.worker_health.live(worker_id="fixture-agent")[0]["executors"] == ["agent"]
    with store.engine.begin() as connection:
        connection.execute(text("UPDATE ai_worker_health SET heartbeat_at=clock_timestamp()-interval '60 seconds'"))
    assert not store.worker_health.live(worker_id="fixture-agent")
    stale = client.get(ROOT + "/operations/health", headers=ADMIN).json()
    assert {"code": "QUEUED_TASK_NO_COMPATIBLE_WORKER", "count": 1} in stale["alerts"]
    with store.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM ai_task_attempt WHERE task_id=:task"), {"task": task_id}) == 0
    store.worker_health.heartbeat(instance, "stopped")

    for key in ("one", "two", "three"):
        assert client.post(ROOT + "/operations/callbacks", headers=ADMIN, json={"key": key,
            "url": "https://callback.example.invalid/notify", "credential_ref": "fixture",
            "max_attempts": 1, "retry_delay_seconds": 1, "timeout_seconds": 1}).status_code == 201
    page_url = ROOT + "/operations/pages/callbacks"
    first = client.get(page_url, headers=ADMIN, params={"limit": 2}).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get(page_url, headers=ADMIN, params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert len({row["key"] for row in first["items"] + second["items"]}) == 3
    assert client.get(page_url, headers=headers).status_code == 403
    assert client.get(page_url, headers=ADMIN, params={"cursor": "bad"}).status_code == 422
    assert client.get(ROOT + "/operations/pages/deliveries", headers=ADMIN,
                      params={"cursor": first["next_cursor"]}).status_code == 422

    setup_tool(client, store)
    identity, _, tool_headers = service_identity(store, key="readonly-trial")
    grant(client, identity)
    target = accept(client, tool_headers)
    assert asyncio.run(run_workflow_worker(store, once=True, worker_id="readonly-smoke"))
    assert client.get(target + "/result", headers=tool_headers).json()["result"] == {"name": "Synthetic visible name"}
    assert client.get(target + "/tool-calls", headers=tool_headers).json()[0]["status"] == "succeeded"
    assert not store.worker_health.live(worker_id="readonly-smoke")
    deliveries = client.get(ROOT + "/operations/pages/deliveries", headers=ADMIN).json()
    assert all("lease_token" not in row for row in deliveries["items"])

    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-password-and-customer-data")

    monkeypatch.setattr(client.app.state.store.worker_health, "summary", broken)
    failed = client.get(ROOT + "/operations/health", headers=ADMIN)
    assert failed.status_code == 500
    assert failed.json()["error"]["request_id"] == failed.headers["X-Request-ID"]
    assert "synthetic-private" not in failed.text and "synthetic-private" not in caplog.text


def test_production_gate_requires_explicit_policy_without_network(tmp_path):
    assert validate_deployment({}, tmp_path).mode == "trial"
    with pytest.raises(ValueError):
        validate_deployment({"platform_runtime": {"mode": "production"}}, tmp_path)
    policy = {name: "operator-reviewed-reference" for name in (
        "owner", "identity_policy_ref", "financial_policy_ref", "retention_policy_ref", "backup_restore_ref",
        "incident_runbook_ref", "edge_security_ref")}
    policy["shared_edge_limits_confirmed"] = True
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    settings = {"platform_runtime": {"mode": "production", "policy_file": str(path)},
                "database": {"host": "db.example.test", "sslmode": "verify-full"}}
    assert validate_deployment(settings, tmp_path).mode == "production"
    policy["retention_policy_ref"] = ""
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError):
        validate_deployment(settings, tmp_path)
