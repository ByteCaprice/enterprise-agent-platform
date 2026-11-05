"""Cross-worker task concurrency policies share one transactional authority."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_platform_agent_storage import root_fixture
from test_platform_tasks import ADMIN, publish, submit
from test_platform_tasks import inbox as inbox_fixture

from domain.capability import CapabilitySpec, CapabilityVersionDraft
from domain.platform_agent import AgentCapacityUnavailable
from orchestration.capability_service import CapabilityService

inbox = inbox_fixture


def test_global_and_capability_limits_hold_across_workers(inbox):
    client, store = inbox
    publish(store, key="other-agent")
    assert client.put("/v2/operations/concurrency", headers=ADMIN, json={"max_running": 2}).status_code == 200
    assert client.put("/v2/projects/payments/capabilities/review/concurrency", headers=ADMIN,
                      json={"max_running": 1}).status_code == 200
    assert submit(client, key="a").status_code == 202
    assert submit(client, key="b").status_code == 202
    assert submit(client, key="c", capability_key="other-agent").status_code == 202
    with ThreadPoolExecutor(max_workers=3) as workers:
        claims = list(workers.map(lambda n: store.tasks.claim(f"worker-{n}"), range(3)))
    claims = [claim for claim in claims if claim is not None]
    assert len(claims) == 2
    assert {claim.task.capability_key for claim in claims} == {"review", "other-agent"}
    assert client.put("/v2/operations/concurrency", headers=ADMIN, json={"max_running": 1}).status_code == 200
    assert store.tasks.claim("extra") is None
    for claim in claims:
        store.tasks.complete(claim.lease, status="failed", error_code="SYNTHETIC_STOP")
    remaining = store.tasks.claim("after-finish")
    assert remaining is not None and remaining.task.capability_key == "review"


def test_root_and_embedded_agent_share_one_capability_limit(inbox, monkeypatch):
    client, store = inbox
    root, _, _ = root_fixture(client, store, monkeypatch, begin=False)
    assert client.put("/v2/projects/payments/capabilities/root-agent/concurrency", headers=ADMIN,
                      json={"max_running": 1}).status_code == 200
    service = CapabilityService(store.capabilities)
    service.create("payments", CapabilitySpec(key="flow", kind="workflow", name="Flow"), "task-admin")
    service.create_draft("payments", "flow", CapabilityVersionDraft(version="1", definition={
        "nodes": [{"key": "review", "kind": "agent", "capability": {"key": "root-agent", "version": "1"}}],
        "output_mapping": {},
    }, dependencies=[{"key": "root-agent", "version": "1"}]), "task-admin")
    service.publish("payments", "flow", "1", "task-admin")
    assert submit(client, key="workflow", capability_key="flow").status_code == 202
    workflow = store.tasks.claim("workflow-worker", kinds=("workflow",), workflow_agents=True, lease_seconds=60)
    node = store.workflows.begin(workflow.lease, "review", "a" * 64).record
    with pytest.raises(AgentCapacityUnavailable):
        store.agents.begin(workflow.lease, runtime_profile="enterprise-ai.pi.v1", parent_node_id=node.id)
    store.tasks.complete(root.lease, status="failed", error_code="SYNTHETIC_STOP")
    child = store.agents.begin(workflow.lease, runtime_profile="enterprise-ai.pi.v1", parent_node_id=node.id)
    assert child.status == "running" and child.parent_node_id == node.id
    status = client.get("/v2/projects/payments/capabilities/root-agent/concurrency", headers=ADMIN).json()
    assert status["running_tasks"] == 0 and status["running_embedded_agents"] == 1
    assert submit(client, key="next-root", capability_key="root-agent").status_code == 202
    assert store.tasks.claim("root-worker", kinds=("agent",)) is None
    store.tasks.complete(workflow.lease, status="failed", error_code="SYNTHETIC_STOP")
    assert store.tasks.claim("root-worker", kinds=("agent",)) is not None
