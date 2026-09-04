"""Read-only FTS5 plus exact cosine serving repository."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import sqlite3
import unicodedata
from collections.abc import Iterable
from datetime import date
from typing import Any, Literal, cast

import numpy as np
from cardrag_core import EMBEDDING_DIMENSION
from numpy.typing import NDArray

from cardrag_mcp.embeddings import EmbeddingUnavailable, OpenRouterEmbedder
from cardrag_mcp.exact import V5ExactRepository
from cardrag_mcp.launch_date import parse_launch_date
from cardrag_mcp.models import (
    ContractBundle,
    ContractSearchPage,
    ContractSearchRequest,
    Document,
    Evidence,
    EvidencePage,
    Issuer,
    MerchantSearchHit,
    MerchantSearchPage,
    OCRFailedProduct,
    Product,
    ProductCatalogEntry,
    ProductCatalogPage,
    ProductRevisionList,
    ProductSummary,
    SearchFilters,
    SearchPage,
    SearchRequest,
    SourcePage,
    TemporalStatus,
    UnsupportedProduct,
)
from cardrag_mcp.reranker import RerankerShadowLane
from cardrag_mcp.schema import LoadedVectors
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
        reranker_shadow: RerankerShadowLane | None = None,
    ) -> None:
        if maximum_candidates < 10:
            raise ValueError("maximum_candidates must be at least ten")
        self.store = store
        self.embedder = embedder
        self.maximum_candidates = maximum_candidates
        self.cursors = CursorCodec(cursor_secret)
        self.reranker_shadow = reranker_shadow
        self.exact = V5ExactRepository(store, embedder, reranker_shadow)

    @property
    def ready(self) -> bool:
        return self.store.active_generation_id is not None

    async def search_contracts(self, request: ContractSearchRequest) -> ContractSearchPage:
        return await self.exact.search(request)

    async def get_contract_bundle(
        self,
        contract_revision_id: str,
        *,
        scope: str = "full",
        include_links: bool = True,
    ) -> ContractBundle | None:
        if scope not in {"full", "benefits", "notices"}:
            raise ValueError("scope must be full, benefits, or notices")
        scope_literal = cast(Literal["full", "benefits", "notices"], scope)
        return await asyncio.to_thread(
            self.exact.get_bundle,
            contract_revision_id,
            scope=scope_literal,
            include_links=include_links,
        )

    async def list_product_revisions(
        self,
        issuer: str,
        product_lineage_id: str,
    ) -> ProductRevisionList:
        return await asyncio.to_thread(self.exact.list_revisions, issuer, product_lineage_id)

    async def search(self, request: SearchRequest) -> SearchPage:
        with self.store.pin() as handle:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                return await self._search_v5_compat(request, handle=handle)
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

    async def _search_v5_compat(
        self,
        request: SearchRequest,
        *,
        handle: GenerationHandle,
    ) -> SearchPage:
        """Preserve search_evidence while exposing original v5 source spans."""

        lineage_id: str | None = None
        with handle.connect() as connection:
            if request.filters.document_id is not None:
                row = connection.execute(
                    "SELECT product_lineage_id FROM contract_revisions WHERE document_id=?",
                    (request.filters.document_id,),
                ).fetchone()
                lineage_id = None if row is None else str(row[0])
            elif request.filters.product_code is not None:
                if request.filters.issuer is None:
                    rows = connection.execute(
                        """SELECT product_lineage_id FROM product_lineages
                             WHERE product_code=? ORDER BY product_lineage_id""",
                        (request.filters.product_code,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """SELECT product_lineage_id FROM product_lineages
                             WHERE product_code=? AND issuer=?
                             ORDER BY product_lineage_id""",
                        (request.filters.product_code, request.filters.issuer),
                    ).fetchall()
                if len(rows) == 1:
                    lineage_id = str(rows[0][0])
        exact = await self.exact.search(
            ContractSearchRequest(
                query=request.query,
                issuer=request.filters.issuer,
                product_lineage_id=lineage_id,
                limit=50,
            ),
            handle=handle,
        )
        ranked: list[Evidence] = []
        vector_rank = 0
        for bundle in exact.bundles:
            for match in bundle.matches:
                node = match.node
                if (
                    request.filters.section_type is not None
                    and request.filters.section_type.casefold()
                    not in {node.node_type.casefold(), node.major_class.casefold()}
                ):
                    continue
                best_view = max(
                    match.matched_views,
                    key=lambda view: (view.score, -view.row_index),
                )
                if not best_view.spans:
                    continue
                vector_rank += 1
                first, last = best_view.spans[0], best_view.spans[-1]
                ranked.append(
                    Evidence(
                        evidence_id=node.node_id,
                        document_id=bundle.contract.document_id,
                        issuer=bundle.contract.issuer,
                        product_code=bundle.contract.product_code,
                        product_name=bundle.contract.product_name,
                        document_title=bundle.contract.product_name,
                        page_start=first.page,
                        page_end=last.page,
                        section_type=node.node_type,
                        text=best_view.display_text,
                        source_start=first.source_start,
                        source_end=last.source_end,
                        pdf_sha256=bundle.contract.pdf_sha256,
                        score=match.score,
                        lexical_rank=1 if match.lexical_only else None,
                        vector_rank=vector_rank,
                    )
                )
        binding = _binding(request)
        offset = self.cursors.decode(request.cursor, exact.generation_id, binding, len(ranked))
        items = ranked[offset : offset + request.limit]
        next_cursor = (
            self.cursors.encode(exact.generation_id, offset + len(items), binding)
            if offset + len(items) < len(ranked)
            else None
        )
        return SearchPage(
            generation_id=exact.generation_id,
            items=tuple(items),
            next_cursor=next_cursor,
            retrieval_mode="exact",
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
        legacy_vectors = handle.vectors
        if not isinstance(legacy_vectors, LoadedVectors):
            raise RuntimeError("legacy vector search received a v5 generation")
        indices = [legacy_vectors.index_by_id[str(row[0])] for row in rows]
        scored: list[tuple[float, str]] = []
        for start in range(0, len(indices), VECTOR_CHUNK_ROWS):
            batch_indices = np.asarray(indices[start : start + VECTOR_CHUNK_ROWS], dtype=np.intp)
            if batch_indices.size == 0:
                continue
            matrix = legacy_vectors.matrix[batch_indices]
            scores = (matrix @ query_vector) / (legacy_vectors.norms[batch_indices] * query_norm)
            for matrix_index, score_value in zip(batch_indices, scores, strict=True):
                evidence_id = legacy_vectors.evidence_ids[int(matrix_index)]
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
            return await self._get_evidence_pinned(
                handle,
                evidence_id,
                cursor=cursor,
                limit=limit,
            )

    async def _get_evidence_pinned(
        self,
        handle: GenerationHandle,
        evidence_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> EvidencePage | None:
        generation_id = handle.generation_id
        if handle.metadata.schema_id == "cardrag.serving-db.v5":
            with handle.connect() as connection:
                row = connection.execute(
                    "SELECT contract_revision_id FROM structure_nodes WHERE node_id=?",
                    (evidence_id,),
                ).fetchone()
            revision_id = None if row is None else str(row[0])
            if revision_id is None:
                return None
            bundle = await asyncio.to_thread(
                self.exact.get_bundle,
                revision_id,
                scope="full",
                include_links=False,
                handle=handle,
            )
            if bundle is None:
                return None
            identifiers = [node.node_id for node in bundle.nodes]
            try:
                anchor_index = identifiers.index(evidence_id)
            except ValueError:
                return None
            binding = hashlib.sha256(("evidence\x00" + evidence_id).encode()).hexdigest()
            offset = self.cursors.decode(cursor, generation_id, binding, len(identifiers))
            selected = bundle.nodes[anchor_index + offset : anchor_index + offset + limit]
            items: list[Evidence] = []
            for node in selected:
                if not node.spans:
                    continue
                first, last = node.spans[0], node.spans[-1]
                items.append(
                    Evidence(
                        evidence_id=node.node_id,
                        document_id=bundle.contract.document_id,
                        issuer=bundle.contract.issuer,
                        product_code=bundle.contract.product_code,
                        product_name=bundle.contract.product_name,
                        document_title=bundle.contract.product_name,
                        page_start=first.page,
                        page_end=last.page,
                        section_type=node.node_type,
                        text="\n".join(span.text for span in node.spans),
                        source_start=first.source_start,
                        source_end=last.source_end,
                        pdf_sha256=bundle.contract.pdf_sha256,
                    )
                )
            consumed = anchor_index + offset + len(selected)
            next_cursor = (
                self.cursors.encode(generation_id, offset + len(selected), binding)
                if consumed < len(bundle.nodes)
                else None
            )
            return EvidencePage(
                generation_id=generation_id,
                evidence_id=evidence_id,
                document_id=bundle.contract.document_id,
                items=tuple(items),
                next_cursor=next_cursor,
            )
        binding = hashlib.sha256(("evidence\x00" + evidence_id).encode()).hexdigest()
        offset = self.cursors.decode(cursor, generation_id, binding, 1_000_000)
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
            self.cursors.encode(generation_id, offset + len(rows), binding) if has_more else None
        )
        return EvidencePage(
            generation_id=generation_id,
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
    ) -> Product | UnsupportedProduct | OCRFailedProduct | None:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._get_product, handle, issuer, product_code)

    @staticmethod
    def _get_product(
        handle: GenerationHandle,
        issuer: str,
        product_code: str,
    ) -> Product | UnsupportedProduct | OCRFailedProduct | None:
        with handle.connect() as connection:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                rows = connection.execute(
                    """SELECT l.issuer,l.product_code,l.name,
                              r.document_id,l.name AS title,r.pdf_sha256,
                              r.pdf_size_bytes,r.page_count
                         FROM product_lineages AS l
                         JOIN contract_revisions AS r
                           ON r.product_lineage_id=l.product_lineage_id
                        WHERE l.issuer=? AND l.product_code=?
                          AND r.temporal_status='current'
                        ORDER BY l.document_type,r.contract_revision_id""",
                    (issuer, product_code),
                ).fetchall()
                if len(rows) > 1:
                    return None
                if rows:
                    current = rows[0]
                    document = Document(
                        document_id=current["document_id"],
                        issuer=current["issuer"],
                        product_code=current["product_code"],
                        title=current["title"],
                        pdf_sha256=current["pdf_sha256"],
                        pdf_size_bytes=current["pdf_size_bytes"],
                        page_count=current["page_count"],
                    )
                    return Product(
                        issuer=current["issuer"],
                        product_code=current["product_code"],
                        name=current["name"],
                        document=document,
                    )
                unsupported = connection.execute(
                    """SELECT issuer,product_code,name,source_id,source_version,source_url,
                              protected_magic,protected_sha256,protected_size_bytes
                         FROM unsupported_products
                        WHERE issuer=? AND product_code=?""",
                    (issuer, product_code),
                ).fetchone()
                if unsupported is not None:
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
                failed = connection.execute(
                    """SELECT issuer,product_code,name,document_id,title,pdf_sha256,
                              pdf_size_bytes,page_count,reason_code,reason,attempts
                         FROM ocr_failed_products
                        WHERE issuer=? AND product_code=?""",
                    (issuer, product_code),
                ).fetchone()
                if failed is None:
                    return None
                failed_document = Document(
                    document_id=failed["document_id"],
                    issuer=failed["issuer"],
                    product_code=failed["product_code"],
                    title=failed["title"],
                    pdf_sha256=failed["pdf_sha256"],
                    pdf_size_bytes=failed["pdf_size_bytes"],
                    page_count=failed["page_count"],
                )
                return OCRFailedProduct(
                    issuer=failed["issuer"],
                    product_code=failed["product_code"],
                    name=failed["name"],
                    document=failed_document,
                    reason_code=failed["reason_code"],
                    reason=failed["reason"],
                    attempts=failed["attempts"],
                )
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
                if unsupported is None and handle.metadata.schema_id == "cardrag.serving-db.v4":
                    ocr_failed = connection.execute(
                        """SELECT issuer,product_code,name,document_id,title,pdf_sha256,
                                  pdf_size_bytes,page_count,reason_code,reason,attempts
                             FROM ocr_failed_products
                            WHERE issuer=? AND product_code=?""",
                        (issuer, product_code),
                    ).fetchone()
                else:
                    ocr_failed = None
            else:
                unsupported = None
                ocr_failed = None
        if row is None:
            if unsupported is not None:
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
            if ocr_failed is None:
                return None
            failed_document = Document(
                document_id=ocr_failed["document_id"],
                issuer=ocr_failed["issuer"],
                product_code=ocr_failed["product_code"],
                title=ocr_failed["title"],
                pdf_sha256=ocr_failed["pdf_sha256"],
                pdf_size_bytes=ocr_failed["pdf_size_bytes"],
                page_count=ocr_failed["page_count"],
            )
            return OCRFailedProduct(
                issuer=ocr_failed["issuer"],
                product_code=ocr_failed["product_code"],
                name=ocr_failed["name"],
                document=failed_document,
                reason_code=ocr_failed["reason_code"],
                reason=ocr_failed["reason"],
                attempts=ocr_failed["attempts"],
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
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                row = connection.execute(
                    """SELECT r.document_id,l.issuer,l.product_code,l.name AS title,
                              r.pdf_sha256,r.pdf_size_bytes,r.page_count
                         FROM contract_revisions AS r
                         JOIN product_lineages AS l
                           ON l.product_lineage_id=r.product_lineage_id
                        WHERE r.document_id=?""",
                    (document_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM documents WHERE document_id=?", (document_id,)
                ).fetchone()
            if row is None and handle.metadata.schema_id in {
                "cardrag.serving-db.v4",
                "cardrag.serving-db.v5",
            }:
                row = connection.execute(
                    """SELECT document_id,issuer,product_code,title,pdf_sha256,
                              pdf_size_bytes,page_count
                         FROM ocr_failed_products WHERE document_id=?""",
                    (document_id,),
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
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                row = connection.execute(
                    """SELECT r.document_id,p.page,p.text,p.text_sha256,
                              r.page_count,r.pdf_sha256
                         FROM contract_revisions AS r
                         JOIN document_pages AS p
                           ON p.contract_revision_id=r.contract_revision_id
                        WHERE r.document_id=? AND p.page=?""",
                    (document_id, page),
                ).fetchone()
            else:
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
    ) -> tuple[Product | UnsupportedProduct | OCRFailedProduct, ...]:
        with self.store.pin() as handle:
            with handle.connect() as connection:
                if handle.metadata.schema_id == "cardrag.serving-db.v5":
                    sql = """SELECT DISTINCT l.issuer,l.product_code
                               FROM product_lineages AS l
                               JOIN contract_revisions AS r
                                 ON r.product_lineage_id=l.product_lineage_id
                              WHERE r.temporal_status='current'"""
                    v5_values: tuple[str, ...] = ()
                    if issuer is None:
                        sql += " UNION ALL SELECT issuer,product_code FROM unsupported_products"
                        sql += " UNION ALL SELECT issuer,product_code FROM ocr_failed_products"
                    else:
                        sql += " AND l.issuer=?"
                        sql += (
                            " UNION ALL SELECT issuer,product_code "
                            "FROM unsupported_products WHERE issuer=?"
                        )
                        sql += (
                            " UNION ALL SELECT issuer,product_code "
                            "FROM ocr_failed_products WHERE issuer=?"
                        )
                        v5_values = (issuer, issuer, issuer)
                    rows = connection.execute(
                        sql + " ORDER BY issuer,product_code", v5_values
                    ).fetchall()
                elif issuer is None:
                    sql = """
                    SELECT issuer,product_code FROM products
                    UNION ALL
                    SELECT issuer,product_code FROM unsupported_products
                    """
                    if handle.metadata.schema_id == "cardrag.serving-db.v4":
                        sql += " UNION ALL SELECT issuer,product_code FROM ocr_failed_products"
                    rows = connection.execute(sql + " ORDER BY issuer,product_code").fetchall()
                else:
                    sql = """
                    SELECT issuer,product_code FROM products WHERE issuer=?
                    UNION ALL
                    SELECT issuer,product_code FROM unsupported_products WHERE issuer=?
                    """
                    legacy_values: tuple[str, ...] = (issuer, issuer)
                    if handle.metadata.schema_id == "cardrag.serving-db.v4":
                        sql += (
                            " UNION ALL SELECT issuer,product_code "
                            "FROM ocr_failed_products WHERE issuer=?"
                        )
                        legacy_values += (issuer,)
                    rows = connection.execute(
                        sql + " ORDER BY product_code", legacy_values
                    ).fetchall()
            products = [self._get_product(handle, str(row[0]), str(row[1])) for row in rows]
        return tuple(product for product in products if product is not None)

    async def list_documents(self) -> tuple[Document, ...]:
        with self.store.pin() as handle:
            with handle.connect() as connection:
                if handle.metadata.schema_id == "cardrag.serving-db.v5":
                    sql = """SELECT r.document_id,l.issuer,l.product_code,l.name AS title,
                                     r.pdf_sha256,r.pdf_size_bytes,r.page_count
                                FROM contract_revisions AS r
                                JOIN product_lineages AS l
                                  ON l.product_lineage_id=r.product_lineage_id
                               WHERE r.temporal_status IN ('current','ambiguous')"""
                else:
                    sql = "SELECT * FROM documents"
                if handle.metadata.schema_id in {
                    "cardrag.serving-db.v4",
                    "cardrag.serving-db.v5",
                }:
                    sql += """ UNION ALL
                        SELECT document_id,issuer,product_code,title,pdf_sha256,
                               pdf_size_bytes,page_count FROM ocr_failed_products"""
                rows = connection.execute(sql + " ORDER BY document_id").fetchall()
        return tuple(Document(**dict(row)) for row in rows)

    async def list_pages(self, document_id: str) -> tuple[SourcePage, ...]:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._list_pages, handle, document_id)

    @staticmethod
    def _list_pages(handle: GenerationHandle, document_id: str) -> tuple[SourcePage, ...]:
        with handle.connect() as connection:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                rows = connection.execute(
                    """SELECT r.document_id,p.page,p.text,p.text_sha256,
                              r.page_count,r.pdf_sha256
                         FROM contract_revisions AS r
                         JOIN document_pages AS p
                           ON p.contract_revision_id=r.contract_revision_id
                        WHERE r.document_id=? ORDER BY p.page""",
                    (document_id,),
                ).fetchall()
            else:
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

    async def list_recent_products(
        self,
        months: int = 3,
        issuer: str | None = None,
    ) -> ProductCatalogPage:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._list_recent_products, handle, months, issuer)

    @staticmethod
    def _list_recent_products(
        handle: GenerationHandle,
        months: int,
        issuer: str | None,
    ) -> ProductCatalogPage:
        today = date.today()
        year = today.year
        month = today.month - months
        while month <= 0:
            year -= 1
            month += 12
        cutoff_date = date(year, month, 1)

        with handle.connect() as connection:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                launch_rows = connection.execute(
                    """SELECT contract_revision_id, display_text
                         FROM structure_nodes
                        WHERE display_text LIKE '%출시%'"""
                ).fetchall()
                rev_launch_dates: dict[str, date] = {}
                for row in launch_rows:
                    ld = parse_launch_date(str(row["display_text"]))
                    if ld is not None:
                        cr_id = str(row["contract_revision_id"])
                        if cr_id not in rev_launch_dates or ld > rev_launch_dates[cr_id]:
                            rev_launch_dates[cr_id] = ld

                sql = """SELECT pl.issuer, pl.product_code, pl.product_lineage_id,
                                pl.name AS product_name, pl.document_type,
                                cr.contract_revision_id, cr.effective_date, cr.temporal_status
                           FROM contract_revisions AS cr
                           JOIN product_lineages AS pl
                             ON pl.product_lineage_id = cr.product_lineage_id
                          WHERE cr.temporal_status = 'current'"""
                params: list[Any] = []
                if issuer is not None:
                    sql += " AND pl.issuer = ?"
                    params.append(issuer)

                products = connection.execute(sql, params).fetchall()
                items: list[ProductCatalogEntry] = []
                for p in products:
                    eff_raw = p["effective_date"]
                    eff_date = date.fromisoformat(str(eff_raw)) if eff_raw else None
                    cr_id = str(p["contract_revision_id"])
                    launch_d = rev_launch_dates.get(cr_id)
                    if (launch_d is not None and launch_d >= cutoff_date) or (
                        eff_date is not None and eff_date >= cutoff_date
                    ):
                        items.append(
                            ProductCatalogEntry(
                                issuer=str(p["issuer"]),
                                product_code=str(p["product_code"]),
                                product_lineage_id=str(p["product_lineage_id"]),
                                product_name=str(p["product_name"]),
                                document_type=str(p["document_type"]),
                                effective_date=eff_date,
                                launch_date=launch_d,
                                temporal_status=cast(TemporalStatus, str(p["temporal_status"])),
                            )
                        )
                items.sort(
                    key=lambda x: (
                        x.launch_date or x.effective_date or date.min,
                        x.effective_date or date.min,
                        x.product_name,
                    ),
                    reverse=True,
                )
                return ProductCatalogPage(
                    generation_id=handle.generation_id,
                    items=tuple(items),
                    total_count=len(items),
                )
            return ProductCatalogPage(
                generation_id=handle.generation_id,
                items=(),
                total_count=0,
            )

    async def find_products(
        self,
        keyword: str,
        issuer: str | None = None,
    ) -> ProductCatalogPage:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._find_products, handle, keyword, issuer)

    @staticmethod
    def _find_products(
        handle: GenerationHandle,
        keyword: str,
        issuer: str | None,
    ) -> ProductCatalogPage:
        norm_keyword = " ".join(unicodedata.normalize("NFKC", keyword).casefold().split())
        if not norm_keyword:
            raise ValueError("keyword must not be blank")

        with handle.connect() as connection:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                sql = """SELECT pl.issuer, pl.product_code, pl.product_lineage_id,
                                pl.name AS product_name, pl.document_type,
                                cr.contract_revision_id, cr.effective_date, cr.temporal_status
                           FROM contract_revisions AS cr
                           JOIN product_lineages AS pl
                             ON pl.product_lineage_id = cr.product_lineage_id
                          WHERE cr.temporal_status = 'current'"""
                params: list[Any] = []
                if issuer is not None:
                    sql += " AND pl.issuer = ?"
                    params.append(issuer)

                rows = connection.execute(sql, params).fetchall()
                matched_rows: list[sqlite3.Row] = []
                matched_cr_ids: list[str] = []
                for row in rows:
                    name_norm = " ".join(
                        unicodedata.normalize("NFKC", str(row["product_name"])).casefold().split()
                    )
                    if norm_keyword in name_norm:
                        matched_rows.append(row)
                        matched_cr_ids.append(str(row["contract_revision_id"]))

                rev_launch_dates: dict[str, date] = {}
                if matched_cr_ids:
                    placeholders = ",".join("?" for _ in matched_cr_ids)
                    launch_rows = connection.execute(
                        f"""SELECT contract_revision_id, display_text
                              FROM structure_nodes
                             WHERE contract_revision_id IN ({placeholders})
                               AND display_text LIKE '%출시%'""",  # noqa: S608 - placeholders only
                        matched_cr_ids,
                    ).fetchall()
                    for r in launch_rows:
                        ld = parse_launch_date(str(r["display_text"]))
                        if ld is not None:
                            c_id = str(r["contract_revision_id"])
                            if c_id not in rev_launch_dates or ld > rev_launch_dates[c_id]:
                                rev_launch_dates[c_id] = ld

                items: list[ProductCatalogEntry] = []
                for p in matched_rows:
                    eff_raw = p["effective_date"]
                    eff_date = date.fromisoformat(str(eff_raw)) if eff_raw else None
                    cr_id = str(p["contract_revision_id"])
                    items.append(
                        ProductCatalogEntry(
                            issuer=str(p["issuer"]),
                            product_code=str(p["product_code"]),
                            product_lineage_id=str(p["product_lineage_id"]),
                            product_name=str(p["product_name"]),
                            document_type=str(p["document_type"]),
                            effective_date=eff_date,
                            launch_date=rev_launch_dates.get(cr_id),
                            temporal_status=cast(TemporalStatus, str(p["temporal_status"])),
                        )
                    )
                items.sort(key=lambda x: (x.issuer, x.product_name))
                return ProductCatalogPage(
                    generation_id=handle.generation_id,
                    items=tuple(items),
                    total_count=len(items),
                )
            return ProductCatalogPage(
                generation_id=handle.generation_id,
                items=(),
                total_count=0,
            )

    async def find_cards_by_merchant(
        self,
        merchant_name: str,
        issuer: str | None = None,
    ) -> MerchantSearchPage:
        with self.store.pin() as handle:
            return await asyncio.to_thread(
                self._find_cards_by_merchant, handle, merchant_name, issuer
            )

    @staticmethod
    def _find_cards_by_merchant(
        handle: GenerationHandle,
        merchant_name: str,
        issuer: str | None,
    ) -> MerchantSearchPage:
        cleaned_query = merchant_name.strip()
        if not cleaned_query:
            raise ValueError("merchant_name must not be blank")

        with handle.connect() as connection:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                sql = """SELECT pl.issuer, pl.product_code, pl.name AS product_name,
                                sn.display_text
                           FROM structure_nodes AS sn
                           JOIN contract_revisions AS cr
                             ON cr.contract_revision_id = sn.contract_revision_id
                           JOIN product_lineages AS pl
                             ON pl.product_lineage_id = cr.product_lineage_id
                          WHERE cr.temporal_status = 'current'
                            AND sn.major_class IN ('BENEFIT', 'MIXED')
                            AND sn.display_text LIKE ?"""
                params: list[Any] = [f"%{cleaned_query}%"]
                if issuer is not None:
                    sql += " AND pl.issuer = ?"
                    params.append(issuer)
                sql += " ORDER BY pl.issuer, pl.product_code, sn.ordinal"

                rows = connection.execute(sql, params).fetchall()
                card_texts: dict[tuple[str, str, str], list[str]] = {}
                for r in rows:
                    key = (str(r["issuer"]), str(r["product_code"]), str(r["product_name"]))
                    txt = " ".join(str(r["display_text"]).split())
                    if len(txt) > 300:
                        txt = txt[:297] + "..."
                    if key not in card_texts:
                        card_texts[key] = []
                    if txt not in card_texts[key]:
                        card_texts[key].append(txt)

                hits = [
                    MerchantSearchHit(
                        issuer=k[0],
                        product_code=k[1],
                        product_name=k[2],
                        matched_texts=tuple(texts[:5]),
                    )
                    for k, texts in card_texts.items()
                ]
                return MerchantSearchPage(
                    generation_id=handle.generation_id,
                    merchant_query=cleaned_query,
                    items=tuple(hits),
                    total_count=len(hits),
                )
            return MerchantSearchPage(
                generation_id=handle.generation_id,
                merchant_query=cleaned_query,
                items=(),
                total_count=0,
            )

    async def get_product_summary(
        self,
        issuer: str,
        identifier: str,
    ) -> ProductSummary | None:
        with self.store.pin() as handle:
            return await asyncio.to_thread(self._get_product_summary, handle, issuer, identifier)

    @staticmethod
    def _get_product_summary(
        handle: GenerationHandle,
        issuer: str,
        identifier: str,
    ) -> ProductSummary | None:
        cleaned_id = identifier.strip()
        if not cleaned_id:
            raise ValueError("identifier must not be blank")

        with handle.connect() as connection:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                row = connection.execute(
                    """SELECT pl.issuer, pl.product_code, pl.name AS product_name,
                              cr.contract_revision_id, cr.effective_date
                         FROM product_lineages AS pl
                         JOIN contract_revisions AS cr
                           ON cr.product_lineage_id = pl.product_lineage_id
                        WHERE pl.issuer = ? AND pl.product_code = ?
                          AND cr.temporal_status = 'current'
                        ORDER BY cr.contract_revision_id
                        LIMIT 1""",
                    (issuer, cleaned_id),
                ).fetchone()

                if row is None:
                    norm_id = " ".join(unicodedata.normalize("NFKC", cleaned_id).casefold().split())
                    candidates = connection.execute(
                        """SELECT pl.issuer, pl.product_code, pl.name AS product_name,
                                  cr.contract_revision_id, cr.effective_date
                             FROM product_lineages AS pl
                             JOIN contract_revisions AS cr
                               ON cr.product_lineage_id = pl.product_lineage_id
                            WHERE pl.issuer = ?
                              AND cr.temporal_status = 'current'""",
                        (issuer,),
                    ).fetchall()
                    for cand in candidates:
                        cand_name = " ".join(
                            unicodedata.normalize("NFKC", str(cand["product_name"]))
                            .casefold()
                            .split()
                        )
                        if norm_id in cand_name:
                            row = cand
                            break

                if row is None:
                    return None

                cr_id = str(row["contract_revision_id"])
                p_issuer = str(row["issuer"])
                p_code = str(row["product_code"])
                p_name = str(row["product_name"])
                eff_raw = row["effective_date"]
                eff_date = date.fromisoformat(str(eff_raw)) if eff_raw else None

                nodes = connection.execute(
                    """SELECT node_type, major_class, raw_heading, display_text, ordinal
                         FROM structure_nodes
                        WHERE contract_revision_id = ?
                        ORDER BY ordinal""",
                    (cr_id,),
                ).fetchall()

                launch_d: date | None = None
                annual_fee: str | None = None
                benefit_headings: list[str] = []
                benefit_summaries: list[str] = []

                for n in nodes:
                    txt = str(n["display_text"]).strip()
                    mclass = str(n["major_class"])
                    ntype = str(n["node_type"])
                    heading = str(n["raw_heading"] or "").strip()

                    if launch_d is None and "출시" in txt:
                        launch_d = parse_launch_date(txt)

                    if annual_fee is None and "연회비" in txt:
                        cleaned_fee = " ".join(txt.split())
                        if len(cleaned_fee) > 10 and not any(
                            k in cleaned_fee for k in ["반환", "기준", "산정", "중도해지"]
                        ):
                            annual_fee = cleaned_fee[:250]

                    if mclass == "BENEFIT":
                        if heading and ntype in ("MAJOR_SECTION", "ITEM"):
                            h_clean = heading.replace("#", "").strip()
                            if h_clean and h_clean not in benefit_headings and len(h_clean) > 2:
                                if not any(
                                    k in h_clean
                                    for k in ["유의사항", "이용안내", "공통", "기준", "기타"]
                                ):
                                    benefit_headings.append(h_clean)
                        if (
                            ntype in ("ITEM", "PARAGRAPH", "TABLE_ROW")
                            and len(benefit_summaries) < 5
                        ):
                            b_clean = " ".join(txt.split())
                            benefit_keywords = (
                                "할인",
                                "적립",
                                "캐시백",
                                "면제",
                                "무료",
                                "제공",
                                "포인트",
                            )
                            ignore_keywords = (
                                "유의사항",
                                "연회비",
                                "금융소비자",
                                "기준",
                                "실적제외",
                            )
                            if any(w in b_clean for w in benefit_keywords):
                                if not any(k in b_clean for k in ignore_keywords):
                                    if len(b_clean) > 10 and b_clean not in benefit_summaries:
                                        benefit_summaries.append(b_clean[:180])

                return ProductSummary(
                    generation_id=handle.generation_id,
                    issuer=p_issuer,
                    product_code=p_code,
                    product_name=p_name,
                    effective_date=eff_date,
                    launch_date=launch_d,
                    annual_fee_text=annual_fee,
                    benefit_headings=tuple(benefit_headings[:5]),
                    benefit_summary_texts=tuple(benefit_summaries[:5]),
                )
            return None
