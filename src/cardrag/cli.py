"""Local operator CLI.  No command here is exposed through MCP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
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
from cardrag.legacy import (
    LegacyBundlePreparer,
    LegacyImportService,
    LegacyMigrator,
    export_adoption_ledger,
    export_current_inventory,
    export_data_kit_inventory,
    verify_bundle,
    write_adoption_ledger,
    write_current_inventory,
    write_data_kit_inventory,
)
from cardrag.observability import prune_database_retention
from cardrag.scheduler import DailyScheduler
from cardrag.state_transfer import (
    ExportRequest,
    PackageVerification,
    PortableStateService,
    PostgresStateInspector,
    PostgresToolConfig,
    PostgresToolRunner,
    RestoreRequest,
    RolePasswordSecret,
    RuntimeCompatibility,
    StateProgress,
    deployment_release_contract,
    validate_archive_mount_identity,
    verify_state_package_with_progress,
)
from cardrag.storage import ContentAddressedObjectStore
from cardrag.storage_admission import enforce_storage_admission

app = typer.Typer(help="CardRAG offline administration", no_args_is_help=True)
db_app = typer.Typer(help="Schema migration and database diagnostics")
job_app = typer.Typer(help="Durable job lifecycle")
run_app = typer.Typer(help="BULK and daily pipeline runs")
generation_app = typer.Typer(help="Immutable generation lifecycle")
legacy_app = typer.Typer(help="Portable legacy bundle preparation and import")
state_app = typer.Typer(help="Portable runtime state export and restore")
retention_app = typer.Typer(help="Owner-only data retention")
app.add_typer(db_app, name="db")
app.add_typer(job_app, name="job")
app.add_typer(run_app, name="run")
app.add_typer(generation_app, name="generation")
app.add_typer(legacy_app, name="legacy")
app.add_typer(state_app, name="state")
app.add_typer(retention_app, name="retention")

RUN_STATES = frozenset({"queued", "running", "paused", "succeeded", "failed", "cancelled"})
_LEGACY_BUNDLE_ID_RE = re.compile(r"^bundle-[0-9a-f]{12}$")


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _database(settings: Settings) -> Postgres:
    database = Postgres(settings.database_url_value())
    database.open()
    return database


def _environment_path(name: str, default: str | None = None) -> Path:
    raw = os.environ.get(name, default)
    if raw is None or not raw.strip():
        raise typer.BadParameter(f"{name} must be configured")
    path = Path(raw)
    if not path.is_absolute():
        raise typer.BadParameter(f"{name} must be an absolute path")
    return path


def _validated_cli_archive_root(path: Path) -> Path:
    expected_source = os.environ.get("CARDRAG_ARCHIVE_EXPECTED_SOURCE", "")
    if not expected_source:
        raise typer.BadParameter("CARDRAG_ARCHIVE_EXPECTED_SOURCE must be configured")
    try:
        return validate_archive_mount_identity(path, expected_source)
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc


def _postgres_tool_config() -> PostgresToolConfig:
    try:
        port = int(os.environ.get("CARDRAG_POSTGRES_ADMIN_PORT", "5432"))
    except ValueError as exc:
        raise typer.BadParameter("CARDRAG_POSTGRES_ADMIN_PORT must be an integer") from exc
    return PostgresToolConfig(
        host=os.environ.get("CARDRAG_POSTGRES_ADMIN_HOST", "postgres"),
        port=port,
        user=os.environ.get("CARDRAG_POSTGRES_ADMIN_USER", "postgres"),
        password_file=_environment_path("CARDRAG_POSTGRES_ADMIN_PASSWORD_FILE"),
        cardrag_database=os.environ.get("CARDRAG_POSTGRES_CARDRAG_DATABASE", "cardrag"),
        keycloak_database=os.environ.get("CARDRAG_POSTGRES_KEYCLOAK_DATABASE", "keycloak"),
        pg_dump_bin=os.environ.get("CARDRAG_PG_DUMP_BIN", "pg_dump"),
        pg_restore_bin=os.environ.get("CARDRAG_PG_RESTORE_BIN", "pg_restore"),
        psql_bin=os.environ.get("CARDRAG_PSQL_BIN", "psql"),
    )


def _runtime_compatibility(settings: Settings, deployment_root: Path) -> RuntimeCompatibility:
    if settings.application_version != __version__:
        raise typer.BadParameter("running image version does not match the application package")
    if re.fullmatch(r"[0-9a-f]{40}", settings.image_revision) is None:
        raise typer.BadParameter("running image revision must be an exact Git commit SHA")
    try:
        images, release_version, release_revision = deployment_release_contract(deployment_root)
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc
    if release_version != settings.application_version:
        raise typer.BadParameter("running image version differs from signed release metadata")
    if release_revision != settings.image_revision:
        raise typer.BadParameter("running image revision differs from signed release metadata")
    return RuntimeCompatibility(
        application_version=settings.application_version,
        image_revision=settings.image_revision,
        embedding_provider="openrouter",
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
        image_digests=images,
    )


def _role_password_secrets() -> tuple[RolePasswordSecret, ...]:
    return (
        RolePasswordSecret("cardrag", _environment_path("CARDRAG_DB_PASSWORD_FILE")),
        RolePasswordSecret("cardrag_worker", _environment_path("CARDRAG_WORKER_DB_PASSWORD_FILE")),
        RolePasswordSecret("cardrag_mcp", _environment_path("CARDRAG_MCP_DB_PASSWORD_FILE")),
        RolePasswordSecret("keycloak", _environment_path("KEYCLOAK_DB_PASSWORD_FILE")),
    )


def _emit_state_progress(progress: StateProgress) -> None:
    typer.echo(json.dumps(progress.model_dump(mode="json"), sort_keys=True), err=True)


def _state_result_payload(verification: PackageVerification) -> dict[str, object]:
    # PackageVerification is deliberately rendered without its absolute host path.
    return {
        "export_id": verification.manifest.export_id,
        "package": verification.package_path.name,
        "files": verification.checked_files,
        "bytes": verification.checked_bytes,
        "status": verification.status,
    }


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
    if bulk:
        enforce_storage_admission(
            (settings.storage_root, settings.generation_root, settings.build_root),
            phase="preflight",
            warning=lambda message: typer.echo(message, err=True),
        )
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
            result: dict[str, object] = {
                "run_id": str(run_id),
                "generation_id": generation_id,
                "state": run_state,
                "publication": publication_state,
                "job_ids": list(map(str, job_ids)),
            }
            if bulk and run_state != "running":
                enforce_storage_admission(
                    (settings.storage_root, settings.generation_root, settings.build_root),
                    phase="postflight",
                    warning=lambda message: typer.echo(message, err=True),
                )
            return result
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
    enforce_storage_admission(
        (settings.storage_root, settings.build_root),
        phase="preflight",
        warning=lambda message: typer.echo(message, err=True),
    )
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


@generation_app.command("deactivate")
def generation_deactivate(
    expected_generation_id: Annotated[
        str,
        typer.Option("--expected", help="currently published generation ID"),
    ],
) -> None:
    """Deactivate the first generation when no rollback target exists."""

    _, database, _, builder = _generation_components()
    try:
        typer.echo(json.dumps({"deactivated": builder.deactivate(expected_generation_id)}))
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


@legacy_app.command("export-current-inventory")
def legacy_export_current_inventory(
    output: Annotated[
        Path,
        typer.Option(
            dir_okay=False,
            resolve_path=True,
            help="new JSONL path outside CARDRAG_STORAGE_ROOT",
        ),
    ],
) -> None:
    """Export the published latest PDF/OCR ledger without modifying v0.2.1 state."""

    settings = _settings()
    database = _database(settings)
    try:
        rows = export_current_inventory(database, settings.storage_root)
        written = write_current_inventory(rows, output, protected_root=settings.storage_root)
        typer.echo(
            json.dumps(
                {"documents": len(rows), "output": str(written)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        database.close()


@legacy_app.command("export-adoption-ledger")
def legacy_export_adoption_ledger(
    output: Annotated[
        Path,
        typer.Option(
            dir_okay=False,
            help="new absolute JSONL path outside CARDRAG_STORAGE_ROOT",
        ),
    ],
    import_id: Annotated[
        uuid.UUID | None,
        typer.Option(
            "--import-id",
            help="required when more than one succeeded legacy import exists",
        ),
    ] = None,
) -> None:
    """Export the DB-bound succeeded legacy-adoption ledger without mutation."""

    settings = _settings()
    database = _database(settings)
    try:
        rows = export_adoption_ledger(database, import_id=import_id)
        written = write_adoption_ledger(
            rows,
            output,
            protected_root=settings.storage_root,
        )
        typer.echo(
            json.dumps(
                {
                    "documents": len(rows),
                    "import_id": rows[0]["import_id"],
                    "output": str(written),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        database.close()


@legacy_app.command("export-data-kit-inventory")
def legacy_export_data_kit_inventory(
    source: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="absolute read-only cardrag-conveyor-data directory",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            dir_okay=False,
            help="new absolute Worker-compatible JSONL path outside the data-kit",
        ),
    ],
    rejected_output: Annotated[
        Path | None,
        typer.Option(
            "--rejected-output",
            dir_okay=False,
            help="new absolute rejection JSONL; defaults to OUTPUT.rejected.jsonl",
        ),
    ] = None,
) -> None:
    """Validate a raw data-kit read-only and export reusable latest OCR rows."""

    result = export_data_kit_inventory(source.absolute())
    inventory_path, rejected_path = write_data_kit_inventory(
        result,
        output,
        source_root=source.absolute(),
        rejected_output=rejected_output,
    )
    typer.echo(
        json.dumps(
            {
                "accepted": len(result.rows),
                "output": str(inventory_path),
                "rejected": len(result.rejected),
                "rejected_output": str(rejected_path),
                "selected": result.selected_documents,
                "source_bundle_id": result.source_bundle_id,
                "source_bundle_sha256": result.source_bundle_sha256,
                "source_database_id": result.source_database_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@legacy_app.command("prepare")
def legacy_prepare(
    source: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    manifest: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    output: Annotated[Path, typer.Option(file_okay=False, resolve_path=True)],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Create a deterministic immutable bundle outside the legacy source tree."""

    result = LegacyBundlePreparer(source).prepare(manifest, output, dry_run=dry_run)
    typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))


