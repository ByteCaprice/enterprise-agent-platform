import httpx
import pytest

from domain.platform_model import ModelMessage, ModelSecret, ModelTransportFailure, ModelTransportRequest
from infra.model_credentials import EnvironmentCredentialResolver, credential_variable_name
from infra.model_http import OpenAICompatibleTransport


@pytest.mark.asyncio
async def test_pinned_http_transport_preserves_host_sni_and_response_fields():
    observed = {}

    async def handler(request):
        observed["host"] = request.headers["host"]
        observed["url_host"] = request.url.host
        observed["sni"] = request.extensions["sni_hostname"]
        observed["payload"] = request.read()
        return httpx.Response(200, json={"model": "provider/model-v2", "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}})

    async def resolver(host):
        assert host == "api.example.com"
        return ["8.8.8.8"]

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    request = ModelTransportRequest(
        base_url="https://api.example.com/v1", model="requested", messages=(ModelMessage(role="user", content="hi"),),
        secret=ModelSecret(reference="secret", value="dont-log-this"), timeout_seconds=2, max_tokens=50,
    )
    result = await OpenAICompatibleTransport(client_factory=factory, resolver=resolver).complete(request)
    assert observed["host"] == "api.example.com"
    assert observed["url_host"] == "8.8.8.8"
    assert observed["sni"] == "api.example.com"
    assert b'"model":"requested"' in observed["payload"]
    assert result.content == "answer" and result.actual_model == "provider/model-v2"
    assert result.usage.prompt_tokens == 4 and result.usage.completion_tokens == 2


@pytest.mark.asyncio
async def test_transport_rejects_any_private_dns_answer():
    async def resolver(_host):
        return ["8.8.8.8", "127.0.0.1"]

    request = ModelTransportRequest("https://api.example.com", "m", (ModelMessage(role="user", content="hi"),), ModelSecret("r", "s"), 2, 10)
    with pytest.raises(ModelTransportFailure) as error:
        await OpenAICompatibleTransport(resolver=resolver).complete(request)
    assert error.value.code == "MODEL_PROVIDER_ADDRESS_REJECTED"


def test_environment_credentials_are_scoped_and_secret_is_not_represented(monkeypatch):
    first = credential_variable_name("project_a", "provider_key")
    second = credential_variable_name("project_b", "provider_key")
    assert first != second
    monkeypatch.setenv(first, "sensitive-token")
    secret = EnvironmentCredentialResolver().resolve("project_a", "provider_key")
    assert secret.value == "sensitive-token"
    assert "sensitive-token" not in repr(secret)
    with pytest.raises(ModelTransportFailure) as error:
        EnvironmentCredentialResolver().resolve("project_b", "provider_key")
    assert error.value.code == "MODEL_CREDENTIAL_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("message,reason,code", [
    ({"content": "{}", "refusal": "denied"}, "stop", "MODEL_RESPONSE_REFUSED"),
    ({"content": "{}", "tool_calls": [{"type": "function"}]}, "stop", "MODEL_TOOL_CALL_REJECTED"),
    ({"content": "{}", "function_call": {"name": "legacy"}}, "stop", "MODEL_TOOL_CALL_REJECTED"),
    ({"content": "{}"}, "length", "MODEL_RESPONSE_INVALID"),
])
async def test_refusal_tools_and_truncation_cannot_be_success(message, reason, code):
    async def handler(_request):
        return httpx.Response(200, json={"model": "m", "choices": [{"finish_reason": reason, "message": message}]})

    async def resolver(_host):
        return ["8.8.8.8"]

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    request = ModelTransportRequest("https://api.example.com", "m", (ModelMessage(role="user", content="hi"),),
                                    ModelSecret("r", "synthetic"), 2, 10)
    with pytest.raises(ModelTransportFailure) as error:
        await OpenAICompatibleTransport(client_factory=factory, resolver=resolver).complete(request)
    assert error.value.code == code and not error.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["4000::1", "127.0.0.1", "::ffff:127.0.0.1", "224.0.0.1"])
async def test_special_dns_addresses_are_rejected_before_http(address):
    async def resolver(_host):
        return [address]

    def forbidden_client(**_kwargs):
        pytest.fail("Rejected DNS must not construct a client")

    request = ModelTransportRequest("https://api.example.com", "m", (ModelMessage(role="user", content="hi"),),
                                    ModelSecret("r", "synthetic"), 2, 10)
    with pytest.raises(ModelTransportFailure, match="MODEL_PROVIDER_ADDRESS_REJECTED"):
        await OpenAICompatibleTransport(client_factory=forbidden_client, resolver=resolver).complete(request)
