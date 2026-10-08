"""CLI wiring for explicit partial runs, with lazy provider initialization."""

from __future__ import annotations

from typing import Any

from .partial_execution import (
    ExecutionPlan,
    LocalWebDAV,
    PartialExecutionError,
    ReuseSource,
)


async def run_partial(plan: ExecutionPlan, resume: str | None) -> dict[str, Any]:
    from .aggregation_profile_v5 import load_verified_aggregation_profile_v5
    from .capacity_v5 import V5CapacityPolicy, preflight_worker_start_capacity
    from .cli import _ocr_resolver, _pipeline_result_payload, _qwen_embedding_provider
    from .embedding_v5 import OpenRouterQwenEmbeddingProviderV5, QwenEmbeddingProfileV5
    from .issuers import enabled_adapters
    from .ocr_requests import load_next_reprocess_request
    from .pipeline import WorkerPipeline, validate_document_aggregation_head
    from .settings import WorkerSettings
    from .state import WorkerState, worker_lock
    from .tokenizer_v5 import QwenTokenizerV5
    from .webdav import WebDAVClient

    settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
    with worker_lock(settings.lock_file):
        if resume:
            import json

            path = settings.state_dir / "runs" / resume / "execution-plan.json"
            if not path.is_file() or json.loads(path.read_bytes()) != plan.identity():
                raise PartialExecutionError("skip_artifact_incompatible", "plan")
        source = (
            ReuseSource(settings.state_dir, plan.source_run_id, settings.state_database)
            if plan.source_run_id
            else None
        )
        preflight = source.preflight(plan) if source is not None else plan.payload()
        if plan.skips("ocr") and load_next_reprocess_request(settings.state_dir) is not None:
            raise PartialExecutionError("skip_artifact_incompatible", "ocr")
        if (
            source is not None
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
            if plan.channel == "stable" and source is not None and not plan.skips("webdav"):
                current = await webdav.validated_current_generation()
                if current is None or current.generation_id != source.manifest.generation_id:
                    raise PartialExecutionError("skip_source_unavailable", "webdav")
            with WorkerState(
                settings.state_database,
                sqlite_cache_mib=settings.sqlite_cache_mib,
                sqlite_mmap_mib=settings.sqlite_mmap_mib,
            ) as state:
                embeddings: OpenRouterQwenEmbeddingProviderV5
                if source is not None:
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
                ocr = _ocr_resolver(
                    settings, state, None if plan.skips("webdav") else webdav, skip=plan.skips("ocr")
                )
                if not plan.skips("webdav"):
                    webdav.configure_verification(state, settings.webdav_verification)
                pipeline = WorkerPipeline(
                    state=state,
                    state_dir=settings.state_dir,
                    adapters=enabled_adapters(),
                    ocr=ocr,  # type: ignore[arg-type]
                    embeddings=embeddings,
                    webdav=webdav,
                    lock_held=True,
                    execution_plan=plan,
                    reuse_source=source,
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
                return _pipeline_result_payload(result)
        finally:
            await webdav.close()
