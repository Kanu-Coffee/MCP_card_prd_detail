"""Local operator CLI.  No command here is exposed through MCP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import uuid
from pathlib import Path
from typing import Annotated

import typer

from cardrag import __version__
from cardrag.config import Settings
from cardrag.db import Postgres
from cardrag.domain import Issuer
from cardrag.generation import GenerationStore
from cardrag.generation_builder import (
    GenerationBuilder,
    NoChangesDetected,
    QualityGateReport,
    RetrievalGateReport,
)
from cardrag.jobs import JobRepository
from cardrag.legacy import LegacyMigrator
from cardrag.observability import prune_database_retention
from cardrag.scheduler import DailyScheduler
from cardrag.storage import ContentAddressedObjectStore

app = typer.Typer(help="CardRAG offline administration", no_args_is_help=True)
db_app = typer.Typer(help="Schema migration and database diagnostics")
job_app = typer.Typer(help="Durable job lifecycle")
run_app = typer.Typer(help="BULK and daily pipeline runs")
generation_app = typer.Typer(help="Immutable generation lifecycle")
legacy_app = typer.Typer(help="Read-only legacy reuse pilot")
retention_app = typer.Typer(help="Owner-only data retention")
app.add_typer(db_app, name="db")
app.add_typer(job_app, name="job")
app.add_typer(run_app, name="run")
app.add_typer(generation_app, name="generation")
app.add_typer(legacy_app, name="legacy")
app.add_typer(retention_app, name="retention")

RUN_STATES = frozenset({"queued", "running", "paused", "succeeded", "failed", "cancelled"})


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _database(settings: Settings) -> Postgres:
    database = Postgres(settings.database_url.get_secret_value())
    database.open()
    return database


def _publish_completed_run(
    settings: Settings,
    database: Postgres,
    run_id: uuid.UUID,
    generation_id: str,
) -> tuple[str, str]:
    store = GenerationStore(
        settings.generation_root,
        settings.build_root / "generation-candidates",
    )
    builder = GenerationBuilder(database, store)
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM generations WHERE generation_id=%s", (generation_id,))
        row = cursor.fetchone()
    if row is None:
        raise ValueError("run generation does not exist")
    generation_state = str(row["state"])
    if generation_state == "published":
        return "succeeded", "already_published"
    if generation_state == "ready":
        builder.publish(generation_id)
        return "succeeded", "published"
    if generation_state == "failed":
        return "succeeded", "skipped_or_failed"
    try:
        if builder.skip_if_unchanged(
            generation_id,
            embedding_provider="openrouter",
            embedding_model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        ):
            return "succeeded", "no_change"
        quality_report, retrieval_report = builder.evaluate(
            generation_id,
            output_dir=settings.build_root / "validation-reports" / generation_id,
        )
        builder.seal(
            generation_id,
            embedding_provider="openrouter",
            embedding_model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            quality_report=quality_report,
            retrieval_report=retrieval_report,
        )
        builder.publish(generation_id)
        return "succeeded", "published"
    except NoChangesDetected:
        return "succeeded", "no_change"
    except Exception as exc:
        with database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pipeline_runs SET state='failed',
                    report=report || jsonb_build_object(
                        'generation_validation', 'failed',
                        'generation_reason', %s::text
                    ) WHERE run_id=%s
                """,
                (type(exc).__name__, run_id),
            )
            DailyScheduler._fail_candidate(cursor, run_id, reason=type(exc).__name__)
            connection.commit()
        return "failed", "validation_failed"


@app.command()
def version() -> None:
    typer.echo(__version__)


@db_app.command("migrate")
def migrate() -> None:
    settings = _settings()
    database = _database(settings)
    try:
        typer.echo(json.dumps({"applied": database.migrate()}))
    finally:
        database.close()


@db_app.command("ping")
def ping() -> None:
    settings = _settings()
    database = _database(settings)
    try:
        typer.echo(json.dumps({"ok": database.ping()}))
    finally:
        database.close()


@job_app.command("status")
def job_status() -> None:
    settings = _settings()
    database = _database(settings)
    try:
        typer.echo(json.dumps(JobRepository(database).progress(), sort_keys=True))
    finally:
        database.close()


@job_app.command("reclaim")
def reclaim() -> None:
    settings = _settings()
    database = _database(settings)
    try:
        typer.echo(json.dumps({"reclaimed": JobRepository(database).reclaim_expired()}))
    finally:
        database.close()


@job_app.command("cancel")
def cancel(job_id: uuid.UUID) -> None:
    settings = _settings()
    database = _database(settings)
    try:
        state = JobRepository(database).cancel(job_id)
        typer.echo(json.dumps({"job_id": str(job_id), "state": state.value}))
    finally:
        database.close()


