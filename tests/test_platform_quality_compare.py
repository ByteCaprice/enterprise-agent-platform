"""Version comparisons preserve dataset and authorization boundaries."""

from test_platform_tasks import ADMIN, publish
from test_platform_tasks import inbox as inbox_fixture
from test_platform_worker import run

inbox = inbox_fixture
ROOT = "/v2/projects/payments/quality"


def test_comparison_uses_same_dataset_and_separates_completion_from_scoring(inbox):
    client, store = inbox
    publish(store, version="2")
    dataset = {"key": "fixture", "version": "1", "cases": [
        {"key": "case", "input": {"name": "private-fixture"}, "expected": {"value": 1}, "matcher": "exact"},
    ]}
    assert client.post(ROOT + "/datasets", headers=ADMIN, json=dataset).status_code == 201
    ids = []
    for version, value in (("1", 1), ("2", 2)):
        payload = {"request_key": "run-" + version, "dataset_key": "fixture", "dataset_version": "1",
                   "capability_key": "review", "capability_version": version}
        evaluation = client.post(ROOT + "/runs", headers=ADMIN, json=payload)
        assert evaluation.status_code == 202
        ids.append(evaluation.json()["id"])
        if version == "2":
            assert client.get(ROOT + f"/runs/{ids[0]}/compare/{ids[1]}", headers=ADMIN).status_code == 409

        async def execute(_claim, value=value):
            return {"value": value}

        assert run(store, execute)
        assert client.post(ROOT + f"/runs/{ids[-1]}/score", headers=ADMIN).status_code == 200

    result = client.get(ROOT + f"/runs/{ids[0]}/compare/{ids[1]}", headers=ADMIN)
    assert result.status_code == 200
    body = result.json()
    assert body["left"]["technical_succeeded"] == body["right"]["technical_succeeded"] == 1
    assert body["left"]["evaluation_passed"] == 1 and body["right"]["evaluation_passed"] == 0
    assert "private-fixture" not in result.text