def _legacy_import_service(settings: Settings, database: Postgres) -> LegacyImportService:
    jobs = JobRepository(database)
    return LegacyImportService(
        database,
        jobs,
        ContentAddressedObjectStore(settings.storage_root),
        DailyScheduler(database, jobs),
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
        ocr_model=settings.ocr_model,
        ocr_reasoning_effort=settings.ocr_reasoning_effort,
        ocr_fallback_model=settings.ocr_fallback_model,
        render_scale=settings.render_scale,
        ocr_chunk_pages=settings.ocr_chunk_pages,
        max_job_attempts=settings.max_job_attempts,
    )


@legacy_app.command("verify")
def legacy_verify(
    bundle: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
) -> None:
    typer.echo(json.dumps(verify_bundle(bundle).model_dump(mode="json"), sort_keys=True))


@legacy_app.command("import")
def legacy_import(
    bundle: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = False,
    no_publish: Annotated[bool, typer.Option("--no-publish")] = True,
) -> None:
    """Seed a candidate; publication always requires an explicit finalize."""

    settings = _settings()
    enforce_storage_admission(
        (settings.storage_root, settings.build_root),
        phase="preflight",
        warning=lambda message: typer.echo(message, err=True),
    )
    database = _database(settings)
    try:
        service = _legacy_import_service(settings, database)
        emit = lambda item: typer.echo(  # noqa: E731 - tiny immediate flush callback
            json.dumps(item.model_dump(mode="json"), sort_keys=True), err=True
        )
        status = service.start(
            bundle,
            no_publish=no_publish,
            created=emit,
            progress=emit,
        )
        if wait:
            status = service.wait(
                status.import_id,
                progress=emit,
            )
        if status.state in {"ready_to_finalize", "succeeded", "failed", "cancelled"}:
            enforce_storage_admission(
                (settings.storage_root, settings.build_root),
                phase="postflight",
                warning=lambda message: typer.echo(message, err=True),
            )
        typer.echo(json.dumps(status.model_dump(mode="json"), sort_keys=True))
    finally:
        database.close()


