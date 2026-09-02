"""Fail-closed local capacity planning for the v5 Worker generation boundary.

The pipeline has an important read-only preflight point after every derived
view is known and before any Qwen embedding miss is downloaded.  This module
keeps the arithmetic and filesystem inspection for that boundary independent
from pipeline orchestration.  It deliberately has no cleanup or publication
capability.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cardrag_core import QWEN3_EMBEDDING_DIMENSION

from .exporter_v5 import (
    ContractRevisionInput,
    DocumentPageInput,
    EmbeddingProfileInput,
    IssuerInput,
    NodeLinkInput,
    NodeSpanInput,
    OCRFailedProductInput,
    ProductLineageInput,
    StructureNodeInput,
    UnsupportedProductInput,
)
from .state import WORKER_STATE_SQLITE_PAGE_BYTES, WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES
from .structure import DerivedView

MIB: Final = 1024 * 1024
GIB: Final = 1024 * MIB
MAX_SAFE_BYTES: Final = (1 << 63) - 1

# Keep the Worker's containing state policy aligned with the MCP state gate.
DEFAULT_MAX_STATE_BYTES: Final = 128 * GIB
DEFAULT_RESERVED_FREE_SPACE_BYTES: Final = 2 * GIB
DEFAULT_MINIMUM_START_FREE_BYTES: Final = 2 * GIB
DEFAULT_MAX_VECTOR_SIDECAR_BYTES: Final = 16 * GIB
DEFAULT_MAX_SERVING_DATABASE_BYTES: Final = 32 * GIB

FLOAT32_BYTES: Final = 4
VECTOR_ROW_BYTES: Final = QWEN3_EMBEDDING_DIMENSION * FLOAT32_BYTES
SQLITE_PAGE_BYTES: Final = 4096

# A cache row has one 16 KiB BLOB plus a WITHOUT ROWID primary tree and a
# profile/input secondary index. Two vector rows are reserved per miss so the
# estimate includes overflow/index pages rather than pretending the BLOB is the
# whole persistent cost. WorkerState seals a 1,000-page WAL auto-checkpoint;
# that trigger is not a hard bound when another reader pins old frames. Reserve
# one further cache-row envelope per miss plus an 8 MiB fixed high-water, then
# enforce that predicted WAL limit around every provider batch.
EMBEDDING_CACHE_ROW_ENVELOPE_BYTES: Final = 2 * VECTOR_ROW_BYTES
EMBEDDING_CACHE_WAL_ROW_ENVELOPE_BYTES: Final = EMBEDDING_CACHE_ROW_ENVELOPE_BYTES
EMBEDDING_CACHE_WAL_PEAK_BYTES: Final = max(
    8 * MIB,
    2 * WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES * WORKER_STATE_SQLITE_PAGE_BYTES,
)

# The database prediction starts from exact caller-counted SQLite input bytes.
# The ledger already counts every stored identity/text value and the explicit
# FTS node/display duplicate, so multiplying that payload again would charge
# the same bytes twice. It adds 768 bytes of table/index/numeric/page
# bookkeeping per logical row, exact secondary-index text copies, four further
# indexed-text bytes for FTS5 token shadow structures/token and page slack,
# and a fixed metadata allowance.  The FTS allowance is calibrated by the
# exporter-backed globally unique-token regression; two copies undercounted
# the sealed SQLite artifact once the fixture crossed page boundaries.
# Charging a full 4 KiB page per row is not a SQLite upper bound (pages pack
# many rows) and would reject the measured 1.95M-row partial corpus before its
# actual payload was considered. During export the
# unvacuumed build is hard-limited to twice the predicted sealed size. Because
# SQLite cannot impose that smaller prediction directly on ``VACUUM INTO``'s
# destination, its failure path may also reach twice the prediction before the
# post-write cap rejects it; four predicted sizes are therefore charged at peak.
DATABASE_FIXED_BYTES: Final = MIB
DATABASE_PAYLOAD_MULTIPLIER: Final = 1
DATABASE_FTS_INDEXED_TEXT_MULTIPLIER: Final = 4
# Exporter-backed 500/1,000-row fixtures showed both 256 and 512 bytes failed
# at SQLite overflow-page discontinuities even after exact index bindings were
# counted. 768 bytes retains calibrated margin at the 500-byte key crossover.
DATABASE_ROW_ENVELOPE_BYTES: Final = 768
DATABASE_EXPORT_PEAK_MULTIPLIER: Final = 4

# exporter_v5 writes 41 base metadata rows, six view-count rows, ten node-type
# rows, and four major-class rows.  The optional sealed-profile row and exact
# caller-supplied extra metadata are added by the ledger helper.
BASE_DATABASE_METADATA_ROWS: Final = 61


class V5CapacityError(RuntimeError):
    """A v5 build cannot be proven to fit a configured or physical bound."""


def _bounded_integer(value: int, *, label: str, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum or value > MAX_SAFE_BYTES:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be a {qualifier} bounded integer")
    return value


def _checked_add(left: int, right: int, *, label: str) -> int:
    _bounded_integer(left, label=label, allow_zero=True)
    _bounded_integer(right, label=label, allow_zero=True)
    if right > MAX_SAFE_BYTES - left:
        raise V5CapacityError(f"{label} exceeds the supported byte range")
    return left + right


def _checked_multiply(left: int, right: int, *, label: str) -> int:
    _bounded_integer(left, label=label, allow_zero=True)
    _bounded_integer(right, label=label, allow_zero=True)
    if left and right > MAX_SAFE_BYTES // left:
        raise V5CapacityError(f"{label} exceeds the supported byte range")
    return left * right


def _round_up_to_page(value: int, *, label: str) -> int:
    bounded = _bounded_integer(value, label=label, allow_zero=True)
    remainder = bounded % SQLITE_PAGE_BYTES
    if remainder == 0:
        return bounded
    return _checked_add(bounded, SQLITE_PAGE_BYTES - remainder, label=label)


@dataclass(frozen=True, slots=True)
class V5CapacityPolicy:
    """Configured persistent, artifact, and reserved-free-space boundaries."""

    maximum_state_bytes: int = DEFAULT_MAX_STATE_BYTES
    reserved_free_space_bytes: int = DEFAULT_RESERVED_FREE_SPACE_BYTES
    maximum_vector_sidecar_bytes: int = DEFAULT_MAX_VECTOR_SIDECAR_BYTES
    maximum_serving_database_bytes: int = DEFAULT_MAX_SERVING_DATABASE_BYTES

    def __post_init__(self) -> None:
        _bounded_integer(self.maximum_state_bytes, label="maximum state bytes", allow_zero=False)
        _bounded_integer(
            self.reserved_free_space_bytes,
            label="reserved free-space bytes",
            allow_zero=True,
        )
        _bounded_integer(
            self.maximum_vector_sidecar_bytes,
            label="maximum vector sidecar bytes",
            allow_zero=False,
        )
        _bounded_integer(
            self.maximum_serving_database_bytes,
            label="maximum serving database bytes",
            allow_zero=False,
        )


@dataclass(frozen=True, slots=True)
class V5ArtifactPrediction:
    """A bounded prediction made from the complete, actual derived-view set."""

    derived_view_count: int
    embedding_cache_miss_count: int
    embedding_cache_wal_baseline_bytes: int
    vector_dimension: int
    vector_row_bytes: int
    embedding_cache_growth_bytes: int
    embedding_cache_transaction_bytes: int
    vector_sidecar_bytes: int
    serving_database_bytes: int
    database_export_peak_bytes: int
    logical_growth_bytes: int
    peak_growth_bytes: int

    def __post_init__(self) -> None:
        views = _bounded_integer(
            self.derived_view_count,
            label="derived view count",
            allow_zero=False,
        )
        misses = _bounded_integer(
            self.embedding_cache_miss_count,
            label="embedding cache miss count",
            allow_zero=True,
        )
        if misses > views:
            raise ValueError("embedding cache miss count cannot exceed derived view count")
        wal_baseline = _bounded_integer(
            self.embedding_cache_wal_baseline_bytes,
            label="embedding cache WAL baseline bytes",
            allow_zero=True,
        )
        if type(self.vector_dimension) is not int or self.vector_dimension != QWEN3_EMBEDDING_DIMENSION:
            raise ValueError("v5 capacity prediction requires the exact 4096D contract")
        if type(self.vector_row_bytes) is not int or self.vector_row_bytes != VECTOR_ROW_BYTES:
            raise ValueError("v5 capacity prediction requires exact FP32 vector rows")
        for label, value, allow_zero in (
            ("v5 embedding cache growth", self.embedding_cache_growth_bytes, True),
            ("v5 embedding cache transaction", self.embedding_cache_transaction_bytes, True),
            ("v5 vector sidecar bytes", self.vector_sidecar_bytes, False),
            ("predicted serving database bytes", self.serving_database_bytes, False),
            ("v5 database export peak", self.database_export_peak_bytes, False),
            ("v5 logical state growth", self.logical_growth_bytes, False),
            ("v5 peak filesystem growth", self.peak_growth_bytes, False),
        ):
            _bounded_integer(value, label=label, allow_zero=allow_zero)

        expected_sidecar = _checked_multiply(
            views,
            VECTOR_ROW_BYTES,
            label="v5 vector sidecar bytes",
        )
        expected_cache = _checked_multiply(
            misses,
            EMBEDDING_CACHE_ROW_ENVELOPE_BYTES,
            label="v5 embedding cache growth",
        )
        expected_transaction = _embedding_cache_transaction_bytes(misses)
        if wal_baseline:
            expected_transaction = _checked_add(
                wal_baseline,
                expected_transaction,
                label="v5 embedding cache transaction",
            )
        expected_database_peak = _checked_multiply(
            self.serving_database_bytes,
            DATABASE_EXPORT_PEAK_MULTIPLIER,
            label="v5 database export peak",
        )
        expected_logical = _checked_add(
            expected_cache,
            expected_transaction,
            label="v5 logical state growth",
        )
        expected_logical = _checked_add(
            expected_logical,
            expected_sidecar,
            label="v5 logical state growth",
        )
        expected_logical = _checked_add(
            expected_logical,
            self.serving_database_bytes,
            label="v5 logical state growth",
        )
        expected_peak = _checked_add(
            expected_cache,
            expected_transaction,
            label="v5 peak filesystem growth",
        )
        expected_peak = _checked_add(
            expected_peak,
            expected_sidecar,
            label="v5 peak filesystem growth",
        )
        expected_peak = _checked_add(
            expected_peak,
            expected_database_peak,
            label="v5 peak filesystem growth",
        )
        if (
            self.embedding_cache_growth_bytes != expected_cache
            or self.embedding_cache_transaction_bytes != expected_transaction
            or self.vector_sidecar_bytes != expected_sidecar
            or self.database_export_peak_bytes != expected_database_peak
            or self.logical_growth_bytes != expected_logical
            or self.peak_growth_bytes != expected_peak
        ):
            raise ValueError("v5 capacity prediction arithmetic is inconsistent")


@dataclass(frozen=True, slots=True)
class V5CapacitySnapshot:
    """Observed state after a successful, non-mutating capacity preflight."""

    root: Path
    state_usage_bytes: int
    filesystem_free_bytes: int
    prediction: V5ArtifactPrediction

    @property
    def projected_state_bytes(self) -> int:
        return self.state_usage_bytes + self.prediction.logical_growth_bytes

    @property
    def projected_free_bytes(self) -> int:
        return self.filesystem_free_bytes - self.prediction.peak_growth_bytes


@dataclass(frozen=True, slots=True)
class V5DatabaseLedger:
    """Exact non-vector text bindings and logical rows destined for SQLite."""

    payload_bytes: int
    fts_indexed_text_bytes: int
    secondary_index_text_bytes: int
    row_count: int
    table_row_counts: tuple[tuple[str, int], ...]
    fixed_metadata_allowance_bytes: int = DATABASE_FIXED_BYTES

    def __post_init__(self) -> None:
        _bounded_integer(self.payload_bytes, label="database payload bytes", allow_zero=True)
        _bounded_integer(
            self.fts_indexed_text_bytes,
            label="database FTS indexed text bytes",
            allow_zero=True,
        )
        _bounded_integer(
            self.secondary_index_text_bytes,
            label="database secondary-index text bytes",
            allow_zero=True,
        )
        _bounded_integer(self.row_count, label="database row count", allow_zero=True)
        if self.fixed_metadata_allowance_bytes != DATABASE_FIXED_BYTES:
            raise ValueError("database ledger fixed metadata allowance is invalid")
        names: set[str] = set()
        total = 0
        for name, count in self.table_row_counts:
            if type(name) is not str or not name or name in names or type(count) is not int or count < 0:
                raise ValueError("database ledger table row counts are invalid")
            names.add(name)
            total = _checked_add(total, count, label="database row count")
        if total != self.row_count:
            raise ValueError("database ledger row count is inconsistent")

    @property
    def rows(self) -> dict[str, int]:
        return dict(self.table_row_counts)


@dataclass(frozen=True, slots=True)
class WorkerStartCapacitySnapshot:
    """Read-only filesystem evidence gathered before Worker state creation."""

    requested_state_root: Path
    filesystem_probe_path: Path
    filesystem_free_bytes: int
    minimum_free_bytes: int
    filesystem_device: int
    filesystem_id: int
    probe_device: int
    probe_inode: int
    state_root_existed: bool
    state_root_device: int | None
    state_root_inode: int | None


@dataclass(frozen=True, slots=True)
class V5RemainingCapacitySnapshot:
    """O(1) free-space evidence immediately before a paid/large operation."""

    root: Path
    filesystem_free_bytes: int
    required_free_bytes: int
    remaining_embedding_cache_miss_count: int


def _text_binding_bytes(*values: str | None) -> int:
    total = 0
    for value in values:
        if value is None:
            continue
        if type(value) is not str:
            raise ValueError("database text binding must be a string or null")
        try:
            amount = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise V5CapacityError("database text binding is not valid UTF-8") from None
        total = _checked_add(total, amount, label="database payload bytes")
    return total


def _add_text_bindings(total: int, *values: str | None) -> int:
    return _checked_add(
        total,
        _text_binding_bytes(*values),
        label="database payload bytes",
    )


def _embedding_cache_transaction_bytes(misses: int) -> int:
    row_growth = _checked_multiply(
        misses,
        EMBEDDING_CACHE_WAL_ROW_ENVELOPE_BYTES,
        label="v5 embedding cache transaction",
    )
    return _checked_add(
        EMBEDDING_CACHE_WAL_PEAK_BYTES,
        row_growth,
        label="v5 embedding cache transaction",
    )


def build_v5_database_ledger(
    *,
    issuers: Sequence[IssuerInput],
    product_lineages: Sequence[ProductLineageInput],
    unsupported_products: Sequence[UnsupportedProductInput],
    ocr_failed_products: Sequence[OCRFailedProductInput],
    contract_revisions: Sequence[ContractRevisionInput],
    document_pages: Sequence[DocumentPageInput],
    structure_nodes: Sequence[StructureNodeInput],
    node_spans: Sequence[NodeSpanInput],
    node_links: Sequence[NodeLinkInput],
    embedding_profiles: Sequence[EmbeddingProfileInput],
    derived_views: Sequence[DerivedView],
    primary_embedding_profile_id: str,
    extra_metadata: Mapping[str, str],
    sealed_profile: bool,
) -> V5DatabaseLedger:
    """Count the exact text-bound exporter inputs without requiring vectors.

    Every field below mirrors an explicit non-vector SQLite or FTS binding in
    :mod:`cardrag_worker.exporter_v5`.  Integer columns are covered by the
    per-row envelope.  Generated base metadata values are covered by the fixed
    metadata allowance; caller-supplied metadata is counted byte-for-byte.
    """

    if type(primary_embedding_profile_id) is not str or not primary_embedding_profile_id:
        raise ValueError("primary embedding profile ID must be non-empty text")
    if type(sealed_profile) is not bool:
        raise ValueError("sealed profile flag must be boolean")

    payload = 0
    secondary_index_text_bytes = 0
    for issuer in issuers:
        payload = _add_text_bindings(payload, issuer.code, issuer.display_name)
        secondary_index_text_bytes = _checked_add(
            secondary_index_text_bytes,
            _text_binding_bytes(issuer.code),
            label="database secondary-index text bytes",
        )
    for lineage in product_lineages:
        payload = _add_text_bindings(
            payload,
            lineage.product_lineage_id,
            lineage.issuer,
            lineage.product_code,
            lineage.document_type,
            lineage.name,
        )
        secondary_index_text_bytes = _checked_add(
            secondary_index_text_bytes,
            _text_binding_bytes(
                lineage.issuer,
                lineage.product_code,
                lineage.document_type,
                lineage.product_lineage_id,
            ),
            label="database secondary-index text bytes",
        )
    for unsupported in unsupported_products:
        payload = _add_text_bindings(
            payload,
            unsupported.issuer,
            unsupported.product_code,
            unsupported.name,
            unsupported.disposition,
            unsupported.source_id,
            unsupported.source_version,
            unsupported.source_url,
            unsupported.protected_magic,
            unsupported.protected_sha256,
            unsupported.source_payload_json,
        )
    for failed in ocr_failed_products:
        payload = _add_text_bindings(
            payload,
            failed.issuer,
            failed.product_code,
            failed.name,
            failed.document_id,
            failed.title,
            failed.pdf_sha256,
            failed.reason_code,
            failed.reason,
        )
        secondary_index_text_bytes = _checked_add(
            secondary_index_text_bytes,
            _text_binding_bytes(failed.document_id, failed.issuer, failed.product_code),
            label="database secondary-index text bytes",
        )
    for revision in contract_revisions:
        payload = _add_text_bindings(
            payload,
            revision.contract_revision_id,
            revision.product_lineage_id,
            revision.document_id,
            revision.source_id,
            revision.source_version,
            revision.source_url,
            revision.effective_date,
            revision.pdf_sha256,
            revision.temporal_status,
            revision.supersedes_revision_id,
        )
        revision_index_bytes = _text_binding_bytes(
            revision.contract_revision_id,
            revision.document_id,
            revision.product_lineage_id,
            revision.contract_revision_id,
            revision.product_lineage_id,
            revision.effective_date,
            revision.contract_revision_id,
        )
        if revision.temporal_status == "current":
            revision_index_bytes = _checked_add(
                revision_index_bytes,
                _text_binding_bytes(revision.product_lineage_id),
                label="database secondary-index text bytes",
            )
        secondary_index_text_bytes = _checked_add(
            secondary_index_text_bytes,
            revision_index_bytes,
            label="database secondary-index text bytes",
        )
    for page_row in document_pages:
        payload = _add_text_bindings(
            payload,
            page_row.contract_revision_id,
            page_row.text,
            page_row.text_sha256,
        )
    for node in structure_nodes:
        table_headers_json = json.dumps(
            list(node.table_headers),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        table_cells_json = json.dumps(
            list(node.table_cells),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = _add_text_bindings(
            payload,
            node.node_id,
            node.contract_revision_id,
            node.parent_id,
            node.parent_contract_revision_id,
            node.node_type,
            node.major_class,
            node.raw_heading,
            node.display_text,
            table_headers_json,
            table_cells_json,
            node.table_role,
        )
        secondary_index_text_bytes = _checked_add(
            secondary_index_text_bytes,
            _text_binding_bytes(
                node.contract_revision_id,
                node.node_id,
                node.contract_revision_id,
            ),
            label="database secondary-index text bytes",
        )
    for node_span in node_spans:
        payload = _add_text_bindings(
            payload,
            node_span.node_id,
            node_span.contract_revision_id,
            node_span.text_sha256,
        )
        secondary_index_text_bytes = _checked_add(
            secondary_index_text_bytes,
            _text_binding_bytes(
                node_span.contract_revision_id,
                node_span.node_id,
                node_span.contract_revision_id,
            ),
            label="database secondary-index text bytes",
        )
    for node_link in node_links:
        payload = _add_text_bindings(
            payload,
            node_link.from_node_id,
            node_link.from_contract_revision_id,
            node_link.to_node_id,
            node_link.to_contract_revision_id,
            node_link.link_type,
        )
    for embedding_profile in embedding_profiles:
        payload = _add_text_bindings(
            payload,
            embedding_profile.profile_id,
            embedding_profile.provider,
            embedding_profile.model,
            embedding_profile.provider_id,
            embedding_profile.dtype,
            embedding_profile.normalization,
            embedding_profile.document_policy,
            embedding_profile.query_policy,
        )
        secondary_index_text_bytes = _checked_add(
            secondary_index_text_bytes,
            _text_binding_bytes(embedding_profile.provider_id, embedding_profile.profile_id),
            label="database secondary-index text bytes",
        )

    embedding_view_span_count = 0
    fts_indexed_text_bytes = 0
    for view in derived_views:
        # embedding_views table (embedding_input is intentionally validation-
        # only and is never stored by exporter_v5).
        payload = _add_text_bindings(
            payload,
            view.node_id,
            view.contract_revision_id,
            view.view_type,
            view.input_sha256,
            primary_embedding_profile_id,
            view.display_text,
        )
        # embedding_views_fts explicitly stores node_id and display_text again.
        payload = _add_text_bindings(payload, view.node_id, view.display_text)
        secondary_index_text_bytes = _checked_add(
            secondary_index_text_bytes,
            _text_binding_bytes(
                view.contract_revision_id,
                view.node_id,
                view.view_type,
                primary_embedding_profile_id,
            ),
            label="database secondary-index text bytes",
        )
        fts_indexed_text_bytes = _checked_add(
            fts_indexed_text_bytes,
            _text_binding_bytes(view.display_text),
            label="database FTS indexed text bytes",
        )
        for span in view.spans:
            # embedding_view_spans gets the revision identity from its view.
            payload = _add_text_bindings(
                payload,
                view.contract_revision_id,
                span.text_sha256,
            )
            embedding_view_span_count = _checked_add(
                embedding_view_span_count,
                1,
                label="embedding view span count",
            )

    # revision_coverage stores its revision ID plus two generated SHA-256s.
    for revision in contract_revisions:
        payload = _add_text_bindings(
            payload,
            revision.contract_revision_id,
            "0" * 64,
            "0" * 64,
        )
    for key, value in extra_metadata.items():
        payload = _add_text_bindings(payload, key, value)

    metadata_rows = BASE_DATABASE_METADATA_ROWS + int(sealed_profile) + len(extra_metadata)
    table_rows = (
        ("contract_revisions", len(contract_revisions)),
        ("document_pages", len(document_pages)),
        ("embedding_profiles", len(embedding_profiles)),
        ("embedding_view_spans", embedding_view_span_count),
        ("embedding_views", len(derived_views)),
        ("embedding_views_fts", len(derived_views)),
        ("issuers", len(issuers)),
        ("metadata", metadata_rows),
        ("node_links", len(node_links)),
        ("node_spans", len(node_spans)),
        ("ocr_failed_products", len(ocr_failed_products)),
        ("product_lineages", len(product_lineages)),
        ("revision_coverage", len(contract_revisions)),
        ("structure_nodes", len(structure_nodes)),
        ("unsupported_products", len(unsupported_products)),
    )
    row_count = 0
    for _table, count in table_rows:
        row_count = _checked_add(row_count, count, label="database row count")
    return V5DatabaseLedger(
        payload_bytes=payload,
        fts_indexed_text_bytes=fts_indexed_text_bytes,
        secondary_index_text_bytes=secondary_index_text_bytes,
        row_count=row_count,
        table_row_counts=table_rows,
    )


def predict_serving_database_bytes(
    *,
    payload_bytes: int,
    row_count: int,
    fts_indexed_text_bytes: int = 0,
    secondary_index_text_bytes: int = 0,
) -> int:
    """Return a page-aligned conservative SQLite/FTS artifact prediction.

    ``payload_bytes`` is the sum of UTF-8/blob bytes that the caller will bind
    to non-vector v5 database columns, counting a value each time it is bound.
    ``row_count`` is the sum of logical rows across the v5 tables, including
    the explicit FTS and view-span rows. ``fts_indexed_text_bytes`` charges the
    display text a four-byte-per-byte allowance for FTS5 token/index shadow
    storage and page slack beyond the explicitly counted content row.
    ``secondary_index_text_bytes`` explicitly counts every text binding copied
    into a declared secondary B-tree. The exporter independently enforces
    configured caps on its actual temporary artifacts.
    """

    payload = _bounded_integer(payload_bytes, label="database payload bytes", allow_zero=True)
    rows = _bounded_integer(row_count, label="database row count", allow_zero=True)
    fts_bytes = _bounded_integer(
        fts_indexed_text_bytes,
        label="database FTS indexed text bytes",
        allow_zero=True,
    )
    secondary_index_bytes = _bounded_integer(
        secondary_index_text_bytes,
        label="database secondary-index text bytes",
        allow_zero=True,
    )
    expanded_payload = _checked_multiply(
        payload,
        DATABASE_PAYLOAD_MULTIPLIER,
        label="predicted serving database bytes",
    )
    row_envelope = _checked_multiply(
        rows,
        DATABASE_ROW_ENVELOPE_BYTES,
        label="predicted serving database bytes",
    )
    fts_index = _checked_multiply(
        fts_bytes,
        DATABASE_FTS_INDEXED_TEXT_MULTIPLIER,
        label="predicted serving database bytes",
    )
    prediction = _checked_add(
        DATABASE_FIXED_BYTES,
        expanded_payload,
        label="predicted serving database bytes",
    )
    prediction = _checked_add(
        prediction,
        row_envelope,
        label="predicted serving database bytes",
    )
    prediction = _checked_add(
        prediction,
        fts_index,
        label="predicted serving database bytes",
    )
    prediction = _checked_add(
        prediction,
        secondary_index_bytes,
        label="predicted serving database bytes",
    )
    return _round_up_to_page(prediction, label="predicted serving database bytes")


def predict_v5_local_artifacts(
    *,
    derived_view_count: int,
    database_payload_bytes: int,
    database_row_count: int,
    database_fts_indexed_text_bytes: int = 0,
    database_secondary_index_text_bytes: int = 0,
    embedding_cache_miss_count: int | None = None,
    embedding_cache_wal_baseline_bytes: int = 0,
) -> V5ArtifactPrediction:
    """Predict all new local v5 bytes before an embedding/export mutation.

    Omitting ``embedding_cache_miss_count`` safely assumes every derived view
    is a cache miss.  A caller may provide a smaller count only after a complete
    read-only cache lookup over this exact derived-view/profile set.
    ``embedding_cache_wal_baseline_bytes`` must always be the no-follow size
    observed after that lookup; it reserves one possible copy into the main DB
    if any later Worker bookkeeping write auto-checkpoints pinned frames.
    """

    views = _bounded_integer(derived_view_count, label="derived view count", allow_zero=False)
    if embedding_cache_miss_count is None:
        misses = views
    else:
        misses = _bounded_integer(
            embedding_cache_miss_count,
            label="embedding cache miss count",
            allow_zero=True,
        )
        if misses > views:
            raise ValueError("embedding cache miss count cannot exceed derived view count")
    wal_baseline = _bounded_integer(
        embedding_cache_wal_baseline_bytes,
        label="embedding cache WAL baseline bytes",
        allow_zero=True,
    )

    sidecar_bytes = _checked_multiply(
        views,
        VECTOR_ROW_BYTES,
        label="v5 vector sidecar bytes",
    )
    cache_growth = _checked_multiply(
        misses,
        EMBEDDING_CACHE_ROW_ENVELOPE_BYTES,
        label="v5 embedding cache growth",
    )
    cache_transaction = _embedding_cache_transaction_bytes(misses)
    if wal_baseline:
        cache_transaction = _checked_add(
            wal_baseline,
            cache_transaction,
            label="v5 embedding cache transaction",
        )
    database_bytes = predict_serving_database_bytes(
        payload_bytes=database_payload_bytes,
        row_count=database_row_count,
        fts_indexed_text_bytes=database_fts_indexed_text_bytes,
        secondary_index_text_bytes=database_secondary_index_text_bytes,
    )
    database_peak = _checked_multiply(
        database_bytes,
        DATABASE_EXPORT_PEAK_MULTIPLIER,
        label="v5 database export peak",
    )

    logical_growth = _checked_add(
        cache_growth,
        cache_transaction,
        label="v5 logical state growth",
    )
    logical_growth = _checked_add(
        logical_growth,
        sidecar_bytes,
        label="v5 logical state growth",
    )
    logical_growth = _checked_add(
        logical_growth,
        database_bytes,
        label="v5 logical state growth",
    )
    peak_growth = _checked_add(
        cache_growth,
        cache_transaction,
        label="v5 peak filesystem growth",
    )
    peak_growth = _checked_add(
        peak_growth,
        sidecar_bytes,
        label="v5 peak filesystem growth",
    )
    peak_growth = _checked_add(
        peak_growth,
        database_peak,
        label="v5 peak filesystem growth",
    )
    if peak_growth < logical_growth:  # Defensive if the accounting model is edited later.
        raise V5CapacityError("v5 peak growth is smaller than logical growth")

    return V5ArtifactPrediction(
        derived_view_count=views,
        embedding_cache_miss_count=misses,
        embedding_cache_wal_baseline_bytes=wal_baseline,
        vector_dimension=QWEN3_EMBEDDING_DIMENSION,
        vector_row_bytes=VECTOR_ROW_BYTES,
        embedding_cache_growth_bytes=cache_growth,
        embedding_cache_transaction_bytes=cache_transaction,
        vector_sidecar_bytes=sidecar_bytes,
        serving_database_bytes=database_bytes,
        database_export_peak_bytes=database_peak,
        logical_growth_bytes=logical_growth,
        peak_growth_bytes=peak_growth,
    )


def _unresolved_absolute(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("Worker state capacity path must be a Path")
    return Path(os.path.abspath(path))


def _directory_open_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise V5CapacityError("Worker capacity checks require no-follow directory descriptors")
    return int(os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0))


@contextmanager
def _open_capacity_directory(
    path: Path,
    *,
    allow_missing_suffix: bool,
) -> Iterator[tuple[Path, Path, int]]:
    """Descriptor-walk every existing component without resolving symlinks."""

    requested = _unresolved_absolute(path)
    flags = _directory_open_flags()
    try:
        descriptor = os.open(os.sep, flags)
    except OSError:
        raise V5CapacityError("Worker state filesystem root is unavailable") from None
    probe = Path(os.sep)
    try:
        for component in requested.parts[1:]:
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if allow_missing_suffix:
                    yield requested, probe, descriptor
                    return
                raise V5CapacityError("Worker state capacity root is unavailable") from None
            except OSError:
                raise V5CapacityError("Worker state filesystem path is unavailable") from None
            if stat.S_ISLNK(before.st_mode):
                raise V5CapacityError("Worker state filesystem path contains a symlink")
            if not stat.S_ISDIR(before.st_mode):
                raise V5CapacityError("Worker state filesystem path contains a non-directory")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError:
                raise V5CapacityError("Worker state filesystem path changed during traversal") from None
            try:
                after = os.fstat(child)
                if not stat.S_ISDIR(after.st_mode) or (before.st_dev, before.st_ino) != (
                    after.st_dev,
                    after.st_ino,
                ):
                    raise V5CapacityError("Worker state filesystem path changed during traversal")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
            probe /= component
        yield requested, probe, descriptor
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def _same_observed_entry(before: os.stat_result, after: os.stat_result) -> bool:
    """Compare mutation-sensitive metadata while deliberately ignoring atime."""

    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _tree_usage_fd(descriptor: int, *, filesystem_device: int) -> int:
    try:
        directory_before = os.fstat(descriptor)
    except OSError:
        raise V5CapacityError("Worker state capacity tree changed during traversal") from None
    if not stat.S_ISDIR(directory_before.st_mode) or directory_before.st_dev != filesystem_device:
        raise V5CapacityError("Worker state capacity tree changed during traversal")
    try:
        names = sorted(os.listdir(descriptor))
    except OSError:
        raise V5CapacityError("Worker state capacity tree is unreadable") from None
    total = 0
    for name in names:
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            raise V5CapacityError("Worker state capacity tree changed during traversal") from None
        mode = before.st_mode
        if before.st_dev != filesystem_device:
            raise V5CapacityError("Worker state capacity tree crosses a filesystem boundary")
        if stat.S_ISLNK(mode):
            raise V5CapacityError("Worker state capacity tree contains a symlink")
        if stat.S_ISDIR(mode):
            try:
                child = os.open(name, _directory_open_flags(), dir_fd=descriptor)
            except OSError:
                raise V5CapacityError("Worker state capacity tree changed during traversal") from None
            try:
                opened = os.fstat(child)
                if not stat.S_ISDIR(opened.st_mode) or not _same_observed_entry(before, opened):
                    raise V5CapacityError("Worker state capacity tree changed during traversal")
                amount = _tree_usage_fd(child, filesystem_device=filesystem_device)
                child_after = os.fstat(child)
                if not _same_observed_entry(opened, child_after):
                    raise V5CapacityError("Worker state capacity tree changed during traversal")
            finally:
                os.close(child)
        elif stat.S_ISREG(mode):
            file_flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                child = os.open(name, file_flags, dir_fd=descriptor)
            except OSError:
                raise V5CapacityError("Worker state capacity tree changed during traversal") from None
            try:
                opened = os.fstat(child)
                if not stat.S_ISREG(opened.st_mode) or not _same_observed_entry(before, opened):
                    raise V5CapacityError("Worker state capacity tree changed during traversal")
                amount = opened.st_size
                child_after = os.fstat(child)
                if not _same_observed_entry(opened, child_after):
                    raise V5CapacityError("Worker state capacity tree changed during traversal")
            finally:
                os.close(child)
        else:
            raise V5CapacityError("Worker state capacity tree contains a non-regular entry")
        try:
            entry_after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            raise V5CapacityError("Worker state capacity tree changed during traversal") from None
        if not _same_observed_entry(before, entry_after):
            raise V5CapacityError("Worker state capacity tree changed during traversal")
        total = _checked_add(total, amount, label="Worker state usage")
    try:
        directory_after = os.fstat(descriptor)
    except OSError:
        raise V5CapacityError("Worker state capacity tree changed during traversal") from None
    if not _same_observed_entry(directory_before, directory_after):
        raise V5CapacityError("Worker state capacity tree changed during traversal")
    return total


def _filesystem_free_bytes(descriptor: int) -> int:
    try:
        filesystem = os.fstatvfs(descriptor)
    except OSError:
        raise V5CapacityError("Worker filesystem free space is unavailable") from None
    return _checked_multiply(
        filesystem.f_bavail,
        filesystem.f_frsize,
        label="filesystem free bytes",
    )


def safe_state_usage(root: Path) -> int:
    """Count logical regular-file bytes using no-follow directory descriptors."""

    with _open_capacity_directory(root, allow_missing_suffix=False) as (
        _requested,
        _probe,
        descriptor,
    ):
        return _tree_usage_fd(descriptor, filesystem_device=os.fstat(descriptor).st_dev)


def _remaining_required_free_bytes(
    prediction: V5ArtifactPrediction,
    *,
    remaining_embedding_cache_miss_count: int,
    reserved_free_space_bytes: int,
) -> int:
    remaining = _bounded_integer(
        remaining_embedding_cache_miss_count,
        label="remaining embedding cache miss count",
        allow_zero=True,
    )
    if remaining > prediction.embedding_cache_miss_count:
        raise ValueError("remaining embedding cache misses exceed the initial prediction")
    reserve = _bounded_integer(
        reserved_free_space_bytes,
        label="reserved free-space bytes",
        allow_zero=True,
    )
    required = _checked_multiply(
        remaining,
        EMBEDDING_CACHE_ROW_ENVELOPE_BYTES,
        label="remaining v5 embedding cache growth",
    )
    if remaining:
        required = _checked_add(
            required,
            _embedding_cache_transaction_bytes(remaining),
            label="remaining v5 peak growth",
        )
        required = _checked_add(
            required,
            prediction.embedding_cache_wal_baseline_bytes,
            label="remaining v5 peak growth",
        )
    required = _checked_add(
        required,
        prediction.vector_sidecar_bytes,
        label="remaining v5 peak growth",
    )
    required = _checked_add(
        required,
        prediction.database_export_peak_bytes,
        label="remaining v5 peak growth",
    )
    return _checked_add(required, reserve, label="remaining v5 required filesystem bytes")


def preflight_v5_remaining_free_capacity(
    root: Path,
    prediction: V5ArtifactPrediction,
    *,
    remaining_embedding_cache_miss_count: int,
    policy: V5CapacityPolicy | None = None,
) -> V5RemainingCapacitySnapshot:
    """Recheck free bytes in O(1) without rescanning the Worker state tree."""

    if not isinstance(prediction, V5ArtifactPrediction):
        raise TypeError("prediction must be a V5ArtifactPrediction")
    selected = V5CapacityPolicy() if policy is None else policy
    if not isinstance(selected, V5CapacityPolicy):
        raise TypeError("policy must be a V5CapacityPolicy")
    required = _remaining_required_free_bytes(
        prediction,
        remaining_embedding_cache_miss_count=remaining_embedding_cache_miss_count,
        reserved_free_space_bytes=selected.reserved_free_space_bytes,
    )
    with _open_capacity_directory(root, allow_missing_suffix=False) as (
        requested,
        _probe,
        descriptor,
    ):
        free_bytes = _filesystem_free_bytes(descriptor)
    if required > free_bytes:
        raise V5CapacityError("Worker remaining free-space gate rejected v5 artifacts")
    return V5RemainingCapacitySnapshot(
        root=requested,
        filesystem_free_bytes=free_bytes,
        required_free_bytes=required,
        remaining_embedding_cache_miss_count=remaining_embedding_cache_miss_count,
    )


def preflight_worker_start_capacity(
    state_root: Path,
    *,
    minimum_free_bytes: int = DEFAULT_MINIMUM_START_FREE_BYTES,
) -> WorkerStartCapacitySnapshot:
    """Require a read-only free-space floor before any Worker state exists."""

    minimum = _bounded_integer(
        minimum_free_bytes,
        label="minimum Worker start free bytes",
        allow_zero=True,
    )
    with _open_capacity_directory(state_root, allow_missing_suffix=True) as (
        requested,
        probe,
        descriptor,
    ):
        probe_stat = os.fstat(descriptor)
        state_root_existed = probe == requested
        if state_root_existed:
            _tree_usage_fd(descriptor, filesystem_device=probe_stat.st_dev)
        free_bytes = _filesystem_free_bytes(descriptor)
        try:
            filesystem_id = int(os.fstatvfs(descriptor).f_fsid)
        except (AttributeError, OSError, TypeError, ValueError):
            raise V5CapacityError("Worker startup filesystem identity is unavailable") from None
    if free_bytes < minimum:
        raise V5CapacityError("Worker minimum startup free-space gate rejected this run")
    return WorkerStartCapacitySnapshot(
        requested_state_root=requested,
        filesystem_probe_path=probe,
        filesystem_free_bytes=free_bytes,
        minimum_free_bytes=minimum,
        filesystem_device=probe_stat.st_dev,
        filesystem_id=filesystem_id,
        probe_device=probe_stat.st_dev,
        probe_inode=probe_stat.st_ino,
        state_root_existed=state_root_existed,
        state_root_device=probe_stat.st_dev if state_root_existed else None,
        state_root_inode=probe_stat.st_ino if state_root_existed else None,
    )


def revalidate_worker_start_capacity(
    snapshot: WorkerStartCapacitySnapshot,
) -> WorkerStartCapacitySnapshot:
    """Revalidate the created/existing state root before opening Worker state."""

    if not isinstance(snapshot, WorkerStartCapacitySnapshot):
        raise TypeError("startup capacity snapshot must be a WorkerStartCapacitySnapshot")
    has_state_root_device = snapshot.state_root_device is not None
    has_state_root_inode = snapshot.state_root_inode is not None
    has_state_root_identity = has_state_root_device and has_state_root_inode
    if (
        has_state_root_device != has_state_root_inode
        or snapshot.state_root_existed != has_state_root_identity
    ):
        raise V5CapacityError("Worker startup state-root identity evidence is inconsistent")
    with _open_capacity_directory(snapshot.filesystem_probe_path, allow_missing_suffix=False) as (
        _requested_probe,
        _probe,
        probe_descriptor,
    ):
        probe_stat = os.fstat(probe_descriptor)
        if (probe_stat.st_dev, probe_stat.st_ino) != (
            snapshot.probe_device,
            snapshot.probe_inode,
        ):
            raise V5CapacityError("Worker startup filesystem ancestry changed after preflight")
    with _open_capacity_directory(snapshot.requested_state_root, allow_missing_suffix=False) as (
        requested,
        _state_probe,
        descriptor,
    ):
        state_stat = os.fstat(descriptor)
        if (
            snapshot.state_root_device is not None
            and snapshot.state_root_inode is not None
            and (state_stat.st_dev, state_stat.st_ino)
            != (snapshot.state_root_device, snapshot.state_root_inode)
        ):
            raise V5CapacityError("Worker state root changed after startup preflight")
        if state_stat.st_dev != snapshot.filesystem_device:
            raise V5CapacityError("Worker state root moved to another filesystem")
        _tree_usage_fd(descriptor, filesystem_device=state_stat.st_dev)
        free_bytes = _filesystem_free_bytes(descriptor)
        try:
            filesystem_id = int(os.fstatvfs(descriptor).f_fsid)
        except (AttributeError, OSError, TypeError, ValueError):
            raise V5CapacityError("Worker startup filesystem identity is unavailable") from None
    if filesystem_id != snapshot.filesystem_id:
        raise V5CapacityError("Worker startup filesystem identity changed after preflight")
    if free_bytes < snapshot.minimum_free_bytes:
        raise V5CapacityError("Worker minimum startup free-space gate rejected this run")
    return WorkerStartCapacitySnapshot(
        requested_state_root=requested,
        filesystem_probe_path=snapshot.filesystem_probe_path,
        filesystem_free_bytes=free_bytes,
        minimum_free_bytes=snapshot.minimum_free_bytes,
        filesystem_device=state_stat.st_dev,
        filesystem_id=filesystem_id,
        probe_device=snapshot.probe_device,
        probe_inode=snapshot.probe_inode,
        state_root_existed=True,
        state_root_device=state_stat.st_dev,
        state_root_inode=state_stat.st_ino,
    )


def preflight_v5_capacity(
    root: Path,
    prediction: V5ArtifactPrediction,
    *,
    policy: V5CapacityPolicy | None = None,
) -> V5CapacitySnapshot:
    """Inspect quota and physical free space without creating or deleting data."""

    if not isinstance(prediction, V5ArtifactPrediction):
        raise TypeError("prediction must be a V5ArtifactPrediction")
    selected = V5CapacityPolicy() if policy is None else policy
    if not isinstance(selected, V5CapacityPolicy):
        raise TypeError("policy must be a V5CapacityPolicy")
    if prediction.vector_dimension != 4096 or prediction.vector_row_bytes != 4096 * FLOAT32_BYTES:
        raise V5CapacityError("v5 capacity prediction violates the exact 4096D FP32 contract")
    if prediction.vector_sidecar_bytes > selected.maximum_vector_sidecar_bytes:
        raise V5CapacityError("v5 vector sidecar exceeds the configured file limit")
    if prediction.serving_database_bytes > selected.maximum_serving_database_bytes:
        raise V5CapacityError("predicted v5 serving database exceeds the configured file limit")

    with _open_capacity_directory(root, allow_missing_suffix=False) as (
        requested,
        _probe,
        descriptor,
    ):
        usage = _tree_usage_fd(descriptor, filesystem_device=os.fstat(descriptor).st_dev)
        if usage > selected.maximum_state_bytes or (
            prediction.logical_growth_bytes > selected.maximum_state_bytes - usage
        ):
            raise V5CapacityError("Worker state quota has insufficient capacity for v5 artifacts")
        free_bytes = _filesystem_free_bytes(descriptor)
    required_free = _checked_add(
        prediction.peak_growth_bytes,
        selected.reserved_free_space_bytes,
        label="v5 required filesystem bytes",
    )
    if required_free > free_bytes:
        raise V5CapacityError("Worker reserved free-space gate rejected v5 artifacts")
    return V5CapacitySnapshot(
        root=requested,
        state_usage_bytes=usage,
        filesystem_free_bytes=free_bytes,
        prediction=prediction,
    )


__all__ = [
    "DATABASE_EXPORT_PEAK_MULTIPLIER",
    "DATABASE_FIXED_BYTES",
    "DATABASE_FTS_INDEXED_TEXT_MULTIPLIER",
    "DATABASE_PAYLOAD_MULTIPLIER",
    "DATABASE_ROW_ENVELOPE_BYTES",
    "BASE_DATABASE_METADATA_ROWS",
    "DEFAULT_MINIMUM_START_FREE_BYTES",
    "DEFAULT_MAX_SERVING_DATABASE_BYTES",
    "DEFAULT_MAX_STATE_BYTES",
    "DEFAULT_MAX_VECTOR_SIDECAR_BYTES",
    "DEFAULT_RESERVED_FREE_SPACE_BYTES",
    "EMBEDDING_CACHE_ROW_ENVELOPE_BYTES",
    "EMBEDDING_CACHE_WAL_PEAK_BYTES",
    "EMBEDDING_CACHE_WAL_ROW_ENVELOPE_BYTES",
    "MAX_SAFE_BYTES",
    "V5ArtifactPrediction",
    "V5CapacityError",
    "V5CapacityPolicy",
    "V5CapacitySnapshot",
    "V5DatabaseLedger",
    "V5RemainingCapacitySnapshot",
    "VECTOR_ROW_BYTES",
    "WorkerStartCapacitySnapshot",
    "build_v5_database_ledger",
    "preflight_v5_capacity",
    "preflight_v5_remaining_free_capacity",
    "preflight_worker_start_capacity",
    "revalidate_worker_start_capacity",
    "predict_serving_database_bytes",
    "predict_v5_local_artifacts",
    "safe_state_usage",
]
