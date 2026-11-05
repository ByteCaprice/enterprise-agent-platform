"""Business adoption is versioned separately from Task execution and review."""

from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture
from test_platform_worker import run

inbox = inbox_fixture


def test_feedback_is_scoped_versioned_and_idempotent(inbox):
    client, store = inbox
    _, _, headers = service_identity(store, actions=("task:submit", "task:read", "feedback:read", "feedback:write"))
    _, _, other_headers = service_identity(store, key="other", actions=("task:read", "feedback:read"))
    accepted = submit(client, headers=headers)
    task_url = accepted.headers["Location"]
    feedback_url = task_url + "/feedback"
    first = {"request_key": "first", "expected_revision": 0, "adoption": "adopted"}
    assert client.post(feedback_url, headers=headers, json=first).status_code == 409

    async def execute(_claim):
        return {"decision": "synthetic"}

    assert run(store, execute)
    created = client.post(feedback_url, headers=headers, json=first)
    assert created.status_code == 201 and created.json()["revision"] == 1
    replay = client.post(feedback_url, headers=headers, json=first)
    assert replay.status_code == 200 and replay.json()["id"] == created.json()["id"]
    second = {"request_key": "second", "expected_revision": 1, "adoption": "modified",
              "final_result": {"decision": "corrected"}, "reason": "synthetic"}
    assert client.post(feedback_url, headers=headers, json=second).json()["revision"] == 2
    assert client.post(feedback_url, headers=headers, json={**second, "request_key": "stale"}).status_code == 409
    assert len(client.get(feedback_url, headers=headers).json()) == 2
    assert client.get(feedback_url, headers=other_headers).status_code == 404
    assert client.get(task_url + "/result", headers=ADMIN).json()["result"] == {"decision": "synthetic"}
