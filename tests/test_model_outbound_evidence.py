"""Outbound evidence regressions with in-memory HTTP streams only."""

import asyncio
import gzip
import json
import zlib
from types import SimpleNamespace

import httpx
import pytest

from domain.agent_turn import AgentConversation, AgentTurnRequest
from domain.platform_model import ModelMessage, ModelSecret, ModelTransportFailure, ModelTransportRequest
from infra.agent_turn_http import AgentTurnHTTPTransport
from infra.model_http import OpenAICompatibleTransport
from infra.store.repository.models import _permits_fallback

RESPONSE = {
    "model": "catalog/primary", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
}


@pytest.fixture
def anyio_backend():
    # HTTPX already requires AnyIO; exercise asyncio-specific cancellation without
    # adding a separate pytest-asyncio dependency or requiring Trio.
    return "asyncio"


def transport_for(handler, mode="completion"):
    async def resolver(_host):
        return ["8.8.8.8"]

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    cls = OpenAICompatibleTransport if mode == "completion" else AgentTurnHTTPTransport
    return cls(client_factory=factory, resolver=resolver)


async def invoke(transport, mode="completion", *, timeout=0.2):
    secret = ModelSecret("synthetic", "synthetic")
    if mode == "completion":
        return await transport.complete(ModelTransportRequest(
            "https://api.example.com/v1", "requested", (ModelMessage(role="user", content="hi"),), secret, timeout, 32,
        ))
    conversation = AgentConversation.model_validate({"messages": [
        {"role": "system", "content": "Synthetic instructions"}, {"role": "user", "content": "hi"},
    ]})
    return await transport.generate_turn(AgentTurnRequest(
        "https://api.example.com/v1", "requested", conversation, (), secret, timeout, 32,
    ))


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    b'{"usage":{"prompt_tokens":9,"completion_tokens":3},"usage":{}}',
    b'{"usage":{"total_tokens":12},"usage":null}',
    b'{"usage":{},"error":{"code":"first","code":"second"}}',
    b'{"usage":{},"extra":NaN}',
    b'{"usage":{},"extra":1e999}',
])
async def test_ambiguous_or_nonfinite_rate_response_never_authorizes_fallback(body):
    async def handler(_request):
        return httpx.Response(429, content=body)

    with pytest.raises(ModelTransportFailure) as caught:
        await invoke(transport_for(handler))
    failure = caught.value
    assert failure.code == "MODEL_UPSTREAM_UNAVAILABLE" and not failure.retryable
    assert failure.evidence.response == "received" and failure.evidence.retry_permission == "never"
    call = SimpleNamespace(status="failed", error_code=failure.code, http_status=429,
        actual_model=failure.evidence.actual_model, provider_request_id=failure.evidence.request_id,
        outbound_evidence=failure.evidence.model_dump(), **failure.evidence.usage.model_dump())
    assert not _permits_fallback(call)


@pytest.mark.anyio
async def test_completion_cannot_accept_duplicate_response_fields():
    body = json.dumps(RESPONSE)[:-1].encode() + b',"usage":{}}'

    async def handler(_request):
        return httpx.Response(200, content=body)

    with pytest.raises(ModelTransportFailure, match="^MODEL_RESPONSE_INVALID$") as caught:
        await invoke(transport_for(handler))
    assert caught.value.evidence.acceptance == "rejected" and not caught.value.retryable


@pytest.mark.anyio
@pytest.mark.parametrize("usage,permitted", [(None, True), ({"prompt_tokens": 0, "completion_tokens": 0}, False)])
async def test_only_complete_rate_response_without_usage_permits_fallback(usage, permitted):
    async def handler(_request):
        return httpx.Response(429, json={"usage": usage})

    with pytest.raises(ModelTransportFailure) as caught:
        await invoke(transport_for(handler))
    assert caught.value.retryable is permitted
    assert (caught.value.evidence.retry_permission == "safe_response") is permitted


@pytest.mark.anyio
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "deflate_raw"])
@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("chunk_size", [None, 1])
async def test_compressed_rate_response_requires_a_complete_representation(encoding, truncated, chunk_size):
    body = b'{"usage":null}'
    if encoding == "gzip":
        body = gzip.compress(body)
    else:
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS if encoding == "deflate_raw" else zlib.MAX_WBITS)
        body = compressor.compress(body) + compressor.flush()
    if truncated:
        body = body[:-8] if encoding == "gzip" else body[:-1]
    stream = ResponseStream(body, chunk_size=chunk_size)

    async def handler(request):
        assert request.headers["accept-encoding"] == "gzip, deflate, identity"
        return httpx.Response(429, stream=stream,
                              headers={"content-encoding": "deflate" if encoding == "deflate_raw" else encoding})

    with pytest.raises(ModelTransportFailure) as caught:
        await invoke(transport_for(handler))
    assert stream.close_calls == 1
    if truncated:
        assert caught.value.code == "MODEL_RESPONSE_INVALID" and not caught.value.retryable
        assert caught.value.evidence.response == "headers_received"
    else:
        assert caught.value.code == "MODEL_UPSTREAM_UNAVAILABLE" and caught.value.retryable
        assert caught.value.evidence.response == "received"


