"""Pinned-address HTTP transport for OpenAI-compatible platform model APIs."""

from __future__ import annotations

import asyncio
import json
import re
import socket
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from domain.platform_model import (
    ModelTransportFailure,
    ModelTransportRequest,
    ModelTransportResponse,
    ModelUsage,
    is_public_model_address,
    normalize_provider_url,
)

_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,199}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _public_address(value: str) -> bool:
    return is_public_model_address(value)


async def _system_resolve(hostname: str) -> list[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, hostname, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(record[4][0]) for record in records))


def _safe_failure(code: str, status: int | None = None, retryable: bool = False):
    return ModelTransportFailure(code, http_status=status, retryable=retryable)


class OpenAICompatibleTransport:
    def __init__(self, *, client_factory=None, resolver=None):
        self._client_factory = client_factory or httpx.AsyncClient
        self._resolver = resolver or _system_resolve

    async def complete(self, request: ModelTransportRequest) -> ModelTransportResponse:
        try:
            async with asyncio.timeout(request.timeout_seconds):
                return await self._complete(request)
        except asyncio.CancelledError:
            raise
        except ModelTransportFailure:
            raise
        except TimeoutError:
            raise _safe_failure("MODEL_UPSTREAM_TIMEOUT") from None
        except httpx.ConnectError:
            raise _safe_failure("MODEL_UPSTREAM_UNAVAILABLE", retryable=True) from None
        except Exception:
            raise _safe_failure("MODEL_UPSTREAM_REJECTED") from None

    async def _complete(self, request: ModelTransportRequest) -> ModelTransportResponse:
        payload = {
            "model": request.model,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
            "max_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        response_body, status, request_id = await self._post_json(request.base_url, request.secret, payload)
        try:
            data = json.loads(response_body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError
            message = choice["message"]
            if message.get("refusal") not in {None, ""}:
                raise _safe_failure("MODEL_RESPONSE_REFUSED", status)
            if message.get("tool_calls") or message.get("function_call"):
                raise _safe_failure("MODEL_TOOL_CALL_REJECTED", status)
            if message.get("role", "assistant") != "assistant":
                raise ValueError
            content = message["content"]
            model = data["model"]
            if not isinstance(content, str) or not isinstance(model, str) or _MODEL_PATTERN.fullmatch(model) is None:
                raise ValueError
            return ModelTransportResponse(content=content, actual_model=model, usage=response_usage(data.get("usage")),
                                          http_status=status, request_id=request_id)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, ValidationError):
            raise _safe_failure("MODEL_RESPONSE_INVALID", status) from None

    async def _post_json(self, base_url, secret, payload):
        """Shared bounded egress only; response-mode parsers remain independent."""
        try:
            base_url = normalize_provider_url(base_url)
            parsed = urlsplit(base_url)
            host = parsed.hostname
            if host is None:
                raise ValueError
            addresses = await asyncio.wait_for(self._resolver(host), timeout=5)
            if not addresses or any(not _public_address(address) for address in addresses):
                raise _safe_failure("MODEL_PROVIDER_ADDRESS_REJECTED")
            pinned = addresses[0]
            if not _public_address(pinned):
                raise _safe_failure("MODEL_PROVIDER_ADDRESS_REJECTED")
            original = httpx.URL(base_url + "/chat/completions")
            connect_url = original.copy_with(host=pinned)
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > 1_000_000:
                raise _safe_failure("MODEL_REQUEST_REJECTED")
            headers = {"Authorization": f"Bearer {secret.value}", "Content-Type": "application/json", "Host": original.netloc}
        except ModelTransportFailure:
            raise
        except Exception:
            raise _safe_failure("MODEL_REQUEST_REJECTED") from None

        try:
            async with self._client_factory(
                trust_env=False,
                follow_redirects=False,
                verify=True,
                timeout=None,
            ) as client:
                async with client.stream(
                    "POST", connect_url, headers=headers, content=body,
                    extensions={"sni_hostname": host},
                ) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise _safe_failure("MODEL_REDIRECT_REJECTED", status)
                    if status in {429, 500, 502, 503, 504}:
                        raise _safe_failure("MODEL_UPSTREAM_UNAVAILABLE", status, True)
                    if not 200 <= status < 300:
                        raise _safe_failure("MODEL_UPSTREAM_REJECTED", status)
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > _MAX_RESPONSE_BYTES:
                            raise _safe_failure("MODEL_RESPONSE_TOO_LARGE", status)
                    response_body = bytes(chunks)
                    request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                    if request_id is not None and _REQUEST_ID.fullmatch(request_id) is None:
                        request_id = None
        except ModelTransportFailure:
            raise
        except asyncio.CancelledError:
            raise
        except httpx.ConnectError:
            raise _safe_failure("MODEL_UPSTREAM_UNAVAILABLE", retryable=True) from None
        except (httpx.TimeoutException, TimeoutError):
            raise _safe_failure("MODEL_UPSTREAM_TIMEOUT") from None
        except Exception:
            raise _safe_failure("MODEL_UPSTREAM_REJECTED") from None

        return response_body, status, request_id


def response_usage(raw_usage) -> ModelUsage:
    values = {}
    if raw_usage is not None:
        if not isinstance(raw_usage, dict):
            raise ValueError("Invalid usage")
        for source, target in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
            if source in raw_usage:
                value = raw_usage[source]
                if type(value) is not int or value < 0 or value > 9_223_372_036_854_775_807:
                    raise ValueError("Invalid usage")
                values[target] = value
    return ModelUsage(**values)
