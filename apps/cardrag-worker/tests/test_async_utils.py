from __future__ import annotations

import asyncio
import threading
import traceback

import pytest

from cardrag_worker.async_utils import to_thread_fenced


async def _wait_until_set(event: threading.Event) -> None:
    for _ in range(1_000):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("blocking operation did not start")


@pytest.mark.asyncio
async def test_to_thread_fenced_drains_work_after_repeated_cancellation() -> None:
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def blocking_mutation() -> str:
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release blocking mutation")
        completed.set()
        return "committed"

    task = asyncio.create_task(to_thread_fenced(blocking_mutation))
    await _wait_until_set(started)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    assert not completed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert completed.is_set()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_to_thread_fenced_retrieves_late_failure_without_secret_cancellation_context() -> None:
    raw_sentinel = "RAW_BLOCKING_TRANSPORT_SECRET"
    started = threading.Event()
    release = threading.Event()

    def late_failure() -> None:
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release blocking operation")
        raise RuntimeError(raw_sentinel)

    task = asyncio.create_task(to_thread_fenced(late_failure))
    await _wait_until_set(started)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_sentinel not in rendered


@pytest.mark.asyncio
async def test_to_thread_fenced_preserves_uncancelled_operation_failure() -> None:
    failure = RuntimeError("ordinary blocking failure")

    def fail() -> None:
        raise failure

    with pytest.raises(RuntimeError) as captured:
        await to_thread_fenced(fail)

    assert captured.value is failure


@pytest.mark.asyncio
async def test_to_thread_fenced_normalizes_callable_cancelled_error() -> None:
    raw_cancel = "RAW_BLOCKING_CANCEL_SECRET"
    raw_cause = "RAW_BLOCKING_CANCEL_CAUSE_SECRET"

    def fail() -> None:
        raise asyncio.CancelledError(raw_cancel) from RuntimeError(raw_cause)

    with pytest.raises(asyncio.CancelledError) as captured:
        await to_thread_fenced(fail)

    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert captured.value.args == ()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_cancel not in rendered
    assert raw_cause not in rendered
