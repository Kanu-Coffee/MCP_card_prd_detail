"""Async facade around the psycopg pool; every branch applies the same filters."""

from __future__ import annotations

from typing import Any

import anyio

from cardrag.db import Postgres
from cardrag.search.hybrid import SearchFilters


class PostgresSearchStore:
    def __init__(self, database: Postgres) -> None:
        self.database = database

    async def active_generation_id(self) -> str:
        return await anyio.to_thread.run_sync(self._active_generation_id)

    def _active_generation_id(self) -> str:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.generation_id FROM active_generation a
                JOIN generations g USING (generation_id)
                WHERE g.state = 'published'
                """
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("no published active generation")
            return str(row["generation_id"])

    @staticmethod
    def _filter_sql(filters: SearchFilters) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("d.issuer", filters.issuer.value if filters.issuer else None),
            ("d.product_code", filters.product_code),
            ("e.section_type", filters.section_type),
            ("d.source_version", filters.version),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                params.append(value)
        if filters.as_of is not None:
            clauses.append("d.effective_date <= %s")
            params.append(filters.as_of)
            clauses.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM generation_documents newer
                    WHERE newer.generation_id=d.generation_id
                      AND newer.issuer=d.issuer
                      AND newer.product_code=d.product_code
                      AND newer.document_type=d.document_type
                      AND newer.effective_date <= %s
                      AND (
                          newer.effective_date, newer.version_sort_key,
                          newer.discovered_at, newer.document_id
                      ) > (
                          d.effective_date, d.version_sort_key,
                          d.discovered_at, d.document_id
                      )
                )
                """.strip()
            )
            params.append(filters.as_of)
        elif filters.version is None:
            clauses.append("d.is_latest = true")
        return (" AND ".join(clauses), params)

    async def lexical_candidates(
        self, generation_id: str, query: str, filters: SearchFilters, limit: int
    ) -> list[dict[str, Any]]:
        return await anyio.to_thread.run_sync(self._lexical, generation_id, query, filters, limit)

    def _lexical(
        self, generation_id: str, query: str, filters: SearchFilters, limit: int
    ) -> list[dict[str, Any]]:
        extra, params = self._filter_sql(filters)
        where = " AND " + extra if extra else ""
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT e.generation_id, e.evidence_id, e.document_id,
                       d.issuer, d.product_code, d.product_name, d.document_type,
                       d.effective_date, d.source_version,
                       e.section_type, e.page_start, e.page_end,
                       e.span_start, e.span_end, e.source_spans, e.text, e.text_sha256,
                       d.pdf_sha256, e.confidence,
                       ts_rank_cd(e.search_tsv, websearch_to_tsquery('simple', %s), 32) AS branch_score
                FROM evidence e
                JOIN generation_documents d
                  ON d.generation_id=e.generation_id AND d.document_id=e.document_id
                WHERE e.generation_id = %s
                  AND e.search_tsv @@ websearch_to_tsquery('simple', %s)
                  {where}
                ORDER BY branch_score DESC, e.evidence_id
                LIMIT %s
                """,  # noqa: S608 - only internally generated fixed clause names
                [query, generation_id, query, *params, limit],
            )
            return list(cursor.fetchall())

    async def vector_candidates(
        self, generation_id: str, vector: list[float], filters: SearchFilters, limit: int
    ) -> list[dict[str, Any]]:
        return await anyio.to_thread.run_sync(self._vector, generation_id, vector, filters, limit)

    def _vector(
        self, generation_id: str, vector: list[float], filters: SearchFilters, limit: int
    ) -> list[dict[str, Any]]:
        extra, params = self._filter_sql(filters)
        where = " AND " + extra if extra else ""
        vector_literal = "[" + ",".join(format(value, ".17g") for value in vector) + "]"
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET LOCAL hnsw.ef_search = 100")
            cursor.execute(
                f"""
                    SELECT e.generation_id, e.evidence_id, e.document_id,
                           d.issuer, d.product_code, d.product_name, d.document_type,
                           d.effective_date, d.source_version,
                           e.section_type, e.page_start, e.page_end,
                           e.span_start, e.span_end, e.source_spans, e.text, e.text_sha256,
                           d.pdf_sha256, e.confidence,
                           1 - (e.embedding <=> %s::vector) AS branch_score
                    FROM evidence e
                    JOIN generation_documents d
                      ON d.generation_id=e.generation_id AND d.document_id=e.document_id
                    WHERE e.generation_id = %s AND e.embedding IS NOT NULL {where}
                    ORDER BY e.embedding <=> %s::vector, e.evidence_id
                    LIMIT %s
                    """,  # noqa: S608 - only internally generated fixed clause names
                [vector_literal, generation_id, *params, vector_literal, limit],
            )
            return list(cursor.fetchall())
