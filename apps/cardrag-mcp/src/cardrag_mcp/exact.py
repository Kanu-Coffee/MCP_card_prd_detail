"""Structure-preserving v5 exact search and contract bundle expansion."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray

from cardrag_mcp.aggregation import aggregate_document_view_scores, exhaustive_profile_id
from cardrag_mcp.audit import (
    AuditContractScore,
    AuditNodeScore,
    AuditViewScore,
    ExhaustiveAuditError,
    ExhaustiveAuditLedger,
    ExhaustiveAuditStore,
    ExpectedContract,
    LoadedAudit,
    query_vector_sha256,
)
from cardrag_mcp.embeddings import OpenRouterEmbedder
from cardrag_mcp.models import (
    MAX_SEARCH_RESPONSE_CHARACTERS,
    MAX_SEARCH_RESPONSE_NODES,
    ContractBundle,
    ContractEvidenceBundle,
    ContractRevisionSummary,
    ContractSearchPage,
    ContractSearchRequest,
    DocumentAggregationPolicy,
    ProductRevisionList,
    ScoredEmbeddingView,
    ScoredStructureNode,
    SearchCoverage,
    SourceSpan,
    StructureNode,
    StructureNodeLink,
    ViewType,
)
from cardrag_mcp.reranker import (
    RerankerCandidate,
    RerankerShadowDiagnostics,
    RerankerShadowLane,
    reranker_candidate_id,
)
from cardrag_mcp.schema_v5 import LoadedVectorsV5
from cardrag_mcp.store import GenerationHandle, GenerationStore

# Advanced mmap indexing materializes a dense copy, so keep each 4,096D
# float32 scoring block at roughly 8 MiB instead of an unaccounted 128 MiB.
VECTOR_BLOCK_ROWS = 512
EXHAUSTIVE_CONTRACTS_PER_CALL = 8
FULL_CONTRACT_CONTEXT_NODE_LIMIT = 512
FULL_CONTRACT_CONTEXT_CHARACTER_LIMIT = 500_000
_VIEW_ORDER: dict[ViewType, int] = {
    "TITLE": 0,
    "RAW_ITEM": 1,
    "CONTEXTUAL_ITEM": 2,
    "DETAIL": 3,
    "MAJOR_SECTION": 4,
    "CONTRACT": 5,
}
_REVISION_SELECT = """SELECT r.contract_revision_id,r.product_lineage_id,r.document_id,
                              l.issuer,l.product_code,l.name AS product_name,l.document_type,
                              r.source_id,r.source_version,r.source_url,r.effective_date,
                              r.temporal_status,r.supersedes_revision_id,r.pdf_sha256,r.page_count
                         FROM contract_revisions AS r
                         JOIN product_lineages AS l
                           ON l.product_lineage_id=r.product_lineage_id"""


def _fts_expression(query: str) -> str:
    tokens = [part for part in query.split() if part]
    if not tokens:
        raise ValueError("query must not be blank")
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _canonical_catalog_text(value: str) -> str:
    """Match product names across width, case, and whitespace variants."""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _graph_context_character_count(nodes: Sequence[StructureNode]) -> int:
    return sum(
        len(node.display_text)
        + len(node.raw_heading or "")
        + sum(len(value) for value in node.table_headers)
        + sum(len(value) for value in node.table_cells)
        + sum(len(span.text) for span in node.spans)
        for node in nodes
    )


@dataclass(frozen=True, slots=True)
class _ViewScore:
    row_index: int
    view_type: ViewType
    score: float


@dataclass(frozen=True, slots=True)
class _NodeScore:
    score: float
    view_types: tuple[ViewType, ...]
    views: tuple[_ViewScore, ...]


@dataclass(frozen=True, slots=True)
class _ExhaustiveResult:
    node_scores: dict[tuple[str, str], _NodeScore]
    contract_scores: dict[str, float]
    expected_rows: int
    scored_rows: int
    exact_blocks: int
    job_id: str
    resumed: bool
    status: Literal["running", "complete"]
    artifact_sha256: str | None
    profile_id: str


@dataclass(frozen=True, slots=True)
class ExactCapturedRow:
    """One actual v5 matrix-row score with immutable row provenance."""

    row_index: int
    contract_revision_id: str
    node_id: str
    view_type: ViewType
    input_sha256: str
    embedding_profile_id: str
    score: float


@dataclass(frozen=True, slots=True)
class ExactScoreCapture:
    """All unscoped active-row scores for one gold query."""

    query_sha256: str
    query_vector_sha256: str
    expected_active_contracts: int
    expected_rows: int
    scored_rows: int
    exact_blocks: int
    rows: tuple[ExactCapturedRow, ...]


@dataclass(frozen=True, slots=True)
class _CatalogResolution:
    status: Literal["explicit", "resolved", "unresolved", "ambiguous"]
    candidate_count: int
    product_lineage_id: str | None
    product_name: str | None


@dataclass(frozen=True, slots=True)
class _LexicalAudit:
    enabled: bool
    status: Literal["succeeded", "failed", "deferred"]
    error: Literal["fts_unavailable", "query_invalid", "internal_error"] | None
    nodes_by_revision: dict[str, set[str]]


def _summary(row: sqlite3.Row) -> ContractRevisionSummary:
    raw_effective = row["effective_date"]
    return ContractRevisionSummary(
        product_lineage_id=row["product_lineage_id"],
        contract_revision_id=row["contract_revision_id"],
        document_id=row["document_id"],
        issuer=row["issuer"],
        product_code=row["product_code"],
        product_name=row["product_name"],
        document_type=row["document_type"],
        source_id=row["source_id"],
        source_version=row["source_version"],
        source_url=row["source_url"],
        effective_date=None if raw_effective is None else date.fromisoformat(str(raw_effective)),
        temporal_status=row["temporal_status"],
        supersedes_revision_id=row["supersedes_revision_id"],
        pdf_sha256=row["pdf_sha256"],
        page_count=row["page_count"],
    )


class V5ExactRepository:
    """Execute every active v5 row and expand hits only inside their contract."""

    def __init__(
        self,
        store: GenerationStore,
        embedder: OpenRouterEmbedder,
        reranker_shadow: RerankerShadowLane | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.audit_store = ExhaustiveAuditStore(store.root)
        self.reranker_shadow = reranker_shadow

    async def search(
        self,
        request: ContractSearchRequest,
        *,
        handle: GenerationHandle | None = None,
    ) -> ContractSearchPage:
        if request.mode == "exhaustive" and (
            request.issuer is not None
            or request.product_lineage_id is not None
            or request.as_of is not None
            or request.include_history
        ):
            raise ValueError(
                "exhaustive mode audits the unscoped current corpus; "
                "issuer, product_lineage_id, as_of, and include_history are unsupported"
            )
        pin = self.store.pin() if handle is None else nullcontext(handle)
        with pin as pinned:
            vectors = self._vectors(pinned)
            aggregation_policy = pinned.metadata.document_aggregation_policy
            aggregation_profile_sha256 = pinned.metadata.sealed_profile_sha256
            audit_profile_id = exhaustive_profile_id(
                policy=aggregation_policy,
                sealed_profile_sha256=aggregation_profile_sha256,
            )
            catalog = (
                self._resolve_catalog(pinned, request)
                if request.mode == "exact"
                else _CatalogResolution("unresolved", 0, None, None)
            )
            effective_request = (
                request.model_copy(update={"product_lineage_id": catalog.product_lineage_id})
                if catalog.status == "resolved"
                else request
            )
            revisions = self._active_revisions(pinned, effective_request)
            profile = self._primary_profile(pinned)
            active_ids = {item.contract_revision_id for item in revisions}
            row_indices = [
                index
                for index, revision_id in enumerate(vectors.contract_revision_ids)
                if revision_id in active_ids
            ]
            if request.mode == "exact":
                query_vector = await self._embed_query(request.query, profile)
                started = time.perf_counter()
                scores_by_node, exact_blocks = self._score_rows(
                    vectors,
                    row_indices,
                    query_vector,
                )
                node_scores, contract_scores = self._collapse_scores(
                    scores_by_node,
                    policy=aggregation_policy,
                )
                expected_rows = len(row_indices)
                scored_rows = expected_rows
                elapsed_ms = (time.perf_counter() - started) * 1000
                exhaustive: _ExhaustiveResult | None = None
                search_complete = True
            else:
                started = time.perf_counter()
                exhaustive = await self._exhaustive_scores(
                    pinned,
                    vectors,
                    revisions,
                    row_indices,
                    request.query,
                    profile,
                    aggregation_policy,
                    audit_profile_id,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                node_scores = exhaustive.node_scores
                contract_scores = exhaustive.contract_scores
                expected_rows = exhaustive.expected_rows
                scored_rows = exhaustive.scored_rows
                exact_blocks = exhaustive.exact_blocks
                search_complete = exhaustive.status == "complete"
            if search_complete and len(contract_scores) != len(revisions):
                raise RuntimeError("one or more active contracts have no scored embedding rows")

            if search_complete:
                ranked_revision_ids = sorted(
                    contract_scores,
                    key=lambda value: (-contract_scores[value], value),
                )[: request.limit]
                lexical_scope_request = effective_request.model_copy(
                    update={"issuer": None, "product_lineage_id": None}
                )
                lexical_active_ids = {
                    item.contract_revision_id
                    for item in self._active_revisions(pinned, lexical_scope_request)
                }
                lexical = self._lexical_audit(
                    pinned,
                    request.query,
                    lexical_active_ids,
                )
            else:
                ranked_revision_ids = []
                lexical_active_ids = set()
                lexical = _LexicalAudit(True, "deferred", None, {})

            dense_top_by_revision: dict[str, set[str]] = defaultdict(set)
            for (revision_id, node_id), _value in sorted(
                node_scores.items(),
                key=lambda item: (-item[1].score, item[0][0], item[0][1]),
            ):
                if len(dense_top_by_revision[revision_id]) < 8:
                    dense_top_by_revision[revision_id].add(node_id)
            lexical_global_matched_count = sum(
                len(nodes)
                for revision_id, nodes in lexical.nodes_by_revision.items()
                if revision_id in lexical_active_ids
            )
            lexical_global_additional_count = sum(
                len(nodes - dense_top_by_revision.get(revision_id, set()))
                for revision_id, nodes in lexical.nodes_by_revision.items()
                if revision_id in lexical_active_ids
            )

            additional_count = 0
            summaries = {item.contract_revision_id: item for item in revisions}
            bundles: list[ContractEvidenceBundle] = []
            product_specific_full = (
                catalog.status in {"explicit", "resolved"} and len(revisions) == 1
            )
            response_node_count = 0
            response_character_count = 0
            response_truncated = False
            full_contract_fallback_count = 0
            for revision_id in ranked_revision_ids:
                ranked_nodes = sorted(
                    (
                        (node_id, value)
                        for (candidate_revision, node_id), value in node_scores.items()
                        if candidate_revision == revision_id
                    ),
                    key=lambda item: (-item[1].score, item[0]),
                )
                dense_hit_ids = [node_id for node_id, _ in ranked_nodes[:8]]
                lexical_hit_ids = sorted(
                    lexical.nodes_by_revision.get(revision_id, set()) - set(dense_hit_ids)
                )
                candidate_bundle, used_full_fallback = self._build_evidence_bundle(
                    pinned,
                    revision_id,
                    summaries[revision_id],
                    dense_hit_ids + lexical_hit_ids,
                    set(lexical_hit_ids),
                    node_scores,
                    product_specific_full=product_specific_full,
                )
                candidate_characters = candidate_bundle.context_character_count
                exceeds_budget = (
                    response_node_count + len(candidate_bundle.nodes) > MAX_SEARCH_RESPONSE_NODES
                    or response_character_count + candidate_characters
                    > MAX_SEARCH_RESPONSE_CHARACTERS
                )
                emitted_lexical_count = len(lexical_hit_ids)
                if exceeds_budget and lexical_hit_ids:
                    # The lexical lane is diagnostic-only. A lexical expansion
                    # must never crowd an already exact-ranked dense bundle out
                    # of the sealed response budget.
                    candidate_bundle, used_full_fallback = self._build_evidence_bundle(
                        pinned,
                        revision_id,
                        summaries[revision_id],
                        dense_hit_ids,
                        set(),
                        node_scores,
                        product_specific_full=product_specific_full,
                    )
                    candidate_characters = candidate_bundle.context_character_count
                    exceeds_budget = (
                        response_node_count + len(candidate_bundle.nodes)
                        > MAX_SEARCH_RESPONSE_NODES
                        or response_character_count + candidate_characters
                        > MAX_SEARCH_RESPONSE_CHARACTERS
                    )
                    emitted_lexical_count = 0
                    response_truncated = True
                if exceeds_budget:
                    response_truncated = True
                    break
                bundles.append(candidate_bundle)
                additional_count += emitted_lexical_count
                full_contract_fallback_count += int(used_full_fallback)
                response_node_count += len(candidate_bundle.nodes)
                response_character_count += candidate_characters
            temporal_scope: Literal["current", "as_of", "history"] = (
                "history"
                if request.include_history
                else "as_of"
                if request.as_of is not None
                else "current"
            )
            reranker_diagnostics: RerankerShadowDiagnostics | None = None
            if self.reranker_shadow is not None and search_complete:
                reranker_diagnostics = await self.reranker_shadow.observe(
                    generation_id=pinned.generation_id,
                    query=request.query,
                    candidates=self._reranker_candidates(bundles),
                )
            coverage = SearchCoverage(
                generation_id=pinned.generation_id,
                search_mode=request.mode,
                temporal_scope=temporal_scope,
                expected_active_contracts=len(revisions),
                scored_contracts=len(contract_scores),
                expected_embedding_rows=expected_rows,
                scored_embedding_rows=scored_rows,
                document_aggregation_status=pinned.metadata.document_aggregation_status,
                document_aggregation_policy=aggregation_policy,
                sealed_profile_sha256=aggregation_profile_sha256,
                exact_row_corpus_sha256=pinned.metadata.exact_row_corpus_sha256,
                exact_search_milliseconds=elapsed_ms,
                exact_blocks=exact_blocks,
                lexical_additional_evidence_count=additional_count,
                lexical_enabled=lexical.enabled,
                lexical_status=lexical.status,
                lexical_error=lexical.error,
                lexical_global_matched_evidence_count=lexical_global_matched_count,
                lexical_global_additional_evidence_count=lexical_global_additional_count,
                catalog_resolution_status=catalog.status,
                catalog_candidate_count=catalog.candidate_count,
                catalog_resolved_product_lineage_id=catalog.product_lineage_id,
                catalog_resolved_product_name=catalog.product_name,
                response_node_count=response_node_count,
                response_character_count=response_character_count,
                response_truncated=response_truncated,
                full_contract_fallback_count=full_contract_fallback_count,
                reranker_shadow_status=(
                    None if reranker_diagnostics is None else reranker_diagnostics.status
                ),
                reranker_shadow_candidate_count=(
                    None if reranker_diagnostics is None else reranker_diagnostics.candidate_count
                ),
                reranker_shadow_rank_change_count=(
                    None if reranker_diagnostics is None else reranker_diagnostics.rank_change_count
                ),
                reranker_shadow_artifact_sha256=(
                    None if reranker_diagnostics is None else reranker_diagnostics.artifact_sha256
                ),
                reranker_shadow_failure_reason=(
                    None if reranker_diagnostics is None else reranker_diagnostics.failure_reason
                ),
                exhaustive_job_id=None if exhaustive is None else exhaustive.job_id,
                exhaustive_status=None if exhaustive is None else exhaustive.status,
                exhaustive_profile_id=(None if exhaustive is None else exhaustive.profile_id),
                exhaustive_completed_contracts=(
                    None if exhaustive is None else len(exhaustive.contract_scores)
                ),
                exhaustive_total_contracts=(None if exhaustive is None else len(revisions)),
                exhaustive_resumed=None if exhaustive is None else exhaustive.resumed,
                exhaustive_artifact_sha256=(
                    None if exhaustive is None else exhaustive.artifact_sha256
                ),
            )
            return ContractSearchPage(
                generation_id=pinned.generation_id,
                bundles=tuple(bundles),
                coverage=coverage,
            )

    @staticmethod
    def _build_evidence_bundle(
        handle: GenerationHandle,
        revision_id: str,
        summary: ContractRevisionSummary,
        initial_ids: Sequence[str],
        lexical_hit_ids: set[str],
        node_scores: dict[tuple[str, str], _NodeScore],
        *,
        product_specific_full: bool,
    ) -> tuple[ContractEvidenceBundle, bool]:
        full_fallback = product_specific_full and (
            V5ExactRepository._full_contract_exceeds_context_limit(handle, revision_id)
        )
        graph, parent_expansion_count, linked_notice_count = V5ExactRepository._expanded_graph(
            handle,
            revision_id,
            initial_ids,
            full=product_specific_full and not full_fallback,
            scope="full",
            include_links=True,
        )
        if (
            product_specific_full
            and not full_fallback
            and (
                len(graph) > FULL_CONTRACT_CONTEXT_NODE_LIMIT
                or _graph_context_character_count(graph) > FULL_CONTRACT_CONTEXT_CHARACTER_LIMIT
            )
        ):
            graph, parent_expansion_count, linked_notice_count = V5ExactRepository._expanded_graph(
                handle,
                revision_id,
                initial_ids,
                full=False,
                scope="full",
                include_links=True,
            )
            full_fallback = True
        by_id = {node.node_id: node for node in graph}
        matches: list[ScoredStructureNode] = []
        for node_id in initial_ids:
            node = by_id.get(node_id)
            node_score = node_scores.get((revision_id, node_id))
            if node is None or node_score is None:
                continue
            matches.append(
                ScoredStructureNode(
                    node=node,
                    score=node_score.score,
                    matched_view_types=node_score.view_types,
                    matched_views=V5ExactRepository._matched_views(
                        handle,
                        revision_id,
                        node_id,
                        node_score,
                    ),
                    lexical_only=node_id in lexical_hit_ids,
                )
            )
        return (
            ContractEvidenceBundle(
                contract=summary,
                matches=tuple(matches),
                nodes=graph,
                linked_notice_count=linked_notice_count,
                parent_expansion_count=parent_expansion_count,
            ),
            full_fallback,
        )

    @staticmethod
    def _full_contract_exceeds_context_limit(
        handle: GenerationHandle,
        revision_id: str,
    ) -> bool:
        """Reject an oversized full graph before constructing its response models."""

        with handle.connect() as connection:
            node_row = connection.execute(
                """SELECT COUNT(*),
                          COALESCE(SUM(
                              length(display_text)+length(COALESCE(raw_heading,''))+
                              length(table_headers_json)+length(table_cells_json)
                          ),0)
                     FROM structure_nodes WHERE contract_revision_id=?""",
                (revision_id,),
            ).fetchone()
            span_row = connection.execute(
                """SELECT COALESCE(SUM(source_end-source_start),0)
                     FROM node_spans WHERE contract_revision_id=?""",
                (revision_id,),
            ).fetchone()
        if node_row is None or span_row is None:
            raise RuntimeError("full contract context preflight disappeared")
        return (
            int(node_row[0]) > FULL_CONTRACT_CONTEXT_NODE_LIMIT
            or int(node_row[1]) + int(span_row[0]) > FULL_CONTRACT_CONTEXT_CHARACTER_LIMIT
        )

    @staticmethod
    def _reranker_candidates(
        bundles: Sequence[ContractEvidenceBundle],
    ) -> tuple[RerankerCandidate, ...]:
        dense_matches = sorted(
            (
                (bundle.contract.contract_revision_id, match)
                for bundle in bundles
                for match in bundle.matches
                if not match.lexical_only
            ),
            key=lambda item: (-item[1].score, item[0], item[1].node.node_id),
        )
        return tuple(
            RerankerCandidate(
                candidate_id=reranker_candidate_id(revision_id, match.node.node_id),
                contract_revision_id=revision_id,
                node_id=match.node.node_id,
                display_text=max(
                    match.matched_views,
                    key=lambda view: (view.score, -_VIEW_ORDER[view.view_type]),
                ).display_text,
                dense_rank=dense_rank,
                dense_score=match.score,
                matched_view_types=tuple(match.matched_view_types),
            )
            for dense_rank, (revision_id, match) in enumerate(dense_matches, start=1)
        )

    async def _embed_query(
        self,
        query: str,
        profile: dict[str, str],
    ) -> NDArray[np.float32]:
        raw = await self.embedder.embed(
            query,
            provider=profile["provider"],
            model=profile["model"],
            dimension=int(profile["dimension"]),
            query_policy=profile["query_policy"],
            provider_id=profile["provider_id"],
        )
        vector = np.asarray(raw, dtype="<f4")
        if vector.shape != (4096,) or not bool(np.isfinite(vector).all()):
            raise RuntimeError("v5 query embedding is not a finite 4096D vector")
        return vector

    async def capture_unscoped_current_scores(
        self,
        query: str,
        *,
        handle: GenerationHandle,
    ) -> ExactScoreCapture:
        """Capture every score produced by the real exact scorer for evaluation."""

        vectors = self._vectors(handle)
        request = ContractSearchRequest(query=query)
        revisions = self._active_revisions(handle, request)
        active_ids = {item.contract_revision_id for item in revisions}
        row_indices = [
            index
            for index, revision_id in enumerate(vectors.contract_revision_ids)
            if revision_id in active_ids
        ]
        query_vector = await self._embed_query(request.query, self._primary_profile(handle))
        raw_scores: dict[int, float] = {}
        _scores_by_node, exact_blocks = self._score_rows(
            vectors,
            row_indices,
            query_vector,
            raw_scores=raw_scores,
        )
        if len(raw_scores) != len(row_indices):
            raise RuntimeError("v5 score capture did not record every active row")
        selected = set(row_indices)
        with handle.connect() as connection:
            provenance = {
                int(row[0]): (
                    str(row[1]),
                    str(row[2]),
                    cast(ViewType, str(row[3])),
                    str(row[4]),
                    str(row[5]),
                )
                for row in connection.execute(
                    """SELECT row_index,contract_revision_id,node_id,view_type,
                              input_sha256,profile_id
                         FROM embedding_views ORDER BY row_index"""
                )
                if int(row[0]) in selected
            }
        if set(provenance) != selected:
            raise RuntimeError("v5 score capture row provenance is incomplete")
        rows = tuple(
            ExactCapturedRow(
                row_index=row_index,
                contract_revision_id=provenance[row_index][0],
                node_id=provenance[row_index][1],
                view_type=provenance[row_index][2],
                input_sha256=provenance[row_index][3],
                embedding_profile_id=provenance[row_index][4],
                score=raw_scores[row_index],
            )
            for row_index in row_indices
        )
        return ExactScoreCapture(
            query_sha256=hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
            query_vector_sha256=query_vector_sha256(query_vector),
            expected_active_contracts=len(revisions),
            expected_rows=len(row_indices),
            scored_rows=len(rows),
            exact_blocks=exact_blocks,
            rows=rows,
        )

    @staticmethod
    def _score_rows(
        vectors: LoadedVectorsV5,
        row_indices: Sequence[int],
        query_vector: NDArray[np.float32],
        *,
        raw_scores: dict[int, float] | None = None,
    ) -> tuple[dict[tuple[str, str], dict[ViewType, _ViewScore]], int]:
        scores_by_node: dict[tuple[str, str], dict[ViewType, _ViewScore]] = defaultdict(dict)
        exact_blocks = 0
        for start in range(0, len(row_indices), VECTOR_BLOCK_ROWS):
            selected = np.asarray(row_indices[start : start + VECTOR_BLOCK_ROWS], dtype=np.intp)
            if not selected.size:
                continue
            exact_blocks += 1
            scores = vectors.matrix[selected] @ query_vector
            if not bool(np.isfinite(scores).all()):
                raise RuntimeError("v5 exact scoring produced a non-finite score")
            for matrix_index, score_value in zip(selected, scores, strict=True):
                index = int(matrix_index)
                key = (vectors.contract_revision_ids[index], vectors.node_ids[index])
                lane = cast(ViewType, vectors.view_types[index])
                dense_score = float(score_value)
                if raw_scores is not None:
                    if index in raw_scores:
                        raise RuntimeError("v5 exact scoring visited one row twice")
                    raw_scores[index] = dense_score
                previous = scores_by_node[key].get(lane)
                if previous is None or dense_score > previous.score:
                    scores_by_node[key][lane] = _ViewScore(
                        row_index=index,
                        view_type=lane,
                        score=dense_score,
                    )
        return scores_by_node, exact_blocks

    @staticmethod
    def _collapse_scores(
        scores_by_node: dict[tuple[str, str], dict[ViewType, _ViewScore]],
        *,
        policy: DocumentAggregationPolicy,
    ) -> tuple[dict[tuple[str, str], _NodeScore], dict[str, float]]:
        view_scores_by_contract: defaultdict[str, list[tuple[ViewType, float]]] = defaultdict(list)
        node_scores: dict[tuple[str, str], _NodeScore] = {}
        for key, lanes in scores_by_node.items():
            best = max(value.score for value in lanes.values())
            views = tuple(
                sorted(
                    lanes.values(),
                    key=lambda value: (_VIEW_ORDER[value.view_type], value.row_index),
                )
            )
            view_types = tuple(
                view.view_type for view in views if math.isclose(view.score, best, abs_tol=1e-7)
            )
            node_scores[key] = _NodeScore(
                score=best,
                view_types=view_types,
                views=views,
            )
            view_scores_by_contract[key[0]].extend((view.view_type, view.score) for view in views)
        contract_scores = {
            revision_id: aggregate_document_view_scores(view_scores, policy)
            for revision_id, view_scores in view_scores_by_contract.items()
        }
        return node_scores, contract_scores

    @staticmethod
    def _matched_views(
        handle: GenerationHandle,
        revision_id: str,
        node_id: str,
        node_score: _NodeScore,
    ) -> tuple[ScoredEmbeddingView, ...]:
        row_indices = tuple(view.row_index for view in node_score.views)
        placeholders = ",".join("?" for _ in row_indices)
        with handle.connect() as connection:
            rows = connection.execute(
                f"""SELECT v.row_index,v.node_id,v.contract_revision_id,v.view_type,
                           v.display_text,s.page,s.source_start,s.source_end,s.text_sha256,
                           s.span_ordinal,p.text
                      FROM embedding_views AS v
                      JOIN embedding_view_spans AS s ON s.row_index=v.row_index
                      JOIN document_pages AS p
                        ON p.contract_revision_id=s.contract_revision_id AND p.page=s.page
                     WHERE v.row_index IN ({placeholders})
                     ORDER BY v.row_index,s.span_ordinal""",  # noqa: S608 - placeholders only
                row_indices,
            ).fetchall()
        metadata: dict[int, tuple[str, str, ViewType, str]] = {}
        spans: defaultdict[int, list[SourceSpan]] = defaultdict(list)
        for row in rows:
            row_index = int(row[0])
            metadata[row_index] = (
                str(row[1]),
                str(row[2]),
                cast(ViewType, str(row[3])),
                str(row[4]),
            )
            start, end = int(row[6]), int(row[7])
            page_text = str(row[10])
            spans[row_index].append(
                SourceSpan(
                    node_id=node_id,
                    page=int(row[5]),
                    source_start=start,
                    source_end=end,
                    text_sha256=str(row[8]),
                    span_ordinal=int(row[9]),
                    text=page_text[start:end],
                )
            )
        result: list[ScoredEmbeddingView] = []
        for view in node_score.views:
            row_metadata = metadata.get(view.row_index)
            if row_metadata is None or row_metadata[:3] != (
                node_id,
                revision_id,
                view.view_type,
            ):
                raise RuntimeError("scored embedding view provenance disappeared")
            result.append(
                ScoredEmbeddingView(
                    row_index=view.row_index,
                    view_type=view.view_type,
                    score=view.score,
                    display_text=row_metadata[3],
                    spans=tuple(spans[view.row_index]),
                )
            )
        return tuple(result)

    async def _exhaustive_scores(
        self,
        handle: GenerationHandle,
        vectors: LoadedVectorsV5,
        revisions: tuple[ContractRevisionSummary, ...],
        row_indices: Sequence[int],
        query: str,
        profile: dict[str, str],
        aggregation_policy: DocumentAggregationPolicy,
        audit_profile_id: str,
    ) -> _ExhaustiveResult:
        rows_by_contract: dict[str, list[int]] = {
            item.contract_revision_id: [] for item in revisions
        }
        for index in row_indices:
            revision_id = vectors.contract_revision_ids[index]
            if revision_id in rows_by_contract:
                rows_by_contract[revision_id].append(index)
        missing = sorted(
            revision_id for revision_id, indices in rows_by_contract.items() if not indices
        )
        if missing:
            raise RuntimeError(
                "one or more active contracts have no scored embedding rows: " + ",".join(missing)
            )
        expected_contracts = tuple(
            ExpectedContract(
                contract_revision_id=revision_id,
                embedding_rows=len(rows_by_contract[revision_id]),
            )
            for revision_id in sorted(rows_by_contract)
        )
        query_sha256 = hashlib.sha256(query.encode("utf-8")).hexdigest()
        identity = self.audit_store.identity(
            handle.generation_id,
            query_sha256,
            audit_profile_id,
            document_aggregation_policy=aggregation_policy,
            sealed_profile_sha256=handle.metadata.sealed_profile_sha256,
        )
        loaded = self.audit_store.load(identity, expected_contracts)
        resumed = loaded is not None
        if loaded is not None and loaded.artifact_sha256 is not None:
            completed = loaded
        else:
            if loaded is None:
                query_vector = self.audit_store.load_query_vector(identity)
                if query_vector is None:
                    query_vector = await self._embed_query(query, profile)
                else:
                    resumed = True
                ledger = self.audit_store.begin(
                    identity,
                    expected_contracts,
                    query_vector=query_vector,
                )
            else:
                ledger = loaded.ledger
                query_vector = loaded.query_vector
            remaining = expected_contracts[len(ledger.completed_contracts) :]
            for expected in remaining[:EXHAUSTIVE_CONTRACTS_PER_CALL]:
                revision_id = expected.contract_revision_id
                scores_by_node, exact_blocks = self._score_rows(
                    vectors,
                    rows_by_contract[revision_id],
                    query_vector,
                )
                node_scores, contract_scores = self._collapse_scores(
                    scores_by_node,
                    policy=aggregation_policy,
                )
                if set(contract_scores) != {revision_id}:
                    raise ExhaustiveAuditError(
                        "exhaustive contract batch did not produce exactly one contract score"
                    )
                nodes = tuple(
                    AuditNodeScore(
                        node_id=node_id,
                        score=value.score,
                        matched_view_types=value.view_types,
                        views=tuple(
                            AuditViewScore(
                                row_index=view.row_index,
                                view_type=view.view_type,
                                score=view.score,
                            )
                            for view in value.views
                        ),
                    )
                    for (candidate_revision, node_id), value in sorted(node_scores.items())
                    if candidate_revision == revision_id
                )
                contract = AuditContractScore(
                    contract_revision_id=revision_id,
                    aggregation_policy=aggregation_policy,
                    score=contract_scores[revision_id],
                    scored_embedding_rows=len(rows_by_contract[revision_id]),
                    exact_blocks=exact_blocks,
                    nodes=nodes,
                )
                ledger = self.audit_store.checkpoint(identity, ledger, contract)
            if len(ledger.completed_contracts) == len(expected_contracts):
                completed = self.audit_store.complete(identity, ledger)
            else:
                completed = LoadedAudit(
                    ledger=ledger,
                    query_vector=query_vector,
                    resumed=bool(ledger.completed_contracts),
                )
        return self._result_from_audit(
            completed,
            resumed=resumed,
            profile_id=audit_profile_id,
        )

    @staticmethod
    def _result_from_audit(
        loaded: LoadedAudit,
        *,
        resumed: bool,
        profile_id: str,
    ) -> _ExhaustiveResult:
        ledger: ExhaustiveAuditLedger = loaded.ledger
        status: Literal["running", "complete"] = (
            "complete" if ledger.status == "complete" else "running"
        )
        if status == "complete" and loaded.artifact_sha256 is None:
            raise ExhaustiveAuditError("complete exhaustive audit has no immutable artifact")
        if status == "running" and loaded.artifact_sha256 is not None:
            raise ExhaustiveAuditError("running exhaustive audit unexpectedly has an artifact")
        node_scores: dict[tuple[str, str], _NodeScore] = {}
        contract_scores: dict[str, float] = {}
        for contract in ledger.completed_contracts:
            contract_scores[contract.contract_revision_id] = contract.score
            for node in contract.nodes:
                node_scores[(contract.contract_revision_id, node.node_id)] = _NodeScore(
                    node.score,
                    node.matched_view_types,
                    tuple(
                        _ViewScore(view.row_index, view.view_type, view.score)
                        for view in node.views
                    ),
                )
        return _ExhaustiveResult(
            node_scores=node_scores,
            contract_scores=contract_scores,
            expected_rows=ledger.expected_embedding_rows,
            scored_rows=ledger.scored_embedding_rows,
            exact_blocks=ledger.exact_blocks,
            job_id=ledger.job_id,
            resumed=resumed,
            status=status,
            artifact_sha256=loaded.artifact_sha256,
            profile_id=profile_id,
        )

    def get_bundle(
        self,
        contract_revision_id: str,
        *,
        scope: Literal["full", "benefits", "notices"],
        include_links: bool,
        handle: GenerationHandle | None = None,
    ) -> ContractBundle | None:
        pin = self.store.pin() if handle is None else nullcontext(handle)
        with pin as pinned:
            self._vectors(pinned)
            summary = self._revision(pinned, contract_revision_id)
            if summary is None:
                return None
            nodes, _, _ = self._expanded_graph(
                pinned,
                contract_revision_id,
                (),
                full=True,
                scope=scope,
                include_links=include_links,
            )
            return ContractBundle(
                generation_id=pinned.generation_id,
                contract=summary,
                scope=scope,
                nodes=nodes,
            )

    def list_revisions(
        self,
        issuer: str,
        product_lineage_id: str,
        *,
        handle: GenerationHandle | None = None,
    ) -> ProductRevisionList:
        pin = self.store.pin() if handle is None else nullcontext(handle)
        with pin as pinned:
            self._vectors(pinned)
            with pinned.connect() as connection:
                rows = connection.execute(
                    _REVISION_SELECT + " WHERE l.issuer=? AND l.product_lineage_id=?"
                    " ORDER BY CASE r.temporal_status"
                    " WHEN 'current' THEN 0 WHEN 'ambiguous' THEN 1 ELSE 2 END,"
                    " r.effective_date DESC,r.contract_revision_id",
                    (issuer, product_lineage_id),
                ).fetchall()
            return ProductRevisionList(
                generation_id=pinned.generation_id,
                issuer=issuer,
                product_lineage_id=product_lineage_id,
                revisions=tuple(_summary(row) for row in rows),
            )

    @staticmethod
    def _vectors(handle: GenerationHandle) -> LoadedVectorsV5:
        if handle.metadata.schema_id != "cardrag.serving-db.v5" or not isinstance(
            handle.vectors, LoadedVectorsV5
        ):
            raise RuntimeError("active generation does not provide v5 contract search")
        return handle.vectors

    @staticmethod
    def _all_revision_rows(handle: GenerationHandle) -> list[sqlite3.Row]:
        with handle.connect() as connection:
            return connection.execute(_REVISION_SELECT).fetchall()

    @classmethod
    def _active_revisions(
        cls,
        handle: GenerationHandle,
        request: ContractSearchRequest,
    ) -> tuple[ContractRevisionSummary, ...]:
        summaries = [_summary(row) for row in cls._all_revision_rows(handle)]
        summaries = [
            item
            for item in summaries
            if (request.issuer is None or item.issuer == request.issuer)
            and (
                request.product_lineage_id is None
                or item.product_lineage_id == request.product_lineage_id
            )
        ]
        if request.include_history:
            selected = summaries
        elif request.as_of is None:
            selected = [
                item for item in summaries if item.temporal_status in {"current", "ambiguous"}
            ]
        else:
            ambiguous = [
                item
                for item in summaries
                if item.temporal_status == "ambiguous" and item.effective_date is None
            ]
            eligible = [
                item
                for item in summaries
                if item.effective_date is not None and item.effective_date <= request.as_of
            ]
            latest_by_lineage: dict[str, date] = {}
            for item in eligible:
                effective_date = item.effective_date
                if effective_date is None:
                    raise RuntimeError("eligible revision has no effective date")
                latest_by_lineage[item.product_lineage_id] = max(
                    latest_by_lineage.get(item.product_lineage_id, effective_date),
                    effective_date,
                )
            selected = ambiguous + [
                item
                for item in eligible
                if item.effective_date == latest_by_lineage[item.product_lineage_id]
            ]
            selected_by_lineage: defaultdict[str, list[ContractRevisionSummary]] = defaultdict(list)
            for item in selected:
                selected_by_lineage[item.product_lineage_id].append(item)
            if any(len(items) != 1 for items in selected_by_lineage.values()):
                raise ValueError("as_of revision selection is ambiguous")
        return tuple(sorted(selected, key=lambda item: item.contract_revision_id))

    @staticmethod
    def _resolve_catalog(
        handle: GenerationHandle,
        request: ContractSearchRequest,
    ) -> _CatalogResolution:
        if request.product_lineage_id is not None:
            with handle.connect() as connection:
                row = connection.execute(
                    """SELECT issuer FROM product_lineages
                         WHERE product_lineage_id=?""",
                    (request.product_lineage_id,),
                ).fetchone()
            if row is None:
                raise ValueError("explicit product lineage does not exist")
            if request.issuer is not None and str(row[0]) != request.issuer:
                raise ValueError("explicit product lineage does not belong to issuer")
            return _CatalogResolution(
                "explicit",
                1,
                request.product_lineage_id,
                None,
            )
        normalized_query = _canonical_catalog_text(request.query)
        with handle.connect() as connection:
            if request.issuer is None:
                rows = connection.execute(
                    """SELECT product_lineage_id,name
                         FROM product_lineages
                         ORDER BY product_lineage_id"""
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT product_lineage_id,name
                         FROM product_lineages
                         WHERE issuer=?
                         ORDER BY product_lineage_id""",
                    (request.issuer,),
                ).fetchall()
        candidates: list[tuple[str, str, int]] = []
        for row in rows:
            name = str(row[1])
            normalized_name = _canonical_catalog_text(name)
            if normalized_name and normalized_name in normalized_query:
                candidates.append((str(row[0]), name, len(normalized_name)))
        if not candidates:
            return _CatalogResolution("unresolved", 0, None, None)
        longest = max(candidate[2] for candidate in candidates)
        longest_candidates = [candidate for candidate in candidates if candidate[2] == longest]
        if len(longest_candidates) != 1:
            return _CatalogResolution(
                "ambiguous",
                len(longest_candidates),
                None,
                None,
            )
        lineage_id, product_name, _ = longest_candidates[0]
        return _CatalogResolution("resolved", 1, lineage_id, product_name)

    @staticmethod
    def _primary_profile(handle: GenerationHandle) -> dict[str, str]:
        profile_id = handle.metadata.primary_embedding_profile_id
        if profile_id is None:
            raise RuntimeError("v5 primary embedding profile is absent")
        with handle.connect() as connection:
            row = connection.execute(
                """SELECT provider,model,provider_id,dimension,query_policy
                     FROM embedding_profiles WHERE profile_id=?""",
                (profile_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("v5 primary embedding profile disappeared")
        return {
            "provider": str(row[0]),
            "model": str(row[1]),
            "provider_id": str(row[2]),
            "dimension": str(row[3]),
            "query_policy": str(row[4]),
        }

    @classmethod
    def _revision(
        cls,
        handle: GenerationHandle,
        revision_id: str,
    ) -> ContractRevisionSummary | None:
        return next(
            (
                item
                for item in (_summary(row) for row in cls._all_revision_rows(handle))
                if item.contract_revision_id == revision_id
            ),
            None,
        )

    def _lexical_audit(
        self,
        handle: GenerationHandle,
        query: str,
        active_revision_ids: set[str],
    ) -> _LexicalAudit:
        try:
            additions = self._lexical_additions(
                handle,
                query,
                active_revision_ids,
                active_revision_ids,
            )
        except ValueError:
            return _LexicalAudit(True, "failed", "query_invalid", {})
        except sqlite3.Error as exc:
            detail = str(exc).casefold()
            if "no such table" in detail or "fts5" in detail:
                return _LexicalAudit(False, "failed", "fts_unavailable", {})
            if "syntax" in detail or "malformed" in detail:
                return _LexicalAudit(True, "failed", "query_invalid", {})
            return _LexicalAudit(True, "failed", "internal_error", {})
        return _LexicalAudit(
            True,
            "succeeded",
            None,
            {
                revision_id: set(nodes)
                for revision_id, nodes in additions.items()
                if revision_id in active_revision_ids
            },
        )

    @staticmethod
    def _lexical_additions(
        handle: GenerationHandle,
        query: str,
        active_revision_ids: set[str],
        selected_revision_ids: set[str],
    ) -> dict[str, set[str]]:
        additions: dict[str, set[str]] = defaultdict(set)
        with handle.connect() as connection:
            rows = connection.execute(
                """SELECT v.contract_revision_id,v.node_id
                     FROM embedding_views_fts AS f
                     JOIN embedding_views AS v ON v.row_index=CAST(f.row_index AS INTEGER)
                     WHERE embedding_views_fts MATCH ?
                     ORDER BY f.rowid""",
                (_fts_expression(query),),
            )
            for row in rows:
                revision_id = str(row[0])
                if revision_id in active_revision_ids and revision_id in selected_revision_ids:
                    additions[revision_id].add(str(row[1]))
        return additions

    @staticmethod
    def _expanded_graph(
        handle: GenerationHandle,
        revision_id: str,
        initial_ids: Sequence[str],
        *,
        full: bool,
        scope: Literal["full", "benefits", "notices"],
        include_links: bool,
    ) -> tuple[tuple[StructureNode, ...], int, int]:
        with handle.connect() as connection:
            raw_nodes = connection.execute(
                """SELECT node_id,parent_id,node_type,major_class,raw_heading,ordinal,display_text,
                          table_headers_json,table_cells_json,table_role
                     FROM structure_nodes WHERE contract_revision_id=? ORDER BY ordinal,node_id""",
                (revision_id,),
            ).fetchall()
            raw_spans = connection.execute(
                """SELECT s.node_id,s.page,s.source_start,s.source_end,s.text_sha256,
                          s.span_ordinal,p.text AS page_text
                     FROM node_spans AS s
                     JOIN document_pages AS p
                       ON p.contract_revision_id=s.contract_revision_id AND p.page=s.page
                    WHERE s.contract_revision_id=?
                    ORDER BY s.node_id,s.span_ordinal""",
                (revision_id,),
            ).fetchall()
            raw_links = (
                connection.execute(
                    """SELECT from_node_id,to_node_id,link_type FROM node_links
                        WHERE from_contract_revision_id=? AND to_contract_revision_id=?
                        ORDER BY from_node_id,to_node_id,link_type""",
                    (revision_id, revision_id),
                ).fetchall()
                if include_links
                else []
            )
        node_rows = {str(row[0]): row for row in raw_nodes}
        children: dict[str, set[str]] = defaultdict(set)
        for node_id, row in node_rows.items():
            if row[1] is not None:
                children[str(row[1])].add(node_id)
        links = [
            StructureNodeLink(from_node_id=row[0], to_node_id=row[1], link_type=row[2])
            for row in raw_links
        ]
        spans_by_node: dict[str, list[SourceSpan]] = defaultdict(list)
        for row in raw_spans:
            start, end = int(row[2]), int(row[3])
            page_text = str(row[6])
            spans_by_node[str(row[0])].append(
                SourceSpan(
                    node_id=row[0],
                    page=row[1],
                    source_start=start,
                    source_end=end,
                    text_sha256=row[4],
                    span_ordinal=row[5],
                    text=page_text[start:end],
                )
            )

        selected = (
            set(node_rows) if full else {value for value in initial_ids if value in node_rows}
        )
        initial = set(selected)

        def add_descendants(node_id: str) -> None:
            for child in children.get(node_id, set()):
                if child not in selected:
                    selected.add(child)
                    add_descendants(child)

        def add_item_context(node_id: str) -> None:
            """Add one node, its ancestors, and only its nearest ITEM subtree."""

            if node_id not in node_rows:
                return
            selected.add(node_id)
            current = node_id
            item_id: str | None = None
            while current in node_rows:
                selected.add(current)
                if str(node_rows[current][2]) == "ITEM" and item_id is None:
                    item_id = current
                parent = node_rows[current][1]
                if parent is None:
                    break
                current = str(parent)
            if item_id is not None:
                add_descendants(item_id)

        def add_applies_source_context(node_id: str) -> None:
            """Expand only the explicit NOTICE/MIXED container that carries a condition."""

            if node_id not in node_rows:
                return
            add_item_context(node_id)
            current = node_id
            while current in node_rows:
                row = node_rows[current]
                if str(row[2]) in {"ITEM", "MAJOR_SECTION"} and str(row[3]) in {
                    "NOTICE",
                    "MIXED",
                }:
                    add_descendants(current)
                    return
                parent = row[1]
                if parent is None:
                    return
                current = str(parent)

        if not full:
            for node_id in tuple(selected):
                add_item_context(node_id)

            # Link expansion is deliberately bounded. PREVIOUS/NEXT links form
            # long leaf chains in parser output, so walking to a fixed point
            # would turn one dense hit into an entire-contract bundle.
            base_context = frozenset(selected)
            for link in links:
                if link.link_type == "APPLIES_TO" and (
                    link.from_node_id in base_context or link.to_node_id in base_context
                ):
                    # APPLIES_TO is directional: the condition/notice is the
                    # source, while the benefit it constrains is the target.
                    # A source container owns its descendants, so retain that
                    # one explicit subtree without following newly selected
                    # semantic links to a fixed point.
                    add_applies_source_context(link.from_node_id)
                    add_item_context(link.to_node_id)
            for link in links:
                if link.link_type in {"FOOTNOTE_OF", "CONTINUATION_OF"} and (
                    link.from_node_id in base_context or link.to_node_id in base_context
                ):
                    add_item_context(link.from_node_id)
                    add_item_context(link.to_node_id)
            for link in links:
                if link.link_type in {"PREVIOUS", "NEXT"} and (
                    link.from_node_id in base_context or link.to_node_id in base_context
                ):
                    add_item_context(link.from_node_id)
                    add_item_context(link.to_node_id)
        if scope in {"benefits", "notices"}:
            wanted = "BENEFIT" if scope == "benefits" else "NOTICE"
            selected = {
                node_id
                for node_id in selected
                if str(node_rows[node_id][3]) in {wanted, "MIXED"}
                or str(node_rows[node_id][2]) == "ROOT"
            }
        selected_links = [
            link for link in links if link.from_node_id in selected and link.to_node_id in selected
        ]
        links_by_node: dict[str, list[StructureNodeLink]] = defaultdict(list)
        for link in selected_links:
            links_by_node[link.from_node_id].append(link)
        models = tuple(
            StructureNode(
                node_id=node_id,
                contract_revision_id=revision_id,
                parent_id=row[1],
                node_type=row[2],
                major_class=row[3],
                raw_heading=row[4],
                ordinal=row[5],
                display_text=row[6],
                spans=tuple(spans_by_node.get(node_id, ())),
                links=tuple(links_by_node.get(node_id, ())),
                table_headers=tuple(json.loads(str(row[7]))),
                table_cells=tuple(json.loads(str(row[8]))),
                table_role=row[9],
            )
            for node_id, row in sorted(
                node_rows.items(), key=lambda item: (int(item[1][5]), item[0])
            )
            if node_id in selected
        )
        linked_notice_count = len(
            {
                node_id
                for link in selected_links
                if link.link_type == "APPLIES_TO"
                for node_id in (link.from_node_id, link.to_node_id)
                if str(node_rows[node_id][3]) in {"NOTICE", "MIXED"}
            }
        )
        return models, max(0, len(selected - initial)), linked_notice_count
