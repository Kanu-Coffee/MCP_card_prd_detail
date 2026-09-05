"""Independent v5 serving database and 4,096D float32 sidecar exporter.

The input records in this module are boundary DTOs on purpose.  They do not
import the parser's structure classes, allowing parsing, embedding, and export
to evolve independently while this exporter revalidates every binding.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import re
import sqlite3
import stat
import struct
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from numbers import Real
from pathlib import Path
from typing import Final, Literal, final

from cardrag_core import canonical_json_bytes, canonical_sha256, v5_exact_row_corpus_sha256
from cardrag_core.embedding import (
    QWEN3_DOCUMENT_POLICY,
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_DTYPE,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_EMBEDDING_NORMALIZATION,
    QWEN3_EMBEDDING_PROVIDER,
    QWEN3_QUERY_POLICY,
    qwen3_embedding_profile_id,
)

LOGGER: Final = logging.getLogger(__name__)

SERVING_SCHEMA_ID_V5: Final = "cardrag.serving-db.v5"
VECTOR_SIDECAR_NAME: Final = "vectors.f32"
VECTOR_ROW_BYTES: Final = QWEN3_EMBEDDING_DIMENSION * 4
SQLITE_PAGE_BYTES: Final = 4096

TemporalStatus = Literal["current", "superseded", "ambiguous"]
NodeType = Literal[
    "ROOT",
    "MAJOR_SECTION",
    "ITEM",
    "PARAGRAPH",
    "LIST_ITEM",
    "TABLE",
    "TABLE_ROW",
    "FOOTNOTE",
    "BOILERPLATE",
    "UNCLASSIFIED",
]
MajorClass = Literal["BENEFIT", "NOTICE", "MIXED", "UNKNOWN"]
LinkType = Literal["CONTINUATION_OF", "FOOTNOTE_OF", "APPLIES_TO", "PREVIOUS", "NEXT"]
ViewType = Literal[
    "TITLE",
    "RAW_ITEM",
    "CONTEXTUAL_ITEM",
    "DETAIL",
    "MAJOR_SECTION",
    "CONTRACT",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NODE_TYPES = frozenset(
    {
        "ROOT",
        "MAJOR_SECTION",
        "ITEM",
        "PARAGRAPH",
        "LIST_ITEM",
        "TABLE",
        "TABLE_ROW",
        "FOOTNOTE",
        "BOILERPLATE",
        "UNCLASSIFIED",
    }
)
_CANONICAL_NODE_TYPES = frozenset(
    {"PARAGRAPH", "LIST_ITEM", "TABLE_ROW", "FOOTNOTE", "BOILERPLATE", "UNCLASSIFIED"}
)
_MAJOR_CLASSES = frozenset({"BENEFIT", "NOTICE", "MIXED", "UNKNOWN"})
_LINK_TYPES = frozenset({"CONTINUATION_OF", "FOOTNOTE_OF", "APPLIES_TO", "PREVIOUS", "NEXT"})
_VIEW_TYPES = frozenset({"TITLE", "RAW_ITEM", "CONTEXTUAL_ITEM", "DETAIL", "MAJOR_SECTION", "CONTRACT"})
_VECTOR_NORM_TOLERANCE = 2e-5


class ServingDatabaseV5Error(RuntimeError):
    """Raised when a v5 serving artifact cannot prove its sealed contract."""


@dataclass(frozen=True, slots=True)
class IssuerInput:
    code: str
    display_name: str
    sort_order: int


@dataclass(frozen=True, slots=True)
class ProductLineageInput:
    product_lineage_id: str
    issuer: str
    product_code: str
    document_type: str
    name: str


@dataclass(frozen=True, slots=True)
class UnsupportedProductInput:
    issuer: str
    product_code: str
    name: str
    disposition: Literal["unsupported_drm"]
    source_id: str
    source_version: str
    source_url: str
    protected_magic: Literal["SCDSA002", "SCDSA004", "FASOO_DRMONE"]
    protected_sha256: str
    protected_size_bytes: int
    source_payload_json: str


@dataclass(frozen=True, slots=True)
class OCRFailedProductInput:
    issuer: str
    product_code: str
    name: str
    document_id: str
    title: str
    pdf_sha256: str
    pdf_size_bytes: int
    page_count: int
    reason_code: str
    reason: str
    attempts: int

    @property
    def payload(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "document_id": self.document_id,
            "issuer": self.issuer,
            "page_count": self.page_count,
            "pdf_sha256": self.pdf_sha256,
            "pdf_size_bytes": self.pdf_size_bytes,
            "product_code": self.product_code,
            "product_name": self.name,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class ContractRevisionInput:
    contract_revision_id: str
    product_lineage_id: str
    document_id: str
    source_id: str
    source_version: str
    source_url: str
    effective_date: str
    pdf_sha256: str
    pdf_size_bytes: int
    page_count: int
    temporal_status: TemporalStatus
    supersedes_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentPageInput:
    contract_revision_id: str
    page: int
    text: str
    text_sha256: str


@dataclass(frozen=True, slots=True)
class StructureNodeInput:
    node_id: str
    contract_revision_id: str
    parent_id: str | None
    parent_contract_revision_id: str | None
    node_type: NodeType
    major_class: MajorClass
    raw_heading: str | None
    ordinal: int
    display_text: str
    table_headers: tuple[str, ...] = ()
    table_cells: tuple[str, ...] = ()
    table_role: Literal["HEADER", "SEPARATOR", "BODY"] | None = None


@dataclass(frozen=True, slots=True)
class NodeSpanInput:
    node_id: str
    contract_revision_id: str
    page: int
    source_start: int
    source_end: int
    text_sha256: str
    span_ordinal: int
    is_canonical: bool


@dataclass(frozen=True, slots=True)
class NodeLinkInput:
    from_node_id: str
    from_contract_revision_id: str
    to_node_id: str
    to_contract_revision_id: str
    link_type: LinkType
    ordinal: int


@dataclass(frozen=True, slots=True)
class EmbeddingProfileInput:
    profile_id: str
    provider: str
    model: str
    provider_id: str
    dimension: int
    dtype: str
    normalization: str
    document_policy: str
    query_policy: str
    maximum_tokens: int


@final
@dataclass(frozen=True, slots=True)
class LazyEmbeddingVector:
    """A sealed, cache-bound source for one canonical vector row.

    The no-argument loader must independently resolve and validate the named
    cache row on every call, returning its little-endian float32 bytes.  The
    exporter deliberately calls it during both input validation and sidecar
    writing so a missing or changed cache entry fails closed without retaining
    a corpus of vector blobs.
    """

    cache_identity: str
    profile_id: str
    input_sha256: str
    expected_vector_sha256: str
    loader: Callable[[], bytes]
    dimension: int = QWEN3_EMBEDDING_DIMENSION
    dtype: str = QWEN3_EMBEDDING_DTYPE
    normalization: str = QWEN3_EMBEDDING_NORMALIZATION


@dataclass(frozen=True, slots=True)
class ViewSourceSpanInput:
    page: int
    source_start: int
    source_end: int
    text_sha256: str


@dataclass(frozen=True, slots=True)
class EmbeddingViewInput:
    row_index: int
    node_id: str
    contract_revision_id: str
    view_type: ViewType
    embedding_input: str
    input_sha256: str
    profile_id: str
    display_text: str
    source_spans: tuple[ViewSourceSpanInput, ...]
    # Callers may pass a sealed lazy cache source, WorkerState's exact
    # little-endian float32 row, or a bounded Sequence[float] producer.
    vector: Sequence[float] | bytes | LazyEmbeddingVector


@dataclass(frozen=True, slots=True)
class RevisionCoverage:
    contract_revision_id: str
    source_sha256: str
    source_non_whitespace_count: int
    covered_non_whitespace_count: int
    coverage_sha256: str


@dataclass(frozen=True, slots=True)
class ServingExportV5:
    database_path: Path
    database_sha256: str
    database_size_bytes: int
    vector_path: Path
    vector_sha256: str
    vector_size_bytes: int
    vector_row_count: int
    vector_dimension: int
    issuer_count: int
    product_lineage_count: int
    unsupported_product_count: int
    ocr_failed_product_count: int
    contract_revision_count: int
    current_revision_count: int
    superseded_revision_count: int
    ambiguous_revision_count: int
    document_page_count: int
    structure_node_count: int
    node_span_count: int
    node_link_count: int
    embedding_profile_count: int
    embedding_view_count: int
    source_non_whitespace_count: int
    covered_non_whitespace_count: int
    source_coverage_sha256: str
    exact_row_corpus_sha256: str


DDL_V5 = f"""
PRAGMA page_size=4096;
PRAGMA auto_vacuum=NONE;
PRAGMA foreign_keys=ON;

CREATE TABLE metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE issuers (
  code TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  UNIQUE(sort_order,code)
) STRICT, WITHOUT ROWID;

CREATE TABLE product_lineages (
  product_lineage_id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL REFERENCES issuers(code),
  product_code TEXT NOT NULL,
  document_type TEXT NOT NULL,
  name TEXT NOT NULL,
  UNIQUE(issuer,product_code,document_type)
) STRICT, WITHOUT ROWID;

CREATE TABLE unsupported_products (
  issuer TEXT NOT NULL,
  product_code TEXT NOT NULL,
  name TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition='unsupported_drm'),
  source_id TEXT NOT NULL CHECK(length(source_id)=71),
  source_version TEXT NOT NULL,
  source_url TEXT NOT NULL,
  protected_magic TEXT NOT NULL
    CHECK(protected_magic IN ('SCDSA002','SCDSA004','FASOO_DRMONE')),
  protected_sha256 TEXT NOT NULL CHECK(length(protected_sha256)=64),
  protected_size_bytes INTEGER NOT NULL CHECK(protected_size_bytes > 0),
  source_payload_json TEXT NOT NULL,
  PRIMARY KEY (issuer,product_code),
  FOREIGN KEY (issuer) REFERENCES issuers(code)
) STRICT, WITHOUT ROWID;

CREATE TABLE ocr_failed_products (
  issuer TEXT NOT NULL,
  product_code TEXT NOT NULL,
  name TEXT NOT NULL,
  document_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  pdf_sha256 TEXT NOT NULL CHECK(length(pdf_sha256)=64),
  pdf_size_bytes INTEGER NOT NULL CHECK(pdf_size_bytes > 0),
  page_count INTEGER NOT NULL CHECK(page_count > 0),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 64),
  reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 256),
  attempts INTEGER NOT NULL CHECK(attempts > 0),
  PRIMARY KEY (issuer,product_code),
  FOREIGN KEY (issuer) REFERENCES issuers(code)
) STRICT, WITHOUT ROWID;

CREATE TABLE contract_revisions (
  contract_revision_id TEXT PRIMARY KEY,
  product_lineage_id TEXT NOT NULL REFERENCES product_lineages(product_lineage_id),
  document_id TEXT NOT NULL UNIQUE,
  source_id TEXT NOT NULL,
  source_version TEXT NOT NULL,
  source_url TEXT NOT NULL,
  effective_date TEXT NOT NULL,
  pdf_sha256 TEXT NOT NULL CHECK(length(pdf_sha256)=64),
  pdf_size_bytes INTEGER NOT NULL CHECK(pdf_size_bytes > 0),
  page_count INTEGER NOT NULL CHECK(page_count > 0),
  temporal_status TEXT NOT NULL CHECK(temporal_status IN ('current','superseded','ambiguous')),
  supersedes_revision_id TEXT,
  UNIQUE(product_lineage_id,contract_revision_id),
  FOREIGN KEY(product_lineage_id,supersedes_revision_id)
    REFERENCES contract_revisions(product_lineage_id,contract_revision_id),
  CHECK(supersedes_revision_id IS NULL OR supersedes_revision_id != contract_revision_id)
) STRICT;
CREATE UNIQUE INDEX contract_revisions_current_lineage_idx
  ON contract_revisions(product_lineage_id) WHERE temporal_status='current';
CREATE INDEX contract_revisions_lineage_date_idx
  ON contract_revisions(product_lineage_id,effective_date,contract_revision_id);

CREATE TABLE document_pages (
  contract_revision_id TEXT NOT NULL REFERENCES contract_revisions(contract_revision_id),
  page INTEGER NOT NULL CHECK(page > 0),
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL CHECK(length(text_sha256)=64),
  PRIMARY KEY(contract_revision_id,page)
) STRICT, WITHOUT ROWID;

