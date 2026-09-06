"""Bounded, non-contract diagnostics for a single Worker execution."""

from __future__ import annotations

import hashlib
import resource
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _io_counters() -> dict[str, int]:
    try:
        return {
            name: int(value)
            for line in Path("/proc/self/io").read_text().splitlines()
            for name, value in [line.split(":", 1)]
        }
    except (OSError, ValueError):
        return {}


class WorkerPerformance:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        self._seconds: dict[str, float] = {}
        self._io_start = _io_counters()
        self._started = time.monotonic()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0) + amount

    def set(self, name: str, value: object) -> None:
        with self._lock:
            self._values[name] = value

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            with self._lock:
                self._seconds[name] = self._seconds.get(name, 0.0) + time.monotonic() - started

    def snapshot(self) -> dict[str, Any]:
        current_io = _io_counters()
        with self._lock:
            return {
                "schema_version": "cardrag.worker-performance.v1",
                "elapsed_seconds": time.monotonic() - self._started,
                "metrics": dict(self._values),
                # Parallel task times can overlap; these are accumulated work
                # durations, not additive partitions of the wall clock.
                "accumulated_seconds": dict(self._seconds),
                "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                "process_io_delta": {
                    name: value - self._io_start[name]
                    for name, value in current_io.items()
                    if name in self._io_start
                },
            }


class ExactTokenMemo:
    """Reuse exact counts within one pinned profile and tokenizer execution.

    Only SHA-256 keys and integers are retained, never corpus text. Striped
    locks deduplicate equal inputs while allowing unrelated native tokenizer
    calls to run concurrently.
    """

    def __init__(self, counter: Callable[[str], int], performance: WorkerPerformance) -> None:
        self._counter = counter
        self._performance = performance
        self._locks = tuple(threading.Lock() for _ in range(32))
        self._counts: dict[bytes, int] = {}

    def __call__(self, text: str) -> int:
        key = hashlib.sha256(text.encode("utf-8")).digest()
        with self._locks[key[0] % len(self._locks)]:
            if key in self._counts:
                self._performance.increment("token_cache_hits")
                return self._counts[key]
            with self._performance.measure("token_count"):
                count = self._counter(text)
            self._performance.increment("token_counter_calls")
            if type(count) is not int or count < 1:
                raise ValueError("exact tokenizer returned an invalid count")
            self._counts[key] = count
            return count
