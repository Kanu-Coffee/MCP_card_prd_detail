"""Shared per-issuer request pacing for discovery, handshakes, and PDF streams."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
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


class HostConcurrencyLimiter:
    """Share request-lifetime limits by actual host across session-isolated clients."""

    def __init__(self, concurrency: int = 2) -> None:
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("host concurrency must be a positive integer")
        self.concurrency = concurrency
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        host = httpx.URL(url).host.casefold()
        semaphore = self._semaphores.setdefault(host, asyncio.Semaphore(self.concurrency))
        async with semaphore:
            yield


class _LimitedStream:
    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: IssuerRateLimiter,
        method: str,
        url: str,
        kwargs: Mapping[str, Any],
        host_limiter: HostConcurrencyLimiter | None = None,
    ) -> None:
        self.client = client
        self.limiter = limiter
        self.method = method
        self.url = url
        self.kwargs = dict(kwargs)
        self.context: Any = None
        self.host_context: Any = None if host_limiter is None else host_limiter.slot(url)

    async def __aenter__(self) -> httpx.Response:
        if self.host_context is not None:
            await self.host_context.__aenter__()
        try:
            await self.limiter.wait()
            self.context = self.client.stream(self.method, self.url, **self.kwargs)
            return cast(httpx.Response, await self.context.__aenter__())
        except BaseException as exc:
            from .issuer_metrics import note_transport_failure

            note_transport_failure(exc)
            if self.host_context is not None:
                await self.host_context.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args: object) -> None:
        try:
            await self.context.__aexit__(*args)
        finally:
            if self.host_context is not None:
                await self.host_context.__aexit__(*args)


class RateLimitedClient:
    """The small AsyncClient surface issuer adapters/downloader are allowed to use."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: IssuerRateLimiter,
        host_limiter: HostConcurrencyLimiter | None = None,
        *,
        issuer: str = "unknown",
        purpose: str = "catalog",
    ) -> None:
        self.client = client
        self.limiter = limiter
        self.host_limiter = host_limiter
        self.issuer, self.purpose = issuer, purpose

    def _extensions(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        kwargs["extensions"] = {
            **kwargs.get("extensions", {}),
            "cardrag_issuer": self.issuer,
            "cardrag_purpose": self.purpose,
        }
        return kwargs

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            if self.host_limiter is not None:
                async with self.host_limiter.slot(url):
                    await self.limiter.wait()
                    response = await self.client.request(method, url, **self._extensions(kwargs))
            else:
                await self.limiter.wait()
                response = await self.client.request(method, url, **self._extensions(kwargs))
        except httpx.RequestError as exc:
            from .issuer_metrics import note_transport_failure

            note_transport_failure(exc)
            raise
        self._raise_for_origin_service_error(response)
        return response

    @staticmethod
    def _raise_for_origin_service_error(response: httpx.Response) -> None:
        if response.status_code != 200 or not response.is_stream_consumed:
            return
        content_type = response.headers.get("content-type")
        from .issuer_collection import OriginServiceError, classify_html_service_error, safe_mime_type

        signal = classify_html_service_error(response.content[:65536], content_type)
        if signal is not None:
            raise OriginServiceError(
                status_code=response.status_code,
                mime_type=safe_mime_type(content_type),
                service_signal=signal,
            )

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    def stream(self, method: str, url: str, **kwargs: Any) -> _LimitedStream:
        return _LimitedStream(
            self.client, self.limiter, method, url, self._extensions(kwargs), self.host_limiter
        )
