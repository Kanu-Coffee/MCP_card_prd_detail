"""Composition root for the online, read-only MCP process."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import uvicorn

from cardrag.config import Settings
from cardrag.db import Postgres
from cardrag.generation import GenerationStore
from cardrag.mcp_server import build_app
from cardrag.observability import configure_logging
from cardrag.search import HybridSearchEngine
from cardrag.search.embeddings import EmbeddingError, OpenRouterEmbeddingProvider
from cardrag.search.generation_store import GenerationPinnedPostgresStore
from cardrag.service.postgres_repository import PostgresCardRAGRepository


class UnavailableEmbeddingProvider:
    """Preserve the configured vector contract while the credential is absent."""

    provider = "openrouter"

    def __init__(self, *, model: str, dimension: int) -> None:
        self.model = model
        self.dimension = dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("online embedding provider is not configured")

    async def embed_query(self, query: str) -> list[float]:
        raise EmbeddingError("online embedding provider is not configured")


def create_app(settings: Settings | None = None) -> tuple[Any, Postgres]:
    settings = settings or Settings()  # type: ignore[call-arg]
    database = Postgres(
        settings.database_url_value(),
        min_size=1,
        max_size=8,
        statement_timeout_seconds=settings.postgres_statement_timeout_seconds,
        lock_timeout_seconds=settings.postgres_lock_timeout_seconds,
    )
    database.open()
    generation_store = GenerationStore(
        settings.generation_root, settings.build_root / "generation-candidates"
    )
    api_key = settings.secret_text_from_file(settings.openrouter_api_key_file)
    embedder = (
        OpenRouterEmbeddingProvider(
            api_key=api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            base_url=str(settings.openrouter_base_url),
            timeout_seconds=settings.embedding_timeout_seconds,
        )
        if api_key
        else UnavailableEmbeddingProvider(
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
    )
    search_store = GenerationPinnedPostgresStore(
        database,
        generation_store=generation_store,
        embedding_provider=embedder.provider,
        embedding_model=embedder.model,
        embedding_dimension=embedder.dimension,
    )
    engine = HybridSearchEngine(search_store, embedder)
    repository = PostgresCardRAGRepository(
        database,
        generation_store,
        engine,
        settings.storage_root,
    )
    application = build_app(repository, settings)
    application.state.cardrag_database = database
    application.state.cardrag_repository = repository
    return application, database


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(
        service="mcp",
        environment=settings.environment,
        application_version=settings.application_version,
        image_revision=settings.image_revision,
    )
    application, database = create_app(settings)
    try:
        uvicorn.run(
            application,
            host=settings.host,
            port=settings.port,
            log_config=None,
            access_log=False,  # paths may contain document IDs; audit is structured separately
            timeout_keep_alive=10,
            limit_concurrency=settings.max_concurrent_requests + 16,
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
