"""Bounded, generation-scoped cache of serialized metadata, never pinned handles."""

from __future__ import annotations

import json
from collections import OrderedDict
from threading import Lock
from typing import Any


class MetadataCache:
    """Thread-safe LRU containing JSON values, with byte and entry limits."""

    def __init__(self, *, max_bytes: int = 16 * 1024 * 1024, max_entries: int = 1024) -> None:
        if max_bytes < 1 or max_entries < 1:
            raise ValueError("cache limits must be positive")
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, ...], bytes] = OrderedDict()
        self._bytes = 0
        self._hits = self._misses = self._evictions = 0
        self._lock = Lock()

    def get(self, key: tuple[str, ...]) -> Any | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return json.loads(value)

    def set(self, key: tuple[str, ...], value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        size = len(encoded) + sum(len(part.encode()) for part in key)
        if size > self.max_bytes:
            return
        with self._lock:
            prior = self._entries.pop(key, None)
            if prior is not None:
                self._bytes -= len(prior) + sum(len(part.encode()) for part in key)
            self._entries[key] = encoded
            self._bytes += size
            while self._bytes > self.max_bytes or len(self._entries) > self.max_entries:
                evicted_key, evicted_value = self._entries.popitem(last=False)
                self._bytes -= len(evicted_value) + sum(len(part.encode()) for part in evicted_key)
                self._evictions += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "entries": len(self._entries),
                "bytes": self._bytes,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
            }