@job_app.command("redrive")
def redrive(job_id: uuid.UUID, max_attempts: int | None = None) -> None:
    settings = _settings()
    database = _database(settings)
    try:
        JobRepository(database).redrive(job_id, max_attempts=max_attempts)
        typer.echo(json.dumps({"job_id": str(job_id), "state": "queued"}))
    finally:
        database.close()


async def _start_run(run_type: str, bulk: bool, wait: bool, delay: float) -> dict[str, object]:
    settings = _settings()
    database = _database(settings)
    try:
        jobs = JobRepository(database)
        scheduler = DailyScheduler(database, jobs)
        owner_id: str | None = None
        lease_token: int | None = None
        keeper: asyncio.Task[None] | None = None
        if run_type == "daily":
            owner_id = f"{socket.gethostname()}:{os.getpid()}"
            lease_token = scheduler.acquire(owner_id)
            if lease_token is None:
                raise RuntimeError("another daily scheduler holds the active lease")
            keeper = asyncio.create_task(_keep_scheduler_lease(scheduler, owner_id, lease_token))
        run_id, generation_id = scheduler.create_run(
            run_type=run_type,
            bulk=bulk,
            embedding_provider="openrouter",
            embedding_model=settings.embedding_model,
            embedding_dimension=settings.embedding_dimension,
            ocr_model=settings.ocr_model,
            ocr_reasoning_effort=settings.ocr_reasoning_effort,
            ocr_fallback_model=settings.ocr_fallback_model,
            render_scale=settings.render_scale,
            ocr_chunk_pages=settings.ocr_chunk_pages,
        )
        try:
            if wait:
                sequence = asyncio.create_task(
                    scheduler.enqueue_sequence(
                        run_id,
                        generation_id,
                        bulk=bulk,
                        inter_issuer_seconds=delay,
                    )
                )
                if keeper is not None:
                    done, _ = await asyncio.wait({sequence, keeper}, return_when=asyncio.FIRST_COMPLETED)
                    lease_error = keeper.exception() if keeper in done else None
                    if lease_error is not None:
                        sequence.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await sequence
                        raise lease_error
                job_ids = await sequence
                run_state = scheduler.finish_run(run_id)
                publication_state = "not_attempted"
                if run_state == "succeeded":
                    run_state, publication_state = _publish_completed_run(
                        settings,
                        database,
                        run_id,
                        generation_id,
                    )
            else:
                # Enqueue all immediately for operator-driven/test execution; the
                # scheduled command below enforces the production 10-minute gaps.
                async def no_wait(_: uuid.UUID, __: Issuer) -> None:
                    return None

                job_ids = await scheduler.enqueue_sequence(
                    run_id,
                    generation_id,
                    bulk=bulk,
                    inter_issuer_seconds=0,
                    wait_for_completion=no_wait,
                )
                run_state = "running"
                publication_state = "operator_seal_required"
            return {
                "run_id": str(run_id),
                "generation_id": generation_id,
                "state": run_state,
                "publication": publication_state,
                "job_ids": list(map(str, job_ids)),
            }
        finally:
            if keeper is not None:
                keeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keeper
            if owner_id is not None and lease_token is not None:
                scheduler.release(owner_id, lease_token)
    finally:
        database.close()


async def _keep_scheduler_lease(
    scheduler: DailyScheduler,
    owner_id: str,
    fencing_token: int,
    *,
    lease_seconds: int = 3600,
) -> None:
    while True:
        await asyncio.sleep(lease_seconds / 3)
        if not scheduler.renew(owner_id, fencing_token, lease_seconds=lease_seconds):
            raise RuntimeError("daily scheduler lease was lost")


@run_app.command("bulk")
def bulk(wait: bool = typer.Option(True, help="wait for each issuer to finish")) -> None:
    typer.echo(json.dumps(asyncio.run(_start_run("bulk", True, wait, 600.0)), sort_keys=True))


@run_app.command("daily")
def daily(wait: bool = typer.Option(True, help="enforce issuer completion and 10-minute gaps")) -> None:
    typer.echo(json.dumps(asyncio.run(_start_run("daily", False, wait, 600.0)), sort_keys=True))


