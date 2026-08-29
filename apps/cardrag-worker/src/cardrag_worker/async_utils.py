"""Cancellation-safe bridges to finite blocking operations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def to_thread_fenced[**P, T](function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run blocking work without letting cancellation orphan its thread.

    ``asyncio.to_thread`` cannot stop a function that is already running.  A
    direct cancellation therefore releases coroutine-owned locks while the
    thread may still be mutating local or remote state.  Keep the thread task
    shielded, drain it after any number of cancellation requests, and only then
    propagate a fresh cancellation with no blocking-operation exception chain.
    """

    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation_requested = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if operation.done() and (current is None or current.cancelling() == 0):
                # The blocking callable itself raised CancelledError rather
                # than the coroutine receiving an external cancellation.
                break
            cancellation_requested = True
        except BaseException:
            # Retrieve and propagate the operation outcome below, outside the
            # active exception handler.
            break

    if cancellation_requested or operation.cancelled():
        # Access the outcome so a late blocking exception is never reported as
        # "Task exception was never retrieved".  It is deliberately discarded:
        # cancellation remains the public result and raw transport details must
        # not become its context.  A blocking callable can itself raise a
        # CancelledError with mutable args/notes/causes, so cancelled operation
        # tasks are normalized here too rather than returning that object.
        if not operation.cancelled():
            operation.exception()
        raise asyncio.CancelledError() from None
    return operation.result()