CREATE TABLE structure_nodes (
  node_id TEXT NOT NULL,
  contract_revision_id TEXT NOT NULL REFERENCES contract_revisions(contract_revision_id),
  parent_id TEXT,
  parent_contract_revision_id TEXT,
  node_type TEXT NOT NULL CHECK(node_type IN
    ('ROOT','MAJOR_SECTION','ITEM','PARAGRAPH','LIST_ITEM','TABLE','TABLE_ROW','FOOTNOTE','BOILERPLATE','UNCLASSIFIED')),
  major_class TEXT NOT NULL CHECK(major_class IN ('BENEFIT','NOTICE','MIXED','UNKNOWN')),
  raw_heading TEXT,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  display_text TEXT NOT NULL,
  table_headers_json TEXT NOT NULL,
  table_cells_json TEXT NOT NULL,
  table_role TEXT CHECK(table_role IS NULL OR table_role IN ('HEADER','SEPARATOR','BODY')),
  PRIMARY KEY(node_id,contract_revision_id),
  UNIQUE(contract_revision_id,ordinal),
  CHECK(
    (parent_id IS NULL AND parent_contract_revision_id IS NULL)
    OR
    (parent_id IS NOT NULL AND parent_contract_revision_id=contract_revision_id)
  ),
  CHECK(parent_id IS NULL OR parent_id != node_id),
  FOREIGN KEY(parent_id,parent_contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE node_spans (
  node_id TEXT NOT NULL,
  contract_revision_id TEXT NOT NULL,
  page INTEGER NOT NULL CHECK(page > 0),
  source_start INTEGER NOT NULL CHECK(source_start >= 0),
  source_end INTEGER NOT NULL CHECK(source_end > source_start),
  text_sha256 TEXT NOT NULL CHECK(length(text_sha256)=64),
  span_ordinal INTEGER NOT NULL CHECK(span_ordinal >= 0),
  is_canonical INTEGER NOT NULL CHECK(is_canonical IN (0,1)),
  PRIMARY KEY(node_id,contract_revision_id,span_ordinal),
  FOREIGN KEY(node_id,contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id),
  FOREIGN KEY(contract_revision_id,page)
    REFERENCES document_pages(contract_revision_id,page)
) STRICT, WITHOUT ROWID;
CREATE INDEX node_spans_source_idx
  ON node_spans(contract_revision_id,page,source_start,source_end);

CREATE TABLE node_links (
  from_node_id TEXT NOT NULL,
  from_contract_revision_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  to_contract_revision_id TEXT NOT NULL,
  link_type TEXT NOT NULL CHECK(link_type IN
    ('CONTINUATION_OF','FOOTNOTE_OF','APPLIES_TO','PREVIOUS','NEXT')),
  PRIMARY KEY(from_node_id,from_contract_revision_id,to_node_id,to_contract_revision_id,link_type),
  CHECK(from_contract_revision_id=to_contract_revision_id),
  CHECK(from_node_id != to_node_id),
  FOREIGN KEY(from_node_id,from_contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id),
  FOREIGN KEY(to_node_id,to_contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE embedding_profiles (
  profile_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL CHECK(provider='{QWEN3_EMBEDDING_PROVIDER}'),
  model TEXT NOT NULL CHECK(model='{QWEN3_EMBEDDING_MODEL}'),
  provider_id TEXT NOT NULL CHECK(provider_id IN ('deepinfra','nebius')),
  dimension INTEGER NOT NULL CHECK(dimension={QWEN3_EMBEDDING_DIMENSION}),
  dtype TEXT NOT NULL CHECK(dtype='{QWEN3_EMBEDDING_DTYPE}'),
  normalization TEXT NOT NULL CHECK(normalization='{QWEN3_EMBEDDING_NORMALIZATION}'),
  document_policy TEXT NOT NULL CHECK(document_policy='{QWEN3_DOCUMENT_POLICY}'),
  query_policy TEXT NOT NULL CHECK(query_policy='{QWEN3_QUERY_POLICY}'),
  maximum_tokens INTEGER NOT NULL CHECK(maximum_tokens > 0),
  UNIQUE(provider_id,maximum_tokens)
) STRICT, WITHOUT ROWID;

CREATE TABLE embedding_views (
  view_pk INTEGER PRIMARY KEY CHECK(view_pk > 0),
  row_index INTEGER NOT NULL UNIQUE CHECK(row_index >= 0),
  node_id TEXT NOT NULL,
  contract_revision_id TEXT NOT NULL,
  view_type TEXT NOT NULL CHECK(view_type IN
    ('TITLE','RAW_ITEM','CONTEXTUAL_ITEM','DETAIL','MAJOR_SECTION','CONTRACT')),
  input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
  profile_id TEXT NOT NULL REFERENCES embedding_profiles(profile_id),
  display_text TEXT NOT NULL CHECK(length(display_text) > 0),
  FOREIGN KEY(node_id,contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id)
) STRICT;
CREATE INDEX embedding_views_node_idx
  ON embedding_views(contract_revision_id,node_id,view_type,row_index);
CREATE INDEX embedding_views_profile_idx ON embedding_views(profile_id,row_index);

CREATE TABLE embedding_view_spans (
  row_index INTEGER NOT NULL REFERENCES embedding_views(row_index),
  contract_revision_id TEXT NOT NULL,
  page INTEGER NOT NULL CHECK(page > 0),
  source_start INTEGER NOT NULL CHECK(source_start >= 0),
  source_end INTEGER NOT NULL CHECK(source_end > source_start),
  text_sha256 TEXT NOT NULL CHECK(length(text_sha256)=64),
  span_ordinal INTEGER NOT NULL CHECK(span_ordinal >= 0),
  PRIMARY KEY(row_index,span_ordinal),
  FOREIGN KEY(contract_revision_id,page)
    REFERENCES document_pages(contract_revision_id,page)
) STRICT, WITHOUT ROWID;

CREATE VIRTUAL TABLE embedding_views_fts USING fts5(
  row_index UNINDEXED,
  node_id UNINDEXED,
  display_text,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE revision_coverage (
  contract_revision_id TEXT PRIMARY KEY REFERENCES contract_revisions(contract_revision_id),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  source_non_whitespace_count INTEGER NOT NULL CHECK(source_non_whitespace_count >= 0),
  covered_non_whitespace_count INTEGER NOT NULL CHECK(covered_non_whitespace_count >= 0),
  coverage_sha256 TEXT NOT NULL CHECK(length(coverage_sha256)=64),
  CHECK(source_non_whitespace_count=covered_non_whitespace_count)
) STRICT, WITHOUT ROWID;
"""


def _required_text(value: str, *, field: str, allow_empty: bool = False, maximum: int = 4096) -> str:
    if (not value and not allow_empty) or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise ServingDatabaseV5Error(f"{field} must be trimmed, bounded text")
    return value


def _require_sha256(value: str, *, field: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ServingDatabaseV5Error(f"{field} must be a lowercase sha256")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class _SealedArtifact:
    device: int
    inode: int
    size_bytes: int
    sha256: str


def _snapshot_regular_artifact(path: Path, *, field: str) -> _SealedArtifact:
    """Hash one stable, no-follow regular-file identity."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ServingDatabaseV5Error(f"{field} sealing requires no-follow filesystem support")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    try:
        linked_before = os.lstat(path)
        descriptor = os.open(path, flags)
    except OSError:
        raise ServingDatabaseV5Error(f"{field} is not an available no-follow regular file") from None
    try:
        opened_before = os.fstat(descriptor)
        linked_after_open = os.lstat(path)
        if (
            not stat.S_ISREG(linked_before.st_mode)
            or not stat.S_ISREG(opened_before.st_mode)
            or not stat.S_ISREG(linked_after_open.st_mode)
            or (linked_before.st_dev, linked_before.st_ino) != (opened_before.st_dev, opened_before.st_ino)
            or (linked_after_open.st_dev, linked_after_open.st_ino)
            != (opened_before.st_dev, opened_before.st_ino)
        ):
            raise ServingDatabaseV5Error(f"{field} is not a stable no-follow regular file")

        os.fsync(descriptor)
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)

        opened_after = os.fstat(descriptor)
        linked_after_read = os.lstat(path)
        if (
            not stat.S_ISREG(opened_after.st_mode)
            or not stat.S_ISREG(linked_after_read.st_mode)
            or (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_size,
                opened_after.st_mtime_ns,
                opened_after.st_ctime_ns,
            )
            != (
                opened_before.st_dev,
                opened_before.st_ino,
                opened_before.st_size,
                opened_before.st_mtime_ns,
                opened_before.st_ctime_ns,
            )
            or (linked_after_read.st_dev, linked_after_read.st_ino)
            != (opened_before.st_dev, opened_before.st_ino)
        ):
            raise ServingDatabaseV5Error(f"{field} changed while its artifact seal was read")
        return _SealedArtifact(
            device=opened_before.st_dev,
            inode=opened_before.st_ino,
            size_bytes=opened_before.st_size,
            sha256=digest.hexdigest(),
        )
    except OSError:
        raise ServingDatabaseV5Error(f"{field} artifact seal is unavailable") from None
    finally:
        os.close(descriptor)


def _seal_regular_artifact(
    path: Path,
    *,
    field: str,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
    maximum_size_bytes: int | None = None,
) -> _SealedArtifact:
    sealed = _snapshot_regular_artifact(path, field=field)
    if expected_size_bytes is not None and sealed.size_bytes != expected_size_bytes:
        raise ServingDatabaseV5Error(f"{field} size does not match its expected seal")
    if expected_sha256 is not None and sealed.sha256 != expected_sha256:
        raise ServingDatabaseV5Error(f"{field} hash does not match its expected seal")
    if maximum_size_bytes is not None and sealed.size_bytes > maximum_size_bytes:
        raise ServingDatabaseV5Error(f"{field} exceeds its effective capacity limit")
    return sealed


def _revalidate_sealed_artifact(
    path: Path,
    sealed: _SealedArtifact,
    *,
    field: str,
    maximum_size_bytes: int | None = None,
) -> None:
    observed = _snapshot_regular_artifact(path, field=field)
    if observed != sealed:
        raise ServingDatabaseV5Error(f"{field} changed from its sealed artifact")
    if maximum_size_bytes is not None and observed.size_bytes > maximum_size_bytes:
        raise ServingDatabaseV5Error(f"{field} exceeds its effective capacity limit")


def _unlink_installed_artifact_if_owned(path: Path, sealed: _SealedArtifact) -> None:
    """Remove only the inode this export installed, never a raced replacement."""

    try:
        linked = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.S_ISREG(linked.st_mode) and (linked.st_dev, linked.st_ino) == (
        sealed.device,
        sealed.inode,
    ):
        with suppress(FileNotFoundError):
            path.unlink()


def _source_sha256(pages: Sequence[DocumentPageInput]) -> str:
    return canonical_sha256(
        {
            "pages": [{"page": page.page, "text_sha256": page.text_sha256} for page in pages],
            "schema_version": "cardrag.structure-source.v1",
        }
    )


def _coverage_sha256(pages: Sequence[DocumentPageInput]) -> str:
    return canonical_sha256(
        {
            "pages": [
                {
                    "non_whitespace_characters": sum(not char.isspace() for char in page.text),
                    "non_whitespace_sha256": hashlib.sha256(
                        "".join(char for char in page.text if not char.isspace()).encode("utf-8")
                    ).hexdigest(),
                    "page": page.page,
                    "text_sha256": page.text_sha256,
                }
                for page in pages
            ],
            "schema_version": "cardrag.structure-coverage.v1",
        }
    )


def _aggregate_source_coverage_sha256(pages: Sequence[DocumentPageInput]) -> str:
    digest = hashlib.sha256()
    for page in sorted(pages, key=lambda row: (row.contract_revision_id, row.page)):
        for character in page.text:
            if not character.isspace():
                digest.update(character.encode("utf-8"))
    return digest.hexdigest()


def _validate_profile(profile: EmbeddingProfileInput) -> None:
    if (
        profile.provider != QWEN3_EMBEDDING_PROVIDER
        or profile.model != QWEN3_EMBEDDING_MODEL
        or profile.provider_id not in {"deepinfra", "nebius"}
        or profile.dimension != QWEN3_EMBEDDING_DIMENSION
        or profile.dtype != QWEN3_EMBEDDING_DTYPE
        or profile.normalization != QWEN3_EMBEDDING_NORMALIZATION
        or profile.document_policy != QWEN3_DOCUMENT_POLICY
        or profile.query_policy != QWEN3_QUERY_POLICY
        or isinstance(profile.maximum_tokens, bool)
        or profile.maximum_tokens <= 0
    ):
        raise ServingDatabaseV5Error("embedding profile is not the sealed Qwen v5 contract")
    expected_id = qwen3_embedding_profile_id(
        profile.provider_id,  # type: ignore[arg-type]
        maximum_tokens=profile.maximum_tokens,
    )
    if profile.profile_id != expected_id:
        raise ServingDatabaseV5Error("embedding profile_id does not bind provider and token limit")


def _encode_vector(values: Sequence[float] | bytes, *, row_index: int) -> bytes:
    if isinstance(values, bytes):
        if len(values) != VECTOR_ROW_BYTES:
            raise ServingDatabaseV5Error(
                f"vector row {row_index} byte length {len(values)} is not {VECTOR_ROW_BYTES}"
            )
        # Validate only this row.  The returned object is the caller's already
        # canonical LE-f32 blob, so the exporter never builds a corpus-sized
        # Python-float representation.
        stored = struct.unpack(f"<{QWEN3_EMBEDDING_DIMENSION}f", values)
        if not all(math.isfinite(value) for value in stored):
            raise ServingDatabaseV5Error(f"vector row {row_index} contains a non-finite value")
        stored_norm_squared = sum(value * value for value in stored)
        if not math.isclose(
            stored_norm_squared,
            1.0,
            rel_tol=_VECTOR_NORM_TOLERANCE,
            abs_tol=_VECTOR_NORM_TOLERANCE,
        ):
            raise ServingDatabaseV5Error(f"vector row {row_index} is not L2 normalized")
        return values
    if len(values) != QWEN3_EMBEDDING_DIMENSION:
        raise ServingDatabaseV5Error(
            f"vector row {row_index} dimension {len(values)} is not {QWEN3_EMBEDDING_DIMENSION}"
        )
    if any(isinstance(value, bool) or not isinstance(value, Real) for value in values):
        raise ServingDatabaseV5Error(f"vector row {row_index} contains a non-real or boolean value")
    converted = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in converted):
        raise ServingDatabaseV5Error(f"vector row {row_index} contains a non-finite value")
    norm_squared = sum(value * value for value in converted)
    if not math.isclose(
        norm_squared,
        1.0,
        rel_tol=_VECTOR_NORM_TOLERANCE,
        abs_tol=_VECTOR_NORM_TOLERANCE,
    ):
        raise ServingDatabaseV5Error(f"vector row {row_index} is not L2 normalized")
    packed = struct.pack(f"<{QWEN3_EMBEDDING_DIMENSION}f", *converted)
    stored = struct.unpack(f"<{QWEN3_EMBEDDING_DIMENSION}f", packed)
    stored_norm_squared = sum(value * value for value in stored)
    if not math.isclose(
        stored_norm_squared,
        1.0,
        rel_tol=_VECTOR_NORM_TOLERANCE,
        abs_tol=_VECTOR_NORM_TOLERANCE,
    ):
        raise ServingDatabaseV5Error(f"vector row {row_index} is not normalized after FP32 encoding")
    return packed


def _encode_view_vector(view: EmbeddingViewInput) -> bytes:
    """Resolve and validate exactly one embedding view vector."""

    source = view.vector
    if not isinstance(source, LazyEmbeddingVector):
        return _encode_vector(source, row_index=view.row_index)
    if source.profile_id != view.profile_id:
        raise ServingDatabaseV5Error(
            f"lazy vector row {view.row_index} profile_id does not match its embedding view"
        )
    if source.input_sha256 != view.input_sha256:
        raise ServingDatabaseV5Error(
            f"lazy vector row {view.row_index} input_sha256 does not match its embedding view"
        )
    if not isinstance(source.cache_identity, str) or _SHA256.fullmatch(source.cache_identity) is None:
        raise ServingDatabaseV5Error(
            f"lazy vector row {view.row_index} cache_identity must be a lowercase sha256"
        )
    if (
        not isinstance(source.expected_vector_sha256, str)
        or _SHA256.fullmatch(source.expected_vector_sha256) is None
    ):
        raise ServingDatabaseV5Error(
            f"lazy vector row {view.row_index} expected_vector_sha256 must be a lowercase sha256"
        )
    if source.dimension != QWEN3_EMBEDDING_DIMENSION:
        raise ServingDatabaseV5Error(
            f"lazy vector row {view.row_index} dimension is not {QWEN3_EMBEDDING_DIMENSION}"
        )
    if source.dtype != QWEN3_EMBEDDING_DTYPE:
        raise ServingDatabaseV5Error(f"lazy vector row {view.row_index} dtype is not {QWEN3_EMBEDDING_DTYPE}")
    if source.normalization != QWEN3_EMBEDDING_NORMALIZATION:
        raise ServingDatabaseV5Error(
            f"lazy vector row {view.row_index} normalization is not {QWEN3_EMBEDDING_NORMALIZATION}"
        )
    if not callable(source.loader):
        raise ServingDatabaseV5Error(f"lazy vector row {view.row_index} loader is not callable")

    # Loader exceptions deliberately propagate.  A cache miss, identity
    # collision, or integrity failure must abort the export rather than be
    # translated into an apparently valid serving artifact.
    loaded = source.loader()
    if not isinstance(loaded, bytes):
        raise ServingDatabaseV5Error(
            f"lazy vector row {view.row_index} loader did not return canonical bytes"
        )
    if hashlib.sha256(loaded).hexdigest() != source.expected_vector_sha256:
        raise ServingDatabaseV5Error(
            f"lazy vector row {view.row_index} changed after its cache identity was sealed"
        )
    encoded = _encode_vector(loaded, row_index=view.row_index)
    del loaded
    return encoded


def _validate_inputs(
    *,
    issuers: Sequence[IssuerInput],
    lineages: Sequence[ProductLineageInput],
    unsupported_products: Sequence[UnsupportedProductInput],
    ocr_failed_products: Sequence[OCRFailedProductInput],
    revisions: Sequence[ContractRevisionInput],
    pages: Sequence[DocumentPageInput],
    nodes: Sequence[StructureNodeInput],
    spans: Sequence[NodeSpanInput],
    links: Sequence[NodeLinkInput],
    profiles: Sequence[EmbeddingProfileInput],
    views: Sequence[EmbeddingViewInput],
    primary_embedding_profile_id: str,
) -> tuple[RevisionCoverage, ...]:
    if not issuers or not lineages or not revisions or not pages or not nodes or not profiles or not views:
        raise ServingDatabaseV5Error("v5 export requires non-empty serving and embedding records")

    issuer_by_code: dict[str, IssuerInput] = {}
    issuer_sort_orders: set[int] = set()
    for issuer in issuers:
        _required_text(issuer.code, field="issuer code", maximum=32)
        _required_text(issuer.display_name, field="issuer display_name", maximum=128)
        if isinstance(issuer.sort_order, bool) or not isinstance(issuer.sort_order, int):
            raise ServingDatabaseV5Error("issuer sort_order must be an integer")
        if issuer.code in issuer_by_code or issuer.sort_order in issuer_sort_orders:
            raise ServingDatabaseV5Error("duplicate issuer code or sort order")
        issuer_by_code[issuer.code] = issuer
        issuer_sort_orders.add(issuer.sort_order)

    lineage_by_id: dict[str, ProductLineageInput] = {}
    lineage_identities: set[tuple[str, str, str]] = set()
    for lineage in lineages:
        for field, value in (
            ("product_lineage_id", lineage.product_lineage_id),
            ("issuer", lineage.issuer),
            ("product_code", lineage.product_code),
            ("document_type", lineage.document_type),
            ("name", lineage.name),
        ):
            _required_text(value, field=field)
        if lineage.issuer not in issuer_by_code:
            raise ServingDatabaseV5Error("product lineage references an unknown issuer")
        lineage_identity = (lineage.issuer, lineage.product_code, lineage.document_type)
        if lineage.product_lineage_id in lineage_by_id or lineage_identity in lineage_identities:
            raise ServingDatabaseV5Error("duplicate product lineage identity")
        lineage_by_id[lineage.product_lineage_id] = lineage
        lineage_identities.add(lineage_identity)

    if len(unsupported_products) > 100:
        raise ServingDatabaseV5Error("unsupported product count exceeds the promotion limit")
    unsupported_identities: set[tuple[str, str]] = set()
    for unsupported in unsupported_products:
        identity = (unsupported.issuer, unsupported.product_code)
        if identity in unsupported_identities or unsupported.issuer not in issuer_by_code:
            raise ServingDatabaseV5Error("unsupported product identity is duplicate or unbound")
        if identity in {(row.issuer, row.product_code) for row in lineages}:
            raise ServingDatabaseV5Error("an active lineage cannot also be unsupported")
        if (
            unsupported.disposition != "unsupported_drm"
            or unsupported.protected_magic not in {"SCDSA002", "SCDSA004", "FASOO_DRMONE"}
            or unsupported.protected_size_bytes <= 0
            or not unsupported.source_url.startswith("https://")
            or re.fullmatch(r"source_[0-9a-f]{64}", unsupported.source_id) is None
        ):
            raise ServingDatabaseV5Error("unsupported product contains an invalid bounded value")
        _require_sha256(unsupported.protected_sha256, field="unsupported protected_sha256")
        try:
            source_payload = json.loads(unsupported.source_payload_json)
        except (TypeError, ValueError):
            raise ServingDatabaseV5Error("unsupported source payload is not JSON") from None
        if (
            not isinstance(source_payload, dict)
            or canonical_sha256(source_payload) != unsupported.source_id.removeprefix("source_")
            or json.dumps(
                source_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            != unsupported.source_payload_json
            or source_payload.get("issuer") != unsupported.issuer
            or source_payload.get("product_code") != unsupported.product_code
            or source_payload.get("product_name") != unsupported.name
            or source_payload.get("source_version") != unsupported.source_version
            or source_payload.get("source_url") != unsupported.source_url
        ):
            raise ServingDatabaseV5Error("unsupported source payload is not canonically bound")
        unsupported_identities.add(identity)

    failed_identities: set[tuple[str, str]] = set()
    failed_document_ids: set[str] = set()
    for failed in ocr_failed_products:
        identity = (failed.issuer, failed.product_code)
        if (
            identity in failed_identities
            or identity in unsupported_identities
            or identity in {(row.issuer, row.product_code) for row in lineages}
            or failed.issuer not in issuer_by_code
            or failed.document_id in failed_document_ids
            or re.fullmatch(r"doc_[0-9a-f]{64}", failed.document_id) is None
            or failed.pdf_size_bytes <= 0
            or failed.page_count <= 0
            or re.fullmatch(r"[a-z0-9_]{1,64}", failed.reason_code) is None
            or not failed.reason
            or len(failed.reason) > 256
            or "\n" in failed.reason
            or "\r" in failed.reason
            or failed.attempts <= 0
        ):
            raise ServingDatabaseV5Error("OCR-failed product contains an invalid bounded value")
        _require_sha256(failed.pdf_sha256, field="OCR-failed pdf_sha256")
        failed_identities.add(identity)
        failed_document_ids.add(failed.document_id)

    revision_by_id: dict[str, ContractRevisionInput] = {}
    document_ids: set[str] = set()
    current_by_lineage: set[str] = set()
    for revision in revisions:
        for field, value in (
            ("contract_revision_id", revision.contract_revision_id),
            ("document_id", revision.document_id),
            ("source_id", revision.source_id),
            ("source_version", revision.source_version),
        ):
            _required_text(value, field=field)
        if revision.product_lineage_id not in lineage_by_id:
            raise ServingDatabaseV5Error("contract revision references an unknown lineage")
        if revision.contract_revision_id in revision_by_id or revision.document_id in document_ids:
            raise ServingDatabaseV5Error("duplicate contract revision or document identity")
        if not revision.source_url.startswith("https://"):
            raise ServingDatabaseV5Error("contract revision source_url must use HTTPS")
        try:
            date.fromisoformat(revision.effective_date)
        except ValueError:
            raise ServingDatabaseV5Error("contract revision effective_date is invalid") from None
        _require_sha256(revision.pdf_sha256, field="pdf_sha256")
        if revision.pdf_size_bytes <= 0 or revision.page_count <= 0:
            raise ServingDatabaseV5Error("contract revision PDF size/page count must be positive")
        if revision.temporal_status not in {"current", "superseded", "ambiguous"}:
            raise ServingDatabaseV5Error("contract revision temporal_status is invalid")
        if revision.temporal_status == "current":
            if revision.product_lineage_id in current_by_lineage:
                raise ServingDatabaseV5Error("a product lineage has multiple current revisions")
            current_by_lineage.add(revision.product_lineage_id)
        revision_by_id[revision.contract_revision_id] = revision
        document_ids.add(revision.document_id)
    revision_lineage_ids = {revision.product_lineage_id for revision in revisions}
    if revision_lineage_ids != set(lineage_by_id):
        raise ServingDatabaseV5Error("every product lineage must have a contract revision")
    if document_ids.intersection(failed_document_ids):
        raise ServingDatabaseV5Error("an OCR-failed document cannot also be an active revision")
    for revision in revisions:
        if revision.supersedes_revision_id is None:
            continue
        previous = revision_by_id.get(revision.supersedes_revision_id)
        if previous is None or previous.product_lineage_id != revision.product_lineage_id:
            raise ServingDatabaseV5Error("revision supersedes a revision outside its lineage")
        if previous.contract_revision_id == revision.contract_revision_id:
            raise ServingDatabaseV5Error("revision cannot supersede itself")
    for revision in revisions:
        visited: set[str] = set()
        cursor: ContractRevisionInput | None = revision
        while cursor is not None and cursor.supersedes_revision_id is not None:
            if cursor.contract_revision_id in visited:
                raise ServingDatabaseV5Error("revision supersession chain contains a cycle")
            visited.add(cursor.contract_revision_id)
            cursor = revision_by_id[cursor.supersedes_revision_id]

    pages_by_revision: dict[str, list[DocumentPageInput]] = defaultdict(list)
    page_by_identity: dict[tuple[str, int], DocumentPageInput] = {}
    for page_row in pages:
        bound_revision = revision_by_id.get(page_row.contract_revision_id)
        if bound_revision is None:
            raise ServingDatabaseV5Error("document page references an unknown revision")
        page_identity = (page_row.contract_revision_id, page_row.page)
        if page_identity in page_by_identity or page_row.page <= 0:
            raise ServingDatabaseV5Error("document page identity is duplicate or invalid")
        if hashlib.sha256(page_row.text.encode("utf-8")).hexdigest() != page_row.text_sha256:
            raise ServingDatabaseV5Error("document page text_sha256 does not match its text")
        page_by_identity[page_identity] = page_row
        pages_by_revision[page_row.contract_revision_id].append(page_row)
    for revision in revisions:
        revision_pages = sorted(pages_by_revision[revision.contract_revision_id], key=lambda row: row.page)
        if [row.page for row in revision_pages] != list(range(1, revision.page_count + 1)):
            raise ServingDatabaseV5Error("revision pages are not contiguous or complete")

    node_by_identity: dict[tuple[str, str], StructureNodeInput] = {}
    nodes_by_revision: dict[str, list[StructureNodeInput]] = defaultdict(list)
    for node_row in nodes:
        node_identity = (node_row.node_id, node_row.contract_revision_id)
        if node_row.contract_revision_id not in revision_by_id or node_identity in node_by_identity:
            raise ServingDatabaseV5Error("structure node identity is duplicate or unbound")
        _required_text(node_row.node_id, field="node_id")
        if node_row.node_type not in _NODE_TYPES or node_row.major_class not in _MAJOR_CLASSES:
            raise ServingDatabaseV5Error("structure node type/class is invalid")
        if node_row.ordinal < 0:
            raise ServingDatabaseV5Error("structure node ordinal must be non-negative")
        if (node_row.parent_id is None) != (node_row.parent_contract_revision_id is None):
            raise ServingDatabaseV5Error("structure parent identity must be paired-null")
        if (
            node_row.parent_id is not None
            and node_row.parent_contract_revision_id != node_row.contract_revision_id
        ):
            raise ServingDatabaseV5Error("structure parent crosses a contract revision")
        if node_row.raw_heading is not None and (not node_row.raw_heading or "\x00" in node_row.raw_heading):
            raise ServingDatabaseV5Error("structure raw_heading is invalid")
        if "\x00" in node_row.display_text:
            raise ServingDatabaseV5Error("structure display_text contains NUL")
        if any(not isinstance(value, str) or "\x00" in value for value in node_row.table_headers):
            raise ServingDatabaseV5Error("structure table headers are invalid")
        if any(not isinstance(value, str) or "\x00" in value for value in node_row.table_cells):
            raise ServingDatabaseV5Error("structure table cells are invalid")
        if node_row.node_type == "TABLE_ROW":
            if node_row.table_role not in {"HEADER", "SEPARATOR", "BODY"} or not node_row.table_cells:
                raise ServingDatabaseV5Error("table row lacks its original cells or role")
        elif node_row.node_type == "TABLE":
            if node_row.table_cells or node_row.table_role is not None:
                raise ServingDatabaseV5Error("table container contains row-only metadata")
        elif node_row.table_headers or node_row.table_cells or node_row.table_role is not None:
            raise ServingDatabaseV5Error("non-table structure node contains table metadata")
        node_by_identity[node_identity] = node_row
        nodes_by_revision[node_row.contract_revision_id].append(node_row)
    for revision_id, revision_nodes in nodes_by_revision.items():
        ordered = sorted(revision_nodes, key=lambda row: row.ordinal)
        if [row.ordinal for row in ordered] != list(range(len(ordered))):
            raise ServingDatabaseV5Error("structure node ordinals are not contiguous")
        roots = [row for row in ordered if row.parent_id is None]
        if len(roots) != 1 or roots[0].node_type != "ROOT" or roots[0].ordinal != 0:
            raise ServingDatabaseV5Error("each revision requires one first, parentless ROOT")
        for ordered_node in ordered:
            if ordered_node.parent_id is None:
                continue
            parent = node_by_identity.get((ordered_node.parent_id, revision_id))
            if parent is None or parent.ordinal >= ordered_node.ordinal:
                raise ServingDatabaseV5Error("structure parent is missing or does not precede child")
            if ordered_node.node_type == "TABLE_ROW" and (
                parent.node_type != "TABLE" or ordered_node.table_headers != parent.table_headers
            ):
                raise ServingDatabaseV5Error("table row lost its original header relationship")
        for table in (row for row in ordered if row.node_type == "TABLE"):
            table_children = [row for row in ordered if row.parent_id == table.node_id]
            if not table_children or any(row.node_type != "TABLE_ROW" for row in table_children):
                raise ServingDatabaseV5Error("table must contain only one or more table rows")
    if set(nodes_by_revision) != set(revision_by_id):
        raise ServingDatabaseV5Error("every revision must have a structure tree")

    spans_by_node: dict[tuple[str, str], list[NodeSpanInput]] = defaultdict(list)
    for span_row in spans:
        span_node_identity = (span_row.node_id, span_row.contract_revision_id)
        node = node_by_identity.get(span_node_identity)
        page = page_by_identity.get((span_row.contract_revision_id, span_row.page))
        if node is None or page is None:
            raise ServingDatabaseV5Error("node span references an unknown node or page")
        if (
            span_row.span_ordinal < 0
            or span_row.source_start < 0
            or span_row.source_end <= span_row.source_start
        ):
            raise ServingDatabaseV5Error("node span coordinates are invalid")
        if span_row.source_end > len(page.text):
            raise ServingDatabaseV5Error("node span exceeds its source page")
        source = page.text[span_row.source_start : span_row.source_end]
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != span_row.text_sha256:
            raise ServingDatabaseV5Error("node span hash does not match its exact source text")
        if span_row.is_canonical != (node.node_type in _CANONICAL_NODE_TYPES):
            raise ServingDatabaseV5Error("canonical coverage flag disagrees with node type")
        spans_by_node[span_node_identity].append(span_row)
    for bound_node_identity, bound_node in node_by_identity.items():
        node_spans = sorted(spans_by_node[bound_node_identity], key=lambda item: item.span_ordinal)
        if [row.span_ordinal for row in node_spans] != list(range(len(node_spans))):
            raise ServingDatabaseV5Error("node span ordinals are not contiguous")
        positions = [(row.page, row.source_start, row.source_end) for row in node_spans]
        if positions != sorted(positions):
            raise ServingDatabaseV5Error("node spans do not follow source order")
        display = "".join(
            page_by_identity[(row.contract_revision_id, row.page)].text[row.source_start : row.source_end]
            for row in node_spans
        )
        if display != bound_node.display_text:
            raise ServingDatabaseV5Error("node display_text does not equal its source spans")
        if bound_node.node_type in _CANONICAL_NODE_TYPES and not node_spans:
            raise ServingDatabaseV5Error("canonical structure node has no source span")

    coverage_rows: list[RevisionCoverage] = []
    for revision in revisions:
        revision_pages = sorted(pages_by_revision[revision.contract_revision_id], key=lambda row: row.page)
        covered = {page.page: [False] * len(page.text) for page in revision_pages}
        for coverage_span in spans:
            if (
                coverage_span.contract_revision_id != revision.contract_revision_id
                or not coverage_span.is_canonical
            ):
                continue
            for offset in range(coverage_span.source_start, coverage_span.source_end):
                if covered[coverage_span.page][offset]:
                    raise ServingDatabaseV5Error("canonical source spans overlap")
                covered[coverage_span.page][offset] = True
        if any(not marker for page_markers in covered.values() for marker in page_markers):
            raise ServingDatabaseV5Error("canonical source spans do not reconstruct every OCR character")
        source_non_whitespace = sum(not char.isspace() for page in revision_pages for char in page.text)
        covered_non_whitespace = sum(
            not page.text[offset].isspace()
            for page in revision_pages
            for offset, marker in enumerate(covered[page.page])
            if marker
        )
        if source_non_whitespace != covered_non_whitespace:
            raise ServingDatabaseV5Error("non-whitespace OCR source coverage is not 100 percent")
        coverage_rows.append(
            RevisionCoverage(
                contract_revision_id=revision.contract_revision_id,
                source_sha256=_source_sha256(revision_pages),
                source_non_whitespace_count=source_non_whitespace,
                covered_non_whitespace_count=covered_non_whitespace,
                coverage_sha256=_coverage_sha256(revision_pages),
            )
        )

    seen_links: set[tuple[str, str, str, str, str]] = set()
    link_ordinals: dict[str, list[int]] = defaultdict(list)
    for link in links:
        if link.link_type not in _LINK_TYPES or link.ordinal < 0:
            raise ServingDatabaseV5Error("node link type/ordinal is invalid")
        if link.from_contract_revision_id != link.to_contract_revision_id:
            raise ServingDatabaseV5Error("node link crosses a contract revision")
        source_identity = (link.from_node_id, link.from_contract_revision_id)
        target_identity = (link.to_node_id, link.to_contract_revision_id)
        if source_identity not in node_by_identity or target_identity not in node_by_identity:
            raise ServingDatabaseV5Error("node link references an unknown node")
        if source_identity == target_identity:
            raise ServingDatabaseV5Error("node link cannot target itself")
        link_identity = (*source_identity, *target_identity, link.link_type)
        if link_identity in seen_links:
            raise ServingDatabaseV5Error("duplicate node link")
        seen_links.add(link_identity)
        link_ordinals[link.from_contract_revision_id].append(link.ordinal)
    for ordinals in link_ordinals.values():
        if sorted(ordinals) != list(range(len(ordinals))):
            raise ServingDatabaseV5Error("node link ordinals are not contiguous per revision")

    profile_by_id: dict[str, EmbeddingProfileInput] = {}
    for profile_row in profiles:
        _validate_profile(profile_row)
        if profile_row.profile_id in profile_by_id:
            raise ServingDatabaseV5Error("duplicate embedding profile_id")
        profile_by_id[profile_row.profile_id] = profile_row
    if primary_embedding_profile_id not in profile_by_id:
        raise ServingDatabaseV5Error("primary embedding profile is missing")

    row_indices = [row.row_index for row in views]
    if sorted(row_indices) != list(range(len(views))) or len(set(row_indices)) != len(row_indices):
        raise ServingDatabaseV5Error("embedding view row_index must be contiguous and 0-based")
    used_profiles: set[str] = set()
    for view in views:
        if view.view_type not in _VIEW_TYPES:
            raise ServingDatabaseV5Error("embedding view type is invalid")
        if (view.node_id, view.contract_revision_id) not in node_by_identity:
            raise ServingDatabaseV5Error("embedding view references an unknown structure node")
        profile = profile_by_id.get(view.profile_id)
        if profile is None:
            raise ServingDatabaseV5Error("embedding view references an unknown profile")
        used_profiles.add(view.profile_id)
        if not view.embedding_input or not view.display_text:
            raise ServingDatabaseV5Error("embedding view input/display text cannot be empty")
        input_sha256 = hashlib.sha256(view.embedding_input.encode("utf-8")).hexdigest()
        if input_sha256 != view.input_sha256:
            raise ServingDatabaseV5Error("embedding view input_sha256 does not match its exact input")
        if not view.source_spans:
            raise ServingDatabaseV5Error("embedding view display_text has no source spans")
        positions = [(span.page, span.source_start, span.source_end) for span in view.source_spans]
        if positions != sorted(set(positions)):
            raise ServingDatabaseV5Error("embedding view source spans are duplicate or unordered")
        exact_display_parts: list[str] = []
        for view_span in view.source_spans:
            page = page_by_identity.get((view.contract_revision_id, view_span.page))
            if (
                page is None
                or view_span.source_start < 0
                or view_span.source_end <= view_span.source_start
                or view_span.source_end > len(page.text)
            ):
                raise ServingDatabaseV5Error("embedding view source span is invalid")
            text = page.text[view_span.source_start : view_span.source_end]
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != view_span.text_sha256:
                raise ServingDatabaseV5Error("embedding view source span hash mismatch")
            exact_display_parts.append(text)
        if "".join(exact_display_parts) != view.display_text:
            raise ServingDatabaseV5Error("embedding view display_text is not exact OCR source")
        _encode_view_vector(view)
    if used_profiles != set(profile_by_id):
        raise ServingDatabaseV5Error("every sealed embedding profile must own at least one view")
    return tuple(sorted(coverage_rows, key=lambda row: row.contract_revision_id))


def _write_vector_sidecar(path: Path, views: Sequence[EmbeddingViewInput]) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("xb") as target:
        for row in sorted(views, key=lambda item: item.row_index):
            encoded = _encode_view_vector(row)
            target.write(encoded)
            digest.update(encoded)
            # Do not retain the preceding lazy-loaded row while resolving the
            # next one.  In particular, assignment evaluation would otherwise
            # keep ``encoded`` alive until the following loader returned.
            del encoded
        target.flush()
        os.fsync(target.fileno())
    size = path.stat().st_size
    expected_size = len(views) * VECTOR_ROW_BYTES
    if size != expected_size:
        raise ServingDatabaseV5Error(f"vector sidecar size {size} does not equal {expected_size}")
    return digest.hexdigest(), size


def _verify_vector_sidecar(
    path: Path,
    *,
    expected_sha256: str,
    expected_rows: int,
) -> None:
    expected_size = expected_rows * VECTOR_ROW_BYTES
    if path.stat().st_size != expected_size or _sha256_file(path) != expected_sha256:
        raise ServingDatabaseV5Error("vector sidecar hash or size does not match its seal")
    row_struct = struct.Struct(f"<{QWEN3_EMBEDDING_DIMENSION}f")
    with path.open("rb") as source:
        for row_index in range(expected_rows):
            raw = source.read(VECTOR_ROW_BYTES)
            if len(raw) != VECTOR_ROW_BYTES:
                raise ServingDatabaseV5Error("vector sidecar ended before its declared row count")
            vector = row_struct.unpack(raw)
            if not all(math.isfinite(value) for value in vector):
                raise ServingDatabaseV5Error(f"vector sidecar row {row_index} is non-finite")
            norm_squared = sum(value * value for value in vector)
            if not math.isclose(
                norm_squared,
                1.0,
                rel_tol=_VECTOR_NORM_TOLERANCE,
                abs_tol=_VECTOR_NORM_TOLERANCE,
            ):
                raise ServingDatabaseV5Error(f"vector sidecar row {row_index} is not normalized")
            del raw, vector
        if source.read(1):
            raise ServingDatabaseV5Error("vector sidecar has undeclared trailing bytes")


def _verify_database(
    connection: sqlite3.Connection,
    *,
    expected_metadata: Mapping[str, str],
    expected_counts: Mapping[str, int],
    run_fts_integrity_check: bool = False,
    is_vacuumed_verify: bool = False,
) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise ServingDatabaseV5Error(f"SQLite integrity check failed: {integrity}")
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign:
        raise ServingDatabaseV5Error(f"SQLite foreign key check failed: {foreign[:3]}")
    metadata = {str(key): str(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
    if metadata != dict(expected_metadata):
        raise ServingDatabaseV5Error("serving metadata differs from the sealed exporter metadata")
    table_queries = {
        "issuers": "SELECT count(*) FROM issuers",
        "product_lineages": "SELECT count(*) FROM product_lineages",
        "unsupported_products": "SELECT count(*) FROM unsupported_products",
        "ocr_failed_products": "SELECT count(*) FROM ocr_failed_products",
        "contract_revisions": "SELECT count(*) FROM contract_revisions",
        "document_pages": "SELECT count(*) FROM document_pages",
        "structure_nodes": "SELECT count(*) FROM structure_nodes",
        "node_spans": "SELECT count(*) FROM node_spans",
        "node_links": "SELECT count(*) FROM node_links",
        "embedding_profiles": "SELECT count(*) FROM embedding_profiles",
        "embedding_views": "SELECT count(*) FROM embedding_views",
        "embedding_view_spans": "SELECT count(*) FROM embedding_view_spans",
        "embedding_views_fts": "SELECT count(*) FROM embedding_views_fts",
        "revision_coverage": "SELECT count(*) FROM revision_coverage",
    }
    for name, query in table_queries.items():
        actual = int(connection.execute(query).fetchone()[0])
        if actual != expected_counts[name]:
            raise ServingDatabaseV5Error(f"{name} row count {actual} != {expected_counts[name]}")
    row_count, minimum, maximum, distinct_count = connection.execute(
        "SELECT count(*),min(row_index),max(row_index),count(DISTINCT row_index) FROM embedding_views"
    ).fetchone()
    if (
        int(row_count) != expected_counts["embedding_views"]
        or int(minimum) != 0
        or int(maximum) != expected_counts["embedding_views"] - 1
        or int(distinct_count) != expected_counts["embedding_views"]
    ):
        raise ServingDatabaseV5Error("embedding view row_index is not contiguous and 0-based")
    view_pk_count, view_pk_min, view_pk_max = connection.execute(
        "SELECT count(*),min(view_pk),max(view_pk) FROM embedding_views"
    ).fetchone()
    if (
        int(view_pk_count) != expected_counts["embedding_views"]
        or int(view_pk_min) != 1
        or int(view_pk_max) != expected_counts["embedding_views"]
    ):
        raise ServingDatabaseV5Error("embedding view primary keys are not contiguous and 1-based")

    if is_vacuumed_verify:
        sample = connection.execute(
            "SELECT row_index,display_text FROM embedding_views ORDER BY row_index LIMIT 1"
        ).fetchone()
        if sample is not None:
            token = re.search(r"[0-9A-Za-z가-힣]{2,}", str(sample[1]))
            if token is not None:
                matched = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT row_index FROM embedding_views_fts WHERE embedding_views_fts MATCH ?",
                        ('"' + token.group(0).replace('"', '""') + '"',),
                    )
                }
                if int(sample[0]) not in matched:
                    raise ServingDatabaseV5Error("FTS shadow smoke query did not find its source view")
        return

    unbound = int(
        connection.execute(
            """SELECT count(*) FROM embedding_views v
               LEFT JOIN structure_nodes n
                 ON n.node_id=v.node_id AND n.contract_revision_id=v.contract_revision_id
               LEFT JOIN embedding_profiles p ON p.profile_id=v.profile_id
               WHERE n.node_id IS NULL OR p.profile_id IS NULL"""
        ).fetchone()[0]
    )
    if unbound:
        raise ServingDatabaseV5Error("embedding view has an unbound profile or structure node")

    total_views = expected_counts["embedding_views"]
    verified_views = 0
    views_stream = connection.execute(
        """SELECT v.row_index, v.contract_revision_id, v.display_text,
                  s.page, s.source_start, s.source_end, s.text_sha256, s.span_ordinal, p.text
             FROM embedding_views AS v
             LEFT JOIN embedding_view_spans AS s
               ON s.row_index=v.row_index AND s.contract_revision_id=v.contract_revision_id
             LEFT JOIN document_pages AS p
               ON p.contract_revision_id=s.contract_revision_id AND p.page=s.page
            ORDER BY v.row_index, s.span_ordinal"""
    )
    for (row_idx, _rev_id, display_txt), spans_iter in itertools.groupby(
        views_stream, key=lambda row: (int(row[0]), str(row[1]), str(row[2]))
    ):
        if row_idx != verified_views:
            raise ServingDatabaseV5Error("embedding view row_index is not contiguous and 0-based")
        stored_view_display_parts: list[str] = []
        span_ordinals: list[int] = []
        for span_row in spans_iter:
            _r_idx, _r_rev, _r_disp, _page, start, end, text_sha256, span_ordinal, page_text = span_row
            if span_ordinal is None or page_text is None:
                break
            span_ordinals.append(int(span_ordinal))
            text = str(page_text)[int(start) : int(end)]
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != str(text_sha256):
                raise ServingDatabaseV5Error("stored embedding view span hash is invalid")
            stored_view_display_parts.append(text)

        if span_ordinals != list(range(len(span_ordinals))):
            raise ServingDatabaseV5Error("stored embedding view spans are not contiguous")
        if not stored_view_display_parts or "".join(stored_view_display_parts) != display_txt:
            raise ServingDatabaseV5Error("stored embedding view display text is not source-bound")

        verified_views += 1
        if verified_views % 50000 == 0 or verified_views == total_views:
            LOGGER.info(
                "Verified embedding views: %d/%d (%.1f%%)",
                verified_views,
                total_views,
                (verified_views / total_views * 100.0) if total_views else 100.0,
            )

    if verified_views != total_views:
        raise ServingDatabaseV5Error("embedding view row count does not match expected")

    invalid_current = int(
        connection.execute(
            """SELECT count(*) FROM (
                 SELECT product_lineage_id FROM contract_revisions
                 WHERE temporal_status='current' GROUP BY product_lineage_id HAVING count(*) > 1
               )"""
        ).fetchone()[0]
    )
    if invalid_current:
        raise ServingDatabaseV5Error("a lineage contains multiple current revisions")
    invalid_coverage = int(
        connection.execute(
            """SELECT count(*) FROM revision_coverage
               WHERE source_non_whitespace_count != covered_non_whitespace_count"""
        ).fetchone()[0]
    )
    if invalid_coverage:
        raise ServingDatabaseV5Error("stored revision coverage is incomplete")

    stored_pages: dict[tuple[str, int], DocumentPageInput] = {}
    pages_by_revision: dict[str, list[DocumentPageInput]] = defaultdict(list)
    for revision_id, page_number, text, text_sha256 in connection.execute(
        """SELECT contract_revision_id,page,text,text_sha256 FROM document_pages
           ORDER BY contract_revision_id,page"""
    ):
        page_row = DocumentPageInput(
            contract_revision_id=str(revision_id),
            page=int(page_number),
            text=str(text),
            text_sha256=str(text_sha256),
        )
        if hashlib.sha256(page_row.text.encode("utf-8")).hexdigest() != page_row.text_sha256:
            raise ServingDatabaseV5Error("stored source page hash does not match its text")
        stored_pages[(page_row.contract_revision_id, page_row.page)] = page_row
        pages_by_revision[page_row.contract_revision_id].append(page_row)

    stored_spans: dict[tuple[str, str], list[tuple[int, int, int, str, int]]] = defaultdict(list)
    canonical_spans_by_revision: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for (
        node_id,
        revision_id,
        page_number,
        source_start,
        source_end,
        text_sha256,
        span_ordinal,
        is_canonical,
    ) in connection.execute(
        """SELECT node_id,contract_revision_id,page,source_start,source_end,text_sha256,
                  span_ordinal,is_canonical
           FROM node_spans ORDER BY contract_revision_id,node_id,span_ordinal"""
    ):
        identity = (str(node_id), str(revision_id))
        bound_page = stored_pages.get((identity[1], int(page_number)))
        if bound_page is None or int(source_start) < 0 or int(source_end) > len(bound_page.text):
            raise ServingDatabaseV5Error("stored node span exceeds its bound source page")
        source_text = bound_page.text[int(source_start) : int(source_end)]
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != str(text_sha256):
            raise ServingDatabaseV5Error("stored node span hash does not match its source text")
        stored_spans[identity].append(
            (
                int(page_number),
                int(source_start),
                int(source_end),
                source_text,
                int(is_canonical),
            )
        )
        if int(span_ordinal) != len(stored_spans[identity]) - 1:
            raise ServingDatabaseV5Error("stored node span ordinals are not contiguous")
        if is_canonical:
            canonical_spans_by_revision[str(revision_id)].append(
                (int(page_number), int(source_start), int(source_end))
            )

    for node_id, revision_id, display_text in connection.execute(
        """SELECT node_id,contract_revision_id,display_text FROM structure_nodes
           ORDER BY contract_revision_id,ordinal"""
    ):
        identity = (str(node_id), str(revision_id))
        exact_display = "".join(span[3] for span in stored_spans[identity])
        if exact_display != str(display_text):
            raise ServingDatabaseV5Error("stored node display_text differs from its source spans")

    total_revisions = len(pages_by_revision)
    for rev_idx, (revision_id, revision_pages) in enumerate(sorted(pages_by_revision.items()), start=1):
        canonical_by_page: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for page_num, s_start, s_end in canonical_spans_by_revision.get(revision_id, []):
            canonical_by_page[page_num].append((s_start, s_end))

        for page in revision_pages:
            intervals = canonical_by_page.get(page.page, [])
            intervals.sort(key=lambda item: (item[0], item[1]))
            page_len = len(page.text)
            if page_len == 0:
                if intervals:
                    raise ServingDatabaseV5Error("stored canonical spans do not reconstruct the OCR source")
                continue
            if not intervals or intervals[0][0] != 0:
                raise ServingDatabaseV5Error("stored canonical spans do not reconstruct the OCR source")
            current_end = 0
            for start, end in intervals:
                if start < current_end:
                    raise ServingDatabaseV5Error("stored canonical source spans overlap")
                if start > current_end:
                    raise ServingDatabaseV5Error("stored canonical spans do not reconstruct the OCR source")
                if end <= start:
                    raise ServingDatabaseV5Error("stored canonical source spans overlap")
                current_end = end
            if current_end != page_len:
                raise ServingDatabaseV5Error("stored canonical spans do not reconstruct the OCR source")

        source_non_whitespace = sum(not char.isspace() for page in revision_pages for char in page.text)
        coverage = RevisionCoverage(
            contract_revision_id=revision_id,
            source_sha256=_source_sha256(revision_pages),
            source_non_whitespace_count=source_non_whitespace,
            covered_non_whitespace_count=source_non_whitespace,
            coverage_sha256=_coverage_sha256(revision_pages),
        )
        stored_coverage = connection.execute(
            """SELECT source_sha256,source_non_whitespace_count,
                      covered_non_whitespace_count,coverage_sha256
               FROM revision_coverage WHERE contract_revision_id=?""",
            (revision_id,),
        ).fetchone()
        if stored_coverage is None or tuple(stored_coverage) != (
            coverage.source_sha256,
            coverage.source_non_whitespace_count,
            coverage.covered_non_whitespace_count,
            coverage.coverage_sha256,
        ):
            raise ServingDatabaseV5Error("stored revision coverage metadata is not source-bound")

        if rev_idx % 500 == 0 or rev_idx == total_revisions:
            LOGGER.info(
                "Verified revision coverage: %d/%d (%.1f%%)",
                rev_idx,
                total_revisions,
                (rev_idx / total_revisions * 100.0) if total_revisions else 100.0,
            )

    aggregate_coverage_sha256 = _aggregate_source_coverage_sha256(
        tuple(page for revision_pages in pages_by_revision.values() for page in revision_pages)
    )
    if metadata["source_coverage_sha256"] != aggregate_coverage_sha256:
        raise ServingDatabaseV5Error("aggregate source coverage metadata does not match revisions")
    if run_fts_integrity_check:
        connection.execute("INSERT INTO embedding_views_fts(embedding_views_fts) VALUES('integrity-check')")
    sample = connection.execute(
        "SELECT row_index,display_text FROM embedding_views ORDER BY row_index LIMIT 1"
    ).fetchone()
    if sample is not None:
        token = re.search(r"[0-9A-Za-z가-힣]{2,}", str(sample[1]))
        if token is not None:
            matched = {
                int(row[0])
                for row in connection.execute(
                    "SELECT row_index FROM embedding_views_fts WHERE embedding_views_fts MATCH ?",
                    ('"' + token.group(0).replace('"', '""') + '"',),
                )
            }
            if int(sample[0]) not in matched:
                raise ServingDatabaseV5Error("FTS shadow smoke query did not find its source view")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_MAX_CAPACITY_BYTES: Final = (1 << 63) - 1


def _capacity_bound(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0 or value > _MAX_CAPACITY_BYTES:
        raise ServingDatabaseV5Error(f"{field} must be a positive bounded integer")
    return value


def _capacity_reserve(value: int) -> int:
    if type(value) is not int or value < 0 or value > _MAX_CAPACITY_BYTES:
        raise ServingDatabaseV5Error("reserved free-space bytes must be a non-negative bounded integer")
    return value


def _directory_free_bytes(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ServingDatabaseV5Error("export capacity check requires no-follow filesystem support")
    try:
        descriptor = os.open(path, flags | nofollow)
    except OSError:
        raise ServingDatabaseV5Error("export filesystem capacity is unavailable") from None
    try:
        filesystem = os.fstatvfs(descriptor)
    except OSError:
        raise ServingDatabaseV5Error("export filesystem capacity is unavailable") from None
    finally:
        os.close(descriptor)
    free_bytes = filesystem.f_bavail * filesystem.f_frsize
    if free_bytes < 0 or free_bytes > _MAX_CAPACITY_BYTES:
        raise ServingDatabaseV5Error("export filesystem capacity is outside the supported range")
    return free_bytes


class ServingDatabaseExporterV5:
    def export(
        self,
        database_target: Path,
        vectors_target: Path,
        *,
        generation_id: str,
        corpus_sha256: str,
        contract_sha256: str,
        primary_embedding_profile_id: str,
        issuers: Sequence[IssuerInput],
        product_lineages: Sequence[ProductLineageInput],
        unsupported_products: Sequence[UnsupportedProductInput] = (),
        ocr_failed_products: Sequence[OCRFailedProductInput] = (),
        contract_revisions: Sequence[ContractRevisionInput],
        document_pages: Sequence[DocumentPageInput],
        structure_nodes: Sequence[StructureNodeInput],
        node_spans: Sequence[NodeSpanInput],
        node_links: Sequence[NodeLinkInput],
        embedding_profiles: Sequence[EmbeddingProfileInput],
        embedding_views: Sequence[EmbeddingViewInput],
        extra_metadata: Mapping[str, str] | None = None,
        document_aggregation_policy: (Literal["max_child", "top3_mean", "contract_plus_child"] | None) = None,
        sealed_profile_sha256: str | None = None,
        expected_exact_row_corpus_sha256: str | None = None,
        predicted_serving_database_bytes: int | None = None,
        maximum_serving_database_bytes: int | None = None,
        maximum_vector_sidecar_bytes: int | None = None,
        reserved_free_space_bytes: int = 0,
        replace_incomplete_owned_targets: bool = False,
    ) -> ServingExportV5:
        if database_target.absolute() == vectors_target.absolute():
            raise ServingDatabaseV5Error("database and vector sidecar targets must differ")
        _required_text(generation_id, field="generation_id", maximum=512)
        _require_sha256(corpus_sha256, field="corpus_sha256")
        _require_sha256(contract_sha256, field="contract_sha256")
        predicted_database_limit = _capacity_bound(
            predicted_serving_database_bytes,
            field="predicted serving database bytes",
        )
        database_limit = _capacity_bound(
            maximum_serving_database_bytes,
            field="maximum serving database bytes",
        )
        vector_limit = _capacity_bound(
            maximum_vector_sidecar_bytes,
            field="maximum vector sidecar bytes",
        )
        reserve = _capacity_reserve(reserved_free_space_bytes)
        if type(replace_incomplete_owned_targets) is not bool:
            raise ServingDatabaseV5Error("replace-incomplete-owned-targets capability must be boolean")
        sealed_limits = tuple(
            limit for limit in (predicted_database_limit, database_limit) if limit is not None
        )
        effective_database_limit = min(sealed_limits) if sealed_limits else None
        expected_vector_size = len(embedding_views) * VECTOR_ROW_BYTES
        if expected_vector_size > _MAX_CAPACITY_BYTES:
            raise ServingDatabaseV5Error("vector sidecar size exceeds the supported range")
        if vector_limit is not None and expected_vector_size > vector_limit:
            raise ServingDatabaseV5Error("vector sidecar exceeds its configured capacity limit")
        if (
            os.path.lexists(database_target) or os.path.lexists(vectors_target)
        ) and not replace_incomplete_owned_targets:
            raise ServingDatabaseV5Error("v5 export targets must not already exist")
        coverage_rows = _validate_inputs(
            issuers=issuers,
            lineages=product_lineages,
            unsupported_products=unsupported_products,
            ocr_failed_products=ocr_failed_products,
            revisions=contract_revisions,
            pages=document_pages,
            nodes=structure_nodes,
            spans=node_spans,
            links=node_links,
            profiles=embedding_profiles,
            views=embedding_views,
            primary_embedding_profile_id=primary_embedding_profile_id,
        )
        database_target.parent.mkdir(parents=True, exist_ok=True)
        vectors_target.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        database_working = database_target.parent / f".{database_target.name}.{token}.build"
        database_vacuumed = database_target.parent / f".{database_target.name}.{token}.vacuum"
        vectors_working = vectors_target.parent / f".{vectors_target.name}.{token}.build"
        temporary_paths = (database_working, database_vacuumed, vectors_working)
        if any(path.exists() for path in temporary_paths):
            raise ServingDatabaseV5Error("export temporary path already exists")

        ordered_issuers = tuple(sorted(issuers, key=lambda row: (row.sort_order, row.code)))
        ordered_lineages = tuple(sorted(product_lineages, key=lambda row: row.product_lineage_id))
        ordered_unsupported = tuple(
            sorted(unsupported_products, key=lambda row: (row.issuer, row.product_code))
        )
        ordered_ocr_failed = tuple(
            sorted(ocr_failed_products, key=lambda row: (row.issuer, row.product_code))
        )
        ordered_revisions = tuple(sorted(contract_revisions, key=lambda row: row.contract_revision_id))
        ordered_pages = tuple(sorted(document_pages, key=lambda row: (row.contract_revision_id, row.page)))
        ordered_nodes = tuple(
            sorted(structure_nodes, key=lambda row: (row.contract_revision_id, row.ordinal))
        )
        ordered_spans = tuple(
            sorted(
                node_spans,
                key=lambda row: (row.contract_revision_id, row.node_id, row.span_ordinal),
            )
        )
        ordered_links = tuple(
            sorted(node_links, key=lambda row: (row.from_contract_revision_id, row.ordinal))
        )
        ordered_profiles = tuple(sorted(embedding_profiles, key=lambda row: row.profile_id))
        ordered_views = tuple(sorted(embedding_views, key=lambda row: row.row_index))
        primary_profile = next(
            row for row in ordered_profiles if row.profile_id == primary_embedding_profile_id
        )
        source_non_whitespace_count = sum(row.source_non_whitespace_count for row in coverage_rows)
        covered_non_whitespace_count = sum(row.covered_non_whitespace_count for row in coverage_rows)
        aggregate_coverage_sha256 = _aggregate_source_coverage_sha256(ordered_pages)
        current_count = sum(row.temporal_status == "current" for row in ordered_revisions)
        superseded_count = sum(row.temporal_status == "superseded" for row in ordered_revisions)
        ambiguous_count = sum(row.temporal_status == "ambiguous" for row in ordered_revisions)
        unsupported_payload = sorted(
            (
                {
                    "disposition": row.disposition,
                    "protected_magic": row.protected_magic,
                    "protected_sha256": row.protected_sha256,
                    "protected_size_bytes": row.protected_size_bytes,
                    "source": json.loads(row.source_payload_json),
                    "source_id": row.source_id,
                }
                for row in ordered_unsupported
            ),
            key=canonical_json_bytes,
        )
        vector_sha256 = ""
        vector_size_bytes = 0
        exact_row_corpus_sha256 = ""
        connection: sqlite3.Connection | None = None
        database_seal: _SealedArtifact | None = None
        vector_seal: _SealedArtifact | None = None
        installed_targets: list[tuple[Path, _SealedArtifact]] = []
        invalid_installed_targets: set[Path] = set()

        def verify_installed_artifact(
            path: Path,
            sealed: _SealedArtifact,
            *,
            field: str,
            maximum_size_bytes: int | None,
        ) -> None:
            try:
                _revalidate_sealed_artifact(
                    path,
                    sealed,
                    field=field,
                    maximum_size_bytes=maximum_size_bytes,
                )
            except ServingDatabaseV5Error:
                invalid_installed_targets.add(path)
                raise

        try:
            vector_sha256, vector_size_bytes = _write_vector_sidecar(vectors_working, ordered_views)
            if vector_size_bytes != expected_vector_size:
                raise ServingDatabaseV5Error("vector sidecar size changed from its exact prediction")
            if vector_limit is not None and vector_size_bytes > vector_limit:
                raise ServingDatabaseV5Error("vector sidecar exceeds its configured capacity limit")
            exact_row_corpus_sha256 = v5_exact_row_corpus_sha256(
                embedding_profile_id=primary_profile.profile_id,
                vector_sidecar_sha256=vector_sha256,
                rows=tuple(
                    (
                        row.row_index,
                        row.contract_revision_id,
                        row.node_id,
                        row.view_type,
                        row.input_sha256,
                        row.profile_id,
                    )
                    for row in ordered_views
                ),
                revisions=tuple(
                    (
                        row.contract_revision_id,
                        row.product_lineage_id,
                        row.effective_date,
                        row.temporal_status,
                    )
                    for row in ordered_revisions
                ),
            )
            aggregation_presence = (
                document_aggregation_policy is not None,
                sealed_profile_sha256 is not None,
                expected_exact_row_corpus_sha256 is not None,
            )
            if len(set(aggregation_presence)) != 1:
                raise ServingDatabaseV5Error("sealed document aggregation exporter inputs are all-or-nothing")
            if expected_exact_row_corpus_sha256 is not None:
                if expected_exact_row_corpus_sha256 != exact_row_corpus_sha256:
                    raise ServingDatabaseV5Error("sealed document aggregation uses another exact-row corpus")
                if sealed_profile_sha256 is None or _SHA256.fullmatch(sealed_profile_sha256) is None:
                    raise ServingDatabaseV5Error("sealed document aggregation profile SHA-256 is invalid")
                if document_aggregation_policy not in {
                    "max_child",
                    "top3_mean",
                    "contract_plus_child",
                }:
                    raise ServingDatabaseV5Error("sealed document aggregation policy is invalid")
                counts_by_revision: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
                for row in ordered_views:
                    counts_by_revision[row.contract_revision_id][0 if row.view_type == "CONTRACT" else 1] += 1
                if any(
                    counts_by_revision[row.contract_revision_id][0] != 1
                    or counts_by_revision[row.contract_revision_id][1] < 1
                    for row in ordered_revisions
                ):
                    raise ServingDatabaseV5Error(
                        "sealed aggregation requires one CONTRACT and at least one child row"
                    )
            metadata = {
                "schema_id": SERVING_SCHEMA_ID_V5,
                "generation_id": generation_id,
                "corpus_sha256": corpus_sha256,
                "contract_sha256": contract_sha256,
                "embedding_provider": primary_profile.provider,
                "embedding_model": primary_profile.model,
                "embedding_dimension": str(primary_profile.dimension),
                "embedding_count": str(len(ordered_views)),
                "embedding_input_policy_version": primary_profile.document_policy,
                "primary_embedding_profile_id": primary_profile.profile_id,
                "vector_sidecar_sha256": vector_sha256,
                "vector_sidecar_size_bytes": str(vector_size_bytes),
                "vector_sidecar_row_count": str(len(ordered_views)),
                "vector_sidecar_dimension": str(QWEN3_EMBEDDING_DIMENSION),
                "vector_sidecar_dtype": QWEN3_EMBEDDING_DTYPE,
                "vector_sidecar_normalization": QWEN3_EMBEDDING_NORMALIZATION,
                "vector_sidecar_byte_order": "little-endian",
                "vector_sidecar_layout": "row-major",
                "vector_sidecar_profile_id": primary_profile.profile_id,
                "exact_row_corpus_sha256": exact_row_corpus_sha256,
                "document_aggregation_status": (
                    "sealed" if document_aggregation_policy is not None else "candidate_default"
                ),
                "document_aggregation_policy": document_aggregation_policy or "max_child",
                "source_non_whitespace_count": str(source_non_whitespace_count),
                "covered_non_whitespace_count": str(covered_non_whitespace_count),
                "source_coverage_sha256": aggregate_coverage_sha256,
                "issuer_count": str(len(ordered_issuers)),
                "product_lineage_count": str(len(ordered_lineages)),
                "contract_revision_count": str(len(ordered_revisions)),
                "current_revision_count": str(current_count),
                "superseded_revision_count": str(superseded_count),
                "ambiguous_revision_count": str(ambiguous_count),
                "document_page_count": str(len(ordered_pages)),
                "structure_node_count": str(len(ordered_nodes)),
                "node_span_count": str(len(ordered_spans)),
                "node_link_count": str(len(ordered_links)),
                "embedding_profile_count": str(len(ordered_profiles)),
                "embedding_view_span_count": str(sum(len(row.source_spans) for row in ordered_views)),
                "unsupported_document_count": str(len(ordered_unsupported)),
                "unsupported_documents_sha256": canonical_sha256(
                    {
                        "schema_version": "cardrag.unsupported-documents.v1",
                        "documents": unsupported_payload,
                    }
                ),
                "ocr_failed_document_count": str(len(ordered_ocr_failed)),
                "ocr_failed_documents_sha256": canonical_sha256(
                    {
                        "schema_version": "cardrag.ocr-failed-products.v1",
                        "documents": [row.payload for row in ordered_ocr_failed],
                    }
                ),
            }
            if sealed_profile_sha256 is not None:
                metadata["sealed_profile_sha256"] = sealed_profile_sha256
            for view_type in sorted(_VIEW_TYPES):
                metadata[f"embedding_view_count.{view_type}"] = str(
                    sum(row.view_type == view_type for row in ordered_views)
                )
            for node_type in sorted(_NODE_TYPES):
                metadata[f"structure_node_count.{node_type}"] = str(
                    sum(row.node_type == node_type for row in ordered_nodes)
                )
            for major_class in sorted(_MAJOR_CLASSES):
                metadata[f"structure_major_class_count.{major_class}"] = str(
                    sum(
                        row.node_type == "MAJOR_SECTION" and row.major_class == major_class
                        for row in ordered_nodes
                    )
                )
            if extra_metadata:
                overlap = set(metadata).intersection(extra_metadata)
                if overlap:
                    raise ServingDatabaseV5Error(
                        "reserved metadata keys cannot be overridden: " + ",".join(sorted(overlap))
                    )
                for key, value in extra_metadata.items():
                    _required_text(key, field="extra metadata key", maximum=128)
                    if "\x00" in value or len(value) > 4096:
                        raise ServingDatabaseV5Error("extra metadata value is invalid")
                metadata.update(extra_metadata)

            connection = sqlite3.connect(database_working)
            connection.execute(f"PRAGMA page_size={SQLITE_PAGE_BYTES}")
            sealed_page_size = connection.execute("PRAGMA page_size").fetchone()
            if sealed_page_size is None or int(sealed_page_size[0]) != SQLITE_PAGE_BYTES:
                raise ServingDatabaseV5Error("working serving database SQLite page size could not be sealed")
            if effective_database_limit is not None:
                maximum_working_pages = (2 * effective_database_limit) // SQLITE_PAGE_BYTES
                if maximum_working_pages < 1:
                    raise ServingDatabaseV5Error("serving database capacity cannot fit one SQLite page")
                sealed_maximum = connection.execute(
                    f"PRAGMA max_page_count={maximum_working_pages}"
                ).fetchone()
                if sealed_maximum is None or int(sealed_maximum[0]) != maximum_working_pages:
                    raise ServingDatabaseV5Error("working serving database page limit could not be sealed")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(DDL_V5)
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?)",
                sorted(metadata.items()),
            )
            connection.executemany(
                "INSERT INTO issuers(code,display_name,sort_order) VALUES(?,?,?)",
                ((row.code, row.display_name, row.sort_order) for row in ordered_issuers),
            )
            connection.executemany(
                """INSERT INTO product_lineages
                   (product_lineage_id,issuer,product_code,document_type,name)
                   VALUES(?,?,?,?,?)""",
                (
                    (
                        row.product_lineage_id,
                        row.issuer,
                        row.product_code,
                        row.document_type,
                        row.name,
                    )
                    for row in ordered_lineages
                ),
            )
            connection.executemany(
                """INSERT INTO unsupported_products
                   (issuer,product_code,name,disposition,source_id,source_version,source_url,
                    protected_magic,protected_sha256,protected_size_bytes,source_payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        row.issuer,
                        row.product_code,
                        row.name,
                        row.disposition,
                        row.source_id,
                        row.source_version,
                        row.source_url,
                        row.protected_magic,
                        row.protected_sha256,
                        row.protected_size_bytes,
                        row.source_payload_json,
                    )
                    for row in ordered_unsupported
                ),
            )
            connection.executemany(
                """INSERT INTO ocr_failed_products
                   (issuer,product_code,name,document_id,title,pdf_sha256,pdf_size_bytes,
                    page_count,reason_code,reason,attempts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        row.issuer,
                        row.product_code,
                        row.name,
                        row.document_id,
                        row.title,
                        row.pdf_sha256,
                        row.pdf_size_bytes,
                        row.page_count,
                        row.reason_code,
                        row.reason,
                        row.attempts,
                    )
                    for row in ordered_ocr_failed
                ),
            )
            pending_revisions = list(ordered_revisions)
            inserted_revisions: set[str] = set()
            while pending_revisions:
                ready = [
                    row
                    for row in pending_revisions
                    if row.supersedes_revision_id is None or row.supersedes_revision_id in inserted_revisions
                ]
                if not ready:
                    raise ServingDatabaseV5Error("revision insertion order cannot satisfy supersession")
                connection.executemany(
                    """INSERT INTO contract_revisions
                       (contract_revision_id,product_lineage_id,document_id,source_id,
                        source_version,source_url,effective_date,pdf_sha256,pdf_size_bytes,
                        page_count,temporal_status,supersedes_revision_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        (
                            row.contract_revision_id,
                            row.product_lineage_id,
                            row.document_id,
                            row.source_id,
                            row.source_version,
                            row.source_url,
                            row.effective_date,
                            row.pdf_sha256,
                            row.pdf_size_bytes,
                            row.page_count,
                            row.temporal_status,
                            row.supersedes_revision_id,
                        )
                        for row in ready
                    ),
                )
                inserted_revisions.update(row.contract_revision_id for row in ready)
                ready_ids = {row.contract_revision_id for row in ready}
                pending_revisions = [
                    row for row in pending_revisions if row.contract_revision_id not in ready_ids
                ]
            connection.executemany(
                """INSERT INTO document_pages(contract_revision_id,page,text,text_sha256)
                   VALUES(?,?,?,?)""",
                ((row.contract_revision_id, row.page, row.text, row.text_sha256) for row in ordered_pages),
            )
            for revision_id in sorted({row.contract_revision_id for row in ordered_nodes}):
                pending_nodes = [row for row in ordered_nodes if row.contract_revision_id == revision_id]
                inserted_nodes: set[str] = set()
                while pending_nodes:
                    ready_nodes = [
                        row
                        for row in pending_nodes
                        if row.parent_id is None or row.parent_id in inserted_nodes
                    ]
                    if not ready_nodes:
                        raise ServingDatabaseV5Error("node insertion order cannot satisfy parents")
                    connection.executemany(
                        """INSERT INTO structure_nodes
                           (node_id,contract_revision_id,parent_id,parent_contract_revision_id,
                            node_type,major_class,raw_heading,ordinal,display_text,
                            table_headers_json,table_cells_json,table_role)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            (
                                row.node_id,
                                row.contract_revision_id,
                                row.parent_id,
                                row.parent_contract_revision_id,
                                row.node_type,
                                row.major_class,
                                row.raw_heading,
                                row.ordinal,
                                row.display_text,
                                json.dumps(
                                    list(row.table_headers),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                json.dumps(
                                    list(row.table_cells),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                row.table_role,
                            )
                            for row in ready_nodes
                        ),
                    )
                    inserted_nodes.update(row.node_id for row in ready_nodes)
                    ready_node_ids = {row.node_id for row in ready_nodes}
                    pending_nodes = [row for row in pending_nodes if row.node_id not in ready_node_ids]
            connection.executemany(
                """INSERT INTO node_spans
                   (node_id,contract_revision_id,page,source_start,source_end,text_sha256,
                    span_ordinal,is_canonical)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    (
                        row.node_id,
                        row.contract_revision_id,
                        row.page,
                        row.source_start,
                        row.source_end,
                        row.text_sha256,
                        row.span_ordinal,
                        int(row.is_canonical),
                    )
                    for row in ordered_spans
                ),
            )
            connection.executemany(
                """INSERT INTO node_links
                   (from_node_id,from_contract_revision_id,to_node_id,to_contract_revision_id,
                    link_type)
                   VALUES(?,?,?,?,?)""",
                (
                    (
                        row.from_node_id,
                        row.from_contract_revision_id,
                        row.to_node_id,
                        row.to_contract_revision_id,
                        row.link_type,
                    )
                    for row in ordered_links
                ),
            )
            connection.executemany(
                """INSERT INTO embedding_profiles
                   (profile_id,provider,model,provider_id,dimension,dtype,normalization,
                    document_policy,query_policy,maximum_tokens)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        row.profile_id,
                        row.provider,
                        row.model,
                        row.provider_id,
                        row.dimension,
                        row.dtype,
                        row.normalization,
                        row.document_policy,
                        row.query_policy,
                        row.maximum_tokens,
                    )
                    for row in ordered_profiles
                ),
            )
            connection.executemany(
                """INSERT INTO embedding_views
                   (view_pk,row_index,node_id,contract_revision_id,view_type,input_sha256,
                    profile_id,display_text)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    (
                        row.row_index + 1,
                        row.row_index,
                        row.node_id,
                        row.contract_revision_id,
                        row.view_type,
                        row.input_sha256,
                        row.profile_id,
                        row.display_text,
                    )
                    for row in ordered_views
                ),
            )
            connection.executemany(
                """INSERT INTO embedding_view_spans
                   (row_index,contract_revision_id,page,source_start,source_end,
                    text_sha256,span_ordinal)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    (
                        row.row_index,
                        row.contract_revision_id,
                        span.page,
                        span.source_start,
                        span.source_end,
                        span.text_sha256,
                        span_ordinal,
                    )
                    for row in ordered_views
                    for span_ordinal, span in enumerate(row.source_spans)
                ),
            )
            connection.executemany(
                "INSERT INTO embedding_views_fts(row_index,node_id,display_text) VALUES(?,?,?)",
                ((row.row_index, row.node_id, row.display_text) for row in ordered_views),
            )
            connection.executemany(
                """INSERT INTO revision_coverage
                   (contract_revision_id,source_sha256,source_non_whitespace_count,
                    covered_non_whitespace_count,coverage_sha256)
                   VALUES(?,?,?,?,?)""",
                (
                    (
                        row.contract_revision_id,
                        row.source_sha256,
                        row.source_non_whitespace_count,
                        row.covered_non_whitespace_count,
                        row.coverage_sha256,
                    )
                    for row in coverage_rows
                ),
            )
            connection.commit()
            expected_counts = {
                "issuers": len(ordered_issuers),
                "product_lineages": len(ordered_lineages),
                "unsupported_products": len(ordered_unsupported),
                "ocr_failed_products": len(ordered_ocr_failed),
                "contract_revisions": len(ordered_revisions),
                "document_pages": len(ordered_pages),
                "structure_nodes": len(ordered_nodes),
                "node_spans": len(ordered_spans),
                "node_links": len(ordered_links),
                "embedding_profiles": len(ordered_profiles),
                "embedding_views": len(ordered_views),
                "embedding_view_spans": sum(len(row.source_spans) for row in ordered_views),
                "embedding_views_fts": len(ordered_views),
                "revision_coverage": len(coverage_rows),
            }
            _verify_database(
                connection,
                expected_metadata=metadata,
                expected_counts=expected_counts,
                run_fts_integrity_check=True,
            )
            connection.commit()
            working_database_size = database_working.stat().st_size
            if effective_database_limit is not None and working_database_size > 2 * effective_database_limit:
                raise ServingDatabaseV5Error(
                    "working serving database exceeds twice its effective capacity limit"
                )
            if effective_database_limit is not None:
                required_vacuum_free = 2 * effective_database_limit + reserve
                if required_vacuum_free > _MAX_CAPACITY_BYTES:
                    raise ServingDatabaseV5Error(
                        "serving database vacuum capacity exceeds the supported range"
                    )
                if _directory_free_bytes(database_target.parent) < required_vacuum_free:
                    raise ServingDatabaseV5Error(
                        "serving database vacuum lacks configured reserved free space"
                    )
            connection.execute("VACUUM INTO ?", (str(database_vacuumed),))
            connection.close()
            connection = None
            vacuumed_database_size = database_vacuumed.stat().st_size
            if database_limit is not None and vacuumed_database_size > database_limit:
                raise ServingDatabaseV5Error(
                    "vacuumed serving database exceeds its configured capacity limit"
                )
            if predicted_database_limit is not None and vacuumed_database_size > predicted_database_limit:
                raise ServingDatabaseV5Error("vacuumed serving database exceeds its preflight prediction")

            # Bind SQLite verification to one no-follow identity and digest.
            # Hashing only after verification would adopt a same-size mutation
            # made between the immutable reader closing and publication.
            database_seal = _seal_regular_artifact(
                database_vacuumed,
                field="serving database",
                expected_size_bytes=vacuumed_database_size,
                maximum_size_bytes=effective_database_limit,
            )
            verify = sqlite3.connect(f"file:{database_vacuumed}?mode=ro&immutable=1", uri=True)
            try:
                _verify_database(
                    verify,
                    expected_metadata=metadata,
                    expected_counts=expected_counts,
                    is_vacuumed_verify=True,
                )
            finally:
                verify.close()
            _revalidate_sealed_artifact(
                database_vacuumed,
                database_seal,
                field="serving database",
                maximum_size_bytes=effective_database_limit,
            )
            _verify_vector_sidecar(
                vectors_working,
                expected_sha256=vector_sha256,
                expected_rows=len(ordered_views),
            )

            # Seal the exact identities and bytes that passed the expensive
            # SQLite/vector verification.  Revalidate both artifacts before
            # publishing either target so a detected source mutation cannot
            # disturb an owned incomplete pair from an earlier attempt.
            vector_seal = _seal_regular_artifact(
                vectors_working,
                field="vector sidecar",
                expected_size_bytes=vector_size_bytes,
                expected_sha256=vector_sha256,
                maximum_size_bytes=vector_limit,
            )
            _revalidate_sealed_artifact(
                vectors_working,
                vector_seal,
                field="vector sidecar",
                maximum_size_bytes=vector_limit,
            )
            _revalidate_sealed_artifact(
                database_vacuumed,
                database_seal,
                field="serving database",
                maximum_size_bytes=effective_database_limit,
            )

            # The immediate source check closes the final verification-to-
            # rename window; the target check proves os.replace installed that
            # same inode, size, and digest rather than raced bytes.
            _revalidate_sealed_artifact(
                vectors_working,
                vector_seal,
                field="vector sidecar",
                maximum_size_bytes=vector_limit,
            )
            os.replace(vectors_working, vectors_target)
            installed_targets.append((vectors_target, vector_seal))
            verify_installed_artifact(
                vectors_target,
                vector_seal,
                field="installed vector sidecar",
                maximum_size_bytes=vector_limit,
            )
            _revalidate_sealed_artifact(
                database_vacuumed,
                database_seal,
                field="serving database",
                maximum_size_bytes=effective_database_limit,
            )
            os.replace(database_vacuumed, database_target)
            installed_targets.append((database_target, database_seal))
            verify_installed_artifact(
                database_target,
                database_seal,
                field="installed serving database",
                maximum_size_bytes=effective_database_limit,
            )
            for directory in {database_target.parent, vectors_target.parent}:
                _fsync_directory(directory)
            verify_installed_artifact(
                vectors_target,
                vector_seal,
                field="installed vector sidecar",
                maximum_size_bytes=vector_limit,
            )
            verify_installed_artifact(
                database_target,
                database_seal,
                field="installed serving database",
                maximum_size_bytes=effective_database_limit,
            )
        except sqlite3.DatabaseError as exc:
            if not replace_incomplete_owned_targets:
                for target, sealed in reversed(installed_targets):
                    _unlink_installed_artifact_if_owned(target, sealed)
            else:
                for target, sealed in reversed(installed_targets):
                    if target in invalid_installed_targets:
                        _unlink_installed_artifact_if_owned(target, sealed)
            if effective_database_limit is not None and "full" in str(exc).casefold():
                raise ServingDatabaseV5Error(
                    "working serving database reached its sealed page limit"
                ) from None
            raise
        except BaseException:
            if not replace_incomplete_owned_targets:
                for target, sealed in reversed(installed_targets):
                    _unlink_installed_artifact_if_owned(target, sealed)
            else:
                for target, sealed in reversed(installed_targets):
                    if target in invalid_installed_targets:
                        _unlink_installed_artifact_if_owned(target, sealed)
            raise
        finally:
            if connection is not None:
                connection.close()
            for path in temporary_paths:
                path.unlink(missing_ok=True)

        if database_seal is None or vector_seal is None:
            raise ServingDatabaseV5Error("v5 export artifacts were not sealed")
        return ServingExportV5(
            database_path=database_target,
            database_sha256=database_seal.sha256,
            database_size_bytes=database_seal.size_bytes,
            vector_path=vectors_target,
            vector_sha256=vector_seal.sha256,
            vector_size_bytes=vector_seal.size_bytes,
            vector_row_count=len(ordered_views),
            vector_dimension=QWEN3_EMBEDDING_DIMENSION,
            issuer_count=len(ordered_issuers),
            product_lineage_count=len(ordered_lineages),
            unsupported_product_count=len(ordered_unsupported),
            ocr_failed_product_count=len(ordered_ocr_failed),
            contract_revision_count=len(ordered_revisions),
            current_revision_count=current_count,
            superseded_revision_count=superseded_count,
            ambiguous_revision_count=ambiguous_count,
            document_page_count=len(ordered_pages),
            structure_node_count=len(ordered_nodes),
            node_span_count=len(ordered_spans),
            node_link_count=len(ordered_links),
            embedding_profile_count=len(ordered_profiles),
            embedding_view_count=len(ordered_views),
            source_non_whitespace_count=source_non_whitespace_count,
            covered_non_whitespace_count=covered_non_whitespace_count,
            source_coverage_sha256=aggregate_coverage_sha256,
            exact_row_corpus_sha256=exact_row_corpus_sha256,
        )


__all__ = [
    "ContractRevisionInput",
    "DDL_V5",
    "DocumentPageInput",
    "EmbeddingProfileInput",
    "EmbeddingViewInput",
    "IssuerInput",
    "LazyEmbeddingVector",
    "LinkType",
    "MajorClass",
    "NodeLinkInput",
    "NodeSpanInput",
    "NodeType",
    "OCRFailedProductInput",
    "ProductLineageInput",
    "RevisionCoverage",
    "SERVING_SCHEMA_ID_V5",
    "ServingDatabaseExporterV5",
    "ServingDatabaseV5Error",
    "ServingExportV5",
    "StructureNodeInput",
    "TemporalStatus",
    "UnsupportedProductInput",
    "VECTOR_ROW_BYTES",
    "VECTOR_SIDECAR_NAME",
    "ViewSourceSpanInput",
    "ViewType",
]
