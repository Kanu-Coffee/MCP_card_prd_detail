from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from cardrag_worker.performance import ExactTokenMemo, WorkerPerformance


def test_exact_token_memo_deduplicates_threads_without_retaining_source_text() -> None:
    calls: list[str] = []
    lock = threading.Lock()

    def count(text: str) -> int:
        with lock:
            calls.append(text)
        return len(text.encode()) + 1

    performance = WorkerPerformance()
    memo = ExactTokenMemo(count, performance)
    values = ["동일한 정확 입력", "다른 입력"] * 100
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(memo, values))
    assert counts == [len(text.encode()) + 1 for text in values]
    assert sorted(calls) == sorted(set(values))
    assert performance.snapshot()["metrics"]["token_cache_hits"] == 198
    # A new profile/run owns another memo and must invoke its own counter.
    other = ExactTokenMemo(lambda _text: 123, WorkerPerformance())
    assert other(values[0]) == 123
