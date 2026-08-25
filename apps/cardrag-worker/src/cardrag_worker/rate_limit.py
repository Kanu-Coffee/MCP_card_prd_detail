"""Shared per-issuer request pacing for discovery, handshakes, and PDF streams."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import httpx


class IssuerRateLimiter:
    def __init__(
        self,
        interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError("rate interval cannot be negative")
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.sleep = sleep
        self._last_request: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self.clock()
            if self._last_request is not None:
                delay = self.interval_seconds - (now - self._last_request)
                if delay > 0:
                    await self.sleep(delay)
                    now = self.clock()
            self._last_request = now


class _LimitedStream:
    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: IssuerRateLimiter,
        method: str,
        url: str,
        kwargs: Mapping[str, Any],
    ) -> None:
        self.client = client
        self.limiter = limiter
        self.method = method
        self.url = url
        self.kwargs = dict(kwargs)
        self.context: Any = None

    async def __aenter__(self) -> httpx.Response:
        await self.limiter.wait()
        self.context = self.client.stream(self.method, self.url, **self.kwargs)
        return cast(httpx.Response, await self.context.__aenter__())

    async def __aexit__(self, *args: object) -> None:
        await self.context.__aexit__(*args)


class RateLimitedClient:
    """The small AsyncClient surface issuer adapters/downloader are allowed to use."""

    def __init__(self, client: httpx.AsyncClient, limiter: IssuerRateLimiter) -> None:
        self.client = client
        self.limiter = limiter

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        await self.limiter.wait()
        return await self.client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        await self.limiter.wait()
        return await self.client.post(url, **kwargs)

    def stream(self, method: str, url: str, **kwargs: Any) -> _LimitedStream:
        return _LimitedStream(self.client, self.limiter, method, url, kwargs)