@legacy_app.command("status")
def legacy_status(import_id: uuid.UUID) -> None:
    settings = _settings()
    database = _database(settings)
    try:
        status = _legacy_import_service(settings, database).refresh(import_id)
        typer.echo(json.dumps(status.model_dump(mode="json"), sort_keys=True))
    finally:
        database.close()


@legacy_app.command("resume")
def legacy_resume(
    import_id: uuid.UUID,
    bundle: Annotated[
        Path | None,
        typer.Option(file_okay=False, resolve_path=True),
    ] = None,
) -> None:
    settings = _settings()
    mutation_roots = (settings.storage_root, settings.build_root)
    enforce_storage_admission(
        mutation_roots,
        phase="preflight",
        warning=lambda message: typer.echo(message, err=True),
    )
    database = _database(settings)
    try:
        service = _legacy_import_service(settings, database)
        current = service.status(import_id)
        if not _LEGACY_BUNDLE_ID_RE.fullmatch(current.bundle_id):
            raise ValueError("import ledger contains an invalid bundle identity")
        selected_bundle = bundle or (
            _environment_path("CARDRAG_IMPORT_ROOT", "/mnt/cardrag-imports") / current.bundle_id
        )
        if selected_bundle.is_symlink() or not selected_bundle.is_dir():
            raise ValueError("the original normalized bundle is not mounted for resume")
        status = service.resume(import_id, selected_bundle)
        enforce_storage_admission(
            mutation_roots,
            phase="postflight",
            warning=lambda message: typer.echo(message, err=True),
        )
        typer.echo(json.dumps(status.model_dump(mode="json"), sort_keys=True))
    finally:
        database.close()


