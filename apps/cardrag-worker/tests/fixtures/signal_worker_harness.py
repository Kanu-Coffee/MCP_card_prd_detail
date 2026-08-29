"""Subprocess harness for the real Worker CLI signal boundary."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from cardrag_core import EMBEDDING_DIMENSION

import cardrag_worker.cli as cli
from cardrag_worker.async_utils import to_thread_fenced
from cardrag_worker.pipeline import WorkerPipeline
from cardrag_worker.state import WorkerState


class _Spec:
    code = "signal-test"
    minimum_interval_seconds = 0.0


class _Adapter:
    spec = _Spec()


class _Embeddings:
    dimension = EMBEDDING_DIMENSION


class _WebDAV:
    channel = "candidate-v1.0.9"


async def _blocked_run(resume: str | None) -> dict[str, Any]:
    state_dir = Path(os.environ["CARDRAG_SIGNAL_TEST_STATE_DIR"]).resolve()
    started = state_dir / "blocking-operation.started"
    release = state_dir / "blocking-operation.release"
    finished = state_dir / "blocking-operation.finished"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    with WorkerState(state_dir / "worker-state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=state_dir,
            adapters=[_Adapter()],  # type: ignore[list-item]
            ocr=object(),  # type: ignore[arg-type]
            embeddings=_Embeddings(),  # type: ignore[arg-type]
            webdav=_WebDAV(),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )

        async def block(run_id: str, *, refresh_sources: bool = False) -> Any:
            del refresh_sources

            def mutation() -> None:
                started.write_text(run_id, encoding="utf-8")
                deadline = time.monotonic() + 20
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("signal test release timed out")
                    time.sleep(0.01)
                finished.write_text(run_id, encoding="utf-8")

            await to_thread_fenced(mutation)
            raise RuntimeError("cancelled blocking operation unexpectedly returned")

        pipeline._run_locked = block  # type: ignore[method-assign]
        result = await pipeline.run(resume_run_id=resume)
        return cli._pipeline_result_payload(result)


cli._run = _blocked_run
cli.main()
