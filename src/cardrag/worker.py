"""Offline worker executable."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket

from prometheus_client import start_http_server

from cardrag.config import Settings
from cardrag.db import Postgres
from cardrag.jobs import JobRepository
from cardrag.observability import (
    PostgresMetricRollupWriter,
    WorkerMaintenance,
    configure_logging,
    get_observability,
)
from cardrag.pipeline.runtime import OfflinePipeline, WorkerLoop


async def run_worker(*, once: bool = False) -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(
        service="worker",
        environment=settings.environment,
        application_version=settings.application_version,
        image_revision=settings.image_revision,
    )
    database = Postgres(settings.database_url_value(), min_size=1, max_size=4)
    database.open()
    jobs = JobRepository(database)
    pipeline = OfflinePipeline(settings, database, jobs)
    observability = get_observability(service="worker", environment=settings.environment)
    rollups = PostgresMetricRollupWriter(database, observability.metrics)
    metrics_server = None
    if settings.worker_metrics_enabled:
        # Compose binds this only to the private backend network; the safer
        # standalone default remains loopback.
        metrics_server, _ = start_http_server(
            settings.worker_metrics_port,
            addr=settings.worker_metrics_host,
            registry=observability.metrics.registry,
        )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    loop = WorkerLoop(
        jobs,
        pipeline,
        worker_id=worker_id,
        lease_seconds=settings.worker_lease_seconds,
        observability=observability,
        rollups=rollups,
        maintenance=WorkerMaintenance(rollups),
    )
    event_loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        event_loop.add_signal_handler(signum, setattr, loop, "stopping", True)
    try:
        await loop.run(once=once)
    finally:
        if metrics_server is not None:
            metrics_server.shutdown()
            metrics_server.server_close()
        database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CardRAG durable offline worker")
    parser.add_argument("--once", action="store_true", help="claim at most one job and exit")
    args = parser.parse_args()
    asyncio.run(run_worker(once=args.once))


if __name__ == "__main__":
    main()