@legacy_app.command("cancel")
def legacy_cancel(import_id: uuid.UUID) -> None:
    settings = _settings()
    database = _database(settings)
    try:
        status = _legacy_import_service(settings, database).cancel(import_id)
        typer.echo(json.dumps(status.model_dump(mode="json"), sort_keys=True))
    finally:
        database.close()


@legacy_app.command("finalize")
def legacy_finalize(import_id: uuid.UUID) -> None:
    settings = _settings()
    mutation_roots = (settings.storage_root, settings.generation_root, settings.build_root)
    enforce_storage_admission(
        mutation_roots,
        phase="preflight",
        warning=lambda message: typer.echo(message, err=True),
    )
    database = _database(settings)
    try:
        service = _legacy_import_service(settings, database)
        status = service.finalize(
            import_id,
            lambda run_id, generation_id: _publish_completed_run(settings, database, run_id, generation_id),
        )
        enforce_storage_admission(
            mutation_roots,
            phase="postflight",
            warning=lambda message: typer.echo(message, err=True),
        )
        typer.echo(json.dumps(status.model_dump(mode="json"), sort_keys=True))
    finally:
        database.close()


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


def _state_service(
    settings: Settings,
    database: Postgres,
) -> tuple[PortableStateService, Path, RuntimeCompatibility]:
    deployment_root = _environment_path("CARDRAG_DEPLOYMENT_METADATA_ROOT", "/mnt/cardrag-deployment")
    compatibility = _runtime_compatibility(settings, deployment_root)
    tools = PostgresToolRunner(_postgres_tool_config())
    return (
        PortableStateService(
            PostgresStateInspector(database),
            tools,
            progress=_emit_state_progress,
        ),
        deployment_root,
        compatibility,
    )


