"""Process entry point."""

from __future__ import annotations

import hashlib

import uvicorn
from fastapi import FastAPI

from cardrag_mcp.app import build_app
from cardrag_mcp.config import Settings
from cardrag_mcp.embeddings import OpenRouterEmbedder
from cardrag_mcp.observability import Metrics, configure_logging
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.reranker import OpenRouterReranker, RerankerShadowLane, RerankerShadowStore
from cardrag_mcp.store import GenerationStore
from cardrag_mcp.transport import build_core_reader
from cardrag_mcp.updater import WebDAVUpdater


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    token = settings.bearer_token_value()
    store = GenerationStore(
        settings.mcp_state_dir,
        maximum_vector_bytes=settings.mcp_max_vector_bytes,
        maximum_vector_sidecar_bytes=settings.mcp_max_vector_sidecar_bytes,
        maximum_resident_vector_bytes=settings.resident_vector_limit_bytes(),
        maximum_pdf_bytes=settings.maximum_pdf_bytes,
        maximum_database_bytes=settings.mcp_max_serving_database_bytes,
        maximum_generation_download_bytes=settings.mcp_max_generation_download_bytes,
        maximum_state_bytes=settings.mcp_max_state_bytes,
        reserved_free_space_bytes=settings.mcp_reserved_free_space_bytes,
        exhaustive_audit_max_jobs=settings.mcp_exhaustive_audit_max_jobs,
        exhaustive_audit_max_total_bytes=settings.mcp_exhaustive_audit_max_total_bytes,
        exhaustive_audit_max_artifact_bytes=settings.mcp_exhaustive_audit_max_artifact_bytes,
        reranker_audit_max_jobs=settings.mcp_reranker_audit_max_jobs,
        reranker_audit_max_total_bytes=settings.mcp_reranker_audit_max_total_bytes,
        reranker_audit_max_artifact_bytes=settings.mcp_reranker_audit_max_artifact_bytes,
        retention=settings.mcp_retain_generations,
    )
    store.load_current()
    embedder = OpenRouterEmbedder(
        base_url=str(settings.openrouter_base_url),
        api_key=settings.openrouter_api_key_value(),
        timeout_seconds=settings.embedding_timeout_seconds,
        maximum_response_bytes=settings.embedding_max_response_bytes,
    )
    reranker_shadow = None
    if settings.reranker_shadow_enabled:
        reranker_api_key = settings.openrouter_api_key_value()
        if reranker_api_key is None:  # guarded by Settings validation
            raise RuntimeError("reranker shadow API key is unavailable")
        reranker_shadow = RerankerShadowLane(
            OpenRouterReranker(
                base_url=str(settings.openrouter_base_url),
                api_key=reranker_api_key,
                timeout_seconds=settings.reranker_shadow_timeout_seconds,
                maximum_response_bytes=settings.reranker_shadow_max_response_bytes,
            ),
            RerankerShadowStore(
                store.root,
                maximum_jobs=settings.mcp_reranker_audit_max_jobs,
                maximum_total_bytes=settings.mcp_reranker_audit_max_total_bytes,
                maximum_artifact_bytes=settings.mcp_reranker_audit_max_artifact_bytes,
            ),
            maximum_candidates=settings.reranker_shadow_max_candidates,
        )
    repository = ServingRepository(
        store,
        embedder,
        cursor_secret=hashlib.sha256(token.encode() + b"\x00cardrag-cursor-v1").digest(),
        maximum_candidates=settings.maximum_candidate_count,
        reranker_shadow=reranker_shadow,
    )
    metrics = Metrics.create()
    updater = None
    if settings.webdav_base_url is not None:
        reader = build_core_reader(settings)
        updater = WebDAVUpdater(
            reader,
            store,
            metrics,
            poll_seconds=settings.mcp_update_interval_seconds,
            maximum_pdf_bytes=settings.maximum_pdf_bytes,
            maximum_database_bytes=settings.mcp_max_serving_database_bytes,
            maximum_generation_download_bytes=settings.mcp_max_generation_download_bytes,
        )
    return build_app(repository, store, settings, updater=updater, metrics=metrics)


def main() -> None:
    configure_logging()
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.mcp_host,
        port=settings.mcp_port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
