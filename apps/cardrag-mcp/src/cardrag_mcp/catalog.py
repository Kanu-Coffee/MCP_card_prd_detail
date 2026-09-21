"""Read-only catalog queries against a single pinned generation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from datetime import date
from typing import Any, Literal, cast

from cardrag_core import (
    DerivedTextSegment,
    resolve_launch_date_segments,
    resolve_lineage_launch_date,
)
from cardrag_core import LaunchDateResolution as CoreLaunchDateResolution

from cardrag_mcp.issuer_input import CANONICAL_ISSUER_CODES, normalize_issuer
from cardrag_mcp.launch_date import (
    PARSER_VERSION,
    LaunchDateResolution,
    LaunchDateStatus,
)
from cardrag_mcp.metadata_cache import MetadataCache
from cardrag_mcp.models import (
    IssuerCoverage,
    ProductCatalogEntry,
    ProductCatalogPage,
    ProductCoverage,
    ProductSummary,
    ProductSummaryBatch,
    ProductSummaryRequest,
    RecentProductCatalogPage,
    SummaryEvidence,
    TemporalStatus,
)
from cardrag_mcp.store import GenerationHandle

_CURRENT_PRODUCTS = """SELECT pl.issuer, pl.product_code, pl.product_lineage_id,
    pl.name AS product_name, pl.document_type, cr.contract_revision_id,
    cr.effective_date, cr.temporal_status, cr.document_id, cr.pdf_sha256,
    cr.source_id, cr.source_url
    FROM contract_revisions AS cr JOIN product_lineages AS pl
    ON pl.product_lineage_id=cr.product_lineage_id WHERE cr.temporal_status='current'"""


def normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def issuer_scope(issuer: str | None, issuers: list[str] | None) -> tuple[str, ...] | None:
    if issuer is not None and issuers is not None:
        raise ValueError("issuer and issuers are mutually exclusive")
    if issuers is not None:
        if not isinstance(issuers, (list, tuple)) or not 1 <= len(issuers) <= 8:
            raise ValueError("issuers must contain between 1 and 8 issuer names")
        return tuple(sorted({normalize_issuer(value) for value in issuers}))
    return (normalize_issuer(issuer),) if issuer is not None else None


def check_generation(handle: GenerationHandle, expected: str | None) -> None:
    if expected is not None and expected != handle.generation_id:
        raise ValueError("generation mismatch: restart the query using the active generation")


def page_limit(limit: int | None, *, enabled: bool) -> int | None:
    if limit is None:
        return 50 if enabled else None
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    return limit


def _rows(connection: sqlite3.Connection, scope: tuple[str, ...] | None) -> list[sqlite3.Row]:
    sql = _CURRENT_PRODUCTS
    params: tuple[str, ...] = ()
    if scope is not None:
        sql += " AND pl.issuer IN (" + ",".join("?" for _ in scope) + ")"
        params = scope
    return connection.execute(
        sql + " ORDER BY pl.issuer,pl.product_code,pl.product_lineage_id,cr.contract_revision_id",
        params,
    ).fetchall()


def revision_resolutions(
    connection: sqlite3.Connection,
    generation_id: str,
    products: list[sqlite3.Row],
    cache: MetadataCache,
) -> dict[str, LaunchDateResolution]:
    if not products:
        return {}
    schema_id = str(
        connection.execute("SELECT value FROM metadata WHERE key='schema_id'").fetchone()[0]
    )
    lineage_ids = sorted({str(product["product_lineage_id"]) for product in products})
    placeholders = ",".join("?" for _ in lineage_ids)
    if schema_id == "cardrag.serving-db.v6":
        revisions: dict[str, list[CoreLaunchDateResolution]] = {
            lineage_id: [] for lineage_id in lineage_ids
        }
        for row in connection.execute(
            "SELECT product_lineage_id,launch_date,launch_date_status "  # noqa: S608
            "FROM contract_revisions WHERE product_lineage_id IN ("
            + placeholders
            + ") ORDER BY product_lineage_id,effective_date,contract_revision_id",
            lineage_ids,
        ):
            raw_date = None if row[1] is None else date.fromisoformat(str(row[1]))
            revisions[str(row[0])].append(
                CoreLaunchDateResolution(raw_date, cast(LaunchDateStatus, str(row[2])))
            )
        by_lineage = {
            lineage_id: resolve_lineage_launch_date(values)
            for lineage_id, values in revisions.items()
        }
        evidence_by_lineage: dict[str, list[str]] = {lineage_id: [] for lineage_id in lineage_ids}
        for evidence_row in connection.execute(
            "SELECT r.product_lineage_id,p.text,e.source_start,e.source_end "  # noqa: S608
            "FROM derived_field_evidence AS e JOIN contract_revisions AS r "
            "ON r.contract_revision_id=e.contract_revision_id JOIN document_pages AS p "
            "ON p.contract_revision_id=e.contract_revision_id AND p.page=e.page "
            "WHERE r.product_lineage_id IN ("
            + placeholders
            + ") ORDER BY r.product_lineage_id,e.contract_revision_id,"
            "e.candidate_ordinal,e.span_ordinal",
            lineage_ids,
        ):
            excerpt = " ".join(
                str(evidence_row[1])[int(evidence_row[2]) : int(evidence_row[3])].split()
            )[:240]
            target = evidence_by_lineage[str(evidence_row[0])]
            if excerpt and excerpt not in target and len(target) < 8:
                target.append(excerpt)
        return {
            str(product["contract_revision_id"]): LaunchDateResolution(
                by_lineage[str(product["product_lineage_id"])].launch_date,
                by_lineage[str(product["product_lineage_id"])].status,
                tuple(evidence_by_lineage[str(product["product_lineage_id"])]),
            )
            for product in products
        }

    revision_rows = connection.execute(
        "SELECT product_lineage_id,contract_revision_id,pdf_sha256 "  # noqa: S608
        "FROM contract_revisions WHERE product_lineage_id IN ("
        + placeholders
        + ") ORDER BY product_lineage_id,effective_date,contract_revision_id",
        lineage_ids,
    ).fetchall()
    revision_ids = [str(row[1]) for row in revision_rows]
    revision_to_lineage = {str(row[1]): str(row[0]) for row in revision_rows}
    identity_by_lineage: dict[str, list[str]] = {lineage_id: [] for lineage_id in lineage_ids}
    for row in revision_rows:
        identity_by_lineage[str(row[0])].append(str(row[2]))
    cached_by_lineage: dict[str, LaunchDateResolution] = {}
    missing_lineages: set[str] = set()
    keys: dict[str, tuple[str, ...]] = {}
    for lineage_id in lineage_ids:
        key = (
            generation_id,
            lineage_id,
            hashlib.sha256("|".join(identity_by_lineage[lineage_id]).encode()).hexdigest(),
            "lineage_launch_date",
            PARSER_VERSION,
        )
        keys[lineage_id] = key
        cached = cache.get(key)
        if cached is None:
            missing_lineages.add(lineage_id)
        else:
            cached_by_lineage[lineage_id] = LaunchDateResolution(
                date.fromisoformat(cached["launch_date"]) if cached["launch_date"] else None,
                cast(LaunchDateStatus, cached["status"]),
                tuple(cached["evidence"]),
            )
    wanted_revisions = [
        revision_id
        for revision_id in revision_ids
        if revision_to_lineage[revision_id] in missing_lineages
    ]
    per_revision: dict[str, list[DerivedTextSegment]] = {
        revision_id: [] for revision_id in wanted_revisions
    }
    if wanted_revisions:
        revision_placeholders = ",".join("?" for _ in wanted_revisions)
        continuations = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT from_contract_revision_id,from_node_id FROM node_links "  # noqa: S608
                "WHERE from_contract_revision_id IN ("
                + revision_placeholders
                + ") AND link_type='CONTINUATION_OF'",
                wanted_revisions,
            )
        }
        for row in connection.execute(
            "SELECT contract_revision_id,node_id,parent_id,display_text FROM structure_nodes "  # noqa: S608
            "WHERE contract_revision_id IN ("
            + revision_placeholders
            + ") AND node_type IN ('PARAGRAPH','LIST_ITEM','TABLE_ROW','FOOTNOTE','UNCLASSIFIED') "
            "ORDER BY contract_revision_id,ordinal",
            wanted_revisions,
        ):
            revision_id, node_id = str(row[0]), str(row[1])
            per_revision[revision_id].append(
                DerivedTextSegment(
                    text=str(row[3]),
                    node_id=node_id,
                    group_id=str(row[2]) if row[2] is not None else node_id,
                    continuation_from_previous=(revision_id, node_id) in continuations,
                )
            )
    core_by_lineage: dict[str, list[CoreLaunchDateResolution]] = {
        lineage_id: [] for lineage_id in missing_lineages
    }
    for revision_id, segments in per_revision.items():
        core_by_lineage[revision_to_lineage[revision_id]].append(
            resolve_launch_date_segments(tuple(segments))
        )
    for lineage_id, values in core_by_lineage.items():
        core = resolve_lineage_launch_date(values)
        resolution = LaunchDateResolution(
            core.launch_date,
            core.status,
            tuple(item.excerpt for item in core.evidence),
        )
        cached_by_lineage[lineage_id] = resolution
        cache.set(
            keys[lineage_id],
            {
                "launch_date": resolution.launch_date.isoformat()
                if resolution.launch_date
                else None,
                "status": resolution.status,
                "evidence": list(resolution.evidence),
            },
        )
    return {
        str(product["contract_revision_id"]): cached_by_lineage[str(product["product_lineage_id"])]
        for product in products
    }


def _entry(row: sqlite3.Row, resolution: LaunchDateResolution) -> ProductCatalogEntry:
    return ProductCatalogEntry(
        issuer=row["issuer"],
        product_code=row["product_code"],
        product_lineage_id=row["product_lineage_id"],
        product_name=row["product_name"],
        document_type=row["document_type"],
        effective_date=date.fromisoformat(row["effective_date"]) if row["effective_date"] else None,
        launch_date=resolution.launch_date,
        launch_date_status=resolution.status,
        temporal_status=cast(TemporalStatus, row["temporal_status"]),
        contract_revision_id=row["contract_revision_id"],
        document_id=row["document_id"],
    )


class CatalogRepository:
    def __init__(self, cursors: Any, cache: MetadataCache) -> None:
        self.cursors = cursors
        self.cache = cache

    def _page(
        self,
        handle: GenerationHandle,
        items: list[ProductCatalogEntry],
        *,
        limit: int | None,
        cursor: str | None,
        binding: dict[str, Any],
    ) -> tuple[tuple[ProductCatalogEntry, ...], str | None]:
        query_hash = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        offset = self.cursors.decode(cursor, handle.generation_id, query_hash, len(items))
        selected = items[offset : offset + limit] if limit is not None else items[offset:]
        next_cursor = (
            self.cursors.encode(handle.generation_id, offset + len(selected), query_hash)
            if offset + len(selected) < len(items)
            else None
        )
        return tuple(selected), next_cursor

    def recent(
        self,
        handle: GenerationHandle,
        *,
        start: date,
        end: date,
        scope: tuple[str, ...] | None,
        limit: int | None,
        cursor: str | None,
    ) -> RecentProductCatalogPage:
        if handle.metadata.schema_id not in {"cardrag.serving-db.v5", "cardrag.serving-db.v6"}:
            return RecentProductCatalogPage(
                generation_id=handle.generation_id,
                items=(),
                total_count=0,
                period_start=start,
                period_end=end,
                unknown_launch_date_count=0,
                schema_status="unsupported_schema",
            )
        with handle.connect() as connection:
            rows = _rows(connection, scope)
            dates = revision_resolutions(connection, handle.generation_id, rows, self.cache)
        entries = [_entry(row, dates[row["contract_revision_id"]]) for row in rows]
        unknown = sum(entry.launch_date is None for entry in entries)
        items = [
            entry
            for entry in entries
            if entry.launch_date is not None and start <= entry.launch_date <= end
        ]
        items.sort(
            key=lambda entry: (
                -cast(date, entry.launch_date).toordinal(),
                entry.issuer,
                entry.product_code,
                entry.product_lineage_id,
                entry.contract_revision_id or "",
            )
        )
        selected, next_cursor = self._page(
            handle,
            items,
            limit=limit,
            cursor=cursor,
            binding={
                "tool": "recent",
                "scope": scope,
                "start": str(start),
                "end": str(end),
                "limit": limit,
            },
        )
        return RecentProductCatalogPage(
            generation_id=handle.generation_id,
            items=selected,
            total_count=len(items),
            period_start=start,
            period_end=end,
            unknown_launch_date_count=unknown,
            next_cursor=next_cursor,
        )

    def find(
        self,
        handle: GenerationHandle,
        *,
        keyword: str | None,
        mode: str,
        scope: tuple[str, ...] | None,
        sort: str,
        limit: int | None,
        cursor: str | None,
    ) -> ProductCatalogPage | ProductCoverage:
        if mode == "coverage":
            if keyword is not None or cursor is not None or limit is not None:
                raise ValueError("coverage does not accept keyword, limit, or cursor")
            return self.coverage(handle, scope)
        norm = normalized_text(keyword or "")
        if mode == "search" and not norm:
            raise ValueError("keyword must not be blank")
        if mode == "catalog" and keyword is not None:
            raise ValueError("catalog does not accept keyword; use mode=search")
        if mode not in {"search", "catalog"}:
            raise ValueError("mode must be search, catalog, or coverage")
        if sort not in {"name", "launch_date"}:
            raise ValueError("sort must be name or launch_date")
        if handle.metadata.schema_id not in {"cardrag.serving-db.v5", "cardrag.serving-db.v6"}:
            return ProductCatalogPage(
                generation_id=handle.generation_id,
                items=(),
                total_count=0,
                schema_status="unsupported_schema",
            )
        with handle.connect() as connection:
            rows = _rows(connection, scope)
            if mode == "search":
                rows = [row for row in rows if norm in normalized_text(str(row["product_name"]))]
            dates = revision_resolutions(connection, handle.generation_id, rows, self.cache)
        items = [_entry(row, dates[row["contract_revision_id"]]) for row in rows]
        if sort == "launch_date":
            items.sort(
                key=lambda entry: (
                    -(entry.launch_date or date.min).toordinal(),
                    entry.issuer,
                    entry.product_code,
                    entry.product_lineage_id,
                    entry.contract_revision_id or "",
                )
            )
        else:
            items.sort(
                key=lambda entry: (
                    entry.issuer,
                    normalized_text(entry.product_name),
                    entry.product_code,
                    entry.product_lineage_id,
                    entry.contract_revision_id or "",
                )
            )
        selected, next_cursor = self._page(
            handle,
            items,
            limit=limit,
            cursor=cursor,
            binding={
                "tool": "find",
                "mode": mode,
                "keyword": norm,
                "scope": scope,
                "sort": sort,
                "limit": limit,
            },
        )
        return ProductCatalogPage(
            generation_id=handle.generation_id,
            items=selected,
            total_count=len(items),
            next_cursor=next_cursor,
        )

    def coverage(self, handle: GenerationHandle, scope: tuple[str, ...] | None) -> ProductCoverage:
        with handle.connect() as connection:
            names = {
                str(row["code"]): str(row["display_name"])
                for row in connection.execute("SELECT code,display_name FROM issuers")
            }
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            v5 = handle.metadata.schema_id in {"cardrag.serving-db.v5", "cardrag.serving-db.v6"}
            revisions = _rows(connection, scope) if v5 else []
            dates = (
                revision_resolutions(connection, handle.generation_id, revisions, self.cache)
                if v5
                else {}
            )
            available: dict[str, set[str]] = {}
            if v5:
                # Coverage counts every loaded product, including historical-only lineages.
                available_rows = connection.execute(
                    "SELECT issuer,product_code FROM product_lineages"
                ).fetchall()
            else:
                available_rows = connection.execute(
                    "SELECT issuer,product_code FROM products"
                ).fetchall()
            for row in available_rows:
                available.setdefault(str(row["issuer"]), set()).add(str(row["product_code"]))
            access: dict[str, dict[str, set[str]]] = {}
            for table in ("unsupported_products", "ocr_failed_products"):
                grouped: dict[str, set[str]] = {}
                if table in tables:
                    for row in connection.execute(f"SELECT issuer,product_code FROM {table}"):  # noqa: S608 - fixed table names
                        grouped.setdefault(str(row["issuer"]), set()).add(str(row["product_code"]))
                access[table] = grouped
        codes = (
            scope if scope is not None else tuple(sorted(set(CANONICAL_ISSUER_CODES) | set(names)))
        )
        items: list[IssuerCoverage] = []
        loaded: list[str] = []
        for code in codes:
            products = available.get(code, set())
            unsupported = access["unsupported_products"].get(code, set())
            failed = access["ocr_failed_products"].get(code, set())
            all_products = products | unsupported | failed
            current = [row for row in revisions if row["issuer"] == code]
            confirmed = sum(
                dates[row["contract_revision_id"]].launch_date is not None for row in current
            )
            if all_products:
                loaded.append(code)
            items.append(
                IssuerCoverage(
                    issuer=code,
                    display_name=names.get(code, code),
                    loaded=bool(all_products),
                    product_count=len(all_products),
                    available_product_count=len(products),
                    current_revision_count=len(current) if v5 else None,
                    confirmed_launch_date_count=confirmed if v5 else None,
                    unknown_launch_date_count=len(current) - confirmed if v5 else None,
                    unsupported_drm_count=len(unsupported),
                    ocr_failed_count=len(failed),
                )
            )
        return ProductCoverage(
            generation_id=handle.generation_id,
            schema_id=handle.metadata.schema_id,
            launch_date_support="supported" if v5 else "unsupported_schema",
            supported_issuers=CANONICAL_ISSUER_CODES,
            loaded_issuers=tuple(loaded),
            issuers=tuple(items),
            product_count=sum(item.product_count for item in items),
        )

    def summaries(
        self, handle: GenerationHandle, products: list[ProductSummaryRequest]
    ) -> ProductSummaryBatch:
        if not 1 <= len(products) <= 50:
            raise ValueError("products must contain between 1 and 50 identifiers")
        if handle.metadata.schema_id not in {"cardrag.serving-db.v5", "cardrag.serving-db.v6"}:
            raise ValueError("batch product summaries require serving schema v5")
        with handle.connect() as connection:
            scope = tuple(sorted({normalize_issuer(product.issuer) for product in products}))
            rows = _rows(connection, scope)
            selected: list[sqlite3.Row | None] = []
            for product in products:
                issuer = normalize_issuer(product.issuer)
                identifier = product.identifier.strip()
                issuer_rows = [row for row in rows if row["issuer"] == issuer]
                matches = [
                    row
                    for row in issuer_rows
                    if identifier
                    in (row["product_code"], row["product_lineage_id"], row["contract_revision_id"])
                ]
                if not matches:
                    name = normalized_text(identifier)
                    matches = [
                        row
                        for row in issuer_rows
                        if name in normalized_text(str(row["product_name"]))
                    ]
                if len(matches) > 1:
                    candidates = [
                        {
                            "issuer": row["issuer"],
                            "product_code": row["product_code"],
                            "product_name": row["product_name"],
                            "product_lineage_id": row["product_lineage_id"],
                            "contract_revision_id": row["contract_revision_id"],
                        }
                        for row in matches[:50]
                    ]
                    raise ValueError(
                        "ambiguous product identifier; use product_lineage_id or "
                        "contract_revision_id; candidates="
                        + json.dumps(candidates, ensure_ascii=False)
                    )
                selected.append(matches[0] if matches else None)
            unique = {str(row["contract_revision_id"]): row for row in selected if row is not None}
            summaries: dict[str, ProductSummary] = {}
            missing: dict[str, sqlite3.Row] = {}
            for revision_id, row in unique.items():
                key = (
                    handle.generation_id,
                    revision_id,
                    str(row["pdf_sha256"]),
                    "product_summary",
                    PARSER_VERSION,
                )
                cached = self.cache.get(key)
                if cached is not None:
                    summaries[revision_id] = ProductSummary.model_validate(cached)
                else:
                    missing[revision_id] = row
            if missing:
                launch_resolutions = revision_resolutions(
                    connection,
                    handle.generation_id,
                    list(missing.values()),
                    self.cache,
                )
                launch_sources: dict[str, tuple[str, ...]] = {
                    revision_id: () for revision_id in missing
                }
                launch_summary_evidence: dict[str, tuple[SummaryEvidence, ...]] = {
                    revision_id: () for revision_id in missing
                }
                if handle.metadata.schema_id == "cardrag.serving-db.v6":
                    lineage_to_current = {
                        str(row["product_lineage_id"]): revision_id
                        for revision_id, row in missing.items()
                    }
                    lineage_placeholders = ",".join("?" for _ in lineage_to_current)
                    source_rows: dict[str, list[str]] = {revision_id: [] for revision_id in missing}
                    for source_row in connection.execute(
                        "SELECT DISTINCT r.product_lineage_id,r.contract_revision_id "  # noqa: S608
                        "FROM contract_revisions AS r JOIN derived_field_evidence AS e "
                        "ON e.contract_revision_id=r.contract_revision_id "
                        "WHERE r.product_lineage_id IN ("
                        + lineage_placeholders
                        + ") ORDER BY r.product_lineage_id,r.effective_date,r.contract_revision_id",
                        tuple(lineage_to_current),
                    ):
                        source_rows[lineage_to_current[str(source_row[0])]].append(
                            str(source_row[1])
                        )
                    launch_sources = {
                        revision_id: tuple(values) for revision_id, values in source_rows.items()
                    }
                    precise_evidence: dict[str, list[SummaryEvidence]] = {
                        revision_id: [] for revision_id in missing
                    }
                    for evidence_row in connection.execute(
                        "SELECT r.product_lineage_id,e.contract_revision_id,e.node_id,e.page,"  # noqa: S608
                        "e.source_start,e.source_end,p.text FROM derived_field_evidence AS e "
                        "JOIN contract_revisions AS r "
                        "ON r.contract_revision_id=e.contract_revision_id "
                        "JOIN document_pages AS p ON p.contract_revision_id=e.contract_revision_id "
                        "AND p.page=e.page WHERE r.product_lineage_id IN ("
                        + lineage_placeholders
                        + ") ORDER BY r.product_lineage_id,e.contract_revision_id,"
                        "e.candidate_ordinal,e.span_ordinal",
                        tuple(lineage_to_current),
                    ):
                        target = precise_evidence[lineage_to_current[str(evidence_row[0])]]
                        if len(target) >= 8:
                            continue
                        start, end = int(evidence_row[4]), int(evidence_row[5])
                        target.append(
                            SummaryEvidence(
                                field="launch_date",
                                node_id=str(evidence_row[2]),
                                pages=(int(evidence_row[3]),),
                                excerpt=" ".join(str(evidence_row[6])[start:end].split())[:300],
                                contract_revision_id=str(evidence_row[1]),
                            )
                        )
                    launch_summary_evidence = {
                        revision_id: tuple(values)
                        for revision_id, values in precise_evidence.items()
                    }
                placeholders = ",".join("?" for _ in missing)
                nodes: dict[str, list[sqlite3.Row]] = {revision_id: [] for revision_id in missing}
                for node in connection.execute(
                    "SELECT node_id,contract_revision_id,node_type,major_class,"  # noqa: S608 - placeholders only
                    "raw_heading,ordinal,display_text FROM structure_nodes "
                    "WHERE contract_revision_id IN ("
                    + placeholders
                    + ") AND (major_class IN ('BENEFIT','NOTICE','MIXED') "
                    "OR display_text LIKE '%출시%' OR display_text LIKE '%발매%' "
                    "OR display_text LIKE '%판매%개시%' OR display_text LIKE '%연회비%') "
                    "ORDER BY contract_revision_id,ordinal",
                    tuple(missing),
                ):
                    nodes[str(node["contract_revision_id"])].append(node)
                pages: dict[tuple[str, str], set[int]] = {}
                for span in connection.execute(
                    "SELECT contract_revision_id,node_id,page FROM node_spans "  # noqa: S608 - placeholders only
                    "WHERE contract_revision_id IN (" + placeholders + ")",
                    tuple(missing),
                ):
                    pages.setdefault(
                        (str(span["contract_revision_id"]), str(span["node_id"])), set()
                    ).add(int(span["page"]))
                for revision_id, row in missing.items():
                    summary = self._summary(
                        handle,
                        row,
                        nodes[revision_id],
                        pages,
                        launch_resolutions[revision_id],
                        launch_sources[revision_id],
                        launch_summary_evidence[revision_id],
                    )
                    summaries[revision_id] = summary
                    self.cache.set(
                        (
                            handle.generation_id,
                            revision_id,
                            str(row["pdf_sha256"]),
                            "product_summary",
                            PARSER_VERSION,
                        ),
                        summary.model_dump(mode="json"),
                    )
        return ProductSummaryBatch(
            generation_id=handle.generation_id,
            items=tuple(
                summaries[str(row["contract_revision_id"])] if row is not None else None
                for row in selected
            ),
        )

    @staticmethod
    def _summary(
        handle: GenerationHandle,
        row: sqlite3.Row,
        nodes: list[sqlite3.Row],
        pages: dict[tuple[str, str], set[int]],
        resolution: LaunchDateResolution,
        launch_source_revision_ids: tuple[str, ...],
        launch_summary_evidence: tuple[SummaryEvidence, ...],
    ) -> ProductSummary:
        annual_fee: str | None = None
        headings: list[str] = []
        benefits: list[str] = []
        conditions: list[str] = []
        evidence: list[SummaryEvidence] = list(launch_summary_evidence)
        for node in nodes:
            text = " ".join(str(node["display_text"]).split())
            fields: list[Literal["launch_date", "annual_fee", "benefit", "condition"]] = []
            if (
                "출시" in text
                and len([item for item in evidence if item.field == "launch_date"]) < 8
            ):
                fields.append("launch_date")
            if (
                annual_fee is None
                and "연회비" in text
                and len(text) > 10
                and not any(word in text for word in ("반환", "기준", "산정", "중도해지"))
            ):
                annual_fee = text[:250]
                fields.append("annual_fee")
            if str(node["major_class"]) == "BENEFIT":
                heading = str(node["raw_heading"] or "").replace("#", "").strip()
                if (
                    heading
                    and node["node_type"] in ("MAJOR_SECTION", "ITEM")
                    and len(heading) > 2
                    and heading not in headings
                    and not any(
                        word in heading for word in ("유의사항", "이용안내", "공통", "기준", "기타")
                    )
                ):
                    headings.append(heading)
                if (
                    node["node_type"] in ("ITEM", "PARAGRAPH", "TABLE_ROW")
                    and len(benefits) < 5
                    and len(text) > 10
                    and any(
                        word in text
                        for word in ("할인", "적립", "캐시백", "면제", "무료", "제공", "포인트")
                    )
                    and not any(
                        word in text
                        for word in ("유의사항", "연회비", "금융소비자", "기준", "실적제외")
                    )
                    and text[:180] not in benefits
                ):
                    benefits.append(text[:180])
                    fields.append("benefit")
            if (
                str(node["major_class"]) in {"NOTICE", "MIXED"}
                and node["node_type"] in ("ITEM", "PARAGRAPH", "TABLE_ROW", "FOOTNOTE")
                and len(conditions) < 5
                and len(text) > 8
                and any(
                    word in text
                    for word in ("전월", "한도", "횟수", "제외", "유의", "조건", "이상", "미만")
                )
                and text[:180] not in conditions
            ):
                conditions.append(text[:180])
                fields.append("condition")
            for field in fields:
                evidence.append(
                    SummaryEvidence(
                        field=field,
                        node_id=str(node["node_id"]),
                        pages=tuple(
                            sorted(
                                pages.get(
                                    (str(row["contract_revision_id"]), str(node["node_id"])), set()
                                )
                            )
                        ),
                        excerpt=text[:300],
                        contract_revision_id=str(row["contract_revision_id"]),
                    )
                )
        return ProductSummary(
            generation_id=handle.generation_id,
            issuer=row["issuer"],
            product_code=row["product_code"],
            product_name=row["product_name"],
            effective_date=date.fromisoformat(row["effective_date"])
            if row["effective_date"]
            else None,
            launch_date=resolution.launch_date,
            annual_fee_text=annual_fee,
            benefit_headings=tuple(headings[:5]),
            benefit_summary_texts=tuple(benefits),
            condition_summary_texts=tuple(conditions),
            product_lineage_id=row["product_lineage_id"],
            contract_revision_id=row["contract_revision_id"],
            document_id=row["document_id"],
            source_id=row["source_id"],
            source_url=row["source_url"],
            pdf_sha256=row["pdf_sha256"],
            launch_date_status=resolution.status,
            launch_date_evidence=resolution.evidence,
            launch_date_source_revision_ids=launch_source_revision_ids,
            evidence=tuple(evidence),
        )
