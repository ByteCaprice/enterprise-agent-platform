"""Small MCP profile tests: actual HTTP messages, never an external connection."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from domain.platform_mcp import MCPCatalog
from domain.platform_model import ModelSecret
from domain.platform_tool import ToolContext
from domain.task_execution import ExecutionFailure
from infra.mcp_catalog import catalog_metadata, load_mcp_catalog
from infra.mcp_http import MCPJSONTransport
from interfaces.cli.platform import main
from orchestration.mcp_tools import mcp_readonly_tools

INPUT = {"type": "object", "properties": {"data": {"type": "object"},
                                         "fields": {"type": "array", "items": {"type": "string"}}},
         "required": ["data", "fields"], "additionalProperties": False}
OUTPUT = {"type": "object", "properties": {"name": {"type": "string"}},
          "required": ["name"], "additionalProperties": False}
VALUES = {"data": {"name": "Synthetic", "private": "synthetic-private"}, "fields": ["name"]}
CONTEXT = ToolContext("payments", "service:test", "task", "attempt", "node", "select-fields", "1")


def test_cli_and_http_import_do_not_load_optional_sdk_or_agent_adapter():
    code = """
import sys
sys.path.insert(0, sys.argv[1])
from interfaces.cli.platform import main
from interfaces.http.platform_server import create_platform_app
assert callable(main) and callable(create_platform_app)
assert 'mcp' not in sys.modules
assert 'framework.agent_runtime.pi.adapter' not in sys.modules
"""
    subprocess.run([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1])],
                   check=True, capture_output=True, text=True)


def catalog():
    return MCPCatalog.model_validate({"connections": [{
        "project_key": "payments", "key": "directory", "endpoint": "https://mcp.example.com/mcp",
        "credential_ref": "mcp-readonly", "transport": "streamable_http_json",
    }], "tools": [{"project_key": "payments", "connection_key": "directory", "handler_key": "mcp.project",
                   "remote_tool": "project", "operation_type": "read_only", "input_schema": INPUT, "output_schema": OUTPUT}]})


class Credentials:
    def resolve(self, project, reference):
        assert (project, reference) == ("payments", "mcp-readonly")
        return ModelSecret(reference, "synthetic-secret")


class MockMCP:
    def __init__(self, *, changed=False, invalid_output=False, close_failure=False):
        self.requests = []
        self.changed = changed
        self.invalid_output = invalid_output
        self.close_failure = close_failure
        self.options = []

    async def http(self, request):
        payload = json.loads(request.content) if request.content else {}
        self.requests.append((request, payload))
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "mcp.example.com"
        assert request.extensions["sni_hostname"] == "mcp.example.com"
        assert request.headers["authorization"] == "Bearer synthetic-secret"
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "DELETE":
            return httpx.Response(500 if self.close_failure else 204)
        method = payload["method"]
        headers = {}
        if method == "initialize":
            assert payload["params"]["capabilities"] == {}
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "synthetic", "version": "1"}}
            headers = {"Mcp-Session-Id": "synthetic-session"}
        else:
            assert request.headers["Mcp-Session-Id"] == "synthetic-session"
            assert request.headers["MCP-Protocol-Version"] == "2025-06-18"
            if "id" not in payload:
                return httpx.Response(202)
            if method == "tools/list":
                result = {"tools": [{"name": "project", "inputSchema": {} if self.changed else INPUT,
                                     "outputSchema": OUTPUT, "annotations": {"readOnlyHint": False}}]}
            else:
                assert method == "tools/call" and payload["params"] == {"name": "project", "arguments": VALUES}
                output = {"unexpected": "synthetic-private"} if self.invalid_output else {"name": "Synthetic"}
                result = {"content": [], "structuredContent": output, "isError": False}
        return httpx.Response(200, headers=headers, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    async def resolve(self, hostname):
        assert hostname == "mcp.example.com"
        return ["8.8.8.8"]

    def client(self, **kwargs):
        self.options.append(kwargs)
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False and kwargs["verify"] is True
        return httpx.AsyncClient(transport=httpx.MockTransport(self.http), **kwargs)

    def transport(self):
        return MCPJSONTransport(credentials=Credentials(), resolver=self.resolve, client_factory=self.client)


def installed(mock, approved=None):
    approved = approved or catalog()
    pin = approved.tools[0]
    registry = mcp_readonly_tools(approved, transport=mock.transport())
    handler = registry.resolve(pin.handler_key, pin.handler_version(approved.connections[0]))
    assert handler is not None
    return handler


def test_real_json_session_initializes_checks_schema_calls_and_deletes_without_readonly_hint_trust():
    mock = MockMCP()
    result = asyncio.run(installed(mock).execute(CONTEXT, VALUES))
    assert result == {"name": "Synthetic"}
    assert [payload.get("method", "DELETE") for _, payload in mock.requests] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"]
    assert len(mock.options) == 1  # One isolated client; no ambient proxy, reconnect or shared session.


@pytest.mark.parametrize("case,code", [("changed", "TOOL_MCP_SCHEMA_CHANGED"),
                                      ("invalid_output", "TOOL_MCP_OUTPUT_REJECTED"),
                                      ("close_failure", "TOOL_MCP_CLOSE_RESULT_UNKNOWN")])
def test_schema_output_and_close_failures_never_deliver_success(case, code):
    mock = MockMCP(**{case: True})
    with pytest.raises(ExecutionFailure, match=code):
        asyncio.run(installed(mock).execute(CONTEXT, VALUES))
    assert mock.requests[-1][0].method == "DELETE"
    calls = [payload for _, payload in mock.requests if payload.get("method") == "tools/call"]
    assert len(calls) == (0 if case == "changed" else 1)


def test_project_input_and_missing_credentials_fail_before_connecting(monkeypatch):
    mock = MockMCP()
    handler = installed(mock)
    other = ToolContext("other", "service:test", "task", "attempt", "node", "select-fields", "1")
    with pytest.raises(ExecutionFailure, match="TOOL_MCP_PROJECT_DENIED"):
        asyncio.run(handler.execute(other, VALUES))
    with pytest.raises(ExecutionFailure, match="TOOL_MCP_INPUT_INVALID"):
        asyncio.run(handler.execute(CONTEXT, {"endpoint": "https://other.example.com"}))
    assert not mock.requests and not mock.options
    from infra.model_credentials import credential_variable_name

    monkeypatch.delenv(credential_variable_name("payments", "mcp-readonly"), raising=False)
    transport = MCPJSONTransport(resolver=mock.resolve, client_factory=mock.client)

    async def run():
        async with transport.open(catalog().connections[0]):
            pytest.fail("Must not open session without explicit project credential")

    with pytest.raises(ExecutionFailure, match="TOOL_MCP_CREDENTIAL_UNAVAILABLE"):
        asyncio.run(run())
    assert not mock.options


def test_cancelled_call_sends_cancel_then_delete_and_propagates_cancellation():
    mock = MockMCP()
    real_http = mock.http

    async def run():
        started = asyncio.Event()

        async def http(request):
            payload = json.loads(request.content) if request.content else {}
            if payload.get("method") == "tools/call":
                mock.requests.append((request, payload))
                started.set()
                await asyncio.Event().wait()
            return await real_http(request)

        mock.http = http
        task = asyncio.create_task(installed(mock).execute(CONTEXT, VALUES))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(run())
    methods = [payload.get("method", "DELETE") for _, payload in mock.requests]
    assert methods[-2:] == ["notifications/cancelled", "DELETE"]
    call_id = next(payload["id"] for _, payload in mock.requests if payload.get("method") == "tools/call")
    assert mock.requests[-2][1]["params"] == {"requestId": call_id}


@pytest.mark.parametrize("case,code", [
    ("redirect", "TOOL_MCP_HTTP_REJECTED"), ("sse", "TOOL_MCP_JSON_REQUIRED"),
    ("oversized", "TOOL_MCP_RESPONSE_TOO_LARGE"), ("wrong_id", "TOOL_MCP_PROTOCOL_REJECTED"),
    ("server_request", "TOOL_MCP_PROTOCOL_REJECTED"), ("upstream_error", "TOOL_MCP_PROTOCOL_REJECTED"),
    ("repeated_cursor", "TOOL_MCP_CATALOG_REJECTED"), ("duplicate_tool", "TOOL_MCP_CATALOG_REJECTED"),
])
def test_untrusted_protocol_responses_are_bounded_not_followed_and_not_replayed(case, code):
    mock = MockMCP()
    real_http = mock.http

    async def http(request):
        payload = json.loads(request.content) if request.content else {}
        if payload.get("method") != "tools/list":
            return await real_http(request)
        mock.requests.append((request, payload))
        if case == "redirect":
            return httpx.Response(307, headers={"Location": "http://169.254.169.254/private"})
        if case == "sse":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b"data: private\n\n")
        if case == "oversized":
            return httpx.Response(200, content=b"x" * 1_000_001, headers={"Content-Type": "application/json"})
        result = {"tools": [{"name": "project", "inputSchema": INPUT, "outputSchema": OUTPUT}]}
        response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        if case == "wrong_id":
            response["id"] = 123456
        elif case == "server_request":
            response = {"jsonrpc": "2.0", "id": payload["id"], "method": "sampling/createMessage",
                        "params": {"messages": []}}
        elif case == "upstream_error":
            response = {"jsonrpc": "2.0", "id": payload["id"],
                        "error": {"code": -1, "message": "synthetic-private-error"}}
        elif case == "repeated_cursor":
            result["tools"] = []
            result["nextCursor"] = "same"
        elif case == "duplicate_tool":
            result["tools"].append(dict(result["tools"][0]))
        return httpx.Response(200, json=response)

    mock.http = http
    with pytest.raises(ExecutionFailure, match=code) as error:
        asyncio.run(installed(mock).execute(CONTEXT, VALUES))
    assert "private" not in str(error.value)
    assert not [payload for _, payload in mock.requests if payload.get("method") == "tools/call"]
    assert mock.requests[-1][0].method == "DELETE"


def test_failed_cancel_notification_still_attempts_delete_with_bounded_cleanup():
    mock = MockMCP()
    real_http = mock.http

    async def run():
        started = asyncio.Event()

        async def http(request):
            payload = json.loads(request.content) if request.content else {}
            if payload.get("method") == "tools/call":
                mock.requests.append((request, payload))
                started.set()
                await asyncio.Event().wait()
            if payload.get("method") == "notifications/cancelled":
                mock.requests.append((request, payload))
                return httpx.Response(500)
            return await real_http(request)

        mock.http = http
        task = asyncio.create_task(installed(mock).execute(CONTEXT, VALUES))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(run())
    assert [payload.get("method", "DELETE") for _, payload in mock.requests][-2:] == [
        "notifications/cancelled", "DELETE"]


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "10.0.0.1"], []])
def test_non_public_or_mixed_dns_never_opens_client(addresses):
    mock = MockMCP()

    async def resolve(_hostname):
        return addresses

    mock.resolve = resolve
    with pytest.raises(ExecutionFailure, match="TOOL_MCP_ADDRESS_REJECTED"):
        asyncio.run(installed(mock).execute(CONTEXT, VALUES))
    assert not mock.options


@pytest.mark.parametrize("update", [{"transport": "stdio"}, {"command": "sh"}, {"headers": {"Authorization": "secret"}},
                                    {"endpoint": "https://user:secret@example.com/mcp"},
                                    {"endpoint": "http://127.0.0.1/mcp"}, {"endpoint": "https://mcp.example.com/mcp?token=x"}])
def test_catalog_cannot_install_commands_headers_private_or_credential_urls(update):
    data = catalog().model_dump(mode="json")
    data["connections"][0].update(update)
    with pytest.raises(ValidationError):
        MCPCatalog.model_validate(data)


def test_catalog_hash_pins_project_endpoint_tool_and_schemas_and_captures_immutable_snapshot():
    approved = catalog()
    version = catalog_metadata(approved)[0]["handler_version"]
    assert len(version) == 64
    for section, update in [("connections", {"endpoint": "https://other.example.com/mcp"}),
                            ("connections", {"credential_ref": "other-ref"}),
                            ("tools", {"remote_tool": "other"}),
                            ("tools", {"output_schema": {"type": "object"}})]:
        data = approved.model_dump(mode="json")
        data[section][0].update(update)
        assert catalog_metadata(MCPCatalog.model_validate(data))[0]["handler_version"] != version
    mock = MockMCP()
    handler = installed(mock, approved)
    approved.tools[0].output_schema.clear()
    assert asyncio.run(handler.execute(CONTEXT, VALUES)) == {"name": "Synthetic"}


def test_catalog_cli_is_offline_and_does_not_print_endpoint_or_credentials(tmp_path, capsys, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text(catalog().model_dump_json())

    def fail(*_args, **_kwargs):
        pytest.fail("Offline catalog check must not bootstrap or access the database")

    monkeypatch.setattr("interfaces.cli.platform.create_platform_app", fail)
    main(["check-mcp-catalog", "--mcp-catalog", str(path)])
    output = capsys.readouterr().out
    assert json.loads(output)["tools"] == catalog_metadata(catalog())
    assert "https" not in output and "credential" not in output
    path.write_text('{"connections":[],"connections":[]}')
    with pytest.raises(ValueError, match="Invalid MCP deployment catalog"):
        load_mcp_catalog(str(path))


def test_worker_cli_forwards_only_explicit_validated_catalog_and_closes_store(tmp_path, capsys, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text(catalog().model_dump_json())
    closed = []
    store = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr("interfaces.cli.platform.create_platform_app", lambda _path: SimpleNamespace(state=SimpleNamespace(store=store)))

    async def worker(received_store, *, worker_id, once, mcp_catalog):
        assert received_store is store and worker_id == "test-worker" and once is True
        assert catalog_metadata(mcp_catalog) == catalog_metadata(catalog())
        return True

    monkeypatch.setattr("interfaces.cli.platform.run_workflow_worker", worker)
    main(["worker", "--config-dir", "unused", "--once", "--worker-id", "test-worker", "--mcp-catalog", str(path)])
    assert json.loads(capsys.readouterr().out)["claimed"] is True and closed == [True]


@asynccontextmanager
async def official_mock_server():
    # The optional SDK is only a conformance-test server, not a production dependency.
    pytest.importorskip("mcp")
    from mcp.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.types import CallToolResult, Tool

    server = Server("synthetic-readonly")
    messages = []
    calls = []

    @server.list_tools()
    async def tools():
        return [Tool(name="project", inputSchema=INPUT, outputSchema=OUTPUT)]

    @server.call_tool()
    async def call(name, arguments):
        calls.append((name, arguments))
        return CallToolResult(content=[], structuredContent={key: arguments["data"][key] for key in arguments["fields"]})

    manager = StreamableHTTPSessionManager(server, json_response=True)
    asgi = httpx.ASGITransport(app=manager.handle_request)

    async def http(request):
        messages.append((request.method, json.loads(request.content) if request.content else {}, dict(request.headers)))
        return await asgi.handle_async_request(request)

    mock = MockMCP()
    mock.http = http
    async with manager.run():
        yield mock.transport(), messages, calls
    await asgi.aclose()


def test_json_profile_interoperates_with_official_stateful_mcp_server():
    async def run():
        async with official_mock_server() as (transport, messages, calls):
            approved = catalog()
            pin = approved.tools[0]
            registry = mcp_readonly_tools(approved, transport=transport)
            handler = registry.resolve(pin.handler_key, pin.handler_version(approved.connections[0]))
            assert handler is not None
            assert await handler.execute(CONTEXT, VALUES) == {"name": "Synthetic"}
            assert [payload.get("method", method) for method, payload, _ in messages] == [
                "initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"]
            session_ids = {headers["mcp-session-id"] for _, _, headers in messages[1:]}
            assert len(session_ids) == 1
            assert calls == [("project", VALUES)]

    asyncio.run(run())
