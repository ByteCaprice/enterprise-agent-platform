"""One real Workflow/Tool ledger closure against an in-process official MCP server."""

import asyncio

import pytest
from test_platform_mcp import catalog, official_mock_server
from test_platform_tasks import ADMIN, service_identity
from test_platform_tasks import inbox as inbox_fixture
from test_platform_tools import accept, grant, setup_tool

from interfaces.worker.platform import run_workflow_worker

inbox = inbox_fixture


@pytest.mark.parametrize("authorized", [True, False])
def test_mcp_tool_uses_existing_workflow_authorization_and_fenced_call_evidence(inbox, monkeypatch, authorized):
    client, store = inbox
    approved = catalog()
    pin = approved.tools[0]
    version = pin.handler_version(approved.connections[0])
    setup_tool(client, store, handler=pin.handler_key, version=version)
    identity, _, headers = service_identity(store)
    if authorized:
        grant(client, identity)
    target = accept(client, headers)

    async def run():
        async with official_mock_server() as (transport, messages, calls):
            monkeypatch.setattr("orchestration.mcp_tools.MCPJSONTransport", lambda: transport)
            assert await run_workflow_worker(store, once=True, mcp_catalog=approved)
            assert len(calls) == (1 if authorized else 0)
            if authorized:
                assert messages[-1][0] == "DELETE"
            else:
                assert not messages

    asyncio.run(run())
    result = client.get(target + "/result", headers=headers).json()
    if not authorized:
        assert result["status"] == "failed" and result["error_code"] == "TOOL_ACCESS_DENIED"
        assert client.get(target + "/tool-calls", headers=headers).json() == []
        return
    assert result["status"] == "succeeded" and result["result"] == {"name": "Synthetic visible name"}
    calls = client.get(target + "/tool-calls", headers=headers)
    record = calls.json()[0]
    assert record["handler_key"] == pin.handler_key and record["handler_version"] == version
    assert record["status"] == "succeeded" and record["output_hash"]
    assert all(value not in calls.text for value in ("synthetic-private", "synthetic-secret", "https://", "mcp-session-id"))
    assert client.get(target + "/nodes", headers=ADMIN).json()[-1]["status"] == "succeeded"
