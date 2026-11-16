"""Narrow MCP Streamable HTTP JSON profile, with bounded, pinned-address sessions.

No SSE/reconnect, stdio, server requests, sampling, tasks, automatic discovery grants
or text-result inference. Every invocation owns and closes a fresh session.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from domain.platform_mcp import MCPConnection
from domain.platform_model import is_public_model_address
from domain.task import canonical_json
from domain.task_execution import ExecutionFailure
from infra.mcp_catalog import strict_json
from infra.model_credentials import EnvironmentCredentialResolver
from infra.model_http import _system_resolve

_SESSION_ID = re.compile(r"^[\x21-\x7e]{1,256}$")


class MCPJSONSession:
    def __init__(self, client, url, hostname, original_host, secret: str | None, protocol_version):
        self._client = client
        self._url = url
        self._headers = {"Host": original_host,
                          "Accept": "application/json, text/event-stream", "Accept-Encoding": "identity",
                          "Content-Type": "application/json"}
        if secret is not None:
            self._headers["Authorization"] = f"Bearer {secret}"
        self._hostname = hostname
        self._protocol_version = protocol_version
        self._session_id = None
        self._next_id = 0
        self._inflight = None
        self._initialized = False

    async def _http(self, method, payload=None, *, initializing=False):
        headers = dict(self._headers)
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._protocol_version
        body = canonical_json(payload).encode() if payload is not None else None
        async with self._client.stream(method, self._url, headers=headers, content=body,
                                       extensions={"sni_hostname": self._hostname}) as response:
            if method == "DELETE" and response.status_code == 405:
                return None  # Protocol permits a server to decline explicit termination.
            if not 200 <= response.status_code < 300:
                raise ExecutionFailure("TOOL_MCP_HTTP_REJECTED")
            session_id = response.headers.get("mcp-session-id")
            if session_id is not None:
                if not _SESSION_ID.fullmatch(session_id):
                    raise ExecutionFailure("TOOL_MCP_PROTOCOL_REJECTED")
                if initializing:
                    self._session_id = session_id
                elif session_id != self._session_id:
                    raise ExecutionFailure("TOOL_MCP_PROTOCOL_REJECTED")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ExecutionFailure("TOOL_MCP_RESPONSE_REJECTED")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1_000_000:
                    raise ExecutionFailure("TOOL_MCP_RESPONSE_TOO_LARGE")
            if method == "DELETE" or response.status_code in {202, 204}:
                return None
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ExecutionFailure("TOOL_MCP_JSON_REQUIRED")
            try:
                result = strict_json(bytes(content))
                canonical_json(result)
                return result
            except Exception:
                raise ExecutionFailure("TOOL_MCP_PROTOCOL_REJECTED") from None

    async def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if await self._http("POST", payload) is not None:
            raise ExecutionFailure("TOOL_MCP_PROTOCOL_REJECTED")

    async def _rpc(self, method, params=None):
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._inflight = (request_id, method)
        result = await self._http("POST", payload, initializing=method == "initialize")
        self._inflight = None
        if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or
                type(result.get("id")) is not int or result["id"] != request_id or
                "method" in result or "error" in result or not isinstance(result.get("result"), dict)):
            raise ExecutionFailure("TOOL_MCP_PROTOCOL_REJECTED")
        return result["result"]

    async def initialize(self):
        result = await self._rpc("initialize", {
            "protocolVersion": self._protocol_version, "capabilities": {},
            "clientInfo": {"name": "enterprise-ai-platform", "version": "0.1.0"},
        })
        capabilities = result.get("capabilities")
        if (result.get("protocolVersion") != self._protocol_version or not isinstance(capabilities, dict) or
                not isinstance(capabilities.get("tools"), dict)):
            raise ExecutionFailure("TOOL_MCP_PROTOCOL_REJECTED")
        self._initialized = True
        await self._notify("notifications/initialized")

    async def tools(self):
        if not self._initialized:
            raise ExecutionFailure("TOOL_MCP_PROTOCOL_REJECTED")
        tools = []
        cursor = None
        cursors = set()
        names = set()
        for _page in range(10):
            result = await self._rpc("tools/list", {"cursor": cursor} if cursor is not None else {})
            page = result.get("tools")
            if not isinstance(page, list) or len(tools) + len(page) > 1000:
                raise ExecutionFailure("TOOL_MCP_CATALOG_REJECTED")
            for tool in page:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or tool["name"] in names:
                    raise ExecutionFailure("TOOL_MCP_CATALOG_REJECTED")
                names.add(tool["name"])
            tools.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024 or cursor in cursors:
                raise ExecutionFailure("TOOL_MCP_CATALOG_REJECTED")
            cursors.add(cursor)
        raise ExecutionFailure("TOOL_MCP_CATALOG_REJECTED")

    async def call(self, name, arguments):
        if not self._initialized:
            raise ExecutionFailure("TOOL_MCP_PROTOCOL_REJECTED")
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})

    async def close(self):
        failed = False
        if self._inflight is not None and self._inflight[1] == "tools/call":
            try:
                async with asyncio.timeout(1):
                    await self._notify("notifications/cancelled", {"requestId": self._inflight[0]})
            except Exception:
                failed = True
        if self._session_id is not None:
            try:
                async with asyncio.timeout(1):
                    await self._http("DELETE")
            except Exception:
                failed = True
        if failed:
            raise ExecutionFailure("TOOL_MCP_CLOSE_RESULT_UNKNOWN")


async def _close_session(session):
    # Bound cleanup but keep it alive through cancellation of its owning Task.
    task = asyncio.create_task(session.close())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


class MCPJSONTransport:
    def __init__(self, *, credentials=None, resolver=None, client_factory=None):
        self._credentials = credentials or EnvironmentCredentialResolver()
        self._resolver = resolver or _system_resolve
        self._client_factory = client_factory or httpx.AsyncClient

    @asynccontextmanager
    async def open(self, connection: MCPConnection):
        secret = None
        if connection.credential_ref is not None:
            try:
                secret = self._credentials.resolve(connection.project_key, connection.credential_ref).value
            except Exception:
                raise ExecutionFailure("TOOL_MCP_CREDENTIAL_UNAVAILABLE") from None
        try:
            hostname = urlsplit(connection.endpoint).hostname
            if hostname is None:
                raise ValueError
            addresses = await asyncio.wait_for(self._resolver(hostname), timeout=5)
            if not addresses or any(not is_public_model_address(address) for address in addresses):
                raise ExecutionFailure("TOOL_MCP_ADDRESS_REJECTED")
            original = httpx.URL(connection.endpoint)
            url = original.copy_with(host=addresses[0])
            async with self._client_factory(trust_env=False, follow_redirects=False, verify=True,
                                            timeout=httpx.Timeout(10, connect=5)) as client:
                session = MCPJSONSession(client, url, hostname, original.netloc.decode("ascii"),
                                         secret, connection.protocol_version)
                failed = False
                try:
                    yield session
                except BaseException:
                    failed = True
                    raise
                finally:
                    try:
                        await _close_session(session)
                    except ExecutionFailure:
                        if not failed:
                            raise
        except (ExecutionFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise ExecutionFailure("TOOL_MCP_TRANSPORT_RESULT_UNKNOWN") from None
