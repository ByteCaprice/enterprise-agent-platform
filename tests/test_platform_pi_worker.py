"""Two real Pi/Worker smokes against synthetic turns and an owned SIT schema."""

import pytest
from test_platform_agent_broker import MockTurns
from test_platform_agent_storage import root_fixture
from test_platform_tasks import ADMIN, service_identity
from test_platform_tasks import inbox as inbox_fixture

from interfaces.cli.platform import main

inbox = inbox_fixture


@pytest.mark.parametrize("denied", [False, True])
def test_explicit_pi_worker_owns_real_loop_while_default_worker_stays_workflow_only(inbox, monkeypatch, capsys, denied):
    client, store = inbox
    _, _, headers = service_identity(store, actions=("task:submit", "task:read", "model:invoke", "prompt:render", "tool:invoke"))
    # Build a published fixture, but leave task claiming to the actual Worker.
    _, _, target = root_fixture(client, store, monkeypatch, begin=False, headers=headers, claim_task=False)
    main(["worker", "--once", "--config-dir", "unused"])
    capsys.readouterr()
    assert client.get(target, headers=headers).json()["status"] == "queued"
    transport = MockTurns(denied=denied)
    monkeypatch.setattr("interfaces.worker.platform.AgentTurnHTTPTransport", lambda: transport)
    main(["worker", "--once", "--executors", "agent", "--config-dir", "unused"])
    assert '"claimed": true' in capsys.readouterr().out
    result = client.get(target + "/result", headers=headers).json()
    if denied:
        assert result["status"] == "failed" and result["error_code"] == "MODEL_RESPONSE_REFUSED"
        assert len(transport.requests) == 1
        calls = client.get(target + "/model-calls", headers=ADMIN).json()
        assert calls[0]["status"] == "failed" and calls[0]["total_tokens"] == 11
        assert client.get(target + "/tool-calls", headers=ADMIN).json() == []
    else:
        assert result["status"] == "succeeded" and result["result"] == {"name": "Synthetic name"}
        assert len(transport.requests) == 2
        tool = client.get(target + "/tool-calls", headers=headers).json()[0]
        assert tool["status"] == "succeeded" and tool["owner"]["kind"] == "agent_step" and tool["node_id"] is None
        assert client.get(target + "/nodes", headers=headers).json() == []
