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
from cardrag_mcp.store import GenerationStore
from cardrag_mcp.transport import build_core_reader
from cardrag_mcp.updater import WebDAVUpdater


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    token = settings.bearer_token_value()
    store = GenerationStore(
        settings.mcp_state_dir,
        maximum_vector_bytes=settings.mcp_max_vector_bytes,
        maximum_pdf_bytes=settings.maximum_pdf_bytes,
        retention=settings.mcp_retain_generations,
    )
    store.load_current()
    embedder = OpenRouterEmbedder(
        base_url=str(settings.openrouter_base_url),
        api_key=settings.openrouter_api_key_value(),
        timeout_seconds=settings.embedding_timeout_seconds,
    )
    repository = ServingRepository(
        store,
        embedder,
        cursor_secret=hashlib.sha256(token.encode() + b"\x00cardrag-cursor-v1").digest(),
        maximum_candidates=settings.maximum_candidate_count,
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