@pytest.mark.anyio
@pytest.mark.parametrize("body,encoding", [
    (gzip.compress(b'{"usage":null}') + b"trailing-data", "gzip"),
    (b'{"usage":null}', "unsupported"),
])
async def test_ignored_or_unsupported_encoded_content_cannot_authorize_replay(body, encoding):
    stream = ResponseStream(body)

    async def handler(_request):
        return httpx.Response(429, stream=stream, headers={"content-encoding": encoding})

    with pytest.raises(ModelTransportFailure, match="^MODEL_RESPONSE_INVALID$") as caught:
        await invoke(transport_for(handler))
    assert stream.close_calls == 1 and not caught.value.retryable
    assert caught.value.evidence.response == "headers_received"


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, body, *, hold_body=False, hold_close=False, chunk_size=None):
        self.body = body
        self.hold_body = hold_body
        self.hold_close = hold_close
        self.chunk_size = chunk_size
        self.eof = False
        self.close_started = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self):
        size = self.chunk_size or max(len(self.body) // 2, 1)
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]
        if self.hold_body:
            await asyncio.Event().wait()
        self.eof = True

    async def aclose(self):
        self.close_calls += 1
        self.close_started.set()
        if self.hold_close:
            await asyncio.Event().wait()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["completion", "agent"])
@pytest.mark.parametrize("compressed", [False, True])
async def test_cleanup_timeout_retains_complete_decoded_response_evidence(mode, compressed):
    body = json.dumps(RESPONSE).encode()
    stream = ResponseStream(gzip.compress(body) if compressed else body, hold_close=True)

    async def handler(_request):
        headers = {"x-request-id": "known-response"}
        if compressed:
            headers["content-encoding"] = "gzip"
        return httpx.Response(200, stream=stream, headers=headers)

    with pytest.raises(ModelTransportFailure, match="^MODEL_UPSTREAM_TIMEOUT$") as caught:
        await asyncio.wait_for(invoke(transport_for(handler, mode), mode), timeout=2)
    evidence = caught.value.evidence
    assert stream.eof and stream.close_calls == 1
    assert evidence.response == "received" and evidence.acceptance == "unvalidated"
    assert evidence.actual_model == "catalog/primary" and evidence.request_id == "known-response"
    assert evidence.usage.model_dump() == RESPONSE["usage"]
    assert evidence.retry_permission == "never" and not caught.value.retryable


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["completion", "agent"])
async def test_parseable_prefix_without_body_eof_is_not_complete_response(mode):
    stream = ResponseStream(json.dumps(RESPONSE).encode(), hold_body=True)

    async def handler(_request):
        return httpx.Response(429, stream=stream)

    with pytest.raises(ModelTransportFailure, match="^MODEL_UPSTREAM_TIMEOUT$") as caught:
        await asyncio.wait_for(invoke(transport_for(handler, mode), mode), timeout=2)
    assert not stream.eof and stream.close_calls == 1
    assert caught.value.evidence.response == "headers_received"
    assert caught.value.evidence.usage_certainty == "unknown" and not caught.value.retryable


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["completion", "agent"])
async def test_cancellation_during_cleanup_retains_usage_without_detached_cleanup(mode):
    stream = ResponseStream(json.dumps(RESPONSE).encode(), hold_close=True)

    async def handler(_request):
        return httpx.Response(200, stream=stream)

    task = asyncio.create_task(invoke(transport_for(handler, mode), mode, timeout=2))
    try:
        await asyncio.wait_for(stream.close_started.wait(), timeout=1)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=1)
    assert stream.eof and stream.close_calls == 1 and task.done()
    assert caught.value.evidence.response == "received"
    assert caught.value.evidence.usage.model_dump() == RESPONSE["usage"]


@pytest.mark.anyio
async def test_decoded_size_limit_still_rejects_and_closes_owning_response():
    stream = ResponseStream(gzip.compress(b"x" * (4 * 1024 * 1024 + 1)))

    async def handler(_request):
        return httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})

    with pytest.raises(ModelTransportFailure, match="^MODEL_RESPONSE_TOO_LARGE$") as caught:
        await invoke(transport_for(handler))
    assert stream.close_calls == 1
    assert caught.value.evidence.response == "headers_received" and not caught.value.retryable
