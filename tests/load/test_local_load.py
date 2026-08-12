from __future__ import annotations

import asyncio
import time

import pytest

from cardrag.service.models import SearchPage, SearchRequest
from cardrag.service.query import QueryService


class _ConcurrentRepository:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0

    async def search_evidence(self, request: SearchRequest) -> SearchPage:
        del request
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.005)
            return SearchPage(
                generation_id="gen-load",
                items=[],
                no_evidence=True,
                warnings=("no_evidence",),
            )
        finally:
            self.active -= 1


@pytest.mark.load
async def test_five_request_admission_limit_is_bounded_and_completes() -> None:
    repository = _ConcurrentRepository()
    service = QueryService(
        repository,  # type: ignore[arg-type]
        max_concurrent_requests=5,
        request_timeout_seconds=1.0,
    )
    started = time.perf_counter()
    results = await asyncio.gather(
        *(service.search(SearchRequest(query=f"fixture-{index}")) for index in range(50))
    )

    assert repository.maximum_active == 5
    assert all(result.no_evidence for result in results)
    assert time.perf_counter() - started < 1.0