async def _finalize_existing_run(run_id: uuid.UUID) -> dict[str, str]:
    settings = _settings()
    database = _database(settings)
    try:
        scheduler = DailyScheduler(database, JobRepository(database))
        with database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, generation_id FROM pipeline_runs WHERE run_id=%s",
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise ValueError("pipeline run does not exist")
        state = str(row["state"])
        generation_id = str(row["generation_id"])
        if state == "paused":
            raise ValueError("resume the run before finalizing it")
        if state == "running":
            await scheduler.wait_existing_run(run_id)
            state = scheduler.finish_run(run_id)
        publication = "not_attempted"
        if state == "succeeded":
            state, publication = _publish_completed_run(
                settings,
                database,
                run_id,
                generation_id,
            )
        return {
            "run_id": str(run_id),
            "generation_id": generation_id,
            "state": state,
            "publication": publication,
        }
    finally:
        database.close()


@run_app.command("finalize")
def run_finalize(run_id: uuid.UUID) -> None:
    """Resume supervision and publish an already-enqueued durable run."""

    typer.echo(json.dumps(asyncio.run(_finalize_existing_run(run_id)), sort_keys=True))


def _run_control(run_id: uuid.UUID, action: str) -> None:
    settings = _settings()
    database = _database(settings)
    try:
        state = DailyScheduler(database, JobRepository(database)).set_run_control(run_id, action)
        typer.echo(json.dumps({"run_id": str(run_id), "state": state}, sort_keys=True))
    finally:
        database.close()


@run_app.command("pause")
def run_pause(run_id: uuid.UUID) -> None:
    _run_control(run_id, "pause")


@run_app.command("resume")
def run_resume(run_id: uuid.UUID) -> None:
    _run_control(run_id, "resume")


@run_app.command("cancel")
def run_cancel(run_id: uuid.UUID) -> None:
    _run_control(run_id, "cancel")


@run_app.command("status")
def run_status(run_id: uuid.UUID) -> None:
    settings = _settings()
    database = _database(settings)
    try:
        with database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.*, COALESCE(jsonb_agg(jsonb_build_object(
                    'issuer', s.issuer, 'state', s.state, 'discovered', s.discovered_count,
                    'succeeded', s.succeeded_count, 'failed', s.failed_count,
                    'started_at', s.started_at, 'finished_at', s.finished_at
                ) ORDER BY s.sequence_no) FILTER (WHERE s.issuer IS NOT NULL), '[]'::jsonb) AS issuers
                FROM pipeline_runs r LEFT JOIN run_issuer_status s USING (run_id)
                WHERE r.run_id=%s GROUP BY r.run_id
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise typer.BadParameter("run was not found")
        typer.echo(json.dumps(dict(row), default=str, ensure_ascii=False, sort_keys=True))
    finally:
        database.close()


