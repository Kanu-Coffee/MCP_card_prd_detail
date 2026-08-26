"""Read-only FTS5 plus exact cosine serving repository."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import sqlite3
from collections.abc import Iterable

import numpy as np
from cardrag_core import EMBEDDING_DIMENSION
from numpy.typing import NDArray

from cardrag_mcp.embeddings import EmbeddingUnavailable, OpenRouterEmbedder
from cardrag_mcp.models import (
    Document,
    Evidence,
    EvidencePage,
    Issuer,
    Product,
    SearchFilters,
    SearchPage,
    SearchRequest,
    SourcePage,
    UnsupportedProduct,
)
from cardrag_mcp.store import GenerationHandle, GenerationStore

RRF_K = 60
VECTOR_CHUNK_ROWS = 8_192


def _fts_expression(query: str) -> str:
    tokens = [part for part in query.split() if part]
    if not tokens:
        raise ValueError("query must not be blank")
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _filter_clause(filters: SearchFilters, *, document_alias: str = "d") -> tuple[str, list[str]]:
    clauses: list[str] = []
    values: list[str] = []
    for column, value in (
        (f"{document_alias}.issuer", filters.issuer),
        (f"{document_alias}.product_code", filters.product_code),
        (f"{document_alias}.document_id", filters.document_id),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            values.append(value)
    if filters.section_type is not None:
        clauses.append("e.section_type=?")
        values.append(filters.section_type)
    return (" AND " + " AND ".join(clauses) if clauses else "", values)


def _binding(request: SearchRequest) -> str:
    canonical = json.dumps(
        {
            "filters": request.filters.model_dump(mode="json"),
            "query_sha256": hashlib.sha256(request.query.encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class CursorCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("cursor secret must contain at least 16 bytes")
        self._secret = secret

    def encode(self, generation_id: str, offset: int, binding: str) -> str:
        body = json.dumps(
            {"b": binding, "g": generation_id, "o": offset},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.digest(self._secret, body, "sha256")
        return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")

    def decode(self, value: str | None, generation_id: str, binding: str, maximum: int) -> int:
        if value is None:
            return 0
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            if len(raw) <= 32:
                raise ValueError
            body, supplied = raw[:-32], raw[-32:]
            if not hmac.compare_digest(supplied, hmac.digest(self._secret, body, "sha256")):
                raise ValueError
            payload = json.loads(body)
            if set(payload) != {"b", "g", "o"}:
                raise ValueError
            offset = payload["o"]
            if (
                payload["b"] != binding
                or payload["g"] != generation_id
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 0
                or offset > maximum
            ):
                raise ValueError
            return int(offset)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid or stale cursor") from exc


class ServingRepository:
    def __init__(
        self,
        store: GenerationStore,
        embedder: OpenRouterEmbedder,
        *,
        cursor_secret: bytes,
        maximum_candidates: int = 250,
    ) -> None:
        if maximum_candidates < 10:
            raise ValueError("maximum_candidates must be at least ten")
        self.store = store
        self.embedder = embedder
        self.maximum_candidates = maximum_candidates
        self.cursors = CursorCodec(cursor_secret)

    @property
    def ready(self) -> bool:
        return self.store.active_generation_id is not None

    async def search(self, request: SearchRequest) -> SearchPage:
        with self.store.pin() as handle:
            generation_id = handle.generation_id
            binding = _binding(request)
            offset = self.cursors.decode(
                request.cursor,
                generation_id,
                binding,
                self.maximum_candidates * 2,
            )
            lexical = await asyncio.to_thread(
                self._lexical_candidates,
                handle,
                request.query,
                request.filters,
                self.maximum_candidates,
            )
            retrieval_mode = "hybrid"
            degraded = False
            try:
                query_vector = await self.embedder.embed(
                    request.query,
                    provider=handle.metadata.embedding_provider,
                    model=handle.metadata.embedding_model,
                )
                vector = await asyncio.to_thread(
                    self._vector_candidates,
                    handle,
                    query_vector,
                    request.filters,
                    self.maximum_candidates,
                )
            except EmbeddingUnavailable:
                if not request.allow_degraded:
                    raise
                vector = []
                retrieval_mode = "lexical_only"
                degraded = True
            fused = self._rrf(lexical, vector)
            page_ids = fused[offset : offset + request.limit]
            items = await asyncio.to_thread(self._evidence_rows, handle, page_ids)
            next_cursor = (
                self.cursors.encode(generation_id, offset + len(items), binding)
                if offset + len(items) < len(fused)
                else None
            )
            return SearchPage(
                generation_id=generation_id,
                items=tuple(items),
                next_cursor=next_cursor,
                retrieval_mode=retrieval_mode,  # type: ignore[arg-type]
                degraded=degraded,
            )

    @staticmethod
    def _lexical_candidates(
        handle: GenerationHandle,
        query: str,
        filters: SearchFilters,
        limit: int,
    ) -> list[str]:
        suffix, values = _filter_clause(filters)
        sql = f"""
            SELECT e.evidence_id
            FROM evidence_fts AS f
            JOIN evidence AS e ON e.evidence_pk=f.rowid
            JOIN documents AS d ON d.document_id=e.document_id
            WHERE evidence_fts MATCH ?{suffix}
            ORDER BY bm25(evidence_fts), e.evidence_id
            LIMIT ?
        """  # noqa: S608 - suffix contains only fixed, parameterized clauses
        with handle.connect() as connection:
            try:
                rows = connection.execute(sql, [_fts_expression(query), *values, limit]).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError("query cannot be parsed by the lexical index") from exc
        return [str(row[0]) for row in rows]

    @staticmethod
    def _vector_candidates(
        handle: GenerationHandle,
        query_vector: NDArray[np.float32],
        filters: SearchFilters,
        limit: int,
    ) -> list[str]:
        if query_vector.shape != (EMBEDDING_DIMENSION,):
            raise EmbeddingUnavailable(
                "query embedding dimension differs from the serving database"
            )
        query_norm = float(np.linalg.norm(query_vector))
        if not math.isfinite(query_norm) or query_norm <= 0:
            raise EmbeddingUnavailable("query embedding has an invalid norm")
        suffix, values = _filter_clause(filters)
        with handle.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.evidence_id
                FROM evidence AS e
                JOIN documents AS d ON d.document_id=e.document_id
                WHERE 1=1{suffix}
                ORDER BY e.evidence_id
                """,  # noqa: S608 - suffix contains only fixed, parameterized clauses
                values,
            ).fetchall()
        indices = [handle.vectors.index_by_id[str(row[0])] for row in rows]
        scored: list[tuple[float, str]] = []
        for start in range(0, len(indices), VECTOR_CHUNK_ROWS):
            batch_indices = np.asarray(indices[start : start + VECTOR_CHUNK_ROWS], dtype=np.intp)
            if batch_indices.size == 0:
                continue
            matrix = handle.vectors.matrix[batch_indices]
            scores = (matrix @ query_vector) / (handle.vectors.norms[batch_indices] * query_norm)
            for matrix_index, score_value in zip(batch_indices, scores, strict=True):
                evidence_id = handle.vectors.evidence_ids[int(matrix_index)]
                score = float(score_value)
                scored.append((score, evidence_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [evidence_id for _, evidence_id in scored[:limit]]

    @staticmethod
    def _rrf(
        lexical: Iterable[str],
        vector: Iterable[str],
    ) -> list[tuple[str, float, int | None, int | None]]:
        scores: dict[str, float] = {}
        ranks: dict[str, tuple[int | None, int | None]] = {}
        for rank, evidence_id in enumerate(lexical, 1):
            scores[evidence_id] = scores.get(evidence_id, 0.0) + 1 / (RRF_K + rank)
            ranks[evidence_id] = (rank, ranks.get(evidence_id, (None, None))[1])
        for rank, evidence_id in enumerate(vector, 1):
            scores[evidence_id] = scores.get(evidence_id, 0.0) + 1 / (RRF_K + rank)
            ranks[evidence_id] = (ranks.get(evidence_id, (None, None))[0], rank)
        return [
            (evidence_id, scores[evidence_id], *ranks[evidence_id])
            for evidence_id in sorted(scores, key=lambda key: (-scores[key], key))
        ]

    @staticmethod
    def _evidence_rows(
        handle: GenerationHandle,
        ranked: list[tuple[str, float, int | None, int | None]],
    ) -> list[Evidence]:
        if not ranked:
            return []
        identifiers = [item[0] for item in ranked]
        placeholders = ",".join("?" for _ in identifiers)
        with handle.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.evidence_id, e.document_id, d.issuer, d.product_code,
                       p.name AS product_name, d.title AS document_title,
                       e.page_start, e.page_end, e.section_type, e.text,
                       e.source_start, e.source_end, d.pdf_sha256
                FROM evidence AS e
                JOIN documents AS d ON d.document_id=e.document_id
                JOIN products AS p
                  ON p.issuer=d.issuer AND p.product_code=d.product_code
                WHERE e.evidence_id IN ({placeholders})
                """,  # noqa: S608 - placeholders are literal question marks
                identifiers,
            ).fetchall()
        by_id = {str(row["evidence_id"]): row for row in rows}
        result: list[Evidence] = []
        for evidence_id, score, lexical_rank, vector_rank in ranked:
            row = by_id.get(evidence_id)
            if row is None:
                raise RuntimeError("ranked evidence disappeared from immutable database")
            result.append(
                Evidence(
                    **dict(row),
                    score=score,
                    lexical_rank=lexical_rank,
                    vector_rank=vector_rank,
                )
            )
        return result

    async def get_evidence(
        self,
        evidence_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> EvidencePage | None:
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        with self.store.pin() as handle:
            binding = hashlib.sha256(("evidence\x00" + evidence_id).encode()).hexdigest()
            offset = self.cursors.decode(cursor, handle.generation_id, binding, 1_000_000)
            page = await asyncio.to_thread(
                self._adjacent_evidence,
                handle,
                evidence_id,
                offset,
                limit,
            )
            if page is None:
                return None
            document_id, rows, has_more = page
            next_cursor = (
                self.cursors.encode(handle.generation_id, offset + len(rows), binding)
                if has_more
                else None
            )
            return EvidencePage(
                generation_id=handle.generation_id,
                evidence_id=evidence_id,
                document_id=document_id,
                items=tuple(rows),
                next_cursor=next_cursor,
            )

    @classmethod
    def _adjacent_evidence(
        cls,
        handle: GenerationHandle,
        evidence_id: str,
        offset: int,
        limit: int,
    ) -> tuple[str, list[Evidence], bool] | None:
        with handle.connect() as connection:
            anchor = connection.execute(
                """
                SELECT document_id,page_start,source_start,evidence_id
                FROM evidence WHERE evidence_id=?
                """,
                (evidence_id,),
            ).fetchone()
            if anchor is None:
                return None
            rows = connection.execute(
                """
                SELECT evidence_id
                FROM evidence
                WHERE document_id=?
                  AND (page_start,source_start,evidence_id) >= (?,?,?)
                ORDER BY page_start,source_start,evidence_id
                LIMIT ? OFFSET ?
                """,
                (
                    anchor["document_id"],
                    anchor["page_start"],
                    anchor["source_start"],
                    anchor["evidence_id"],
                    limit + 1,
                    offset,
                ),
            ).fetchall()
        identifiers = [str(row[0]) for row in rows[:limit]]
        values = cls._evidence_rows(
            handle,
            [(value, 0.0, None, None) for value in identifiers],
        )
        return (
            str(anchor["document_id"]),
            [value.model_copy(update={"score": None}) for value in values],
            len(rows) > limit,
        )

    async def get_product(
        self, issuer: str, product_code: str
    ) -> Product | UnsupportedProduct | None:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._get_product, handle, issuer, product_code)

    @staticmethod
    def _get_product(
        handle: GenerationHandle,
        issuer: str,
        product_code: str,
    ) -> Product | UnsupportedProduct | None:
        with handle.connect() as connection:
            row = connection.execute(
                """
                SELECT p.issuer, p.product_code, p.name,
                       d.document_id, d.title, d.pdf_sha256,
                       d.pdf_size_bytes, d.page_count
                FROM products AS p
                JOIN documents AS d ON d.document_id=p.document_id
                WHERE p.issuer=? AND p.product_code=?
                """,
                (issuer, product_code),
            ).fetchone()
            if row is None:
                unsupported = connection.execute(
                    """
                    SELECT issuer,product_code,name,source_id,source_version,source_url,
                           protected_magic,protected_sha256,protected_size_bytes
                    FROM unsupported_products
                    WHERE issuer=? AND product_code=?
                    """,
                    (issuer, product_code),
                ).fetchone()
            else:
                unsupported = None
        if row is None:
            if unsupported is None:
                return None
            return UnsupportedProduct(
                issuer=unsupported["issuer"],
                product_code=unsupported["product_code"],
                name=unsupported["name"],
                source_id=unsupported["source_id"],
                source_version=unsupported["source_version"],
                source_url=unsupported["source_url"],
                protected_magic=unsupported["protected_magic"],
                protected_source_sha256=unsupported["protected_sha256"],
                protected_source_size_bytes=unsupported["protected_size_bytes"],
            )
        document = Document(
            document_id=row["document_id"],
            issuer=row["issuer"],
            product_code=row["product_code"],
            title=row["title"],
            pdf_sha256=row["pdf_sha256"],
            pdf_size_bytes=row["pdf_size_bytes"],
            page_count=row["page_count"],
        )
        return Product(
            issuer=row["issuer"],
            product_code=row["product_code"],
            name=row["name"],
            document=document,
        )

    async def get_document(self, document_id: str) -> Document | None:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._get_document, handle, document_id)

    @staticmethod
    def _get_document(handle: GenerationHandle, document_id: str) -> Document | None:
        with handle.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
        return Document(**dict(row)) if row is not None else None

    async def get_source_page(self, document_id: str, page: int) -> SourcePage | None:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._get_source_page, handle, document_id, page)

    @staticmethod
    def _get_source_page(
        handle: GenerationHandle, document_id: str, page: int
    ) -> SourcePage | None:
        with handle.connect() as connection:
            row = connection.execute(
                """
                SELECT p.document_id, p.page, p.text, p.text_sha256,
                       d.page_count, d.pdf_sha256
                FROM pages AS p
                JOIN documents AS d ON d.document_id=p.document_id
                WHERE p.document_id=? AND p.page=?
                """,
                (document_id, page),
            ).fetchone()
        return SourcePage(**dict(row)) if row is not None else None

    async def list_issuers(self) -> tuple[Issuer, ...]:
        with self.store.pin() as handle:
            with handle.connect() as connection:
                rows = connection.execute(
                    "SELECT code, display_name, sort_order FROM issuers ORDER BY sort_order, code"
                ).fetchall()
        return tuple(Issuer(**dict(row)) for row in rows)

    async def list_products(
        self, issuer: str | None = None
    ) -> tuple[Product | UnsupportedProduct, ...]:
        with self.store.pin() as handle:
            with handle.connect() as connection:
                if issuer is None:
                    rows = connection.execute(
                        """
                        SELECT issuer,product_code FROM products
                        UNION ALL
                        SELECT issuer,product_code FROM unsupported_products
                        ORDER BY issuer,product_code
                        """
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT issuer,product_code FROM products WHERE issuer=?
                        UNION ALL
                        SELECT issuer,product_code FROM unsupported_products WHERE issuer=?
                        ORDER BY product_code
                        """,
                        (issuer, issuer),
                    ).fetchall()
            products = [self._get_product(handle, str(row[0]), str(row[1])) for row in rows]
        return tuple(product for product in products if product is not None)

    async def list_documents(self) -> tuple[Document, ...]:
        with self.store.pin() as handle:
            with handle.connect() as connection:
                rows = connection.execute("SELECT * FROM documents ORDER BY document_id").fetchall()
        return tuple(Document(**dict(row)) for row in rows)

    async def list_pages(self, document_id: str) -> tuple[SourcePage, ...]:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._list_pages, handle, document_id)

    @staticmethod
    def _list_pages(handle: GenerationHandle, document_id: str) -> tuple[SourcePage, ...]:
        with handle.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.document_id, p.page, p.text, p.text_sha256,
                       d.page_count, d.pdf_sha256
                FROM pages AS p
                JOIN documents AS d ON d.document_id=p.document_id
                WHERE p.document_id=?
                ORDER BY p.page
                """,
                (document_id,),
            ).fetchall()
        return tuple(SourcePage(**dict(row)) for row in rows)
