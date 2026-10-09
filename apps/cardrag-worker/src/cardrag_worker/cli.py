"""Finite command-line entry points; no scheduler or always-on server lives here."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import stat
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from pathlib import Path
from typing import Any

import typer
from cardrag_core import STABLE_POINTER_PATH, OCRInput, content_addressed_ocr_reuse_key

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
from .backup import BackupLedger
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
    V5CapacityError,
    V5CapacityPolicy,
    preflight_worker_start_capacity,
    revalidate_worker_start_capacity,
)
from .content_cache import ContentOCRVariantStore
from .content_inventory import inventory_content_migration
from .content_migration import apply_content_migration, plan_content_migration
from .corpus_baseline import CorpusBaselineError
from .corpus_diff import CorpusDiffError
from .embedding_seed_v122 import (
    apply_embedding_cache_seed_v122,
    build_embedding_cache_seed_v122_plan,
)
from .embedding_v5 import (
    OpenRouterQwenEmbeddingProviderV5,
    preflight_openrouter_qwen_providers,
)
from .gc import GCPartialFailure, _generation_chain, collect_garbage
from .issuers import enabled_adapters
from .local_publisher import LocalServingTransport
from .ocr import FailoverOCRResolver, OCRResolver, discover_compatible_contracts
from .ocr_recovery import OCRRecoveryError, restore_ocr_seed_from_generation
from .ocr_requests import OCRReprocessRequest, plan_reprocess_requests, queue_reprocess_requests
from .pdf_cache import PDFCache
from .pipeline import (
    OCRDocumentFailuresError,
    OCRSystemicFailureError,
    PipelineResult,
    WorkerPipeline,
    WorkerUnexpectedFailureError,
    resume_sealed_publication,
    sweep_stale_paddle_input_symlinks,
    validate_document_aggregation_head,
)
from .providers import OCRProvider, PaddleOCRVLProvider, make_ocr_provider
from .retirement import RetirementError
from .settings import PublicationResumeSettings, WorkerSettings
from .state import AlreadyRunning, WorkerState, worker_lock
from .state_seed_v122 import (
    StateSeedError,
    apply_state_seed_v122,
    build_state_seed_v122_plan,
)
from .tokenizer_v5 import ensure_qwen_tokenizer
from .webdav import WebDAVClient

app = typer.Typer(no_args_is_help=True, help="CardRAG finite acquisition/OCR/embedding worker")
ocr_cache_app = typer.Typer(no_args_is_help=True, help="OCR cache inspection and explicit maintenance")
app.add_typer(ocr_cache_app, name="ocr-cache")
backup_app = typer.Typer(no_args_is_help=True, help="Incremental WebDAV backup operations")
app.add_typer(backup_app, name="backup")

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


@ocr_cache_app.command("inventory")
def ocr_cache_inventory() -> None:
    """Verify legacy OCR and current stable text without writing state or WebDAV."""

    async def inspect() -> dict[str, object]:
        client = WebDAVClient.from_env()
        try:
            return (await inventory_content_migration(client)).summary()
        finally:
            await client.close()

    _echo(asyncio.run(inspect()))


@ocr_cache_app.command("verify")
def ocr_cache_verify() -> None:
    """Verify every indexed content variant and current stable OCR binding."""

    async def verify() -> dict[str, object]:
        settings = WorkerSettings.from_env()
        client = WebDAVClient.from_env()
        try:
            store = ContentOCRVariantStore(webdav=client, state_root=Path("."))
            variants = await store.verify_all()
            _pointer, (generation,) = await _generation_chain(
                client, retain=1, pointer_path=STABLE_POINTER_PATH
            )
            by_key: dict[str, list[tuple[str, int, str]]] = {}
            for variant in variants:
                by_key.setdefault(variant.reuse_key, []).append(
                    (variant.output.sha256, variant.output.size_bytes, variant.variant_id)
                )
            covered = 0
            for document in generation.documents:
                if document.ocr is None:
                    continue
                source = OCRInput(
                    pdf_sha256=document.pdf.sha256,
                    pdf_size_bytes=document.pdf.size_bytes,
                    page_count=document.page_count,
                )
                key = content_addressed_ocr_reuse_key(source, cache_epoch=settings.ocr_cache_epoch)
                if not any(
                    sha == document.ocr.sha256
                    and size == document.ocr.size_bytes
                    and (document.ocr_variant_id is None or variant_id == document.ocr_variant_id)
                    for sha, size, variant_id in by_key.get(key, ())
                ):
                    raise ValueError("stable generation OCR is not covered by verified content variants")
                covered += 1
            return {
                "verified_variants": len(variants),
                "stable_generation_id": generation.generation_id,
                "stable_ocr_documents_covered": covered,
                "read_only": True,
            }
        finally:
            await client.close()

    _echo(asyncio.run(verify()))


@ocr_cache_app.command("migrate")
def ocr_cache_migrate(
    dry_run: bool = typer.Option(False, "--dry-run"),
    apply: bool = typer.Option(False, "--apply"),
    confirm_stable_generation: str | None = typer.Option(None, "--confirm-stable-generation"),
) -> None:
    """Plan or explicitly apply all verified immutable OCR variants."""

    if dry_run == apply:
        raise typer.BadParameter("choose exactly one of --dry-run or --apply")
    if apply and confirm_stable_generation is None:
        raise typer.BadParameter("--apply requires --confirm-stable-generation from a dry-run")
    if dry_run and confirm_stable_generation is not None:
        raise typer.BadParameter("--confirm-stable-generation is only for --apply")

    async def plan() -> dict[str, object]:
        client = WebDAVClient.from_env()
        try:
            migration = await plan_content_migration(client)
            if not apply:
                return migration.summary()
            if migration.stable_generation_id != confirm_stable_generation:
                raise ValueError("stable generation differs from the confirmed migration plan")
            settings = WorkerSettings.from_env()
            with worker_lock(settings.state_dir / "worker.lock"):
                published = await apply_content_migration(client, migration, state_root=settings.state_dir)
            return {
                **migration.summary(),
                "read_only": False,
                "migration_applied": True,
                "variants_published": published,
            }
        finally:
            await client.close()

    _echo(asyncio.run(plan()))


@ocr_cache_app.command("reprocess")
def ocr_cache_reprocess(
    document_id: list[str] = typer.Option([], "--document-id"),
    pdf_sha256: str | None = typer.Option(None, "--pdf-sha256"),
    issuer: str | None = typer.Option(None, "--issuer"),
    all_documents: bool = typer.Option(False, "--all"),
    confirm_all: bool = typer.Option(False, "--confirm-all"),
    max_documents: int = typer.Option(10, "--max-documents", min=1, max=100),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview or queue bounded OCR reprocessing for later Worker runs."""

    async def prepare() -> tuple[OCRReprocessRequest, ...]:
        client = WebDAVClient.from_env()
        try:
            _pointer, (manifest,) = await _generation_chain(
                client, retain=1, pointer_path=STABLE_POINTER_PATH
            )
            return plan_reprocess_requests(
                manifest,
                document_ids=tuple(document_id),
                pdf_sha256=pdf_sha256,
                issuer=issuer,
                all_documents=all_documents,
                confirm_all=confirm_all,
                max_documents=max_documents,
            )
        finally:
            await client.close()

    requests = asyncio.run(prepare())
    if apply:
        settings = WorkerSettings.from_env()
        queue_reprocess_requests(settings.state_dir, requests)
    _echo(
        {
            "request_count": len(requests),
            "document_count": sum(len(request.targets) for request in requests),
            "page_count": sum(target.page_count for request in requests for target in request.targets),
            "estimated_provider_document_calls": sum(len(request.targets) for request in requests),
            "queued": apply,
            "request_ids": [request.request_id for request in requests],
        }
    )


