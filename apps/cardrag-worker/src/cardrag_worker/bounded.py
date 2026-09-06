"""Finite, ordered asynchronous work with fair group admission and a drain barrier."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import cast


class _WorkerFailed(Exception):
    """Internal TaskGroup signal; callers receive the original operation error."""


async def bounded_ordered_map[T, R](
    items: Sequence[T],
    operation: Callable[[T, int], Awaitable[R]],
    *,
    concurrency: int,
    group_key: Callable[[T], str] | None = None,
    per_group: int | None = None,
) -> tuple[R, ...]:
    """Keep only ``concurrency`` worker tasks alive, returning after all work drains.

    Admission rotates among eligible groups instead of allowing a large issuer
    queue to occupy workers waiting for its own semaphore. Operations and their
    bookkeeping stay on the caller's event loop. The slot argument lets callers
    give each worker its own reusable, session-isolated network client.
    """

    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if per_group is None:
        per_group = concurrency
    if type(per_group) is not int or per_group < 1:
        raise ValueError("per-group concurrency must be a positive integer")
    if concurrency == 1:
        return tuple([await operation(item, 0) for item in items])
    pending: dict[str, deque[tuple[int, T]]] = {}
    for index, item in enumerate(items):
        key = "" if group_key is None else group_key(item)
        pending.setdefault(key, deque()).append((index, item))
    groups = deque(pending)
    active = dict.fromkeys(pending, 0)
    missing = object()
    results: list[R | object] = [missing] * len(items)
    condition = asyncio.Condition()
    failures: list[BaseException] = []
    owner = asyncio.current_task()

    async def worker(slot: int) -> None:
        while True:
            async with condition:
                while True:
                    if failures:
                        return
                    admitted: tuple[str, int, T] | None = None
                    for _ in range(len(groups)):
                        key = groups[0]
                        groups.rotate(-1)
                        if pending[key] and active[key] < per_group:
                            index, item = pending[key].popleft()
                            active[key] += 1
                            admitted = (key, index, item)
                            break
                    if admitted is not None:
                        break
                    if not any(pending.values()):
                        return
                    await condition.wait()
            key, index, item = admitted
            try:
                results[index] = await operation(item, slot)
            except asyncio.CancelledError:
                if failures or (owner is not None and owner.cancelling()):
                    raise
                # TaskGroup otherwise treats an operation's own cancellation as
                # successful task completion, leaving an incomplete result set.
                failures.append(asyncio.CancelledError())
                raise _WorkerFailed() from None
            except Exception as exc:
                failures.append(exc)
                raise _WorkerFailed() from None
            finally:
                async with condition:
                    active[key] -= 1
                    condition.notify_all()

    try:
        async with asyncio.TaskGroup() as tasks:
            for slot in range(min(concurrency, len(items))):
                tasks.create_task(worker(slot))
    except* _WorkerFailed:
        pass
    if failures:
        raise failures[0] from None
    if any(result is missing for result in results):  # pragma: no cover - admission invariant
        raise RuntimeError("bounded work completed without every input result")
    return cast(tuple[R, ...], tuple(results))
