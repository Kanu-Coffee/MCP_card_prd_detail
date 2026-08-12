#!/usr/bin/env python3
"""Measure the bounded online admission path without external services."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cardrag.service.models import SearchPage, SearchRequest
from cardrag.service.query import QueryService


class SyntheticSearchRepository:
    """A deterministic delayed repository used only by this development probe."""

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.active = 0
        self.maximum_active = 0

    async def search_evidence(self, request: SearchRequest) -> SearchPage:
        del request
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(self.delay_seconds)
            return SearchPage(
                generation_id="gen-20260812T000000Z-000000000000",
                items=[],
                no_evidence=True,
                warnings=("no_evidence",),
            )
        finally:
            self.active -= 1


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def benchmark(*, requests: int, concurrency: int, delay_seconds: float) -> dict[str, Any]:
    repository = SyntheticSearchRepository(delay_seconds)
    service = QueryService(
        repository,  # type: ignore[arg-type] - this probe uses only the search protocol surface
        max_concurrent_requests=concurrency,
        request_timeout_seconds=45.0,
    )
    durations: list[float] = []
    errors: list[str] = []

    async def one(index: int) -> None:
        started = time.perf_counter()
        try:
            result = await service.search(SearchRequest(query=f"synthetic quality probe {index}"))
            if not result.no_evidence:
                errors.append("unexpected_result")
        except Exception as exc:  # pragma: no cover - retained in the emitted report
            errors.append(type(exc).__name__)
        finally:
            durations.append(time.perf_counter() - started)

    started = time.perf_counter()
    await asyncio.gather(*(one(index) for index in range(requests)))
    elapsed = time.perf_counter() - started
    p95 = _percentile(durations, 0.95)
    target_p95 = 30.0
    status = (
        "passed"
        if not errors and repository.maximum_active == concurrency and p95 <= target_p95
        else "failed"
    )
    return {
        "schema_version": "cardrag-local-load-report.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "synthetic admission-control probe; not a production corpus/provider benchmark",
        "requests": requests,
        "configured_concurrency": concurrency,
        "observed_max_concurrency": repository.maximum_active,
        "simulated_repository_delay_seconds": delay_seconds,
        "elapsed_seconds": elapsed,
        "throughput_requests_per_second": requests / elapsed,
        "latency_seconds": {
            "mean": statistics.fmean(durations),
            "p50": _percentile(durations, 0.50),
            "p95": p95,
            "maximum": max(durations),
        },
        "resource": {
            "max_resident_set_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "provisional_contract": {
            "p95_seconds": target_p95,
            "request_timeout_seconds": 45.0,
            "max_concurrent_requests": 5,
        },
        "error_count": len(errors),
        "errors": sorted(set(errors)),
        "status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--delay-ms", type=float, default=10.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/benchmarks/local-search-load.json"),
    )
    args = parser.parse_args()
    if args.requests < args.concurrency or args.concurrency < 1 or args.delay_ms < 0:
        parser.error("requests must cover positive concurrency and delay must be non-negative")
    report = asyncio.run(
        benchmark(
            requests=args.requests,
            concurrency=args.concurrency,
            delay_seconds=args.delay_ms / 1000.0,
        )
    )
    _atomic_json(args.output.resolve(), report)
    print(f"local load probe: {report['status']}; report={args.output}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
