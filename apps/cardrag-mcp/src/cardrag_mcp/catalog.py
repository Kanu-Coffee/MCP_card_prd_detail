"""Read-only catalog queries against a single pinned generation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from datetime import date
from typing import Any, Literal, cast

from cardrag_mcp.issuer_input import CANONICAL_ISSUER_CODES, normalize_issuer
from cardrag_mcp.launch_date import (
    PARSER_VERSION,
    LaunchDateResolution,
    LaunchDateStatus,
    resolve_launch_date_details,
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
    resolved: dict[str, LaunchDateResolution] = {}
    missing: list[str] = []
    keys: dict[str, tuple[str, ...]] = {}
    for product in products:
        revision_id = str(product["contract_revision_id"])
        key = (
            generation_id,
            revision_id,
            str(product["pdf_sha256"]),
            "launch_date",
            PARSER_VERSION,
        )
        keys[revision_id] = key
        cached = cache.get(key)
        if cached is None:
            missing.append(revision_id)
        else:
            resolved[revision_id] = LaunchDateResolution(
                date.fromisoformat(cached["launch_date"]) if cached["launch_date"] else None,
                cast(LaunchDateStatus, cached["status"]),
                tuple(cached["evidence"]),
            )
    texts: dict[str, list[str]] = {revision_id: [] for revision_id in missing}
    for start in range(0, len(missing), 500):
        batch = missing[start : start + 500]
        sql = (
            "SELECT contract_revision_id,display_text FROM structure_nodes "  # noqa: S608 - placeholders only
            "WHERE contract_revision_id IN ("
            + ",".join("?" for _ in batch)
            + ") AND display_text LIKE '%출시%' ORDER BY contract_revision_id,ordinal"
        )
        for row in connection.execute(sql, batch):
            texts[str(row["contract_revision_id"])].append(str(row["display_text"]))
    for revision_id, values in texts.items():
        resolution = resolve_launch_date_details(values)
        resolved[revision_id] = resolution
        cache.set(
            keys[revision_id],
            {
                "launch_date": resolution.launch_date.isoformat()
                if resolution.launch_date
                else None,
                "status": resolution.status,
                "evidence": list(resolution.evidence),
            },
        )
    return resolved


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
        if handle.metadata.schema_id != "cardrag.serving-db.v5":
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
        if handle.metadata.schema_id != "cardrag.serving-db.v5":
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
            v5 = handle.metadata.schema_id == "cardrag.serving-db.v5"
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
        if handle.metadata.schema_id != "cardrag.serving-db.v5":
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
                placeholders = ",".join("?" for _ in missing)
                nodes: dict[str, list[sqlite3.Row]] = {revision_id: [] for revision_id in missing}
                for node in connection.execute(
                    "SELECT node_id,contract_revision_id,node_type,major_class,"  # noqa: S608 - placeholders only
                    "raw_heading,ordinal,display_text FROM structure_nodes "
                    "WHERE contract_revision_id IN ("
                    + placeholders
                    + ") AND (major_class='BENEFIT' OR display_text LIKE '%출시%' "
                    "OR display_text LIKE '%연회비%') ORDER BY contract_revision_id,ordinal",
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
                    summary = self._summary(handle, row, nodes[revision_id], pages)
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
    ) -> ProductSummary:
        resolution = resolve_launch_date_details(str(node["display_text"]) for node in nodes)
        annual_fee: str | None = None
        headings: list[str] = []
        benefits: list[str] = []
        evidence: list[SummaryEvidence] = []
        for node in nodes:
            text = " ".join(str(node["display_text"]).split())
            fields: list[Literal["launch_date", "annual_fee", "benefit"]] = []
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
            product_lineage_id=row["product_lineage_id"],
            contract_revision_id=row["contract_revision_id"],
            document_id=row["document_id"],
            source_id=row["source_id"],
            source_url=row["source_url"],
            pdf_sha256=row["pdf_sha256"],
            launch_date_status=resolution.status,
            launch_date_evidence=resolution.evidence,
            evidence=tuple(evidence),
        )
