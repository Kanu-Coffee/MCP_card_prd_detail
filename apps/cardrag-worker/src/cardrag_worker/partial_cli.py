"""CLI wiring for explicit partial runs, with lazy provider initialization."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from .partial_execution import (
    ExecutionPlan,
    LocalWebDAV,
    PartialExecutionError,
    ReuseSource,
)


def resolve_partial_execution_mode(
    selected: tuple[str, ...] | None,
    saved_scope: dict[str, Any] | None,
) -> str:
    """Pick the issuer execution mode for a partial (collect/process) run.

    A manual scoped partial run is ``selected``.  Resuming an already frozen
    run restores the historical frozen scope — runs created before the
    mode-propagation fix stored ``all`` — so the display correction never
    blocks recovery of those runs.
    """

    if saved_scope is not None:
        mode = saved_scope.get("mode")
        if mode in {"all", "selected", "round_robin"}:
            return str(mode)
        raise PartialExecutionError("issuer_scope_changed", "scope")
    return "selected" if selected else "all"


async def run_partial(plan: ExecutionPlan, resume: str | None) -> dict[str, Any]:

    from .aggregation_profile_v5 import load_verified_aggregation_profile_v5
    from .capacity_v5 import V5CapacityPolicy, preflight_worker_start_capacity
    from .cli import _ocr_resolver, _pipeline_result_payload, _qwen_embedding_provider
    from .embedding_v5 import OpenRouterQwenEmbeddingProviderV5, QwenEmbeddingProfileV5
    from .issuers import enabled_adapters
    from .local_publisher import LocalServingTransport
    from .ocr_requests import load_next_reprocess_request
    from .pipeline import WorkerPipeline, validate_document_aggregation_head
    from .settings import WorkerSettings
    from .state import WorkerState, worker_lock
    from .tokenizer_v5 import QwenTokenizerV5
    from .webdav import WebDAVClient

    settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
    with worker_lock(settings.lock_file):
        from .acquisition import AcquisitionSource
        from .issuers.registry import enabled_issuer_codes

        pool = enabled_issuer_codes()
        if plan.selected_issuers is not None and (
            not plan.selected_issuers or not set(plan.selected_issuers) <= set(pool)
        ):
            raise ValueError("phase issuer scope must be inside the configured pool")
        source_type = AcquisitionSource if plan.phase == "process" else ReuseSource
        source = (
            source_type(settings.state_dir, plan.source_run_id, settings.state_database)
            if plan.source_run_id
            else None
        )
        if isinstance(source, AcquisitionSource):
            requested = plan.selected_issuers or source.selected
            if tuple(requested) != source.selected:
                raise PartialExecutionError("acquisition_scope_changed", "scope")
            plan.selected_issuers = requested
        if resume:
            import json

            path = settings.state_dir / "runs" / resume / "execution-plan.json"
            if not path.is_file():
                raise PartialExecutionError("skip_artifact_incompatible", "plan")
            saved_plan = json.loads(path.read_bytes())
            if plan.selected_issuers is None and saved_plan.get("selected_issuers"):
                plan.selected_issuers = tuple(saved_plan["selected_issuers"])
            if saved_plan != plan.identity():
                raise PartialExecutionError("skip_artifact_incompatible", "plan")
        saved_scope = None
        if resume:
            from .partial_execution import read_json

            frozen_path = settings.state_dir / "runs" / resume / "issuer-scope.json"
            if frozen_path.is_file():
                saved_scope = read_json(settings.state_dir, frozen_path, "scope")
        partial_execution_mode = resolve_partial_execution_mode(plan.selected_issuers, saved_scope)
        preflight = source.preflight(plan) if source is not None else plan.payload()
        if plan.skips("ocr") and load_next_reprocess_request(settings.state_dir) is not None:
            raise PartialExecutionError("skip_artifact_incompatible", "ocr")
        if (
            isinstance(source, ReuseSource)
            and plan.skips("embedding")
            and (
                source.profile["provider_id"],
                source.profile["model"],
                source.profile["dimension"],
                source.profile["maximum_tokens"],
            )
            != (
                settings.embedding_provider_id,
                settings.embedding_model,
                settings.embedding_dimension,
                settings.embedding_maximum_tokens,
            )
        ):
            raise PartialExecutionError("skip_artifact_incompatible", "embedding")
        if plan.dry_run:
            return {**preflight, "status": "dry_run", "published": False, "state_changed": False}
        preflight_worker_start_capacity(
            settings.state_dir, minimum_free_bytes=settings.minimum_start_free_bytes
        )
        document_aggregation = None
        if settings.document_aggregation_profile_path is not None:
            digest = settings.document_aggregation_profile_artifact_sha256
            if digest is None:
                raise PartialExecutionError("skip_artifact_incompatible", "export")
            document_aggregation = load_verified_aggregation_profile_v5(
                settings.document_aggregation_profile_path, expected_artifact_sha256=digest
            )
        webdav: Any
        if plan.skips("webdav"):
            webdav = LocalWebDAV()
        elif getattr(settings, "publication_transport", "webdav") == "local":
            webdav = LocalServingTransport(
                getattr(settings, "serving_dir", Path("/var/lib/cardrag-serving")),
                channel=plan.channel,
            )
        else:
            configured = WebDAVClient.from_env(
                stable_publication_approved=settings.stable_publication_approved
            )
            webdav = WebDAVClient(
                configured.core,
                channel=plan.channel,
                stable_publication_approved=settings.stable_publication_approved,
                upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
            )
        try:
            if document_aggregation is not None and not plan.skips("webdav"):
                await validate_document_aggregation_head(webdav, document_aggregation)
            if (
                plan.channel == "stable"
                and source is not None
                and not isinstance(source, AcquisitionSource)
                and not plan.skips("webdav")
            ):
                current = await webdav.validated_current_generation()
                if current is None or current.generation_id != source.manifest.generation_id:
                    from .partial_execution import read_json

                    own_seal = (
                        settings.state_dir / "runs" / resume / "sealed/publish.json" if resume else None
                    )
                    own_generation = (
                        read_json(settings.state_dir, own_seal, "webdav")
                        .get("manifest", {})
                        .get("generation_id")
                        if own_seal is not None and own_seal.is_file()
                        else None
                    )
                    # Exact seal/remote reconciliation occurs before any publication in the pipeline.
                    if current is None or current.generation_id != own_generation:
                        raise PartialExecutionError("skip_source_unavailable", "webdav")
            with WorkerState(
                settings.state_database,
                sqlite_cache_mib=settings.sqlite_cache_mib,
                sqlite_mmap_mib=settings.sqlite_mmap_mib,
            ) as state:
                embeddings: OpenRouterQwenEmbeddingProviderV5
                if plan.phase == "collect":
                    from types import SimpleNamespace

                    from .embedding_v5 import OpenRouterEndpointMetadata
                    from .tokenizer_v5 import QWEN_TOKENIZER_REVISION, QWEN_TOKENIZER_SHA256

                    # Acquisition neither verifies nor uses provider semantics.
                    # Prefer the served profile only to validate preservation.
                    profile = QwenEmbeddingProfileV5.from_endpoint(
                        OpenRouterEndpointMetadata(
                            model=settings.embedding_model,
                            provider_id=settings.embedding_provider_id,
                            provider_name="collection-only",
                            endpoint_name="collection-only",
                            quantization="BF16",
                            maximum_tokens=settings.embedding_maximum_tokens,
                            supported_parameters=("encoding_format",),
                            metadata_sha256="0" * 64,
                        )
                    )
                    for prior_id in state.retained_publication_run_ids(limit=settings.retain_generations):
                        prior = ReuseSource(settings.state_dir, prior_id, settings.state_database)
                        profile = QwenEmbeddingProfileV5(
                            **prior.profile,
                            endpoint_name=prior.metadata["embedding_endpoint_name"],
                            endpoint_metadata_sha256=prior.metadata["embedding_endpoint_metadata_sha256"],
                        )
                        break

                    class UnusedTokenizer:
                        asset_sha256 = QWEN_TOKENIZER_SHA256
                        revision = QWEN_TOKENIZER_REVISION

                        def __call__(self, text: str) -> int:
                            raise PartialExecutionError("collection_provider_forbidden", "embedding")

                    embeddings = OpenRouterQwenEmbeddingProviderV5(
                        api_key="unused-collection", profile=profile, token_counter=UnusedTokenizer()
                    )
                elif isinstance(source, AcquisitionSource):
                    embeddings = await _qwen_embedding_provider(settings)
                elif source is not None:
                    profile = QwenEmbeddingProfileV5(
                        **source.profile,
                        endpoint_name=source.metadata["embedding_endpoint_name"],
                        endpoint_metadata_sha256=source.metadata["embedding_endpoint_metadata_sha256"],
                    )
                    matches = (
                        profile.provider_id,
                        profile.model,
                        profile.dimension,
                        profile.maximum_tokens,
                    ) == (
                        settings.embedding_provider_id,
                        settings.embedding_model,
                        settings.embedding_dimension,
                        settings.embedding_maximum_tokens,
                    )
                    if not matches and plan.skips("embedding"):
                        raise PartialExecutionError("skip_artifact_incompatible", "embedding")
                    if matches:
                        tokenizer = QwenTokenizerV5.from_file(settings.embedding_tokenizer_path)

                        class LazyEmbeddingProvider(OpenRouterQwenEmbeddingProviderV5):
                            live: OpenRouterQwenEmbeddingProviderV5 | None = None

                            async def embed_documents(self, texts: Any) -> list[list[float]]:
                                if plan.skips("embedding"):
                                    raise PartialExecutionError("skip_artifact_missing", "embedding")
                                if self.live is None:
                                    self.live = await _qwen_embedding_provider(settings)
                                    if self.live.profile != self.profile:
                                        raise PartialExecutionError("skip_artifact_incompatible", "embedding")
                                values = await self.live.embed_documents(texts)
                                self.wire_attempt_count = self.live.wire_attempt_count
                                return values

                        embeddings = LazyEmbeddingProvider(
                            api_key="unused-until-cache-miss", profile=profile, token_counter=tokenizer
                        )
                    else:
                        embeddings = await _qwen_embedding_provider(settings)
                else:
                    embeddings = await _qwen_embedding_provider(settings)
                ocr: Any
                if plan.phase == "collect":
                    ocr = SimpleNamespace(
                        contract=SimpleNamespace(cache_epoch=settings.ocr_cache_epoch),
                        cache_epoch=settings.ocr_cache_epoch,
                        cache_mode=settings.ocr_cache_mode,
                    )
                else:
                    ocr = _ocr_resolver(
                        settings, state, None if plan.skips("webdav") else webdav, skip=plan.skips("ocr")
                    )
                if not plan.skips("webdav") and hasattr(webdav, "configure_verification"):
                    webdav.configure_verification(state, settings.webdav_verification)
                pipeline = WorkerPipeline(
                    state=state,
                    state_dir=settings.state_dir,
                    adapters=enabled_adapters(),
                    selected_issuers=plan.selected_issuers,
                    issuer_execution_mode=partial_execution_mode,
                    ocr=ocr,
                    embeddings=embeddings,
                    webdav=webdav,
                    settings=settings,
                    lock_held=True,
                    execution_plan=plan,
                    reuse_source=cast(ReuseSource | None, source),
                    stable_publication_approved=settings.stable_publication_approved,
                    ocr_cache_publication_approved=settings.ocr_cache_publication_approved
                    or plan.skips("webdav"),
                    collect_remote_garbage=False,
                    maximum_attempts=settings.stage_max_attempts,
                    retry_cap_seconds=settings.retry_cap_seconds,
                    pdf_concurrency=settings.pdf_concurrency,
                    pdf_concurrency_per_issuer=settings.pdf_concurrency_per_issuer,
                    issuer_discovery_concurrency=settings.issuer_discovery_concurrency,
                    issuer_discovery_timeout_seconds=settings.issuer_discovery_timeout_seconds,
                    local_processing_workers=settings.local_processing_workers,
                    retained_generations=settings.retain_generations,
                    retained_incomplete_runs=settings.retained_incomplete_runs,
                    pdf_cache_refresh_hours=settings.pdf_cache_refresh_hours,
                    pdf_cache_force_revalidate=settings.pdf_cache_force_revalidate,
                    retirement_grace_runs=settings.retirement_grace_runs,
                    retirement_grace_days=settings.retirement_grace_days,
                    retirement_max_per_run=settings.retirement_max_per_run,
                    garbage_grace_days=settings.garbage_grace_days,
                    document_aggregation=document_aggregation,
                    capacity_policy_v5=V5CapacityPolicy(
                        maximum_state_bytes=settings.maximum_state_bytes,
                        reserved_free_space_bytes=settings.reserved_free_space_bytes,
                        maximum_vector_sidecar_bytes=settings.maximum_vector_sidecar_bytes,
                        maximum_serving_database_bytes=settings.maximum_serving_database_bytes,
                    ),
                )
                result = await pipeline.run(resume_run_id=resume)
                if (
                    isinstance(source, AcquisitionSource)
                    and result.status == "succeeded"
                    and result.published
                ):
                    from .contracts import canonical_json_bytes
                    from .pipeline import _atomic_write

                    _atomic_write(
                        source.directory / "acquisition-consumed.json",
                        canonical_json_bytes(
                            {"process_run_id": result.run_id, "generation_id": result.generation_id}
                        ),
                    )
                payload = _pipeline_result_payload(result)
                if (
                    plan.phase == "process"
                    and result.published
                    and result.status == "succeeded"
                    and getattr(settings, "backup_mode", "disabled") != "disabled"
                ):
                    from .backup import BackupLedger, canonical_remote_root, scoped_backup_result_fields

                    payload.update(
                        {
                            **scoped_backup_result_fields({}),
                            "backup_defer_reasons": None,
                            "backup_diagnostics_mode": None,
                            "backup_scope_run_id": None,
                        }
                    )
                    try:
                        ledger = BackupLedger(settings.state_dir / "backup-ledger.sqlite3")
                        selected = tuple(pipeline.selected_issuers)
                        ledger.record_run_success(
                            result.run_id, settings.state_dir, settings, selected_issuers=selected
                        )
                        status = ledger.get_status(settings)
                        if status["should_trigger"]:
                            allowed, expected = ledger.scoped_backup_scope(
                                result.run_id,
                                settings.state_dir,
                                selected,
                                remote_root=canonical_remote_root(settings),
                            )
                            flushed = await ledger.flush(
                                settings,
                                timeout_seconds=getattr(settings, "backup_inline_budget_seconds", 300.0),
                                allowed_remote_paths=allowed,
                                expected_artifacts=expected,
                            )
                            payload["backup_status"] = flushed.get("status", "failed")
                            payload.update(scoped_backup_result_fields(flushed))
                        else:
                            payload["backup_status"] = "deferred"
                            payload["backup_defer_reasons"] = status.get("trigger_reasons")
                            payload["backup_diagnostics_mode"] = None
                            payload["backup_scope_run_id"] = None
                            if selected:
                                # Threshold deferral keeps the read-only scope
                                # view visible without forcing any transport.
                                payload.update(
                                    ledger.scoped_pending_diagnostics(settings, result.run_id, selected)
                                )
                    except Exception:
                        # Publication truth remains committed. Retry backup through
                        # the existing ledger API, never by repeating OCR.
                        payload["backup_status"] = "error"
                return payload
        finally:
            if hasattr(webdav, "close"):
                await webdav.close()
