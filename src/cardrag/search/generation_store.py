"""Search store that fail-closes unless file, DB, and runtime generations agree."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio

from cardrag.generation import GENERATION_SCHEMA_VERSION, GenerationManifest, GenerationStore
from cardrag.search.postgres_store import PostgresSearchStore


class ActiveGenerationMismatch(RuntimeError):
    """The published generation is not one coherent, query-compatible snapshot."""


@dataclass(frozen=True, slots=True)
class ActiveGenerationSnapshot:
    generation_id: str
    manifest: GenerationManifest


class GenerationPinnedPostgresStore(PostgresSearchStore):
    def __init__(
        self,
        *args: Any,
        generation_store: GenerationStore,
        embedding_provider: str,
        embedding_model: str,
        embedding_dimension: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.generation_store = generation_store
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension

    async def active_generation_id(self) -> str:
        snapshot = await anyio.to_thread.run_sync(self.validate_active_generation_sync)
        return snapshot.generation_id

    def validate_active_generation_sync(self) -> ActiveGenerationSnapshot:
        # PostgreSQL is the atomic serving authority. The file pointer is an
        # operator/recovery mirror and may lag briefly while a publish commits;
        # requests pin the DB generation once, then verify that exact sealed path.
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.generation_id, g.state, g.manifest_sha256, g.schema_version,
                       g.embedding_provider, g.embedding_model, g.embedding_dimension,
                       g.latest_document_count, g.latest_covered_count,
                       (
                           SELECT count(*)::int FROM generation_documents d
                           WHERE d.generation_id=g.generation_id
                       ) AS document_snapshot_count,
                       (
                           SELECT count(*)::int FROM generation_documents d
                           WHERE d.generation_id=g.generation_id AND d.is_latest
                       ) AS latest_snapshot_count,
                       (
                           SELECT count(*)::int FROM generation_documents d
                           WHERE d.generation_id=g.generation_id
                             AND d.embedding_provider IS NOT NULL
                             AND (
                                 d.embedding_provider IS DISTINCT FROM g.embedding_provider
                                 OR d.embedding_model IS DISTINCT FROM g.embedding_model
                                 OR d.embedding_dimension IS DISTINCT FROM g.embedding_dimension
                             )
                       ) AS incompatible_snapshot_count,
                       (
                           SELECT count(*)::int FROM generation_documents d
                           WHERE d.generation_id=g.generation_id AND d.is_latest
                             AND (
                                 d.ocr_sha256 IS NULL
                                 OR d.ocr_pages IS NULL
                                 OR jsonb_array_length(d.ocr_pages) IS DISTINCT FROM d.pdf_page_count
                                 OR d.structured_sha256 IS NULL
                                 OR d.embedding_provider IS NULL
                                 OR d.embedding_provider IS DISTINCT FROM g.embedding_provider
                                 OR d.embedding_model IS DISTINCT FROM g.embedding_model
                                 OR d.embedding_dimension IS DISTINCT FROM g.embedding_dimension
                                 OR d.index_count IS DISTINCT FROM (
                                     SELECT count(*)::int FROM evidence e
                                     WHERE e.generation_id=d.generation_id
                                       AND e.document_id=d.document_id
                                       AND e.embedding IS NOT NULL
                                 )
                             )
                       ) AS uncovered_latest_snapshot_count
                FROM active_generation a
                JOIN generations g USING (generation_id)
                WHERE a.singleton = true
                """
            )
            row = cursor.fetchone()
        if row is None or row["state"] != "published":
            raise ActiveGenerationMismatch("database has no published active generation")
        generation_id = str(row["generation_id"])
        try:
            manifest = self.generation_store.verify_path(
                self.generation_store.generations / generation_id,
                expected_generation_id=generation_id,
            )
        except Exception as exc:
            raise ActiveGenerationMismatch("active generation seal cannot be verified") from exc
        if row["manifest_sha256"] != manifest.sha256:
            raise ActiveGenerationMismatch("generation manifest hashes differ")
        if (
            row["schema_version"] != GENERATION_SCHEMA_VERSION
            or manifest.schema_version != GENERATION_SCHEMA_VERSION
        ):
            raise ActiveGenerationMismatch("generation schema is incompatible")

        database_embedding = (
            str(row["embedding_provider"]),
            str(row["embedding_model"]),
            int(row["embedding_dimension"]),
        )
        manifest_embedding = (
            manifest.embedding_provider,
            manifest.embedding_model,
            manifest.embedding_dimension,
        )
        runtime_embedding = (
            self.embedding_provider,
            self.embedding_model,
            self.embedding_dimension,
        )
        if database_embedding != manifest_embedding or runtime_embedding != manifest_embedding:
            raise ActiveGenerationMismatch("query embedding configuration differs from active generation")
        if int(row["latest_document_count"]) != manifest.latest_document_count:
            raise ActiveGenerationMismatch("generation document counts differ")
        if int(row["latest_covered_count"]) != manifest.latest_index_count:
            raise ActiveGenerationMismatch("generation coverage counts differ")
        if int(row["document_snapshot_count"]) != manifest.document_count:
            raise ActiveGenerationMismatch("generation document snapshot count differs")
        if int(row["latest_snapshot_count"]) != manifest.latest_document_count:
            raise ActiveGenerationMismatch("generation latest snapshot count differs")
        if int(row["incompatible_snapshot_count"]) != 0:
            raise ActiveGenerationMismatch("generation document embedding configuration differs")
        if int(row["uncovered_latest_snapshot_count"]) != 0:
            raise ActiveGenerationMismatch("generation latest document coverage is incomplete")
        return ActiveGenerationSnapshot(generation_id, manifest)
