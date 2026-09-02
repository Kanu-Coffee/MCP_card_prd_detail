"""Finite command-line entry points; no scheduler or always-on server lives here."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from contextlib import suppress
from pathlib import Path
from typing import Any

import typer

from .adoption import (
    ADOPTION_POLICY_VERSION,
    AdoptionError,
    audit_published_adoptions,
    guard_adoption_publication,
    load_inventory,
    load_legacy_prepare_bundle,
    publish_adoptions,
    reconcile_inventories,
    validate_inventory,
    write_reports,
)
from .aggregation_profile_v5 import load_verified_aggregation_profile_v5
from .cache_seed import (
    CacheSeedError,
    apply_cache_seed,
    build_cache_seed_plan,
    paths_overlap,
)
from .cache_seed_v109 import (
    V109CacheSeedError,
    apply_v109_cache_seed,
    build_v109_cache_seed_plan,
)
from .cache_seed_v109 import (
    paths_overlap as v109_paths_overlap,
)
from .capacity_v5 import (
    V5CapacityPolicy,
    preflight_worker_start_capacity,
    revalidate_worker_start_capacity,
)
from .embedding_v5 import (
    OpenRouterQwenEmbeddingProviderV5,
    preflight_openrouter_qwen_providers,
)
from .gc import GCPartialFailure, collect_garbage
from .issuers import enabled_adapters
from .ocr import FailoverOCRResolver, OCRResolver
from .pdf_cache import PDFCache
from .pipeline import (
    OCRDocumentFailuresError,
    OCRSystemicFailureError,
    PipelineResult,
    WorkerPipeline,
    WorkerUnexpectedFailureError,
    validate_document_aggregation_head,
)
from .providers import OCRProvider, make_ocr_provider
from .settings import WorkerSettings
from .state import AlreadyRunning, WorkerState, worker_lock
from .tokenizer_v5 import ensure_qwen_tokenizer
from .webdav import WebDAVClient

app = typer.Typer(no_args_is_help=True, help="CardRAG finite acquisition/OCR/embedding worker")

_WORKER_SHUTDOWN_SIGNALS: tuple[int, int] = (int(signal.SIGTERM), int(signal.SIGINT))


class WorkerSignalShutdown(RuntimeError):
    """A process signal whose cancellation has been fully drained."""

    def __init__(self, signal_number: int) -> None:
        if type(signal_number) is not int or signal_number not in _WORKER_SHUTDOWN_SIGNALS:
            raise ValueError("unsupported worker shutdown signal")
        self.signal_number = signal_number
        super().__init__("Worker signal shutdown completed")


def _configure_worker_logging() -> None:
    """Emit bounded Worker progress without enabling noisy dependency logs."""

    logger = logging.getLogger("cardrag_worker")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _echo(payload: Any) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _echo_ocr_failures(exc: OCRDocumentFailuresError) -> None:
    sample = [
        {
            "attempts": failure.attempts,
            "document_id": failure.document_id,
            "issuer": failure.issuer,
            "product_code": failure.product_code,
            "reason": failure.reason,
            "reason_code": failure.reason_code,
        }
        for failure in exc.failures[:5]
    ]
    _echo(
        {
            "ocr_failure_count": len(exc.failures),
            "reason_code": "ocr_document_failures",
            "report": exc.report,
            "run_id": exc.run_id,
            "sample": sample,
            "status": "failed",
        }
    )


def _echo_ocr_systemic_failure(exc: OCRSystemicFailureError) -> None:
    failure = exc.failure
    payload: dict[str, Any] = {
        "document_id": failure.document_id,
        "error_class_category": failure.error_class_category,
        "issuer": failure.issuer,
        "product_code": failure.product_code,
        "reason": failure.reason,
        "reason_code": failure.reason_code,
        "report": exc.report,
        "run_id": exc.run_id,
        "status": "failed",
    }
    if failure.phase is not None:
        payload["phase"] = failure.phase
    if failure.status_code is not None:
        payload["status_code"] = failure.status_code
    if failure.error_kind is not None:
        payload["error_kind"] = failure.error_kind
    if failure.retryable is not None:
        payload["retryable"] = failure.retryable
    if failure.publication_attempts is not None:
        payload["publication_attempts"] = failure.publication_attempts
    if failure.exit_code is not None:
        payload["exit_code"] = failure.exit_code
    if failure.stderr_size_bytes is not None:
        payload["stderr_size_bytes"] = failure.stderr_size_bytes
        payload["stderr_sha256"] = failure.stderr_sha256
    _echo(payload)


def _echo_worker_unexpected_failure(exc: WorkerUnexpectedFailureError | None = None) -> None:
    payload: dict[str, Any] = {
        "reason": "Worker pipeline failed unexpectedly.",
        "reason_code": "worker_unexpected_failure",
        "status": "failed",
    }
    if exc is not None:
        payload.update(
            {
                "error_class_category": exc.failure.error_class_category,
                "report": exc.report,
                "run_id": exc.run_id,
            }
        )
        if exc.failure.status_code is not None:
            payload["status_code"] = exc.failure.status_code
        if exc.failure.errno is not None:
            payload["errno"] = exc.failure.errno
    _echo(payload)


def _provider(settings: WorkerSettings, name: str, model: str) -> OCRProvider:
    return make_ocr_provider(
        name,
        model=model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        codex_executable=settings.codex_executable,
        codex_auth_root=settings.codex_auth_root,
        reasoning_effort=settings.ocr_reasoning_effort,
        timeout_seconds=settings.ocr_provider_timeout_seconds,
    )


def _pipeline_result_payload(result: PipelineResult) -> dict[str, Any]:
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
        "pdf_cache_hits": result.pdf_cache_hits,
        "pdf_cache_misses": result.pdf_cache_misses,
        "pdf_cache_not_modified": result.pdf_cache_not_modified,
        "pdf_cache_prune_error": (
            result.pdf_cache_prune_error[:1000] if result.pdf_cache_prune_error else None
        ),
        "pdf_cache_prune_status": result.pdf_cache_prune_status,
        "pdf_cache_pruned_bytes": result.pdf_cache_pruned_bytes,
        "pdf_cache_pruned_objects": result.pdf_cache_pruned_objects,
        "pdf_cache_revalidations": result.pdf_cache_revalidations,
        "pdf_downloads": result.pdf_downloads,
        "pdf_revisions": result.pdf_revisions,
        "ocr_cache_publication_deferred": result.ocr_cache_publication_deferred,
        "v5_metrics": result.v5_metrics,
    }


async def _qwen_embedding_provider(
    settings: WorkerSettings,
) -> OpenRouterQwenEmbeddingProviderV5:
    """Run the credentialed provider/tokenizer preflight before touching a corpus."""

    api_key = settings.openrouter_api_key or ""
    tokenizer = await ensure_qwen_tokenizer(
        settings.embedding_tokenizer_path,
        timeout_seconds=settings.embedding_timeout_seconds,
    )
    comparison = await preflight_openrouter_qwen_providers(
        api_key=api_key,
        token_counter=tokenizer,
        base_url=settings.openrouter_base_url,
        timeout_seconds=settings.embedding_timeout_seconds,
        embedding_maximum_response_bytes=settings.embedding_max_response_bytes,
        metadata_maximum_response_bytes=settings.embedding_metadata_max_response_bytes,
        request_max_attempts=settings.embedding_request_max_attempts,
        retry_base_seconds=settings.embedding_retry_base_seconds,
        retry_cap_seconds=settings.embedding_retry_cap_seconds,
    )
    selected = next(
        report
        for report in comparison.providers
        if report.profile.provider_id == settings.embedding_provider_id
    )
    profile = selected.profile
    if profile.maximum_tokens != settings.embedding_maximum_tokens:
        raise ValueError("CARDRAG_EMBEDDING_MAXIMUM_TOKENS differs from live pinned-provider metadata")
    if profile.model != settings.embedding_model or profile.dimension != settings.embedding_dimension:
        raise ValueError("configured embedding model/dimension differs from the verified Qwen profile")
    logging.getLogger("cardrag_worker.cli").info(
        "Qwen provider preflight passed provider=%s samples=%d minimum_repeat_cosine=%.6f "
        "minimum_cross_provider_cosine=%.6f tokenizer_sha256=%s",
        profile.provider_id,
        selected.sample_count,
        selected.minimum_repeat_cosine,
        comparison.minimum_cross_provider_cosine,
        tokenizer.asset_sha256,
    )
    return OpenRouterQwenEmbeddingProviderV5(
        api_key=api_key,
        profile=profile,
        token_counter=tokenizer,
        base_url=settings.openrouter_base_url,
        timeout_seconds=settings.embedding_timeout_seconds,
        maximum_response_bytes=settings.embedding_max_response_bytes,
        request_max_attempts=settings.embedding_request_max_attempts,
        retry_base_seconds=settings.embedding_retry_base_seconds,
        retry_cap_seconds=settings.embedding_retry_cap_seconds,
    )


def _guard_v113_publication_channel(settings: WorkerSettings) -> None:
    if settings.channel == "candidate-v1.0.11":
        return
    if settings.channel == "stable":
        if settings.stable_publication_approved:
            return
        raise ValueError(
            "stable v1.0.13 publication requires explicit CARDRAG_STABLE_PUBLICATION_APPROVED=true approval"
        )
    raise ValueError("v1.0.13 Worker publication channel must be candidate-v1.0.11 or stable")


async def _run(resume: str | None) -> dict[str, Any]:
    _configure_worker_logging()
    settings = WorkerSettings.from_env(require_providers=True, require_webdav=True)
    _guard_v113_publication_channel(settings)
    startup_capacity = preflight_worker_start_capacity(
        settings.state_dir,
        minimum_free_bytes=settings.minimum_start_free_bytes,
    )
    logging.getLogger("cardrag_worker.cli").info(
        "Worker startup capacity preflight passed filesystem_free_bytes=%d minimum_free_bytes=%d",
        startup_capacity.filesystem_free_bytes,
        startup_capacity.minimum_free_bytes,
    )
    document_aggregation = None
    if settings.document_aggregation_profile_path is not None:
        expected_artifact_sha256 = settings.document_aggregation_profile_artifact_sha256
        if expected_artifact_sha256 is None:  # WorkerSettings enforces all-or-nothing.
            raise ValueError("document aggregation profile artifact SHA-256 is absent")
        document_aggregation = load_verified_aggregation_profile_v5(
            settings.document_aggregation_profile_path,
            expected_artifact_sha256=expected_artifact_sha256,
        )
    if document_aggregation is None:
        # Preserve the unsealed M0 startup order byte-for-byte and behaviorally:
        # candidate state precedes construction of its WebDAV client.
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        startup_capacity = revalidate_worker_start_capacity(startup_capacity)
    webdav = WebDAVClient.from_env(stable_publication_approved=settings.stable_publication_approved)
    try:
        if document_aggregation is not None:
            # No provider/tokenizer call or candidate-state mutation is allowed
            # until GET-only proof identifies the evaluated M0 or its sealed M1.
            await validate_document_aggregation_head(webdav, document_aggregation)
            settings.state_dir.mkdir(parents=True, exist_ok=True)
        # Narrow the descriptor-walk-to-use window for both M0 and M1.  This
        # remains immediately before SQLite opens the path, after any allowed
        # WebDAV construction/GET-only aggregation validation.
        startup_capacity = revalidate_worker_start_capacity(startup_capacity)
        with WorkerState(settings.state_database) as state:
            primary = OCRResolver(
                provider=_provider(settings, settings.ocr_provider, settings.ocr_model),
                state=state,
                webdav=webdav,
                chunk_pages=settings.ocr_chunk_pages,
                whole_document_max_pages=settings.ocr_whole_document_max_pages,
                context_pages_before=settings.ocr_context_pages_before,
                context_pages_after=settings.ocr_context_pages_after,
                render_scale_milli=settings.ocr_render_scale_milli,
                cache_epoch=settings.ocr_cache_epoch,
                prompt_version=settings.ocr_prompt_version,
                cache_mode=settings.ocr_cache_mode,
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
                    whole_document_max_pages=settings.ocr_whole_document_max_pages,
                    context_pages_before=settings.ocr_context_pages_before,
                    context_pages_after=settings.ocr_context_pages_after,
                    render_scale_milli=settings.ocr_render_scale_milli,
                    cache_epoch=settings.ocr_cache_epoch,
                    prompt_version=settings.ocr_prompt_version,
                    cache_mode=settings.ocr_cache_mode,
                )
                resolver = FailoverOCRResolver(primary, fallback)
            logging.getLogger("cardrag_worker.cli").info(
                "Remote OCR cache access mode=%s", settings.ocr_cache_mode
            )
            embeddings = await _qwen_embedding_provider(settings)
            result = await WorkerPipeline(
                state=state,
                state_dir=settings.state_dir,
                adapters=enabled_adapters(),
                ocr=resolver,  # type: ignore[arg-type]
                embeddings=embeddings,
                webdav=webdav,
                maximum_attempts=settings.stage_max_attempts,
                retry_cap_seconds=settings.retry_cap_seconds,
                collect_remote_garbage=settings.collect_remote_garbage,
                stable_publication_approved=settings.stable_publication_approved,
                ocr_cache_publication_approved=settings.ocr_cache_publication_approved,
                remote_gc_approved=settings.remote_gc_approved,
                retained_generations=settings.retain_generations,
                retained_incomplete_runs=settings.retained_incomplete_runs,
                garbage_grace_days=settings.garbage_grace_days,
                pdf_cache_refresh_hours=settings.pdf_cache_refresh_hours,
                document_aggregation=document_aggregation,
                capacity_policy_v5=V5CapacityPolicy(
                    maximum_state_bytes=settings.maximum_state_bytes,
                    reserved_free_space_bytes=settings.reserved_free_space_bytes,
                    maximum_vector_sidecar_bytes=settings.maximum_vector_sidecar_bytes,
                    maximum_serving_database_bytes=settings.maximum_serving_database_bytes,
                ),
            ).run(resume_run_id=resume)
            return _pipeline_result_payload(result)
    finally:
        await webdav.close()


async def _run_with_signal_shutdown(resume: str | None) -> dict[str, Any]:
    """Translate SIGTERM/SIGINT into one drained pipeline cancellation.

    A second signal is deliberately coalesced with the first one. Repeated
    ``Task.cancel()`` calls can otherwise interrupt publication
    reconciliation or a cancellation-fenced blocking operation and release
    the worker lock before its mutation has stopped.
    """

    loop = asyncio.get_running_loop()
    task: asyncio.Task[dict[str, Any]] | None = None
    requested_signal: int | None = None
    installed: list[tuple[int, Any]] = []

    def request_shutdown(signal_number: int) -> None:
        nonlocal requested_signal
        if requested_signal is not None:
            logging.getLogger("cardrag_worker.cli").warning(
                "Additional worker shutdown signal ignored while cancellation drains"
            )
            return
        requested_signal = signal_number
        logging.getLogger("cardrag_worker.cli").warning(
            "Worker shutdown requested; cancellation drain started (signal=%s)",
            signal.Signals(signal_number).name,
        )
        if task is not None:
            task.cancel()

    try:
        for signal_number in _WORKER_SHUTDOWN_SIGNALS:
            previous = signal.getsignal(signal_number)
            loop.add_signal_handler(signal_number, request_shutdown, signal_number)
            installed.append((signal_number, previous))
    except (NotImplementedError, RuntimeError):
        for signal_number, previous in reversed(installed):
            loop.remove_signal_handler(signal_number)
            signal.signal(signal_number, previous)
        raise RuntimeError("worker signal handlers are unavailable") from None

    task = asyncio.create_task(_run(resume), name="cardrag-worker-pipeline")
    # A signal may be delivered after its loop handler is installed but before
    # the task reference becomes visible to that handler. Close that narrow
    # startup race instead of silently running after a stop was requested.
    if requested_signal is not None:
        task.cancel()
    cancelled_by_signal = False
    result: dict[str, Any] | None = None
    try:
        try:
            result = await task
        except asyncio.CancelledError:
            if requested_signal is None:
                raise
            cancelled_by_signal = True
    finally:
        for signal_number, previous in reversed(installed):
            loop.remove_signal_handler(signal_number)
            signal.signal(signal_number, previous)

    if cancelled_by_signal:
        assert requested_signal is not None
        raise WorkerSignalShutdown(requested_signal) from None
    if result is None:
        raise RuntimeError("worker pipeline returned no terminal result")
    return result


def _echo_signal_shutdown(exc: WorkerSignalShutdown) -> int:
    signal_number: object = exc.signal_number
    if type(signal_number) is not int or signal_number not in _WORKER_SHUTDOWN_SIGNALS:
        signal_number = signal.SIGTERM
    exit_code = 128 + signal_number
    _echo(
        {
            "exit_code": exit_code,
            "reason": "Worker stopped after cancellation drain and terminal-state reconciliation.",
            "reason_code": "worker_signal_shutdown",
            "signal": signal.Signals(signal_number).name,
            # Process-level status only. Publication reconciliation may have
            # durably proven the run succeeded immediately before shutdown;
            # the state DB remains the authority for that run status.
            "status": "shutdown_complete",
        }
    )
    return exit_code


def _echo_worker_busy() -> None:
    _echo(
        {
            "reason": "Worker did not start because the worker lock is held.",
            "reason_code": "worker_busy",
            "status": "already_running",
        }
    )


@app.command("run")
def run_command(
    resume: str | None = typer.Option(None, "--resume", help="Resume one failed finite run ID."),
) -> None:
    try:
        _echo(asyncio.run(_run_with_signal_shutdown(resume)))
    except WorkerSignalShutdown as exc:
        raise typer.Exit(code=_echo_signal_shutdown(exc)) from None
    except OCRDocumentFailuresError as exc:
        _echo_ocr_failures(exc)
        raise typer.Exit(code=1) from None
    except OCRSystemicFailureError as exc:
        _echo_ocr_systemic_failure(exc)
        raise typer.Exit(code=1) from None
    except AlreadyRunning:
        _echo_worker_busy()
    except WorkerUnexpectedFailureError as exc:
        _echo_worker_unexpected_failure(exc)
        raise typer.Exit(code=1) from None
    except Exception:
        _echo_worker_unexpected_failure()
        raise typer.Exit(code=1) from None


@app.command("resume")
def resume_command(run_id: str = typer.Argument(..., help="Failed finite run ID.")) -> None:
    try:
        _echo(asyncio.run(_run_with_signal_shutdown(run_id)))
    except WorkerSignalShutdown as exc:
        raise typer.Exit(code=_echo_signal_shutdown(exc)) from None
    except OCRDocumentFailuresError as exc:
        _echo_ocr_failures(exc)
        raise typer.Exit(code=1) from None
    except OCRSystemicFailureError as exc:
        _echo_ocr_systemic_failure(exc)
        raise typer.Exit(code=1) from None
    except AlreadyRunning:
        _echo_worker_busy()
    except WorkerUnexpectedFailureError as exc:
        _echo_worker_unexpected_failure(exc)
        raise typer.Exit(code=1) from None
    except Exception:
        _echo_worker_unexpected_failure()
        raise typer.Exit(code=1) from None


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


def _seed_legacy_pdf_cache(legacy_root: Path, *, apply: bool) -> dict[str, Any]:
    plan = build_cache_seed_plan(legacy_root)
    if not apply:
        return plan.report(applied=False)

    settings = WorkerSettings.from_env()
    if settings.channel == "stable":
        raise CacheSeedError("stable_destination_forbidden")
    if settings.channel != "candidate-v1.0.9":
        raise CacheSeedError("candidate_destination_required")
    if paths_overlap(plan.legacy_root, settings.state_dir):
        raise CacheSeedError("destination_overlaps_legacy_root")
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with worker_lock(settings.lock_file), WorkerState(settings.state_database) as state:
            return apply_cache_seed(plan, PDFCache(settings.state_dir, state))
    except AlreadyRunning as exc:
        raise CacheSeedError("destination_busy") from exc


@app.command("cache-seed")
def cache_seed_command(
    legacy_root: Path = typer.Argument(..., help="Absolute read-only v1.0.8 Worker state root."),
    apply: bool = typer.Option(False, "--apply", help="Seed the candidate cache; default is dry-run."),
) -> None:
    """Validate legacy run PDFs and optionally seed only the candidate PDF cache."""

    try:
        _echo(_seed_legacy_pdf_cache(legacy_root, apply=apply))
    except CacheSeedError as exc:
        _echo(
            {
                "applied_candidates": 0,
                "created_pdf_objects": 0,
                "created_revisions": 0,
                "dry_run": not apply,
                "ledger_path": None,
                "ledger_sha256": None,
                "ledger_size_bytes": 0,
                "reason_code": exc.code,
                "reused_candidates": 0,
                "schema_version": "cardrag.cache-seed-report.v1",
                "skipped_stale_runs": 0,
                "status": "blocked",
            }
        )
        raise typer.Exit(code=1) from None


def _seed_v109_pdf_cache(source_state_root: Path, *, apply: bool) -> dict[str, Any]:
    plan = build_v109_cache_seed_plan(source_state_root)
    if not apply:
        return plan.report(applied=False)
    settings = WorkerSettings.from_env()
    if settings.channel != "candidate-v1.0.11":
        raise V109CacheSeedError("candidate_v111_destination_required")
    if v109_paths_overlap(plan.source_root, settings.state_dir):
        raise V109CacheSeedError("source_destination_overlap")
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with worker_lock(settings.lock_file), WorkerState(settings.state_database) as state:
            cache = PDFCache(settings.state_dir, state)
            first = apply_v109_cache_seed(plan, cache)
            second = apply_v109_cache_seed(plan, cache)
    except AlreadyRunning as exc:
        raise V109CacheSeedError("destination_busy") from exc
    if second["imported_pdf_objects"] != 0 or second["imported_revisions"] != 0:
        raise V109CacheSeedError("idempotence_verification_failed")
    return {
        **first,
        "idempotence_imported_pdf_objects": second["imported_pdf_objects"],
        "idempotence_imported_revisions": second["imported_revisions"],
        "idempotence_verified": True,
    }


@app.command("seed-cache-v109")
def seed_cache_v109_command(
    source_state_root: Path = typer.Argument(
        ...,
        help="Absolute read-only v1.0.9 Worker state root.",
    ),
    apply: bool = typer.Option(False, "--apply", help="Seed v1.0.11; default is dry-run."),
) -> None:
    """Audit and idempotently import only v1.0.9 PDF cache/history into v1.0.11."""

    try:
        _echo(_seed_v109_pdf_cache(source_state_root, apply=apply))
    except V109CacheSeedError as exc:
        _echo(
            {
                "applied": False,
                "dry_run": not apply,
                "reason_code": exc.code,
                "schema_version": "cardrag.cache-seed-v109-report.v1",
                "status": "blocked",
            }
        )
        raise typer.Exit(code=1) from None


async def _publish_if_requested(result: Any, publish: bool) -> int:
    if not publish:
        return 0
    if any(conflict.blocking for conflict in result.conflicts):
        raise ValueError("refusing adoption publication while blocking conflicts exist")
    if any(error.get("policy_version") == ADOPTION_POLICY_VERSION for error in result.errors):
        raise ValueError("refusing partial publication of a sealed v2 adoption export")
    # Inventory/bundle structure and ledger binding fail before an
    # AdoptionResult is produced. Errors recorded here are isolated candidate
    # failures (for example, a corrupt PDF or non-canonical OCR file). The v1
    # migration contract deliberately publishes every independently verified
    # candidate and leaves rejected documents for the normal Worker run to
    # download/OCR again.
    settings = WorkerSettings.from_env(require_webdav=True)
    if settings.ocr_cache_mode != "read-write":
        raise ValueError("adoption publication requires CARDRAG_OCR_CACHE_MODE=read-write")
    client = WebDAVClient.from_env()
    try:
        await guard_adoption_publication(client)
        return await publish_adoptions(result, client)
    finally:
        await client.close()


async def _guard_adoption_namespace() -> dict[str, Any]:
    WorkerSettings.from_env(require_webdav=True)
    client = WebDAVClient.from_env()
    try:
        await guard_adoption_publication(client)
        return {"stable_pointer_absent": True, "status": "clear"}
    finally:
        await client.close()


@app.command("adoption-guard")
def adoption_guard() -> None:
    """Prove stable.json is absent using only a read-only existence check."""

    try:
        _echo(asyncio.run(_guard_adoption_namespace()))
    except Exception:
        _echo({"stable_pointer_absent": False, "status": "blocked"})
        raise typer.Exit(code=1) from None


async def _audit_adoption_export(inventory: Path) -> dict[str, Any]:
    rows = load_inventory(inventory)
    result = validate_inventory(rows, source_kind="legacy")
    if result.conflicts or result.errors or len(result.receipts) != len(rows):
        raise AdoptionError("sealed v2 export did not validate completely for audit")
    WorkerSettings.from_env(require_webdav=True)
    client = WebDAVClient.from_env()
    try:
        audited = await audit_published_adoptions(result, client)
    finally:
        await client.close()
    return {"expected": len(result.receipts), "audited": audited, "status": "verified"}


@app.command("adoption-audit")
def adoption_audit(
    inventory: Path = typer.Argument(..., exists=True),
) -> None:
    """Read back a sealed v2 export's published manifests, READYs, and OCR objects."""

    _echo(asyncio.run(_audit_adoption_export(inventory)))


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


