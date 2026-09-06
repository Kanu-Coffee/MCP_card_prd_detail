from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from cardrag_worker.bounded import bounded_ordered_map


async def test_bounded_map_admits_issuers_fairly_and_preserves_input_order() -> None:
    items = tuple((group, index) for group in "abcd" for index in range(5))
    release = asyncio.Event()
    started = asyncio.Event()
    admission: list[str] = []
    active: Counter[str] = Counter()
    peaks: Counter[str] = Counter()

    async def operation(item: tuple[str, int], _slot: int) -> tuple[str, int]:
        group, _index = item
        active[group] += 1
        peaks[group] = max(peaks[group], active[group])
        admission.append(group)
        if len(admission) == 4:
            started.set()
        try:
            await release.wait()
            await asyncio.sleep(0)
            return item
        finally:
            active[group] -= 1

    work = asyncio.create_task(
        bounded_ordered_map(items, operation, concurrency=4, group_key=lambda item: item[0], per_group=2)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    assert admission == list("abcd")
    assert not work.done()
    release.set()
    assert await work == items
    assert all(peak <= 2 for peak in peaks.values())
    assert sum(active.values()) == 0


async def test_bounded_map_waits_for_slow_input_after_fast_inputs_complete() -> None:
    slow = asyncio.Event()
    fast = asyncio.Event()
    completed: list[int] = []

    async def operation(item: int, _slot: int) -> int:
        if item == 0:
            await slow.wait()
        completed.append(item)
        if len(completed) == 2:
            fast.set()
        return item

    work = asyncio.create_task(bounded_ordered_map((0, 1, 2), operation, concurrency=2))
    await asyncio.wait_for(fast.wait(), timeout=2)
    assert completed == [1, 2]
    assert not work.done()
    slow.set()
    assert await work == (0, 1, 2)


@pytest.mark.parametrize("failure_kind", ["error", "raise_cancel", "self_cancel"])
async def test_bounded_map_failure_cancels_and_drains_siblings_with_original_type(
    failure_kind: str,
) -> None:
    sibling_started = asyncio.Event()
    sibling_drained = asyncio.Event()

    class DocumentFailure(ValueError):
        pass

    error = DocumentFailure("document failed")

    async def operation(item: int, _slot: int) -> int:
        if item == 0:
            await sibling_started.wait()
            if failure_kind == "raise_cancel":
                raise asyncio.CancelledError
            if failure_kind == "self_cancel":
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)
            raise error
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_drained.set()
        return item

    expected = DocumentFailure if failure_kind == "error" else asyncio.CancelledError
    with pytest.raises(expected) as captured:
        await bounded_ordered_map((0, 1), operation, concurrency=2)
    assert sibling_drained.is_set()
    if failure_kind == "error":
        assert captured.value is error


async def test_bounded_map_external_cancellation_drains_all_workers() -> None:
    started = asyncio.Event()
    active = 0
    drained = 0

    async def operation(item: int, _slot: int) -> int:
        nonlocal active, drained
        active += 1
        if active == 3:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
            drained += 1
        return item

    work = asyncio.create_task(bounded_ordered_map(tuple(range(10)), operation, concurrency=3))
    await asyncio.wait_for(started.wait(), timeout=2)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert (active, drained) == (0, 3)


async def test_bounded_map_sequential_rollback_uses_input_order_and_one_slot() -> None:
    calls: list[tuple[int, int]] = []

    async def operation(item: int, slot: int) -> int:
        calls.append((item, slot))
        return item

    assert await bounded_ordered_map((3, 1, 2), operation, concurrency=1) == (3, 1, 2)
    assert calls == [(3, 0), (1, 0), (2, 0)]