def _list_runs(database: Postgres, *, state: str | None, limit: int) -> list[dict[str, object]]:
    """Return recent durable runs so an interrupted supervisor is discoverable."""

    if state is not None and state not in RUN_STATES:
        raise ValueError("invalid run state")
    if not 1 <= limit <= 100:
        raise ValueError("run list limit must be between 1 and 100")
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT r.run_id, r.run_type, r.state, r.generation_id, r.started_at,
                   r.finished_at, r.created_at,
                   COALESCE(jsonb_agg(jsonb_build_object(
                       'issuer', s.issuer, 'state', s.state,
                       'discovered', s.discovered_count,
                       'succeeded', s.succeeded_count, 'failed', s.failed_count
                   ) ORDER BY s.sequence_no) FILTER (WHERE s.issuer IS NOT NULL),
                   '[]'::jsonb) AS issuers
            FROM pipeline_runs r LEFT JOIN run_issuer_status s USING (run_id)
            WHERE (%s::text IS NULL OR r.state=%s)
            GROUP BY r.run_id
            ORDER BY r.created_at DESC, r.run_id DESC
            LIMIT %s
            """,
            (state, state, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


@run_app.command("list")
def run_list(
    state: Annotated[
        str | None,
        typer.Option(help="filter by queued/running/paused/succeeded/failed/cancelled"),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=100, help="maximum recent runs")] = 20,
) -> None:
    """List durable runs, including IDs needed after a supervisor crash."""

    if state is not None and state not in RUN_STATES:
        allowed = ", ".join(sorted(RUN_STATES))
        raise typer.BadParameter(f"state must be one of: {allowed}", param_hint="--state")
    settings = _settings()
    database = _database(settings)
    try:
        runs = _list_runs(database, state=state, limit=limit)
        typer.echo(json.dumps({"runs": runs}, default=str, ensure_ascii=False, sort_keys=True))
    finally:
        database.close()


def _generation_components() -> tuple[Settings, Postgres, GenerationStore, GenerationBuilder]:
    settings = _settings()
    database = _database(settings)
    store = GenerationStore(settings.generation_root, settings.build_root / "generation-candidates")
    return settings, database, store, GenerationBuilder(database, store)


@generation_app.command("seal")
def generation_seal(
    generation_id: str,
    quality_report: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    retrieval_report: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    settings, database, _, builder = _generation_components()
    try:
        path = builder.seal(
            generation_id,
            embedding_provider="openrouter",
            embedding_model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            quality_report=QualityGateReport.model_validate_json(quality_report.read_bytes()),
            retrieval_report=RetrievalGateReport.model_validate_json(retrieval_report.read_bytes()),
        )
        typer.echo(json.dumps({"generation_id": generation_id, "sealed": str(path)}))
    finally:
        database.close()


@generation_app.command("verify")
def generation_verify(generation_id: str) -> None:
    _, database, store, _ = _generation_components()
    try:
        manifest = store.verify_path(store.generations / generation_id, expected_generation_id=generation_id)
        typer.echo(json.dumps({"generation_id": generation_id, "manifest_sha256": manifest.sha256}))
    finally:
        database.close()


@generation_app.command("publish")
def generation_publish(generation_id: str) -> None:
    _, database, _, builder = _generation_components()
    try:
        builder.publish(generation_id)
        typer.echo(json.dumps({"published": generation_id}))
    finally:
        database.close()


@generation_app.command("rollback")
def generation_rollback(generation_id: str | None = None) -> None:
    _, database, _, builder = _generation_components()
    try:
        typer.echo(json.dumps({"published": builder.rollback(generation_id)}))
    finally:
        database.close()


@generation_app.command("pin")
def generation_pin(generation_id: str, reason: str) -> None:
    _, database, _, _ = _generation_components()
    try:
        with database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO generation_pins(generation_id, reason) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (generation_id, reason),
            )
            connection.commit()
        typer.echo(json.dumps({"pinned": generation_id}))
    finally:
        database.close()


@generation_app.command("unpin")
def generation_unpin(generation_id: str) -> None:
    _, database, _, _ = _generation_components()
    try:
        with database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM generation_pins WHERE generation_id=%s", (generation_id,))
            connection.commit()
        typer.echo(json.dumps({"unpinned": generation_id}))
    finally:
        database.close()


@generation_app.command("prune")
def generation_prune() -> None:
    _, database, _, builder = _generation_components()
    try:
        typer.echo(json.dumps({"removed": builder.prune()}))
    finally:
        database.close()


@legacy_app.command("inventory")
def legacy_inventory(
    source: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    output: Annotated[Path, typer.Option()],
) -> None:
    settings = _settings()
    migrator = LegacyMigrator(source, ContentAddressedObjectStore(settings.storage_root))
    inventory = migrator.inventory()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(inventory.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    typer.echo(json.dumps({"files": len(inventory.files), "output": str(output)}))


@legacy_app.command("pilot")
def legacy_pilot(
    source: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    manifest: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    limit: int = 20,
    dry_run: bool = True,
    pilot_id: str | None = None,
) -> None:
    settings = _settings()
    settings.build_root.mkdir(parents=True, exist_ok=True)
    pilot_root = LegacyMigrator.create_pilot_root(settings.build_root, pilot_id)
    migrator = LegacyMigrator(source, ContentAddressedObjectStore(pilot_root / "objects"))
    before = migrator.snapshot_source_metadata()
    report = migrator.migrate_manifest(manifest, limit=limit, dry_run=dry_run)
    after = migrator.snapshot_source_metadata()
    if before != after:
        raise typer.Exit(code=2)
    report_path = pilot_root / "migration-report.json"
    report_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    typer.echo(
        json.dumps(
            {**report.model_dump(mode="json"), "pilot_root": str(pilot_root)},
            ensure_ascii=False,
            indent=2,
        )
    )


@legacy_app.command("rollback")
def legacy_rollback(
    pilot_root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
) -> None:
    settings = _settings()
    removed = LegacyMigrator.rollback_pilot(settings.build_root, pilot_root)
    typer.echo(json.dumps({"rolled_back": removed}))


@retention_app.command("prune")
def retention_prune() -> None:
    """Apply all owner-only database and generation retention policies."""

    settings = _settings()
    database = _database(settings)
    try:
        store = GenerationStore(
            settings.generation_root,
            settings.build_root / "generation-candidates",
        )
        generations = GenerationBuilder(database, store).prune()
        audits, rollups = prune_database_retention(database)
        typer.echo(
            json.dumps(
                {
                    "audit_rows_removed": audits,
                    "generation_ids_removed": generations,
                    "metric_rows_removed": rollups,
                },
                sort_keys=True,
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    app()