async def _run_gc(*, apply: bool, retain: int, grace_days: int) -> dict[str, Any]:
    settings = WorkerSettings.from_env(require_webdav=True)
    _guard_remote_gc(settings, apply=apply)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    client = WebDAVClient.from_env(stable_publication_approved=settings.stable_publication_approved)
    try:
        with worker_lock(settings.lock_file), WorkerState(settings.state_database) as state:
            result = await collect_garbage(
                webdav=client,
                state=state,
                apply=apply,
                retain_generations=retain,
                grace_days=grace_days,
                pointer_path=client.pointer_path,
            )
            payload = {
                "dry_run": result.dry_run,
                "retained_generations": result.retained_generations,
                "marked_objects": result.marked_objects,
                "candidates": result.candidates,
                "eligible": result.eligible,
                "deleted": result.deleted,
            }
    except BaseException:
        # A secondary close failure must not replace GCPartialFailure and lose
        # its already-completed DELETE count.
        with suppress(Exception):
            await client.close()
        raise
    await client.close()
    return payload


def _guard_remote_gc(settings: WorkerSettings, *, apply: bool) -> None:
    """Require a separate deletion capability before any local or remote mutation."""

    if not apply:
        return
    if (
        settings.channel != "stable"
        or not settings.collect_remote_garbage
        or not settings.stable_publication_approved
        or not settings.remote_gc_approved
    ):
        raise ValueError(
            "remote GC apply requires stable channel, collection enabled, "
            "stable publication approval, and separate remote-GC approval"
        )


