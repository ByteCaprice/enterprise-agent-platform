"""Fixed reviewed HTTPS destination, public DNS pinning, HMAC and bounded responses."""

import asyncio
import hashlib
import hmac
import time
from urllib.parse import urlsplit

import httpx

from domain.platform_model import is_public_model_address, normalize_provider_url
from domain.task import canonical_json
from infra.model_http import _system_resolve


class CallbackHTTPTransport:
    def __init__(self, *, resolver=None, client_factory=None):
        self.resolver = resolver or _system_resolve
        self.client_factory = client_factory or httpx.AsyncClient

    async def send(self, delivery, endpoint, secret):
        async with asyncio.timeout(endpoint["timeout_seconds"]):
            url = normalize_provider_url(endpoint["url"])
            host = urlsplit(url).hostname
            if host is None:
                raise ValueError("CALLBACK_ADDRESS_REJECTED")
            addresses = await self.resolver(host)
            if not addresses or any(not is_public_model_address(address) for address in addresses):
                raise ValueError("CALLBACK_ADDRESS_REJECTED")
            original = httpx.URL(url)
            body = canonical_json(delivery["payload"]).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(secret.value.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
            headers = {"Host": original.netloc, "Content-Type": "application/json",
                "X-Enterprise-AI-Delivery": delivery["id"], "X-Enterprise-AI-Timestamp": timestamp,
                "X-Enterprise-AI-Signature": "sha256=" + signature}
            async with self.client_factory(trust_env=False, follow_redirects=False, verify=True, timeout=None) as client:
                async with client.stream("POST", original.copy_with(host=addresses[0]), content=body, headers=headers,
                                         extensions={"sni_hostname": host}) as response:
                    count = 0
                    async for chunk in response.aiter_bytes():
                        count += len(chunk)
                        if count > 65536:
                            raise ValueError("CALLBACK_RESPONSE_TOO_LARGE")
                    return response.status_code
