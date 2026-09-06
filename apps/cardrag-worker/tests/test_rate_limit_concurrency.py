from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest

from cardrag_worker.rate_limit import HostConcurrencyLimiter, IssuerRateLimiter, RateLimitedClient


async def test_actual_host_limit_spans_separate_clients_and_entire_stream_lifetime() -> None:
    limiter = HostConcurrencyLimiter(2)
    pacing = IssuerRateLimiter(0)
    release = asyncio.Event()
    first_wave = asyncio.Event()
    entered: list[str] = []

    async def consume(client: RateLimitedClient, host: str) -> None:
        async with client.stream("GET", f"https://{host}/file.pdf") as response:
            entered.append(host)
            if len(entered) == 3:
                first_wave.set()
            await release.wait()
            assert await response.aread() == b"pdf"

    async with AsyncExitStack() as stack:
        clients = [
            RateLimitedClient(
                await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"pdf"))
                    )
                ),
                pacing,
                limiter,
            )
            for _ in range(4)
        ]
        tasks = [
            asyncio.create_task(consume(client, host))
            for client, host in zip(clients, ("a.test", "a.test", "a.test", "b.test"), strict=True)
        ]
        await asyncio.wait_for(first_wave.wait(), timeout=2)
        assert entered.count("a.test") == 2
        assert entered.count("b.test") == 1
        release.set()
        await asyncio.gather(*tasks)
    assert entered.count("a.test") == 3


@pytest.mark.parametrize("method", ["get", "post", "stream"])
async def test_host_slot_is_released_when_request_entry_fails(method: str) -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("failed", request=request)
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as raw:
        client = RateLimitedClient(raw, IssuerRateLimiter(0), HostConcurrencyLimiter(1))

        async def request() -> None:
            if method == "stream":
                async with client.stream("GET", "https://a.test/pdf"):
                    pass
            else:
                await getattr(client, method)("https://a.test/prepare")

        with pytest.raises(httpx.ConnectError):
            await request()
        await asyncio.wait_for(request(), timeout=2)
    assert attempts == 2