def _echo_gc_failure(*, deleted_count: int | None = None, busy: bool = False) -> None:
    if deleted_count is not None:
        _echo(
            {
                "deleted_count": deleted_count,
                "reason": "Remote garbage collection stopped after partial deletion.",
                "reason_code": "remote_gc_partial_failure",
                "status": "failed",
            }
        )
        return
    if busy:
        _echo(
            {
                "reason": "Remote garbage collection did not start because the worker lock is held.",
                "reason_code": "remote_gc_busy",
                "status": "failed",
            }
        )
        return
    _echo(
        {
            "reason": "Remote garbage collection failed.",
            "reason_code": "remote_gc_failed",
            "status": "failed",
        }
    )


def _validated_gc_deleted_count(exc: GCPartialFailure) -> int | None:
    try:
        value: object = exc.deleted_count
    except Exception:
        return None
    if type(value) is not int or value < 1:
        return None
    return value


@app.command("gc")
def gc_command(
    apply: bool = typer.Option(False, "--apply", help="Delete grace-eligible objects; default is dry-run."),
    retain: int = typer.Option(2, "--retain", min=1),
    grace_days: int = typer.Option(30, "--grace-days", min=1),
) -> None:

    try:
        _echo(asyncio.run(_run_gc(apply=apply, retain=retain, grace_days=grace_days)))
    except GCPartialFailure as exc:
        deleted_count = _validated_gc_deleted_count(exc)
        _echo_gc_failure(deleted_count=deleted_count)
        raise typer.Exit(code=1) from None
    except AlreadyRunning:
        _echo_gc_failure(busy=True)
        raise typer.Exit(code=1) from None
    except Exception:
        _echo_gc_failure()
        raise typer.Exit(code=1) from None


def main() -> None:
    app()


if __name__ == "__main__":
    main()
