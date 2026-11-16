"""Pinned-address HTTP transport for OpenAI-compatible platform model APIs."""

from __future__ import annotations

import asyncio
import json
import math
import re
import socket
import zlib
from urllib.parse import urlsplit

import httpx

from domain.outbound_operation import OutboundEvidence, evidence_from_exception
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


def _safe_failure(code: str, evidence: OutboundEvidence):
    return ModelTransportFailure(code, evidence=evidence)


def _response_json(body: bytes):
    """Ambiguous or non-finite JSON cannot establish usage or replay authority."""
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON number")
        return parsed

    def reject_constant(_value):
        raise ValueError("Non-finite JSON constant")

    return json.loads(body, object_pairs_hook=unique_pairs, parse_float=finite_float, parse_constant=reject_constant)


class _ResponseBodyDecoder:
    """Bound decoded bytes and require a complete negotiated representation.

    The owning HTTPX response context closes the network stream, after receipt
    evidence is captured. Reading its raw stream avoids HTTPX's implicit close
    at iterator EOF; no cleanup task outlives the request.
    """

    def __init__(self, encoding: str, evidence: OutboundEvidence):
        self.encoding = encoding
        self.evidence = evidence.with_acceptance(False)
        if encoding not in {"identity", "gzip", "deflate"}:
            raise _safe_failure("MODEL_RESPONSE_INVALID", self.evidence)
        self._decoder = zlib.decompressobj(zlib.MAX_WBITS | 16) if encoding == "gzip" else None
        self._prefix = b""
        self._decoded_bytes = 0

    def decode(self, chunk):
        if self.encoding == "deflate" and self._decoder is None:
            chunk = self._prefix + chunk
            # Detect the RFC 1950 header independently of network chunking.
            # Raw DEFLATE is also used by some compatible HTTP providers.
            if len(chunk) < 2:
                self._prefix = chunk
                return b""
            self._prefix = b""
            cmf, flags = chunk[:2]
            wrapped = cmf & 15 == 8 and cmf >> 4 <= 7 and (cmf * 256 + flags) % 31 == 0
            self._decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
        try:
            decoded = (chunk if self._decoder is None else
                       self._decoder.decompress(chunk, _MAX_RESPONSE_BYTES - self._decoded_bytes + 1))
        except zlib.error:
            raise _safe_failure("MODEL_RESPONSE_INVALID", self.evidence) from None
        self._decoded_bytes += len(decoded)
        if self._decoded_bytes > _MAX_RESPONSE_BYTES:
            raise _safe_failure("MODEL_RESPONSE_TOO_LARGE", self.evidence)
        return decoded

    def require_complete(self):
        if self.encoding != "identity" and (
            self._decoder is None or not self._decoder.eof or self._decoder.unused_data or self._decoder.unconsumed_tail
        ):
            raise _safe_failure("MODEL_RESPONSE_INVALID", self.evidence)


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
        except TimeoutError as exc:
            raise _safe_failure("MODEL_UPSTREAM_TIMEOUT", evidence_from_exception(exc)) from None
        except httpx.ConnectError:
            raise _safe_failure("MODEL_UPSTREAM_UNAVAILABLE", OutboundEvidence()) from None
        except Exception:
            raise _safe_failure("MODEL_UPSTREAM_REJECTED", OutboundEvidence()) from None

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
        response_body, status, request_id = await self._post_json(
            request.base_url, request.secret, payload, timeout_seconds=request.timeout_seconds)
        evidence = response_evidence(None, status, request_id)
        try:
            data = _response_json(response_body)
            evidence = response_evidence(data, status, request_id)
            usage = response_usage(data.get("usage"))
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError
            message = choice["message"]
            if message.get("refusal") not in {None, ""}:
                raise _safe_failure("MODEL_RESPONSE_REFUSED", evidence.with_acceptance(False))
            if message.get("tool_calls") or message.get("function_call"):
                raise _safe_failure("MODEL_TOOL_CALL_REJECTED", evidence.with_acceptance(False))
            if message.get("role", "assistant") != "assistant":
                raise ValueError
            content = message["content"]
            model = data["model"]
            if not isinstance(content, str) or not isinstance(model, str) or _MODEL_PATTERN.fullmatch(model) is None:
                raise ValueError
            return ModelTransportResponse(content=content, actual_model=model, usage=usage,
                                          http_status=status, request_id=request_id)
        except ModelTransportFailure:
            raise
        except Exception:
            raise _safe_failure("MODEL_RESPONSE_INVALID", evidence.with_acceptance(False)) from None

    async def _post_json(self, base_url, secret, payload, *, timeout_seconds=None):
        """Shared bounded egress only; response-mode parsers remain independent."""
        progress = [OutboundEvidence(dispatch="not_dispatched")]
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self._exchange_json(base_url, secret, payload, progress)
        except ModelTransportFailure:
            raise
        except asyncio.CancelledError as exc:
            exc.evidence = progress[0]
            raise
        except httpx.ConnectError:
            raise _safe_failure("MODEL_UPSTREAM_UNAVAILABLE", progress[0]) from None
        except (httpx.TimeoutException, TimeoutError):
            raise _safe_failure("MODEL_UPSTREAM_TIMEOUT", progress[0]) from None
        except Exception:
            raise _safe_failure("MODEL_UPSTREAM_REJECTED", progress[0]) from None

    async def _exchange_json(self, base_url, secret, payload, progress):
        try:
            base_url = normalize_provider_url(base_url)
            parsed = urlsplit(base_url)
            host = parsed.hostname
            if host is None:
                raise ValueError
            addresses = await asyncio.wait_for(self._resolver(host), timeout=5)
            if not addresses or any(not _public_address(address) for address in addresses):
                raise _safe_failure("MODEL_PROVIDER_ADDRESS_REJECTED", progress[0])
            pinned = addresses[0]
            if not _public_address(pinned):
                raise _safe_failure("MODEL_PROVIDER_ADDRESS_REJECTED", progress[0])
            original = httpx.URL(base_url + "/chat/completions")
            connect_url = original.copy_with(host=pinned)
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > 1_000_000:
                raise _safe_failure("MODEL_REQUEST_REJECTED", progress[0])
            headers = {"Authorization": f"Bearer {secret.value}", "Content-Type": "application/json", "Host": original.netloc,
                       "Accept-Encoding": "gzip, deflate, identity"}
        except ModelTransportFailure:
            raise
        except Exception:
            raise _safe_failure("MODEL_REQUEST_REJECTED", progress[0]) from None

        try:
            async with self._client_factory(
                trust_env=False,
                follow_redirects=False,
                verify=True,
                timeout=None,
            ) as client:
                # Crossing this boundary is not proof that the provider did or
                # did not execute. Network errors cannot authorize paid replay.
                progress[0] = OutboundEvidence()
                async with client.stream(
                    "POST", connect_url, headers=headers, content=body,
                    extensions={"sni_hostname": host},
                ) as response:
                    status = response.status_code
                    request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                    if request_id is not None and _REQUEST_ID.fullmatch(request_id) is None:
                        request_id = None
                    progress[0] = OutboundEvidence(dispatch="dispatched", response="headers_received",
                                                   http_status=status, request_id=request_id)
                    encoding = response.headers.get("content-encoding", "identity").strip().lower()
                    decoder = _ResponseBodyDecoder(encoding, progress[0])
                    chunks = bytearray()
                    async for chunk in response.stream:
                        chunks.extend(decoder.decode(chunk))
                    decoder.require_complete()
                    response_body = bytes(chunks)
                    try:
                        data = _response_json(response_body)
                    except (ValueError, UnicodeError, RecursionError):
                        data = None
                    # Capture known metadata before response/client cleanup too.
                    progress[0] = response_evidence(data, status, request_id)
                    if not 200 <= status < 300:
                        evidence = progress[0].with_acceptance(False)
                        # Only an explicit complete rate rejection is classified
                        # retry-safe. Generic 5xx, partial bodies and known Usage
                        # never grant replay, even if the provider says unavailable.
                        if (status == 429 and isinstance(data, dict) and data.get("usage") in (None, {}) and
                                evidence.usage_certainty == "unknown"):
                            evidence = OutboundEvidence(**{**evidence.model_dump(), "retry_permission": "safe_response"})
                        progress[0] = evidence
                        code = ("MODEL_REDIRECT_REJECTED" if 300 <= status < 400 else
                                "MODEL_UPSTREAM_UNAVAILABLE" if status in {429, 500, 502, 503, 504} else
                                "MODEL_UPSTREAM_REJECTED")
                        raise _safe_failure(code, evidence)
        except ModelTransportFailure:
            raise
        except asyncio.CancelledError:
            raise
        except httpx.ConnectError:
            raise _safe_failure("MODEL_UPSTREAM_UNAVAILABLE", progress[0]) from None
        except (httpx.TimeoutException, TimeoutError):
            raise _safe_failure("MODEL_UPSTREAM_TIMEOUT", progress[0]) from None
        except Exception:
            raise _safe_failure("MODEL_UPSTREAM_REJECTED", progress[0]) from None

        return response_body, status, request_id


def response_evidence(data, status, request_id) -> OutboundEvidence:
    """Extract each valid metadata field before validating the answer/Usage shape."""
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, str) or _MODEL_PATTERN.fullmatch(model) is None:
        model = None
    raw_usage = data.get("usage") if isinstance(data, dict) else None
    values = {}
    if isinstance(raw_usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw_usage.get(key)
            if type(value) is int and 0 <= value <= 9_223_372_036_854_775_807:
                values[key] = value
    return OutboundEvidence(dispatch="dispatched", response="received", actual_model=model,
                            usage=ModelUsage(**values), http_status=status, request_id=request_id)


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