@state_app.command("export")
def state_export(
    destination: Annotated[Path, typer.Option(file_okay=False, resolve_path=True)],
    self_contained: Annotated[bool, typer.Option("--self-contained")] = False,
    export_id: Annotated[str | None, typer.Option(hidden=True)] = None,
) -> None:
    """Create a READY-sealed, same-epoch PostgreSQL/CAS/generation package."""

    settings = _settings()
    enforce_storage_admission(
        (destination,),
        phase="preflight",
        warning=lambda message: typer.echo(message, err=True),
    )
    destination = _validated_cli_archive_root(destination)
    database = _database(settings)
    try:
        service, deployment_root, compatibility = _state_service(settings, database)
        verification = service.export(
            ExportRequest(
                archive_root=destination,
                object_root=settings.storage_root,
                generation_root=settings.generation_root,
                compatibility=compatibility,
                deployment_root=deployment_root,
                imports_root=(
                    _environment_path("CARDRAG_IMPORT_ROOT", "/mnt/cardrag-imports")
                    if self_contained
                    else None
                ),
                include_imports=self_contained,
                export_id=export_id,
            )
        )
        enforce_storage_admission(
            (destination,),
            phase="postflight",
            warning=lambda message: typer.echo(message, err=True),
        )
        typer.echo(json.dumps(_state_result_payload(verification), sort_keys=True))
    finally:
        database.close()


@state_app.command("verify")
def state_verify(
    source: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
) -> None:
    """Verify a sealed package without accessing PostgreSQL."""

    archive_root = _environment_path("CARDRAG_ARCHIVE_ROOT", "/mnt/cardrag-archive")
    archive_root = _validated_cli_archive_root(archive_root)
    verification = verify_state_package_with_progress(
        archive_root,
        source,
        _emit_state_progress,
    )
    typer.echo(json.dumps(_state_result_payload(verification), sort_keys=True))


def _restore_request(
    settings: Settings,
    source: Path,
    compatibility: RuntimeCompatibility,
    *,
    include_role_secrets: bool,
) -> RestoreRequest:
    return RestoreRequest(
        archive_root=_validated_cli_archive_root(
            _environment_path("CARDRAG_ARCHIVE_ROOT", "/mnt/cardrag-archive")
        ),
        package_path=source,
        object_root=settings.storage_root,
        generation_root=settings.generation_root,
        expected_compatibility=compatibility,
        role_password_secrets=_role_password_secrets() if include_role_secrets else (),
    )


@state_app.command("restore")
def state_restore(
    source: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    empty_target: Annotated[bool, typer.Option("--empty-target")] = False,
    verify_restored: Annotated[bool, typer.Option("--verify-restored")] = False,
) -> None:
    """Restore empty targets, optionally reconciling the activated state immediately."""

    if not empty_target:
        raise typer.BadParameter("restore requires the explicit --empty-target safety flag")
    settings = _settings()
    restore_roots = (settings.storage_root, settings.generation_root)
    enforce_storage_admission(
        restore_roots,
        phase="preflight",
        warning=lambda message: typer.echo(message, err=True),
    )
    deployment_root = _environment_path("CARDRAG_DEPLOYMENT_METADATA_ROOT", "/mnt/cardrag-deployment")
    compatibility = _runtime_compatibility(settings, deployment_root)
    service = PortableStateService(
        None,
        PostgresToolRunner(_postgres_tool_config()),
        progress=_emit_state_progress,
    )
    request = _restore_request(
        settings,
        source,
        compatibility,
        include_role_secrets=True,
    )
    report = service.restore(request)
    if verify_restored:
        # The CardRAG application connection is intentionally opened only
        # after database activation and password rotation have succeeded.
        database = _database(settings)
        try:
            verification_service, _, verification_compatibility = _state_service(
                settings,
                database,
            )
            report = verification_service.verify_restored(
                _restore_request(
                    settings,
                    source,
                    verification_compatibility,
                    include_role_secrets=False,
                )
            )
        finally:
            database.close()
    enforce_storage_admission(
        restore_roots,
        phase="postflight",
        warning=lambda message: typer.echo(message, err=True),
    )
    typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))


@state_app.command("verify-restored")
def state_verify_restored(
    source: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
) -> None:
    """Reconcile a restored database epoch with every restored filesystem byte."""

    settings = _settings()
    database = _database(settings)
    try:
        service, _, compatibility = _state_service(settings, database)
        report = service.verify_restored(
            _restore_request(
                settings,
                source,
                compatibility,
                include_role_secrets=False,
            )
        )
        typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    finally:
        database.close()


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
