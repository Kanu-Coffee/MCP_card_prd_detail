from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from cardrag_core import canonical_sha256, qwen3_embedding_profile_id

from cardrag_mcp.store import GenerationHandle, GenerationStore, cas_path, load_generation_handle

V5_DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT, WITHOUT ROWID;
CREATE TABLE issuers (
  code TEXT PRIMARY KEY, display_name TEXT NOT NULL, sort_order INTEGER NOT NULL
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
  disposition TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_version TEXT NOT NULL,
  source_url TEXT NOT NULL,
  protected_magic TEXT NOT NULL,
  protected_sha256 TEXT NOT NULL,
  protected_size_bytes INTEGER NOT NULL,
  source_payload_json TEXT NOT NULL,
  PRIMARY KEY(issuer,product_code),
  FOREIGN KEY(issuer) REFERENCES issuers(code)
) STRICT, WITHOUT ROWID;
CREATE TABLE ocr_failed_products (
  issuer TEXT NOT NULL,
  product_code TEXT NOT NULL,
  name TEXT NOT NULL,
  document_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  pdf_sha256 TEXT NOT NULL,
  pdf_size_bytes INTEGER NOT NULL,
  page_count INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  reason TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  PRIMARY KEY(issuer,product_code),
  FOREIGN KEY(issuer) REFERENCES issuers(code)
) STRICT, WITHOUT ROWID;
CREATE TABLE contract_revisions (
  contract_revision_id TEXT PRIMARY KEY,
  product_lineage_id TEXT NOT NULL REFERENCES product_lineages(product_lineage_id),
  document_id TEXT NOT NULL UNIQUE,
  source_id TEXT NOT NULL,
  source_version TEXT NOT NULL,
  source_url TEXT NOT NULL,
  effective_date TEXT NOT NULL,
  pdf_sha256 TEXT NOT NULL,
  pdf_size_bytes INTEGER NOT NULL,
  page_count INTEGER NOT NULL,
  temporal_status TEXT NOT NULL,
  supersedes_revision_id TEXT,
  UNIQUE(product_lineage_id,contract_revision_id),
  FOREIGN KEY(product_lineage_id,supersedes_revision_id)
    REFERENCES contract_revisions(product_lineage_id,contract_revision_id)
) STRICT;
CREATE TABLE document_pages (
  contract_revision_id TEXT NOT NULL REFERENCES contract_revisions(contract_revision_id),
  page INTEGER NOT NULL,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  PRIMARY KEY(contract_revision_id,page)
) STRICT, WITHOUT ROWID;
CREATE TABLE structure_nodes (
  node_id TEXT NOT NULL,
  contract_revision_id TEXT NOT NULL REFERENCES contract_revisions(contract_revision_id),
  parent_id TEXT,
  parent_contract_revision_id TEXT,
  node_type TEXT NOT NULL,
  major_class TEXT NOT NULL,
  raw_heading TEXT,
  ordinal INTEGER NOT NULL,
  display_text TEXT NOT NULL,
  table_headers_json TEXT NOT NULL,
  table_cells_json TEXT NOT NULL,
  table_role TEXT,
  PRIMARY KEY(node_id,contract_revision_id),
  UNIQUE(contract_revision_id,ordinal),
  FOREIGN KEY(parent_id,parent_contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id)
) STRICT, WITHOUT ROWID;
CREATE TABLE node_spans (
  node_id TEXT NOT NULL,
  contract_revision_id TEXT NOT NULL,
  page INTEGER NOT NULL,
  source_start INTEGER NOT NULL,
  source_end INTEGER NOT NULL,
  text_sha256 TEXT NOT NULL,
  span_ordinal INTEGER NOT NULL,
  is_canonical INTEGER NOT NULL,
  PRIMARY KEY(node_id,contract_revision_id,span_ordinal),
  FOREIGN KEY(node_id,contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id),
  FOREIGN KEY(contract_revision_id,page)
    REFERENCES document_pages(contract_revision_id,page)
) STRICT, WITHOUT ROWID;
CREATE TABLE node_links (
  from_node_id TEXT NOT NULL,
  from_contract_revision_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  to_contract_revision_id TEXT NOT NULL,
  link_type TEXT NOT NULL,
  PRIMARY KEY(from_node_id,from_contract_revision_id,to_node_id,to_contract_revision_id,link_type),
  FOREIGN KEY(from_node_id,from_contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id),
  FOREIGN KEY(to_node_id,to_contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id)
) STRICT, WITHOUT ROWID;
CREATE TABLE embedding_profiles (
  profile_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  dtype TEXT NOT NULL,
  normalization TEXT NOT NULL,
  document_policy TEXT NOT NULL,
  query_policy TEXT NOT NULL,
  maximum_tokens INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
CREATE TABLE embedding_views (
  view_pk INTEGER PRIMARY KEY,
  row_index INTEGER NOT NULL UNIQUE,
  node_id TEXT NOT NULL,
  contract_revision_id TEXT NOT NULL,
  view_type TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  profile_id TEXT NOT NULL REFERENCES embedding_profiles(profile_id),
  display_text TEXT NOT NULL,
  FOREIGN KEY(node_id,contract_revision_id)
    REFERENCES structure_nodes(node_id,contract_revision_id)
) STRICT;
CREATE TABLE embedding_view_spans (
  row_index INTEGER NOT NULL REFERENCES embedding_views(row_index),
  contract_revision_id TEXT NOT NULL,
  page INTEGER NOT NULL,
  source_start INTEGER NOT NULL,
  source_end INTEGER NOT NULL,
  text_sha256 TEXT NOT NULL,
  span_ordinal INTEGER NOT NULL,
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
  source_sha256 TEXT NOT NULL,
  source_non_whitespace_count INTEGER NOT NULL,
  covered_non_whitespace_count INTEGER NOT NULL,
  coverage_sha256 TEXT NOT NULL
) STRICT, WITHOUT ROWID;
"""


@dataclass(frozen=True, slots=True)
class V5Fixture:
    generation_id: str
    database: Path
    vectors: Path
    profile_id: str
    vector_count: int
    pdf_objects: tuple[tuple[str, bytes], ...]
    current_revision_id: str
    old_revision_id: str
    ambiguous_revision_id: str
    lineage_id: str
    unsupported_product_code: str = "LOCKED"
    ocr_failed_product_code: str = "OCRFAIL"
    ocr_failed_document_id: str = "doc_" + "f" * 64


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _lineage_id(issuer: str, product_code: str, document_type: str) -> str:
    return "lineage_" + canonical_sha256(
        {
            "document_type": document_type,
            "issuer": issuer,
            "product_code": product_code,
        }
    )


def _revision_id(lineage_id: str, source_id: str, pdf_sha256: str) -> str:
    return "revision_" + canonical_sha256(
        {
            "pdf_sha256": pdf_sha256,
            "product_lineage_id": lineage_id,
            "source_id": source_id,
        }
    )


def _vector(score: float) -> np.ndarray:
    value = np.zeros((4096,), dtype="<f4")
    value[0] = score
    value[1] = math.sqrt(1.0 - score * score)
    value /= np.linalg.norm(value)
    return value


def _source_hash(pages: list[tuple[int, str, str]]) -> str:
    return canonical_sha256(
        {
            "pages": [{"page": page, "text_sha256": text_sha256} for page, _, text_sha256 in pages],
            "schema_version": "cardrag.structure-source.v1",
        }
    )


def _coverage_hash(pages: list[tuple[int, str, str]]) -> str:
    return canonical_sha256(
        {
            "pages": [
                {
                    "non_whitespace_characters": sum(not character.isspace() for character in text),
                    "non_whitespace_sha256": _sha256_text(
                        "".join(character for character in text if not character.isspace())
                    ),
                    "page": page,
                    "text_sha256": text_sha256,
                }
                for page, text, text_sha256 in pages
            ],
            "schema_version": "cardrag.structure-coverage.v1",
        }
    )


def build_v5_fixture(
    directory: Path,
    *,
    generation_id: str = "gen-v5-exact",
    include_dispositions: bool = False,
) -> V5Fixture:
    directory.mkdir(parents=True, exist_ok=False)
    database = directory / "index.sqlite3"
    vectors = directory / "vectors.f32"
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    alpha_lineage_id = _lineage_id("kb", "ALPHA", "product_description")
    beta_lineage_id = _lineage_id("kb", "BETA", "product_description")
    old_source_id = "source_" + _sha256_text("fixture:alpha-old")
    current_source_id = "source_" + _sha256_text("fixture:alpha-current")
    ambiguous_source_id = "source_" + _sha256_text("fixture:beta-ambiguous")
    old_pdf = b"%PDF-1.4\nalpha-old"
    current_pdf = b"%PDF-1.4\nalpha-current"
    ambiguous_pdf = b"%PDF-1.4\nbeta-ambiguous"
    old_revision_id = _revision_id(alpha_lineage_id, old_source_id, _sha256_bytes(old_pdf))
    current_revision_id = _revision_id(
        alpha_lineage_id, current_source_id, _sha256_bytes(current_pdf)
    )
    ambiguous_revision_id = _revision_id(
        beta_lineage_id, ambiguous_source_id, _sha256_bytes(ambiguous_pdf)
    )
    pdf_by_revision = {
        old_revision_id: old_pdf,
        current_revision_id: current_pdf,
        ambiguous_revision_id: ambiguous_pdf,
    }
    source_by_revision = {
        old_revision_id: old_source_id,
        current_revision_id: current_source_id,
        ambiguous_revision_id: ambiguous_source_id,
    }
    failed_pdf = b"%PDF-1.4\nocr-failed"
    if include_dispositions:
        pdf_by_revision["ocr-failed"] = failed_pdf
    unsupported_source = {
        "category": "card",
        "document_type": "product_description",
        "effective_date": "2025-01-01",
        "file_name": "locked.pdf",
        "issuer": "kb",
        "metadata": {},
        "product_code": "LOCKED",
        "product_name": "보호 카드",
        "source_post_id": "post-locked",
        "source_url": "https://public.example/locked.pdf",
        "source_version": "v1",
    }
    unsupported_source_json = json.dumps(
        unsupported_source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    unsupported_source_id = "source_" + _sha256_text(unsupported_source_json)
    unsupported_payload = {
        "disposition": "unsupported_drm",
        "protected_magic": "FASOO_DRMONE",
        "protected_sha256": _sha256_text("protected-pdf"),
        "protected_size_bytes": 1234,
        "source": unsupported_source,
        "source_id": unsupported_source_id,
    }
    failed_payload = {
        "attempts": 3,
        "document_id": "doc_" + "f" * 64,
        "issuer": "kb",
        "page_count": 2,
        "pdf_sha256": _sha256_bytes(failed_pdf),
        "pdf_size_bytes": len(failed_pdf),
        "product_code": "OCRFAIL",
        "product_name": "OCR 실패 카드",
        "reason": "isolated OCR attempts exhausted",
        "reason_code": "provider_failed",
        "title": "OCR 실패 카드 안내장",
    }
    revisions = (
        (
            old_revision_id,
            alpha_lineage_id,
            "doc-alpha-old",
            "2024-01-01",
            "superseded",
            None,
        ),
        (
            current_revision_id,
            alpha_lineage_id,
            "doc-alpha-current",
            "2025-01-01",
            "current",
            old_revision_id,
        ),
        (
            ambiguous_revision_id,
            beta_lineage_id,
            "doc-beta-ambiguous",
            "2023-06-01",
            "ambiguous",
            None,
        ),
    )
    page_text = {
        old_revision_id: "구 혜택 안내\n",
        current_revision_id: "공항 라운지 혜택\n전월 실적 조건\n",
        ambiguous_revision_id: "마일리지 적립 혜택\n",
    }
    pages = {
        revision_id: [(1, text, _sha256_text(text))] for revision_id, text in page_text.items()
    }
    nodes: list[tuple[object, ...]] = []
    spans: list[tuple[object, ...]] = []
    paragraph_by_revision: dict[str, str] = {}
    for revision_id in (old_revision_id, current_revision_id, ambiguous_revision_id):
        root = f"{revision_id}-root"
        major = f"{revision_id}-benefit"
        item = f"{revision_id}-item"
        paragraph = f"{revision_id}-paragraph"
        paragraph_by_revision[revision_id] = paragraph
        nodes.extend(
            (
                (root, revision_id, None, None, "ROOT", "UNKNOWN", None, 0, ""),
                (
                    major,
                    revision_id,
                    root,
                    revision_id,
                    "MAJOR_SECTION",
                    "BENEFIT",
                    "혜택",
                    1,
                    "",
                ),
                (item, revision_id, major, revision_id, "ITEM", "BENEFIT", None, 2, ""),
            )
        )
        text = page_text[revision_id]
        if revision_id == current_revision_id:
            split = text.index("\n") + 1
            benefit_text = text[:split]
            notice_text = text[split:]
            nodes.append(
                (
                    paragraph,
                    revision_id,
                    item,
                    revision_id,
                    "PARAGRAPH",
                    "BENEFIT",
                    None,
                    3,
                    benefit_text,
                )
            )
            notice_major = f"{revision_id}-notice"
            notice_paragraph = f"{revision_id}-notice-paragraph"
            nodes.extend(
                (
                    (
                        notice_major,
                        revision_id,
                        root,
                        revision_id,
                        "MAJOR_SECTION",
                        "NOTICE",
                        "조건",
                        4,
                        "",
                    ),
                    (
                        notice_paragraph,
                        revision_id,
                        notice_major,
                        revision_id,
                        "PARAGRAPH",
                        "NOTICE",
                        None,
                        5,
                        notice_text,
                    ),
                )
            )
            spans.extend(
                (
                    (
                        paragraph,
                        revision_id,
                        1,
                        0,
                        split,
                        _sha256_text(benefit_text),
                        0,
                        1,
                    ),
                    (
                        notice_paragraph,
                        revision_id,
                        1,
                        split,
                        len(text),
                        _sha256_text(notice_text),
                        0,
                        1,
                    ),
                )
            )
        else:
            nodes.append(
                (
                    paragraph,
                    revision_id,
                    item,
                    revision_id,
                    "PARAGRAPH",
                    "BENEFIT",
                    None,
                    3,
                    text,
                )
            )
            spans.append(
                (
                    paragraph,
                    revision_id,
                    1,
                    0,
                    len(text),
                    _sha256_text(text),
                    0,
                    1,
                )
            )
    current_notice = f"{current_revision_id}-notice-paragraph"
    links = (
        (
            current_notice,
            current_revision_id,
            paragraph_by_revision[current_revision_id],
            current_revision_id,
            "APPLIES_TO",
        ),
    )
    view_rows = (
        (
            f"{old_revision_id}-item",
            old_revision_id,
            "DETAIL",
            page_text[old_revision_id],
            0.95,
        ),
        (
            f"{current_revision_id}-item",
            current_revision_id,
            "DETAIL",
            page_text[current_revision_id].splitlines(keepends=True)[0],
            0.80,
        ),
        (
            f"{current_revision_id}-item",
            current_revision_id,
            "TITLE",
            page_text[current_revision_id].splitlines(keepends=True)[0],
            0.70,
        ),
        (
            current_notice,
            current_revision_id,
            "DETAIL",
            page_text[current_revision_id].splitlines(keepends=True)[1],
            0.10,
        ),
        (
            f"{ambiguous_revision_id}-item",
            ambiguous_revision_id,
            "DETAIL",
            page_text[ambiguous_revision_id],
            0.90,
        ),
    )
    matrix = np.stack([_vector(score) for *_, score in view_rows]).astype("<f4")
    vector_body = matrix.tobytes(order="C")
    vectors.write_bytes(vector_body)
    aggregate_digest = hashlib.sha256()
    for revision_id in sorted(pages):
        for _, text, _ in pages[revision_id]:
            for character in text:
                if not character.isspace():
                    aggregate_digest.update(character.encode("utf-8"))
    source_non_whitespace_count = sum(
        not character.isspace()
        for revision_pages in pages.values()
        for _, text, _ in revision_pages
        for character in text
    )
    metadata = {
        "schema_id": "cardrag.serving-db.v5",
        "generation_id": generation_id,
        "corpus_sha256": _sha256_text(f"corpus:{generation_id}"),
        "contract_sha256": _sha256_text(f"contract:{generation_id}"),
        "embedding_provider": "openrouter",
        "embedding_model": "qwen/qwen3-embedding-8b",
        "embedding_input_policy_version": "cardrag.structure-views.v1",
        "embedding_dimension": "4096",
        "embedding_count": str(len(view_rows)),
        "embedding_profile_count": "1",
        "embedding_view_span_count": str(len(view_rows)),
        "primary_embedding_profile_id": profile_id,
        "vector_sidecar_sha256": _sha256_bytes(vector_body),
        "vector_sidecar_size_bytes": str(len(vector_body)),
        "vector_sidecar_dtype": "float32",
        "vector_sidecar_normalization": "l2",
        "vector_sidecar_byte_order": "little-endian",
        "vector_sidecar_layout": "row-major",
        "vector_sidecar_profile_id": profile_id,
        "vector_sidecar_dimension": "4096",
        "vector_sidecar_row_count": str(len(view_rows)),
        "unsupported_document_count": "1" if include_dispositions else "0",
        "unsupported_documents_sha256": canonical_sha256(
            {
                "documents": [unsupported_payload] if include_dispositions else [],
                "schema_version": "cardrag.unsupported-documents.v1",
            }
        ),
        "ocr_failed_document_count": "1" if include_dispositions else "0",
        "ocr_failed_documents_sha256": canonical_sha256(
            {
                "documents": [failed_payload] if include_dispositions else [],
                "schema_version": "cardrag.ocr-failed-products.v1",
            }
        ),
        "issuer_count": "1",
        "product_lineage_count": "2",
        "contract_revision_count": "3",
        "document_page_count": "3",
        "structure_node_count": str(len(nodes)),
        "node_span_count": str(len(spans)),
        "node_link_count": str(len(links)),
        "current_revision_count": "1",
        "superseded_revision_count": "1",
        "ambiguous_revision_count": "1",
        "source_non_whitespace_count": str(source_non_whitespace_count),
        "covered_non_whitespace_count": str(source_non_whitespace_count),
        "source_coverage_sha256": aggregate_digest.hexdigest(),
        "parser_policy_sha256": _sha256_text("parser-policy"),
        "embedding_policy_sha256": _sha256_text("embedding-policy"),
        "retrieval_policy_sha256": _sha256_text("retrieval-policy"),
        "parser_profile_id.kb": "cardrag.issuer-profile.kb.v1",
        "parser_profile_sha256.kb": _sha256_text("parser-profile-kb"),
    }
    for node_type in (
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
    ):
        metadata[f"structure_node_count.{node_type}"] = str(
            sum(str(row[4]) == node_type for row in nodes)
        )
    for major_class in ("BENEFIT", "NOTICE", "MIXED", "UNKNOWN"):
        metadata[f"structure_major_class_count.{major_class}"] = str(
            sum(str(row[4]) == "MAJOR_SECTION" and str(row[5]) == major_class for row in nodes)
        )
    for view_type in (
        "TITLE",
        "RAW_ITEM",
        "CONTEXTUAL_ITEM",
        "DETAIL",
        "MAJOR_SECTION",
        "CONTRACT",
    ):
        metadata[f"embedding_view_count.{view_type}"] = str(
            sum(str(row[2]) == view_type for row in view_rows)
        )
    connection = sqlite3.connect(database)
    try:
        connection.executescript(V5_DDL)
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            sorted(metadata.items()),
        )
        connection.execute("INSERT INTO issuers VALUES('kb','KB국민카드',1)")
        connection.executemany(
            "INSERT INTO product_lineages VALUES(?,?,?,?,?)",
            (
                (alpha_lineage_id, "kb", "ALPHA", "product_description", "알파 카드"),
                (beta_lineage_id, "kb", "BETA", "product_description", "베타 카드"),
            ),
        )
        if include_dispositions:
            connection.execute(
                "INSERT INTO unsupported_products VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "kb",
                    "LOCKED",
                    "보호 카드",
                    "unsupported_drm",
                    unsupported_source_id,
                    "v1",
                    "https://public.example/locked.pdf",
                    "FASOO_DRMONE",
                    _sha256_text("protected-pdf"),
                    1234,
                    unsupported_source_json,
                ),
            )
            connection.execute(
                "INSERT INTO ocr_failed_products VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    failed_payload["issuer"],
                    failed_payload["product_code"],
                    failed_payload["product_name"],
                    failed_payload["document_id"],
                    failed_payload["title"],
                    failed_payload["pdf_sha256"],
                    failed_payload["pdf_size_bytes"],
                    failed_payload["page_count"],
                    failed_payload["reason_code"],
                    failed_payload["reason"],
                    failed_payload["attempts"],
                ),
            )
        for revision_id, lineage_id, document_id, effective_date, status, supersedes in revisions:
            pdf = pdf_by_revision[revision_id]
            connection.execute(
                "INSERT INTO contract_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    lineage_id,
                    document_id,
                    source_by_revision[revision_id],
                    effective_date,
                    f"https://public.example/{revision_id}.pdf",
                    effective_date,
                    _sha256_bytes(pdf),
                    len(pdf),
                    1,
                    status,
                    supersedes,
                ),
            )
        connection.executemany(
            "INSERT INTO document_pages VALUES(?,?,?,?)",
            (
                (revision_id, page, text, text_sha256)
                for revision_id in sorted(pages)
                for page, text, text_sha256 in pages[revision_id]
            ),
        )
        connection.executemany(
            "INSERT INTO structure_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ((*row, "[]", "[]", None) for row in nodes),
        )
        connection.executemany("INSERT INTO node_spans VALUES(?,?,?,?,?,?,?,?)", spans)
        connection.executemany("INSERT INTO node_links VALUES(?,?,?,?,?)", links)
        connection.execute(
            "INSERT INTO embedding_profiles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                profile_id,
                "openrouter",
                "qwen/qwen3-embedding-8b",
                "deepinfra",
                4096,
                "float32",
                "l2",
                "cardrag.structure-views.v1",
                "cardrag.qwen3-query.v1",
                8192,
            ),
        )
        for row_index, (node_id, revision_id, view_type, display_text, _score) in enumerate(
            view_rows
        ):
            connection.execute(
                "INSERT INTO embedding_views VALUES(?,?,?,?,?,?,?,?)",
                (
                    row_index + 1,
                    row_index,
                    node_id,
                    revision_id,
                    view_type,
                    _sha256_text(display_text),
                    profile_id,
                    display_text,
                ),
            )
            connection.execute(
                "INSERT INTO embedding_views_fts(row_index,node_id,display_text) VALUES(?,?,?)",
                (row_index, node_id, display_text),
            )
            source_start = page_text[revision_id].index(display_text)
            source_end = source_start + len(display_text)
            connection.execute(
                "INSERT INTO embedding_view_spans VALUES(?,?,?,?,?,?,?)",
                (
                    row_index,
                    revision_id,
                    1,
                    source_start,
                    source_end,
                    _sha256_text(display_text),
                    0,
                ),
            )
        connection.executemany(
            "INSERT INTO revision_coverage VALUES(?,?,?,?,?)",
            (
                (
                    revision_id,
                    _source_hash(revision_pages),
                    sum(
                        not character.isspace()
                        for _, text, _ in revision_pages
                        for character in text
                    ),
                    sum(
                        not character.isspace()
                        for _, text, _ in revision_pages
                        for character in text
                    ),
                    _coverage_hash(revision_pages),
                )
                for revision_id, revision_pages in sorted(pages.items())
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return V5Fixture(
        generation_id=generation_id,
        database=database,
        vectors=vectors,
        profile_id=profile_id,
        vector_count=len(view_rows),
        pdf_objects=tuple(
            (_sha256_bytes(body), body) for _, body in sorted(pdf_by_revision.items())
        ),
        current_revision_id=current_revision_id,
        old_revision_id=old_revision_id,
        ambiguous_revision_id=ambiguous_revision_id,
        lineage_id=alpha_lineage_id,
    )


def install_v5_fixture(
    store: GenerationStore,
    *,
    generation_id: str = "gen-v5-exact",
    include_dispositions: bool = False,
) -> tuple[V5Fixture, GenerationHandle]:
    fixture = build_v5_fixture(
        store.generations / generation_id,
        generation_id=generation_id,
        include_dispositions=include_dispositions,
    )
    for digest, body in fixture.pdf_objects:
        target = cas_path(store.objects, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    handle = load_generation_handle(
        fixture.database.parent,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
        maximum_vector_sidecar_bytes=store.maximum_vector_sidecar_bytes,
        maximum_resident_vector_bytes=store.maximum_resident_vector_bytes,
        expected_generation_id=generation_id,
        expected_embedding_model="qwen/qwen3-embedding-8b",
        expected_embedding_count=fixture.vector_count,
    )
    store.verify_handle_pdfs(handle)
    store.activate(handle)
    return fixture, handle
