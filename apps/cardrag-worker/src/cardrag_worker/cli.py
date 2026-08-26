"""Finite command-line entry points; no scheduler or always-on server lives here."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from .adoption import (
    load_inventory,
    load_legacy_prepare_bundle,
    publish_adoptions,
    reconcile_inventories,
    validate_inventory,
    write_reports,
)
from .gc import collect_garbage
from .issuers import enabled_adapters
from .ocr import FailoverOCRResolver, OCRResolver
from .pipeline import WorkerPipeline
from .providers import OCRProvider, OpenRouterEmbeddingProvider, make_ocr_provider
from .settings import WorkerSettings
from .state import AlreadyRunning, WorkerState, worker_lock
from .webdav import WebDAVClient

app = typer.Typer(no_args_is_help=True, help="CardRAG finite acquisition/OCR/embedding worker")


def _echo(payload: Any) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _provider(settings: WorkerSettings, name: str, model: str) -> OCRProvider:
    return make_ocr_provider(
        name,
        model=model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        codex_executable=settings.codex_executable,
        codex_auth_root=settings.codex_auth_root,
        reasoning_effort=settings.ocr_reasoning_effort,
    )


async def _run(resume: str | None) -> dict[str, Any]:
    settings = WorkerSettings.from_env(require_providers=True, require_webdav=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    webdav = WebDAVClient.from_env()
    try:
        with WorkerState(settings.state_database) as state:
            primary = OCRResolver(
                provider=_provider(settings, settings.ocr_provider, settings.ocr_model),
                state=state,
                webdav=webdav,
                chunk_pages=settings.ocr_chunk_pages,
                cache_epoch=settings.ocr_cache_epoch,
                prompt_version=settings.ocr_prompt_version,
            )
            resolver: OCRResolver | FailoverOCRResolver = primary
            if settings.ocr_fallback_provider:
                fallback_model = settings.ocr_fallback_model
                if not fallback_model:
                    raise ValueError("CARDRAG_OCR_FALLBACK_MODEL is required with fallback provider")
                fallback = OCRResolver(
                    provider=_provider(settings, settings.ocr_fallback_provider, fallback_model),
                    state=state,
                    webdav=webdav,
                    chunk_pages=settings.ocr_chunk_pages,
                    cache_epoch=settings.ocr_cache_epoch,
                    prompt_version=settings.ocr_prompt_version,
                )
                resolver = FailoverOCRResolver(primary, fallback)
            embeddings = OpenRouterEmbeddingProvider(
                api_key=settings.openrouter_api_key or "",
                model=settings.embedding_model,
                base_url=settings.openrouter_base_url,
            )
            result = await WorkerPipeline(
                state=state,
                state_dir=settings.state_dir,
                adapters=enabled_adapters(),
                ocr=resolver,  # type: ignore[arg-type]
                embeddings=embeddings,
                webdav=webdav,
                maximum_attempts=settings.stage_max_attempts,
                retry_cap_seconds=settings.retry_cap_seconds,
            ).run(resume_run_id=resume)
            return {
                "run_id": result.run_id,
                "status": result.status,
                "generation_id": result.generation_id,
                "corpus_sha256": result.corpus_sha256,
                "contract_sha256": result.contract_sha256,
                "documents": result.document_count,
                "unsupported_documents": result.unsupported_document_count,
                "evidence": result.evidence_count,
                "gc_status": result.gc_status,
                "gc_deleted": result.gc_deleted,
                "gc_error": result.gc_error[:1000] if result.gc_error else None,
            }
    finally:
        await webdav.close()


@app.command("run")
def run_command(
    resume: str | None = typer.Option(None, "--resume", help="Resume one failed finite run ID."),
) -> None:
    try:
        _echo(asyncio.run(_run(resume)))
    except AlreadyRunning as exc:
        _echo({"status": "already_running", "error": str(exc)})


@app.command("resume")
def resume_command(run_id: str = typer.Argument(..., help="Failed finite run ID.")) -> None:
    try:
        _echo(asyncio.run(_run(run_id)))
    except AlreadyRunning as exc:
        _echo({"status": "already_running", "error": str(exc)})


@app.command("webdav-check")
def webdav_check() -> None:
    async def check() -> dict[str, Any]:
        WorkerSettings.from_env(require_webdav=True)
        client = WebDAVClient.from_env()
        try:
            result = await client.check()
            return {
                "reachable": result.reachable,
                "operations": result.operations,
                "overwrite_false_conflict_status": result.overwrite_false_conflict_status,
            }
        finally:
            await client.close()

    _echo(asyncio.run(check()))


async def _publish_if_requested(result: Any, publish: bool) -> int:
    if not publish:
        return 0
    if any(conflict.blocking for conflict in result.conflicts):
        raise ValueError("refusing adoption publication while blocking conflicts exist")
    # Inventory/bundle structure and ledger binding fail before an
    # AdoptionResult is produced. Errors recorded here are isolated candidate
    # failures (for example, a corrupt PDF or non-canonical OCR file). The v1
    # migration contract deliberately publishes every independently verified
    # candidate and leaves rejected documents for the normal Worker run to
    # download/OCR again.
    WorkerSettings.from_env(require_webdav=True)
    client = WebDAVClient.from_env()
    try:
        return await publish_adoptions(result, client)
    finally:
        await client.close()


@app.command("adopt")
def adopt(
    current_inventory: Path | None = typer.Option(None, "--current-inventory", exists=True),
    legacy_inventory: Path | None = typer.Option(None, "--legacy-inventory", exists=True),
    legacy_bundle: Path | None = typer.Option(None, "--legacy-bundle", exists=True),
    legacy_ledger: Path | None = typer.Option(None, "--legacy-ledger", exists=True),
    receipts: Path = typer.Option(..., "--receipts"),
    conflicts: Path = typer.Option(..., "--conflicts"),
    publish: bool = typer.Option(
        False,
        "--publish",
        help="Publish verified candidates only when there are no blocking identity conflicts.",
    ),
) -> None:
    if current_inventory is None and legacy_inventory is None and legacy_bundle is None:
        raise typer.BadParameter("provide current and/or legacy inventory")
    if legacy_inventory is not None and legacy_bundle is not None:
        raise typer.BadParameter("choose legacy inventory or legacy prepare bundle, not both")
    if (legacy_bundle is None) != (legacy_ledger is None):
        raise typer.BadParameter("--legacy-bundle and --legacy-ledger must be supplied together")
    current_rows = load_inventory(current_inventory) if current_inventory else ()
    if legacy_bundle and legacy_ledger:
        legacy_rows = load_legacy_prepare_bundle(legacy_bundle, legacy_ledger)
    else:
        legacy_rows = load_inventory(legacy_inventory) if legacy_inventory else ()
    result = reconcile_inventories(current_rows, legacy_rows)
    write_reports(result, receipts=receipts, conflicts=conflicts)
    published = asyncio.run(_publish_if_requested(result, publish))
    _echo(
        {
            "accepted": len(result.receipts),
            "conflicts": len(result.conflicts),
            "errors": len(result.errors),
            "published": published,
            "receipts": str(receipts.resolve()),
            "conflict_report": str(conflicts.resolve()),
        }
    )


def _single_adoption(
    inventory: Path, receipts: Path, conflicts: Path, publish: bool, source_kind: str
) -> None:
    result = validate_inventory(load_inventory(inventory), source_kind=source_kind)  # type: ignore[arg-type]
    write_reports(result, receipts=receipts, conflicts=conflicts)
    published = asyncio.run(_publish_if_requested(result, publish))
    _echo(
        {
            "accepted": len(result.receipts),
            "conflicts": len(result.conflicts),
            "errors": len(result.errors),
            "published": published,
        }
    )


@app.command("adopt-current")
def adopt_current(
    inventory: Path = typer.Argument(..., exists=True),
    receipts: Path = typer.Option(..., "--receipts"),
    conflicts: Path = typer.Option(..., "--conflicts"),
    publish: bool = typer.Option(False, "--publish"),
) -> None:
    _single_adoption(inventory, receipts, conflicts, publish, "current")


@app.command("adopt-legacy")
def adopt_legacy(
    inventory: Path = typer.Argument(..., exists=True),
    receipts: Path = typer.Option(..., "--receipts"),
    conflicts: Path = typer.Option(..., "--conflicts"),
    publish: bool = typer.Option(False, "--publish"),
) -> None:
    _single_adoption(inventory, receipts, conflicts, publish, "legacy")


@app.command("gc")
def gc_command(
    apply: bool = typer.Option(False, "--apply", help="Delete grace-eligible objects; default is dry-run."),
    retain: int = typer.Option(3, "--retain", min=1),
    grace_days: int = typer.Option(30, "--grace-days", min=1),
) -> None:
    async def run_gc() -> dict[str, Any]:
        settings = WorkerSettings.from_env(require_webdav=True)
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        client = WebDAVClient.from_env()
        try:
            with worker_lock(settings.lock_file), WorkerState(settings.state_database) as state:
                result = await collect_garbage(
                    webdav=client,
                    state=state,
                    apply=apply,
                    retain_generations=retain,
                    grace_days=grace_days,
                )
                return {
                    "dry_run": result.dry_run,
                    "retained_generations": result.retained_generations,
                    "marked_objects": result.marked_objects,
                    "candidates": result.candidates,
                    "eligible": result.eligible,
                    "deleted": result.deleted,
                }
        finally:
            await client.close()

    try:
        _echo(asyncio.run(run_gc()))
    except AlreadyRunning as exc:
        _echo({"status": "already_running", "error": str(exc)})


def main() -> None:
    app()


if __name__ == "__main__":
    main()