@ocr_cache_app.command("restore")
def ocr_cache_restore(
    document_id: str = typer.Option(..., "--document-id"),
    variant: str = typer.Option(..., "--variant"),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview or promote verified historical OCR bytes without calling a provider."""

    async def restore() -> dict[str, object]:
        settings = WorkerSettings.from_env()
        client = WebDAVClient.from_env()
        try:
            _pointer, (generation,) = await _generation_chain(
                client, retain=1, pointer_path=STABLE_POINTER_PATH
            )
            document = next((doc for doc in generation.documents if doc.document_id == document_id), None)
            if document is None or document.ocr is None:
                raise ValueError("document is not served in the current generation")
            source = OCRInput(
                pdf_sha256=document.pdf.sha256,
                pdf_size_bytes=document.pdf.size_bytes,
                page_count=document.page_count,
            )
            store = ContentOCRVariantStore(webdav=client, state_root=settings.state_dir)
            hits = await store.verified_variants(source=source, cache_epoch=settings.ocr_cache_epoch)
            original = next((hit for hit in hits if hit.manifest.variant_id == variant), None)
            if original is None:
                raise ValueError("requested historical variant is not verified")
            result: dict[str, object] = {
                "document_id": document_id,
                "source_variant_id": variant,
                "source_ocr_sha256": original.manifest.output.sha256,
                "current_ocr_sha256": document.ocr.sha256,
                "variant_count": len(hits),
                "applied": apply,
            }
            if apply:
                published = await store.restore_variant(
                    source=source, cache_epoch=settings.ocr_cache_epoch, variant_id=variant
                )
                result["new_variant_id"] = published.variant_id
            return result
        finally:
            await client.close()

    _echo(asyncio.run(restore()))


@ocr_cache_app.command("show")
def ocr_cache_show(document_id: str = typer.Option(..., "--document-id")) -> None:
    """Show verified OCR variant provenance for one served document."""

    async def inspect() -> dict[str, object]:
        settings = WorkerSettings.from_env()
        client = WebDAVClient.from_env()
        try:
            _pointer, (generation,) = await _generation_chain(
                client, retain=1, pointer_path=STABLE_POINTER_PATH
            )
            document = next((doc for doc in generation.documents if doc.document_id == document_id), None)
            if document is None or document.ocr is None:
                raise ValueError("document is not served in the current generation")
            source = OCRInput(
                pdf_sha256=document.pdf.sha256,
                pdf_size_bytes=document.pdf.size_bytes,
                page_count=document.page_count,
            )
            store = ContentOCRVariantStore(webdav=client, state_root=settings.state_dir)
            hits = await store.verified_variants(source=source, cache_epoch=settings.ocr_cache_epoch)
            return {
                "document_id": document_id,
                "served_ocr_sha256": document.ocr.sha256,
                "selected_variant_id": hits[-1].manifest.variant_id if hits else None,
                "variants": [
                    {
                        "variant_id": hit.manifest.variant_id,
                        "ocr_sha256": hit.manifest.output.sha256,
                        "created_at": hit.manifest.created_at,
                        "provider": hit.manifest.provenance.provider,
                        "restored_from": hit.manifest.restored_from,
                        "reprocess_request_id": hit.manifest.reprocess_request_id,
                    }
                    for hit in hits
                ],
            }
        finally:
            await client.close()

    _echo(asyncio.run(inspect()))


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
        if exc.failure.phase is not None:
            payload["phase"] = exc.failure.phase
        if exc.failure.status_code is not None:
            payload["status_code"] = exc.failure.status_code
        if exc.failure.errno is not None:
            payload["errno"] = exc.failure.errno
    _echo(payload)


def _echo_corpus_diff_error(exc: CorpusDiffError) -> None:
    payload: dict[str, Any] = {
        "candidate_count": exc.candidate_count,
        "missing_count": exc.missing_count,
        "reason": (
            f"Corpus diff check failed: {exc.missing_count} prior documents disappeared without "
            f"justification (retired={exc.retired_count}, candidates={exc.candidate_count})."
        ),
        "retired_count": exc.retired_count,
        "reason_code": exc.reason_code,
        "report": exc.report,
        "run_id": exc.run_id,
        "status": "failed",
    }
    if exc.sample:
        payload["sample"] = list(exc.sample)
    _echo(payload)


def _provider(settings: WorkerSettings, name: str, model: str) -> OCRProvider:
    resolved_model = model
    if name.strip().casefold() == "openrouter" and (
        not resolved_model or resolved_model in {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4"}
    ):
        resolved_model = settings.openrouter_ocr_model
    return make_ocr_provider(
        name,
        model=resolved_model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        codex_executable=settings.codex_executable,
        codex_auth_root=settings.codex_auth_root,
        codex_provider_id=settings.codex_model_provider,
        codex_provider_base_url=settings.codex_model_provider_base_url,
        codex_provider_env_key=settings.codex_model_provider_env_key,
        codex_provider_wire_api=settings.codex_model_provider_wire_api,
        codex_model_catalog_json=settings.codex_model_catalog_json,
        reasoning_effort=(
            settings.opencode_ocr_reasoning_effort
            if name.strip().casefold() == "opencode"
            else settings.ocr_reasoning_effort
        ),
        timeout_seconds=settings.ocr_provider_timeout_seconds,
        openrouter_fallback_model=settings.openrouter_ocr_fallback_model,
        paddleocr_pipeline_version=settings.paddleocr_pipeline_version,
        paddleocr_cache_dir=settings.paddleocr_cache_dir,
        paddleocr_pdf_dpi=settings.paddleocr_pdf_dpi,
        paddleocr_cpu_threads=settings.paddleocr_cpu_threads,
        paddleocr_timeout_seconds=settings.paddleocr_timeout_seconds,
        opencode_executable=settings.opencode_executable,
        opencode_config_path=settings.opencode_config,
        opencode_agent=settings.opencode_agent,
        opencode_api_key_env_var=settings.opencode_api_key_env_var,
    )


def _pipeline_result_payload(result: PipelineResult) -> dict[str, Any]:
    return {
        "collection_status": result.collection_status,
        "failed_issuers": list(result.failed_issuers),
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
        "retired_count": result.retired_count,
        "retirement_candidate_count": result.retirement_candidate_count,
        "v5_metrics": result.v5_metrics,
        "execution": result.execution,
        "published": result.published,
        "local_artifacts": result.local_artifacts,
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


def _guard_v114_publication_channel(settings: WorkerSettings | PublicationResumeSettings) -> None:
    if settings.channel == "candidate-v1.0.11":
        return
    if settings.channel == "stable":
        if settings.stable_publication_approved:
            return
        raise ValueError(
            "stable v1.0.14 publication requires explicit CARDRAG_STABLE_PUBLICATION_APPROVED=true approval"
        )
    raise ValueError("v1.0.14 Worker publication channel must be candidate-v1.0.11 or stable")


def _ocr_resolver(
    settings: WorkerSettings, state: WorkerState, webdav: Any, *, skip: bool = False
) -> OCRResolver | FailoverOCRResolver:
    from .partial_execution import SkippedOCRProvider

    primary = OCRResolver(
        provider=SkippedOCRProvider()
        if skip
        else _provider(settings, settings.ocr_provider, settings.ocr_model),
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
        require_cache_hit=settings.ocr_cache_require_hit,
    )
    compatible_contracts = (
        ()
        if skip
        else discover_compatible_contracts(
            state_dir=settings.state_dir,
            current_contract=primary.contract,
            compatible_models=settings.compatible_ocr_models,
        )
    )
    if compatible_contracts:
        primary.set_compatible_contracts(compatible_contracts)
        logging.getLogger("cardrag_worker.cli").info(
            "OCR discovered %d compatible contracts: %s",
            len(compatible_contracts),
            ", ".join(c.model for c in compatible_contracts),
        )
    resolver: OCRResolver | FailoverOCRResolver = primary
    if settings.ocr_fallback_provider and not skip:
        fallback_model = settings.ocr_fallback_model
        if not fallback_model:
            if settings.ocr_fallback_provider.strip().casefold() == "openrouter":
                fallback_model = settings.openrouter_ocr_model
            elif settings.ocr_fallback_provider.strip().casefold() in {"codex", "codex-exec"}:
                fallback_model = "gpt-5.6-terra"
            elif settings.ocr_fallback_provider.strip().casefold() in {
                "local-paddleocr",
                "paddleocr",
                "paddleocr-vl",
            }:
                fallback_model = "PaddleOCR-VL-1.6"
            elif settings.ocr_fallback_provider.strip().casefold() == "opencode":
                fallback_model = "alibaba-token-plan/qwen3.8-flash"
            else:
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
            require_cache_hit=settings.ocr_cache_require_hit,
            compatible_contracts=compatible_contracts,
        )
        resolver = FailoverOCRResolver(primary, fallback)
    return resolver


async def _run(resume: str | None) -> dict[str, Any]:
    started = time.monotonic()
    _configure_worker_logging()
    settings = WorkerSettings.from_env(require_providers=True, require_webdav=True)
    _guard_v114_publication_channel(settings)
    if resume:
        # An orphaned private Paddle input symlink from a force-cancelled run
        # must not permanently block resuming it through the capacity
        # preflight's symlink refusal.
        sweep_stale_paddle_input_symlinks(settings.state_dir, resume)
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
        try:
            startup_capacity = revalidate_worker_start_capacity(startup_capacity)
        except V5CapacityError:
            # If a concurrent worker holds the lock and is mutating the state
            # directory, probe the worker lock immediately so that we exit cleanly
            # with AlreadyRunning (worker_busy) rather than an unexpected failure.
            lock_file = getattr(settings, "lock_file", None)
            if lock_file is not None:
                with worker_lock(lock_file):
                    raise
            raise
    publication_transport = getattr(settings, "publication_transport", None)
    if publication_transport is None:
        publication_transport = os.environ.get("CARDRAG_PUBLICATION_TRANSPORT", "webdav")
    if publication_transport == "local":
        transport: Any = LocalServingTransport(
            getattr(settings, "serving_dir", Path("/var/lib/cardrag-serving")),
            channel=getattr(settings, "channel", "stable"),
        )
    else:
        transport = WebDAVClient.from_env(
            stable_publication_approved=settings.stable_publication_approved,
            upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
        )
    try:
        if document_aggregation is not None:
            # No provider/tokenizer call or candidate-state mutation is allowed
            # until GET-only proof identifies the evaluated M0 or its sealed M1.
            await validate_document_aggregation_head(transport, document_aggregation)
            settings.state_dir.mkdir(parents=True, exist_ok=True)
        # A lock-rejected process must never open a live writer's SQLite database.
        # The 2026-10-03 production state-loss incident happened because the daily
        # timer's container ran this startup preflight (which opened the state DB),
        # only *then* lost the lock inside the pipeline and closed that connection;
        # the concurrent open/close clobbered page 1 of the WAL database.  Probe the
        # worker lock here, immediately before SQLite touches the path, so a busy
        # second process exits before opening anything.  The authoritative
        # acquisition stays in WorkerPipeline._run_locked, so the winner is unchanged.
        worker_lock_file = getattr(
            settings, "lock_file", getattr(settings, "state_dir", Path(".")) / "worker.lock"
        )
        with worker_lock(worker_lock_file):
            # Narrow the descriptor-walk-to-use window for both M0 and M1 under lock.
            startup_capacity = revalidate_worker_start_capacity(startup_capacity)
            with WorkerState(
                settings.state_database,
                sqlite_cache_mib=settings.sqlite_cache_mib,
                sqlite_mmap_mib=settings.sqlite_mmap_mib,
            ) as state:
                if isinstance(transport, WebDAVClient):
                    transport.configure_verification(state, settings.webdav_verification)
                resolver = _ocr_resolver(
                    settings, state, transport if isinstance(transport, WebDAVClient) else None
                )
                logging.getLogger("cardrag_worker.cli").info(
                    "Remote OCR cache access mode=%s require_hit=%s",
                    settings.ocr_cache_mode,
                    settings.ocr_cache_require_hit,
                )
                embeddings = await _qwen_embedding_provider(settings)
                logging.getLogger("cardrag_worker.cli").info(
                    "Worker startup completed elapsed_seconds=%.3f", time.monotonic() - started
                )
                result = await WorkerPipeline(
                    state=state,
                    state_dir=settings.state_dir,
                    adapters=enabled_adapters(),
                    ocr=resolver,  # type: ignore[arg-type]
                    embeddings=embeddings,
                    webdav=transport,
                    issuer_discovery_concurrency=settings.issuer_discovery_concurrency,
                    issuer_discovery_timeout_seconds=settings.issuer_discovery_timeout_seconds,
                    pdf_concurrency=settings.pdf_concurrency,
                    pdf_concurrency_per_issuer=settings.pdf_concurrency_per_issuer,
                    local_processing_workers=settings.local_processing_workers,
                    maximum_attempts=settings.stage_max_attempts,
                    retry_cap_seconds=settings.retry_cap_seconds,
                    collect_remote_garbage=settings.collect_remote_garbage,
                    stable_publication_approved=settings.stable_publication_approved,
                    ocr_cache_publication_approved=settings.ocr_cache_publication_approved,
                    remote_gc_approved=settings.remote_gc_approved,
                    retained_generations=settings.retain_generations,
                    retained_incomplete_runs=settings.retained_incomplete_runs,
                    retirement_grace_runs=settings.retirement_grace_runs,
                    retirement_grace_days=settings.retirement_grace_days,
                    retirement_max_per_run=settings.retirement_max_per_run,
                    garbage_grace_days=settings.garbage_grace_days,
                    pdf_cache_refresh_hours=settings.pdf_cache_refresh_hours,
                    pdf_cache_force_revalidate=settings.pdf_cache_force_revalidate,
                    document_aggregation=document_aggregation,
                    capacity_policy_v5=V5CapacityPolicy(
                        maximum_state_bytes=settings.maximum_state_bytes,
                        reserved_free_space_bytes=settings.reserved_free_space_bytes,
                        maximum_vector_sidecar_bytes=settings.maximum_vector_sidecar_bytes,
                        maximum_serving_database_bytes=settings.maximum_serving_database_bytes,
                    ),
                    lock_held=True,
                ).run(resume_run_id=resume)

                backup_info: dict[str, Any] = {
                    "backup_status": "disabled",
                    "backup_bytes_uploaded": 0,
                    "backup_requests": 0,
                    "pending_ocr_count": 0,
                    "pending_bytes": 0,
                    "oldest_pending_at": None,
                    "last_backup_at": None,
                    "runs_since_last_backup": 0,
                }
                backup_mode = getattr(settings, "backup_mode", "disabled")
                if backup_mode != "disabled":
                    try:
                        backup_ledger = BackupLedger(settings.state_dir / "backup-ledger.sqlite3")
                        if result.status in {"succeeded", "no_change"}:
                            backup_ledger.record_run_success(result.run_id, settings.state_dir, settings)
                            status_before = backup_ledger.get_status(settings)
                            if status_before["should_trigger"]:
                                flush_res = await backup_ledger.flush(
                                    settings,
                                    timeout_seconds=getattr(settings, "backup_inline_budget_seconds", 300.0),
                                )
                                backup_info["backup_status"] = flush_res.get("status", "failed")
                                backup_info["backup_bytes_uploaded"] = flush_res.get("uploaded_bytes", 0)
                                backup_info["backup_requests"] = flush_res.get("requests", 0)
                            else:
                                backup_info["backup_status"] = "deferred"
                        status_dict = backup_ledger.get_status(settings)
                        backup_info.update(
                            {
                                "pending_ocr_count": status_dict.get(
                                    "pending_ocr_count", status_dict["pending_count"]
                                ),
                                "pending_bytes": status_dict["pending_bytes"],
                                "oldest_pending_at": status_dict["oldest_pending_at"],
                                "last_backup_at": status_dict["last_backup_at"],
                                "runs_since_last_backup": status_dict["runs_since_last_backup"],
                            }
                        )
                    except Exception as backup_exc:
                        logging.getLogger("cardrag_worker.cli").warning(
                            "Backup ledger operation failed (isolated from run success): %s", backup_exc
                        )
                        backup_info["backup_status"] = "error"

                payload = _pipeline_result_payload(result)
                payload.update(
                    {
                        "publication_transport": publication_transport,
                        "local_generation_published": (
                            result.published if publication_transport == "local" else False
                        ),
                        **backup_info,
                    }
                )
                return payload
    finally:
        await transport.close()
        logging.getLogger("cardrag_worker.cli").info(
            "Worker execution finished elapsed_seconds=%.3f", time.monotonic() - started
        )


async def _operation_with_signal_shutdown(
    operation: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    *,
    task_name: str,
) -> dict[str, Any]:
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

    task = asyncio.create_task(operation(), name=task_name)
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


async def _run_with_signal_shutdown(resume: str | None) -> dict[str, Any]:
    return await _operation_with_signal_shutdown(
        lambda: _run(resume),
        task_name="cardrag-worker-pipeline",
    )


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
    skip_stage: list[str] | None = typer.Option(
        None, "--skip-stage", help="Repeat: pdf, ocr, structure, embedding, export, webdav."
    ),
    skip_pdf: bool = typer.Option(False, "--skip-pdf"),
    skip_ocr: bool = typer.Option(False, "--skip-ocr"),
    skip_embedding: bool = typer.Option(False, "--skip-embedding"),
    reuse_from_run: str | None = typer.Option(None, "--reuse-from-run"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    publish_channel: str = typer.Option("candidate-009", "--publish-channel"),
) -> None:
    from .partial_cli import run_partial
    from .partial_execution import ExecutionPlan, PartialExecutionError

    try:
        skipped = set(skip_stage or [])
        skipped.update(
            s
            for s, enabled in (("pdf", skip_pdf), ("ocr", skip_ocr), ("embedding", skip_embedding))
            if enabled
        )
        persisted = None
        if resume:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", resume):
                raise ValueError("invalid resume run ID")
            resume_settings = WorkerSettings.from_env()
            resume_plan_path = resume_settings.state_dir / "runs" / resume / "execution-plan.json"
            if resume_plan_path.is_file():
                persisted = json.loads(resume_plan_path.read_bytes())
                if not skipped and reuse_from_run is None:
                    skipped = set(persisted["skipped_stages"])
                    reuse_from_run = persisted["source_run_id"]
                    publish_channel = persisted.get("channel", "candidate-009")
        if skipped or reuse_from_run or dry_run or persisted:
            if resume and reuse_from_run and resume != reuse_from_run:
                # A resumed partial run uses its persisted original source below.
                settings = WorkerSettings.from_env()
                path = settings.state_dir / "runs" / resume / "execution-plan.json"
                if not path.is_file() or json.loads(path.read_bytes()).get("source_run_id") != reuse_from_run:
                    raise ValueError("resume source differs from the persisted execution plan")
            plan = ExecutionPlan(frozenset(skipped), reuse_from_run or resume, dry_run, publish_channel)
            _echo(
                asyncio.run(
                    _operation_with_signal_shutdown(
                        lambda: run_partial(plan, resume), task_name="worker-partial"
                    )
                )
            )
        else:
            _echo(asyncio.run(_run_with_signal_shutdown(resume)))
    except PartialExecutionError as exc:
        _echo(
            {
                "status": "blocked",
                "reason_code": exc.reason_code,
                "stage": exc.stage,
                "document_id": exc.document_id,
            }
        )
        raise typer.Exit(code=1) from None
    except WorkerSignalShutdown as exc:
        raise typer.Exit(code=_echo_signal_shutdown(exc)) from None
    except OCRDocumentFailuresError as exc:
        _echo_ocr_failures(exc)
        raise typer.Exit(code=1) from None
    except OCRSystemicFailureError as exc:
        _echo_ocr_systemic_failure(exc)
        raise typer.Exit(code=1) from None
    except CorpusDiffError as exc:
        _echo_corpus_diff_error(exc)
        raise typer.Exit(code=1) from None
    except (CorpusBaselineError, RetirementError) as exc:
        _echo(
            {
                "reason": "Corpus gate control state failed validation; refusing to compare or retire.",
                "reason_code": exc.code,
                "status": "blocked",
            }
        )
        raise typer.Exit(code=1) from None
    except AlreadyRunning:
        _echo_worker_busy()
    except WorkerUnexpectedFailureError as exc:
        _echo_worker_unexpected_failure(exc)
        raise typer.Exit(code=1) from None
    except Exception:
        _echo_worker_unexpected_failure()
        raise typer.Exit(code=1) from None


@app.command("paddleocr-prefetch")
def paddleocr_prefetch_command() -> None:
    """Download and initialize the configured local PaddleOCR-VL models."""

    try:
        settings = WorkerSettings.from_env()
        provider = _provider(
            settings,
            "local-paddleocr",
            {
                "v1": "PaddleOCR-VL",
                "v1.5": "PaddleOCR-VL-1.5",
                "v1.6": "PaddleOCR-VL-1.6",
            }.get(settings.paddleocr_pipeline_version, ""),
        )
        if not isinstance(provider, PaddleOCRVLProvider):
            raise RuntimeError("PaddleOCR provider construction failed")
        asyncio.run(provider.prefetch_models())
        _echo(
            {
                "cache_dir": settings.paddleocr_cache_dir,
                "model": provider.model,
                "pipeline_version": provider.pipeline_version,
                "status": "ready",
            }
        )
    except Exception:
        _echo(
            {
                "reason": "Local PaddleOCR-VL model preparation failed.",
                "reason_code": "paddleocr_prefetch_failed",
                "status": "failed",
            }
        )
        raise typer.Exit(code=1) from None


@app.command("resume")
def resume_command(run_id: str = typer.Argument(..., help="Failed finite run ID.")) -> None:
    # Use the same persisted-plan dispatch as `run --resume`; never rediscover a frozen run.
    run_command(
        resume=run_id,
        skip_stage=None,
        skip_pdf=False,
        skip_ocr=False,
        skip_embedding=False,
        reuse_from_run=None,
        dry_run=False,
        publish_channel="candidate-009",
    )


def _require_existing_publication_state(settings: PublicationResumeSettings) -> None:
    """Reject missing or replaceable state before the read/write state open."""

    try:
        root = settings.state_dir.lstat()
        database = settings.state_database.lstat()
    except OSError:
        raise RuntimeError("sealed publication resume state is unavailable") from None
    if (
        stat.S_ISLNK(root.st_mode)
        or not stat.S_ISDIR(root.st_mode)
        or stat.S_ISLNK(database.st_mode)
        or not stat.S_ISREG(database.st_mode)
        or database.st_nlink != 1
    ):
        raise RuntimeError("sealed publication resume state is unavailable or unsafe")


async def _resume_publication(run_id: str) -> dict[str, Any]:
    """Publish one exact local seal without provider/discovery construction."""

    _configure_worker_logging()
    settings = PublicationResumeSettings.from_env()
    _guard_v114_publication_channel(settings)
    _require_existing_publication_state(settings)
    startup_capacity = preflight_worker_start_capacity(
        settings.state_dir,
        minimum_free_bytes=settings.minimum_start_free_bytes,
    )
    document_aggregation = None
    if settings.document_aggregation_profile_path is not None:
        expected_artifact_sha256 = settings.document_aggregation_profile_artifact_sha256
        if expected_artifact_sha256 is None:
            raise ValueError("document aggregation profile artifact SHA-256 is absent")
        document_aggregation = load_verified_aggregation_profile_v5(
            settings.document_aggregation_profile_path,
            expected_artifact_sha256=expected_artifact_sha256,
        )
    if getattr(settings, "publication_transport", "webdav") == "local":
        transport: Any = LocalServingTransport(
            getattr(settings, "serving_dir", Path("/var/lib/cardrag-serving")),
            channel=settings.channel,
        )
    else:
        transport = WebDAVClient.from_env(
            stable_publication_approved=settings.stable_publication_approved,
            upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
        )
    try:
        revalidate_worker_start_capacity(startup_capacity)
        if isinstance(transport, WebDAVClient):
            transport.verification_settings = settings.webdav_verification
        result = await resume_sealed_publication(
            run_id=run_id,
            state_dir=settings.state_dir,
            webdav=transport,
            sqlite_cache_mib=settings.sqlite_cache_mib,
            sqlite_mmap_mib=settings.sqlite_mmap_mib,
            stable_publication_approved=settings.stable_publication_approved,
            document_aggregation=document_aggregation,
        )
        return _pipeline_result_payload(result)
    finally:
        await transport.close()


async def _resume_publication_with_signal_shutdown(run_id: str) -> dict[str, Any]:
    return await _operation_with_signal_shutdown(
        lambda: _resume_publication(run_id),
        task_name="cardrag-worker-sealed-publication",
    )


@app.command("resume-publication")
def resume_publication_command(
    run_id: str = typer.Argument(..., help="Failed run ID with an exact local publication seal."),
) -> None:
    """Resume only sealed WebDAV publication; never run providers or discovery."""

    from .partial_execution import PartialExecutionError

    try:
        _echo(asyncio.run(_resume_publication_with_signal_shutdown(run_id)))
    except PartialExecutionError as exc:
        _echo(
            {
                "status": "blocked",
                "reason_code": exc.reason_code,
                "stage": exc.stage,
                "reason": "Partial runs require run --resume with their persisted execution plan.",
            }
        )
        raise typer.Exit(code=1) from None
    except WorkerSignalShutdown as exc:
        raise typer.Exit(code=_echo_signal_shutdown(exc)) from None
    except AlreadyRunning:
        _echo_worker_busy()
        raise typer.Exit(code=1) from None
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
        with (
            worker_lock(settings.lock_file),
            WorkerState(
                settings.state_database,
                sqlite_cache_mib=settings.sqlite_cache_mib,
                sqlite_mmap_mib=settings.sqlite_mmap_mib,
            ) as state,
        ):
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
        with (
            worker_lock(settings.lock_file),
            WorkerState(
                settings.state_database,
                sqlite_cache_mib=settings.sqlite_cache_mib,
                sqlite_mmap_mib=settings.sqlite_mmap_mib,
            ) as state,
        ):
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


def _seed_v122_state(
    source_state_root: Path,
    generation_id: str,
    *,
    apply: bool,
    expected_documents: int | None = None,
) -> dict[str, Any]:
    plan = build_state_seed_v122_plan(
        source_state_root,
        generation_id=generation_id,
        expected_documents=expected_documents,
    )
    if not apply:
        return plan.report(applied=False)
    settings = WorkerSettings.from_env()
    if settings.channel != "candidate-v1.0.11":
        raise StateSeedError("candidate_channel_required")
    if paths_overlap(plan.source_root, settings.state_dir):
        raise StateSeedError("source_destination_overlap")
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with (
            worker_lock(settings.lock_file),
            WorkerState(
                settings.state_database,
                sqlite_cache_mib=settings.sqlite_cache_mib,
                sqlite_mmap_mib=settings.sqlite_mmap_mib,
            ) as state,
        ):
            first = apply_state_seed_v122(plan, state, settings.state_dir)
            second = apply_state_seed_v122(plan, state, settings.state_dir)
    except AlreadyRunning as exc:
        raise StateSeedError("destination_busy") from exc
    if (
        second["imported_pdf_objects"] != 0
        or second["imported_revisions"] != 0
        or second["imported_ocr_files"] != 0
    ):
        raise StateSeedError("idempotence_verification_failed")
    return {
        **first,
        "idempotence_imported_pdf_objects": second["imported_pdf_objects"],
        "idempotence_imported_revisions": second["imported_revisions"],
        "idempotence_imported_ocr_files": second["imported_ocr_files"],
        "idempotence_verified": True,
    }


def _seed_embedding_cache_v122(
    source_state_root: Path,
    *,
    apply: bool,
    expected_rows: int | None = None,
) -> dict[str, Any]:
    plan = build_embedding_cache_seed_v122_plan(
        source_state_root,
        expected_rows=expected_rows,
    )
    if not apply:
        return plan.report(applied=False)
    settings = WorkerSettings.from_env()
    if settings.channel != "candidate-v1.0.11":
        raise StateSeedError("candidate_channel_required")
    if paths_overlap(plan.source_root, settings.state_dir):
        raise StateSeedError("source_destination_overlap")
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with (
            worker_lock(settings.lock_file),
            WorkerState(
                settings.state_database,
                sqlite_cache_mib=settings.sqlite_cache_mib,
                sqlite_mmap_mib=settings.sqlite_mmap_mib,
            ) as state,
        ):
            first = apply_embedding_cache_seed_v122(plan, state, settings.state_dir)
            second = apply_embedding_cache_seed_v122(plan, state, settings.state_dir)
    except AlreadyRunning as exc:
        raise StateSeedError("destination_busy") from exc
    if second["imported_rows"] != 0:
        raise StateSeedError("idempotence_verification_failed")
    return {
        **first,
        "idempotence_imported_rows": second["imported_rows"],
        "idempotence_verified": True,
    }


@app.command("seed-embedding-cache-v122")
def seed_embedding_cache_v122_command(
    source_state_root: Path = typer.Argument(
        ...,
        help="Absolute read-only v1.0.28 Worker state root.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply the embedding-cache seed to the destination; default is dry-run.",
    ),
    expected_rows: int | None = typer.Option(
        None,
        "--expected-rows",
        help="Optional expected embedding_cache_v5 row count to strictly enforce.",
    ),
) -> None:
    """Audit and idempotently import v1.0.28 embedding_cache_v5 rows into destination."""
    try:
        _echo(
            _seed_embedding_cache_v122(
                source_state_root,
                apply=apply,
                expected_rows=expected_rows,
            )
        )
    except StateSeedError as exc:
        _echo(
            {
                "applied": False,
                "dry_run": not apply,
                "reason_code": exc.code,
                "schema_version": "cardrag.embedding-seed-report.v1",
                "status": "blocked",
            }
        )
        raise typer.Exit(code=1) from None


@app.command("seed-state-v122")
def seed_state_v122_command(
    source_state_root: Path = typer.Argument(
        ...,
        help="Absolute read-only v1.0.28 Worker state root.",
    ),
    generation_id: str = typer.Option(
        ...,
        "--generation-id",
        help="Explicit generation ID to import (e.g. g-bd9a4c513041462886d347af-9b84c2e38c14).",
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply state seed to destination; default is dry-run."),
    expected_documents: int | None = typer.Option(
        None,
        "--expected-documents",
        help="Optional expected manifest document count to strictly enforce.",
    ),
) -> None:
    """Audit and idempotently import v1.0.28 lineage and OCR cache into destination."""
    try:
        _echo(
            _seed_v122_state(
                source_state_root,
                generation_id,
                apply=apply,
                expected_documents=expected_documents,
            )
        )
    except StateSeedError as exc:
        _echo(
            {
                "applied": False,
                "dry_run": not apply,
                "reason_code": exc.code,
                "schema_version": "cardrag.state-seed-report.v1",
                "status": "blocked",
            }
        )
        raise typer.Exit(code=1) from None


async def _restore_ocr_seed(
    settings: WorkerSettings,
    *,
    generation_id: str | None,
    apply: bool,
    concurrency: int,
) -> dict[str, Any]:
    WorkerSettings.from_env(require_webdav=True)
    client = WebDAVClient.from_env()
    try:
        if apply:
            settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with worker_lock(settings.lock_file):
                result = await restore_ocr_seed_from_generation(
                    webdav=client,
                    destination=settings.state_dir,
                    generation_id=generation_id,
                    pointer_path=client.pointer_path,
                    dry_run=False,
                    concurrency=concurrency,
                )
        else:
            result = await restore_ocr_seed_from_generation(
                webdav=client,
                destination=settings.state_dir,
                generation_id=generation_id,
                pointer_path=client.pointer_path,
                dry_run=True,
                concurrency=concurrency,
            )
        return result.to_dict()
    finally:
        await client.close()


@app.command("restore-ocr-seed")
def restore_ocr_seed_command(
    generation_id: str | None = typer.Option(
        None,
        "--generation-id",
        help="Optional generation ID to restore from; defaults to current channel pointer.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Download OCR files and commit seed ledger; default is dry-run verification.",
    ),
    concurrency: int = typer.Option(
        16,
        "--concurrency",
        min=1,
        max=64,
        help="Maximum concurrent WebDAV downloads.",
    ),
) -> None:
    """Restore and seed OCR markdown results directly from WebDAV generation manifest into empty state."""
    settings = WorkerSettings.from_env()
    try:
        result = asyncio.run(
            _restore_ocr_seed(
                settings=settings,
                generation_id=generation_id,
                apply=apply,
                concurrency=concurrency,
            )
        )
        _echo(result)
    except OCRRecoveryError as exc:
        _echo(
            {
                "applied": False,
                "dry_run": not apply,
                "reason_code": exc.code,
                "detail": str(exc),
                "schema_version": "cardrag.ocr-recovery-report.v1",
                "status": "failed",
            }
        )
        raise typer.Exit(code=1) from None
    except AlreadyRunning:
        _echo(
            {
                "applied": False,
                "dry_run": not apply,
                "reason_code": "destination_busy",
                "schema_version": "cardrag.ocr-recovery-report.v1",
                "status": "failed",
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
    client = WebDAVClient.from_env(
        stable_publication_approved=settings.stable_publication_approved,
        upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
    )
    try:
        with (
            worker_lock(settings.lock_file),
            WorkerState(
                settings.state_database,
                sqlite_cache_mib=settings.sqlite_cache_mib,
                sqlite_mmap_mib=settings.sqlite_mmap_mib,
            ) as state,
        ):
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
    if settings.channel == "candidate-v1.0.11":
        if not settings.collect_remote_garbage or not settings.remote_gc_approved:
            raise ValueError("remote GC apply requires collection enabled and separate remote-GC approval")
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


@backup_app.command("status")
def backup_status_command() -> None:
    settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
    ledger = BackupLedger(settings.state_dir / "backup-ledger.sqlite3")
    _echo(ledger.get_status(settings))


@backup_app.command("flush")
def backup_flush_command(
    force: bool = typer.Option(False, "--force", help="Force flush ignoring threshold conditions"),
) -> None:
    settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
    ledger = BackupLedger(settings.state_dir / "backup-ledger.sqlite3")
    res = asyncio.run(ledger.flush(settings, force=force))
    _echo(res)


@backup_app.command("audit")
def backup_audit_command(
    full: bool = typer.Option(False, "--full", help="Perform full audit rather than sample"),
) -> None:
    settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
    ledger = BackupLedger(settings.state_dir / "backup-ledger.sqlite3")
    res = asyncio.run(ledger.audit(settings, full=full))
    _echo(res)


@backup_app.command("restore")
def backup_restore_command(
    target_dir: str | None = typer.Option(None, "--target-dir", help="Target directory to restore OCR cache"),
) -> None:
    settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
    dest = Path(target_dir) if target_dir else settings.state_dir / "cache" / "ocr"
    ledger = BackupLedger(settings.state_dir / "backup-ledger.sqlite3")
    res = asyncio.run(ledger.restore(settings, target_dir=dest))
    _echo(res)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
