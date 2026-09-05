from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import canonical_json_bytes, canonical_sha256
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

import cardrag_worker.exporter_v5 as exporter_module
from cardrag_worker.exporter_v5 import (
    SERVING_SCHEMA_ID_V5,
    VECTOR_ROW_BYTES,
    ContractRevisionInput,
    DocumentPageInput,
    EmbeddingProfileInput,
    EmbeddingViewInput,
    IssuerInput,
    LazyEmbeddingVector,
    NodeLinkInput,
    NodeSpanInput,
    OCRFailedProductInput,
    ProductLineageInput,
    ServingDatabaseExporterV5,
    ServingDatabaseV5Error,
    StructureNodeInput,
    UnsupportedProductInput,
    ViewSourceSpanInput,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit_vector() -> list[float]:
    return [1.0] + [0.0] * (QWEN3_EMBEDDING_DIMENSION - 1)


def _records() -> dict[str, Any]:
    revision_id = "revision_current"
    root_id = "node_root"
    paragraph_id = "node_paragraph"
    page_text = "혜택 안내\n전월 실적 제외\n"
    page_hash = _sha256(page_text)
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    issuer = IssuerInput(code="kb", display_name="KB국민카드", sort_order=1)
    lineage = ProductLineageInput(
        product_lineage_id="lineage_card",
        issuer="kb",
        product_code="CARD-001",
        document_type="product_description",
        name="테스트 카드",
    )
    revision = ContractRevisionInput(
        contract_revision_id=revision_id,
        product_lineage_id=lineage.product_lineage_id,
        document_id="doc_current",
        source_id="source_current",
        source_version="2026-01-01",
        source_url="https://public.example/card.pdf",
        effective_date="2026-01-01",
        pdf_sha256="a" * 64,
        pdf_size_bytes=1024,
        page_count=1,
        temporal_status="current",
    )
    page = DocumentPageInput(
        contract_revision_id=revision_id,
        page=1,
        text=page_text,
        text_sha256=page_hash,
    )
    root = StructureNodeInput(
        node_id=root_id,
        contract_revision_id=revision_id,
        parent_id=None,
        parent_contract_revision_id=None,
        node_type="ROOT",
        major_class="UNKNOWN",
        raw_heading=None,
        ordinal=0,
        display_text="",
    )
    paragraph = StructureNodeInput(
        node_id=paragraph_id,
        contract_revision_id=revision_id,
        parent_id=root_id,
        parent_contract_revision_id=revision_id,
        node_type="PARAGRAPH",
        major_class="BENEFIT",
        raw_heading=None,
        ordinal=1,
        display_text=page_text,
    )
    span = NodeSpanInput(
        node_id=paragraph_id,
        contract_revision_id=revision_id,
        page=1,
        source_start=0,
        source_end=len(page_text),
        text_sha256=page_hash,
        span_ordinal=0,
        is_canonical=True,
    )
    profile = EmbeddingProfileInput(
        profile_id=profile_id,
        provider=QWEN3_EMBEDDING_PROVIDER,
        model=QWEN3_EMBEDDING_MODEL,
        provider_id="deepinfra",
        dimension=QWEN3_EMBEDDING_DIMENSION,
        dtype=QWEN3_EMBEDDING_DTYPE,
        normalization=QWEN3_EMBEDDING_NORMALIZATION,
        document_policy=QWEN3_DOCUMENT_POLICY,
        query_policy=QWEN3_QUERY_POLICY,
        maximum_tokens=8192,
    )
    view = EmbeddingViewInput(
        row_index=0,
        node_id=paragraph_id,
        contract_revision_id=revision_id,
        view_type="DETAIL",
        embedding_input=page_text,
        input_sha256=page_hash,
        profile_id=profile_id,
        display_text=page_text,
        source_spans=(
            ViewSourceSpanInput(
                page=1,
                source_start=0,
                source_end=len(page_text),
                text_sha256=page_hash,
            ),
        ),
        vector=_unit_vector(),
    )
    return {
        "generation_id": "generation-v5-test",
        "corpus_sha256": "b" * 64,
        "contract_sha256": "c" * 64,
        "primary_embedding_profile_id": profile_id,
        "issuers": (issuer,),
        "product_lineages": (lineage,),
        "contract_revisions": (revision,),
        "document_pages": (page,),
        "structure_nodes": (root, paragraph),
        "node_spans": (span,),
        "node_links": (),
        "embedding_profiles": (profile,),
        "embedding_views": (view,),
    }


def _export(tmp_path: Path) -> tuple[Path, Path, Any]:
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    result = ServingDatabaseExporterV5().export(database, vectors, **_records())
    return database, vectors, result


def test_v5_export_writes_bound_database_and_little_endian_sidecar(tmp_path: Path) -> None:
    database, vectors, result = _export(tmp_path)
    assert result.database_path == database
    assert result.vector_path == vectors
    assert result.vector_size_bytes == VECTOR_ROW_BYTES
    assert result.vector_row_count == 1
    assert result.vector_dimension == 4096
    assert result.issuer_count == 1
    assert result.source_non_whitespace_count == result.covered_non_whitespace_count
    assert result.database_sha256 == hashlib.sha256(database.read_bytes()).hexdigest()
    assert result.vector_sha256 == hashlib.sha256(vectors.read_bytes()).hexdigest()
    assert vectors.read_bytes()[:8] == struct.pack("<2f", 1.0, 0.0)
    unpacked = struct.unpack(f"<{QWEN3_EMBEDDING_DIMENSION}f", vectors.read_bytes())
    assert math.isclose(sum(value * value for value in unpacked), 1.0)
    assert not tuple(tmp_path.glob(".*.build"))
    assert not tuple(tmp_path.glob(".*.vacuum"))

    connection = sqlite3.connect(database)
    try:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert metadata["schema_id"] == SERVING_SCHEMA_ID_V5
        assert metadata["embedding_dimension"] == "4096"
        assert metadata["embedding_count"] == "1"
        assert metadata["vector_sidecar_sha256"] == result.vector_sha256
        assert metadata["vector_sidecar_size_bytes"] == str(VECTOR_ROW_BYTES)
        assert metadata["vector_sidecar_byte_order"] == "little-endian"
        assert metadata["vector_sidecar_layout"] == "row-major"
        assert metadata["embedding_view_span_count"] == "1"
        assert metadata["source_non_whitespace_count"] == metadata["covered_non_whitespace_count"]
        assert metadata["unsupported_document_count"] == "0"
        assert metadata["ocr_failed_document_count"] == "0"
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(embedding_views)")}
        assert columns == {
            "view_pk",
            "row_index",
            "node_id",
            "contract_revision_id",
            "view_type",
            "input_sha256",
            "profile_id",
            "display_text",
        }
        assert not {"embedding", "vector", "vector_json"}.intersection(columns)
        assert connection.execute("SELECT view_pk,row_index FROM embedding_views").fetchone() == (1, 0)
        assert connection.execute(
            """SELECT row_index,contract_revision_id,page,source_start,source_end,
                      text_sha256,span_ordinal FROM embedding_view_spans"""
        ).fetchone() == (
            0,
            "revision_current",
            1,
            0,
            len("혜택 안내\n전월 실적 제외\n"),
            _sha256("혜택 안내\n전월 실적 제외\n"),
            0,
        )
        assert connection.execute(
            "SELECT row_index FROM embedding_views_fts WHERE embedding_views_fts MATCH '혜택'"
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v5_export_accepts_already_encoded_rows_without_corpus_float_expansion(
    tmp_path: Path,
) -> None:
    records = _records()
    original = records["embedding_views"][0]
    encoded = struct.pack("<4096f", 1.0, *([0.0] * 4095))
    records["embedding_views"] = (replace(original, vector=encoded),)

    vectors = tmp_path / "vectors.f32"
    ServingDatabaseExporterV5().export(
        tmp_path / "index.sqlite3",
        vectors,
        **records,
    )

    assert vectors.read_bytes() == encoded


def test_v5_export_rejects_exact_vector_cap_before_creating_targets(tmp_path: Path) -> None:
    target_root = tmp_path / "not-created"

    with pytest.raises(ServingDatabaseV5Error, match="vector sidecar exceeds"):
        ServingDatabaseExporterV5().export(
            target_root / "index.sqlite3",
            target_root / "vectors.f32",
            maximum_vector_sidecar_bytes=VECTOR_ROW_BYTES - 1,
            **_records(),
        )

    assert not target_root.exists()


def test_v5_export_rejects_working_database_cap_and_cleans_temporary_files(
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"

    with pytest.raises(ServingDatabaseV5Error, match="working serving database"):
        ServingDatabaseExporterV5().export(
            database,
            vectors,
            maximum_serving_database_bytes=4096,
            reserved_free_space_bytes=0,
            **_records(),
        )

    assert not database.exists()
    assert not vectors.exists()
    assert not tuple(tmp_path.glob(".index.sqlite3.*"))
    assert not tuple(tmp_path.glob(".vectors.f32.*"))


def test_v5_export_rejects_vacuum_free_shortfall_without_replacing_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    maximum = 100 * 1024 * 1024
    reserve = 17
    monkeypatch.setattr(
        exporter_module,
        "_directory_free_bytes",
        lambda _path: maximum + reserve - 1,
    )

    with pytest.raises(ServingDatabaseV5Error, match="reserved free space"):
        ServingDatabaseExporterV5().export(
            database,
            vectors,
            maximum_serving_database_bytes=maximum,
            reserved_free_space_bytes=reserve,
            **_records(),
        )

    assert not database.exists()
    assert not vectors.exists()
    assert not tuple(tmp_path.glob(".index.sqlite3.*"))
    assert not tuple(tmp_path.glob(".vectors.f32.*"))


def test_v5_export_rejects_actual_database_above_prediction_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = ServingDatabaseExporterV5().export(
        tmp_path / "baseline.sqlite3",
        tmp_path / "baseline.f32",
        **_records(),
    )
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    monkeypatch.setattr(exporter_module, "_directory_free_bytes", lambda _path: 1 << 40)

    with pytest.raises(ServingDatabaseV5Error, match="preflight prediction"):
        ServingDatabaseExporterV5().export(
            database,
            vectors,
            predicted_serving_database_bytes=baseline.database_size_bytes - 1,
            maximum_serving_database_bytes=baseline.database_size_bytes * 2,
            maximum_vector_sidecar_bytes=VECTOR_ROW_BYTES,
            reserved_free_space_bytes=0,
            **_records(),
        )

    assert not database.exists()
    assert not vectors.exists()
    assert not tuple(tmp_path.glob(".index.sqlite3.*"))
    assert not tuple(tmp_path.glob(".vectors.f32.*"))


def test_v5_export_removes_installed_sidecar_when_database_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    original_replace = exporter_module.os.replace

    def fail_database_replace(source: Path, target: Path) -> None:
        if Path(target) == database:
            raise OSError("injected database replace failure")
        original_replace(source, target)

    monkeypatch.setattr(exporter_module.os, "replace", fail_database_replace)

    with pytest.raises(OSError, match="injected database replace failure"):
        ServingDatabaseExporterV5().export(database, vectors, **_records())

    assert not database.exists()
    assert not vectors.exists()
    assert not tuple(tmp_path.glob(".index.sqlite3.*"))
    assert not tuple(tmp_path.glob(".vectors.f32.*"))


@pytest.mark.parametrize(
    ("artifact", "append_size"),
    (("vector", VECTOR_ROW_BYTES), ("database", 4096)),
)
def test_v5_export_rejects_temp_artifact_mutated_inside_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
    append_size: int,
) -> None:
    records = _records()
    baseline = ServingDatabaseExporterV5().export(
        tmp_path / "baseline.sqlite3",
        tmp_path / "baseline.f32",
        **records,
    )
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    mutated_target = vectors if artifact == "vector" else database
    original_replace = exporter_module.os.replace
    mutated = False

    def mutate_source_then_replace(source: Path, target: Path) -> None:
        nonlocal mutated
        if Path(target) == mutated_target and not mutated:
            with Path(source).open("ab") as stream:
                stream.write(b"\0" * append_size)
            mutated = True
        original_replace(source, target)

    monkeypatch.setattr(exporter_module.os, "replace", mutate_source_then_replace)

    with pytest.raises(ServingDatabaseV5Error, match="changed from its sealed artifact"):
        ServingDatabaseExporterV5().export(
            database,
            vectors,
            maximum_serving_database_bytes=baseline.database_size_bytes,
            maximum_vector_sidecar_bytes=baseline.vector_size_bytes,
            **records,
        )

    assert mutated
    assert not database.exists()
    assert not vectors.exists()
    assert not tuple(tmp_path.glob(".index.sqlite3.*"))
    assert not tuple(tmp_path.glob(".vectors.f32.*"))


@pytest.mark.parametrize(
    ("field", "append_size"),
    (("vector sidecar", VECTOR_ROW_BYTES), ("serving database", 4096)),
)
def test_v5_export_preserves_owned_previous_targets_when_temp_changes_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    append_size: int,
) -> None:
    records = _records()
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    baseline = ServingDatabaseExporterV5().export(database, vectors, **records)
    database_before = database.read_bytes()
    vectors_before = vectors.read_bytes()
    original_revalidate = exporter_module._revalidate_sealed_artifact
    mutated = False

    def mutate_before_revalidation(
        path: Path,
        sealed: Any,
        *,
        field: str,
        maximum_size_bytes: int | None = None,
    ) -> None:
        nonlocal mutated
        if field == test_field and not mutated:
            with Path(path).open("ab") as stream:
                stream.write(b"\0" * append_size)
            mutated = True
        original_revalidate(
            path,
            sealed,
            field=field,
            maximum_size_bytes=maximum_size_bytes,
        )

    test_field = field
    monkeypatch.setattr(exporter_module, "_revalidate_sealed_artifact", mutate_before_revalidation)

    with pytest.raises(ServingDatabaseV5Error, match="changed from its sealed artifact"):
        ServingDatabaseExporterV5().export(
            database,
            vectors,
            maximum_serving_database_bytes=baseline.database_size_bytes,
            maximum_vector_sidecar_bytes=baseline.vector_size_bytes,
            replace_incomplete_owned_targets=True,
            **records,
        )

    assert mutated
    assert database.read_bytes() == database_before
    assert vectors.read_bytes() == vectors_before
    assert not tuple(tmp_path.glob(".index.sqlite3.*"))
    assert not tuple(tmp_path.glob(".vectors.f32.*"))


def test_v5_export_rejects_same_size_database_mutation_after_final_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    original_verify = exporter_module._verify_database
    mutated = False

    def mutate_after_verify(connection: sqlite3.Connection, **kwargs: Any) -> None:
        nonlocal mutated
        original_verify(connection, **kwargs)
        if kwargs.get("run_fts_integrity_check", False) or mutated:
            return
        database_row = connection.execute("PRAGMA database_list").fetchone()
        assert database_row is not None
        vacuumed = Path(str(database_row[2]))
        size_before = vacuumed.stat().st_size
        with vacuumed.open("r+b") as stream:
            stream.seek(-150, 2)
            original_byte = stream.read(1)
            assert len(original_byte) == 1
            stream.seek(-1, 1)
            stream.write(bytes((original_byte[0] ^ 1,)))
        assert vacuumed.stat().st_size == size_before
        mutated = True

    monkeypatch.setattr(exporter_module, "_verify_database", mutate_after_verify)

    with pytest.raises(ServingDatabaseV5Error, match="serving database changed from its sealed artifact"):
        ServingDatabaseExporterV5().export(database, vectors, **_records())

    assert mutated
    assert not database.exists()
    assert not vectors.exists()
    assert not tuple(tmp_path.glob(".index.sqlite3.*"))
    assert not tuple(tmp_path.glob(".vectors.f32.*"))


def test_v5_export_owned_incomplete_targets_are_overwritten_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    original_replace = exporter_module.os.replace
    failed_once = False

    def fail_first_database_replace(source: Path, target: Path) -> None:
        nonlocal failed_once
        if Path(target) == database and not failed_once:
            failed_once = True
            raise OSError("injected process-death window")
        original_replace(source, target)

    monkeypatch.setattr(exporter_module.os, "replace", fail_first_database_replace)

    with pytest.raises(OSError, match="process-death window"):
        ServingDatabaseExporterV5().export(
            database,
            vectors,
            replace_incomplete_owned_targets=True,
            **_records(),
        )

    assert not database.exists()
    assert vectors.is_file()
    assert not tuple(tmp_path.glob(".index.sqlite3.*"))
    assert not tuple(tmp_path.glob(".vectors.f32.*"))

    resumed = ServingDatabaseExporterV5().export(
        database,
        vectors,
        replace_incomplete_owned_targets=True,
        **_records(),
    )

    assert resumed.database_path == database
    assert resumed.vector_path == vectors
    assert database.is_file()
    assert vectors.is_file()

    rerun = ServingDatabaseExporterV5().export(
        database,
        vectors,
        replace_incomplete_owned_targets=True,
        **_records(),
    )
    assert rerun.database_sha256 == resumed.database_sha256
    assert rerun.vector_sha256 == resumed.vector_sha256


def test_v5_export_rejects_product_lineage_without_revision(tmp_path: Path) -> None:
    records = _records()
    records["product_lineages"] = (
        *records["product_lineages"],
        ProductLineageInput(
            product_lineage_id="lineage_orphan",
            issuer="kb",
            product_code="orphan-card",
            document_type="product_description",
            name="Orphan card",
        ),
    )

    with pytest.raises(ServingDatabaseV5Error, match="every product lineage"):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )

    assert not tuple(tmp_path.iterdir())


def test_v5_export_streams_lazy_rows_without_retaining_the_previous_load(tmp_path: Path) -> None:
    records = _records()
    original = records["embedding_views"][0]
    encoded = struct.pack("<4096f", 1.0, *([0.0] * 4095))
    tracker = {"live": 0, "maximum": 0}
    calls: list[int] = []

    class TrackedRow(bytes):
        def __new__(cls, payload: bytes) -> TrackedRow:
            instance = super().__new__(cls, payload)
            tracker["live"] += 1
            tracker["maximum"] = max(tracker["maximum"], tracker["live"])
            return instance

        def __del__(self) -> None:
            tracker["live"] -= 1

    def lazy_vector(row_index: int) -> LazyEmbeddingVector:
        def load() -> bytes:
            calls.append(row_index)
            return TrackedRow(encoded)

        return LazyEmbeddingVector(
            cache_identity=hashlib.sha256(f"cache-{row_index}".encode()).hexdigest(),
            profile_id=original.profile_id,
            input_sha256=original.input_sha256,
            expected_vector_sha256=hashlib.sha256(encoded).hexdigest(),
            loader=load,
        )

    records["embedding_views"] = (
        replace(original, row_index=0, vector=lazy_vector(0)),
        replace(original, row_index=1, view_type="RAW_ITEM", vector=lazy_vector(1)),
    )
    vectors = tmp_path / "vectors.f32"

    ServingDatabaseExporterV5().export(
        tmp_path / "index.sqlite3",
        vectors,
        **records,
    )

    # Each row is independently proved before artifact creation and again as
    # it is streamed into the sidecar.
    assert calls == [0, 1, 0, 1]
    assert tracker == {"live": 0, "maximum": 1}
    assert vectors.read_bytes() == encoded + encoded


@pytest.mark.parametrize(
    "encoded,match",
    [
        (b"\0" * (VECTOR_ROW_BYTES - 4), "byte length"),
        (struct.pack("<4096f", *([0.0] * 4096)), "not L2 normalized"),
    ],
)
def test_v5_export_rejects_invalid_lazy_rows(
    tmp_path: Path,
    encoded: bytes,
    match: str,
) -> None:
    records = _records()
    original = records["embedding_views"][0]
    records["embedding_views"] = (
        replace(
            original,
            vector=LazyEmbeddingVector(
                cache_identity="d" * 64,
                profile_id=original.profile_id,
                input_sha256=original.input_sha256,
                expected_vector_sha256=hashlib.sha256(encoded).hexdigest(),
                loader=lambda: encoded,
            ),
        ),
    )

    with pytest.raises(ServingDatabaseV5Error, match=match):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )


@pytest.mark.parametrize("failure", ("cache row missing", "cache row tampered"))
def test_v5_export_propagates_lazy_cache_failures_during_sidecar_write(
    tmp_path: Path,
    failure: str,
) -> None:
    records = _records()
    original = records["embedding_views"][0]
    encoded = struct.pack("<4096f", 1.0, *([0.0] * 4095))
    calls = 0

    def load() -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError(failure)
        return encoded

    records["embedding_views"] = (
        replace(
            original,
            vector=LazyEmbeddingVector(
                cache_identity="d" * 64,
                profile_id=original.profile_id,
                input_sha256=original.input_sha256,
                expected_vector_sha256=hashlib.sha256(encoded).hexdigest(),
                loader=load,
            ),
        ),
    )

    with pytest.raises(RuntimeError, match=failure):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )

    assert calls == 2
    assert not (tmp_path / "index.sqlite3").exists()
    assert not (tmp_path / "vectors.f32").exists()
    assert not tuple(tmp_path.glob(".*.build"))


def test_v5_export_rejects_valid_vector_bytes_changed_between_lazy_loads(
    tmp_path: Path,
) -> None:
    records = _records()
    original = records["embedding_views"][0]
    first = struct.pack("<4096f", 1.0, *([0.0] * 4095))
    second = struct.pack("<4096f", 0.0, 1.0, *([0.0] * 4094))
    calls = 0

    def load() -> bytes:
        nonlocal calls
        calls += 1
        return first if calls == 1 else second

    records["embedding_views"] = (
        replace(
            original,
            vector=LazyEmbeddingVector(
                cache_identity="d" * 64,
                profile_id=original.profile_id,
                input_sha256=original.input_sha256,
                expected_vector_sha256=hashlib.sha256(first).hexdigest(),
                loader=load,
            ),
        ),
    )

    with pytest.raises(ServingDatabaseV5Error, match="changed after"):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )

    assert calls == 2
    assert not (tmp_path / "index.sqlite3").exists()
    assert not (tmp_path / "vectors.f32").exists()


def test_v5_export_validates_lazy_cache_binding_before_invoking_loader(tmp_path: Path) -> None:
    records = _records()
    original = records["embedding_views"][0]
    calls = 0

    def must_not_load() -> bytes:
        nonlocal calls
        calls += 1
        return struct.pack("<4096f", 1.0, *([0.0] * 4095))

    records["embedding_views"] = (
        replace(
            original,
            vector=LazyEmbeddingVector(
                cache_identity="d" * 64,
                profile_id=original.profile_id,
                input_sha256="e" * 64,
                expected_vector_sha256="f" * 64,
                loader=must_not_load,
            ),
        ),
    )

    with pytest.raises(ServingDatabaseV5Error, match="input_sha256"):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )
    assert calls == 0


@pytest.mark.parametrize(
    "encoded,match",
    [
        (b"\0" * (VECTOR_ROW_BYTES - 4), "byte length"),
        (struct.pack("<4096f", *([0.0] * 4096)), "not L2 normalized"),
    ],
)
def test_v5_export_rejects_invalid_encoded_rows(
    tmp_path: Path,
    encoded: bytes,
    match: str,
) -> None:
    records = _records()
    original = records["embedding_views"][0]
    records["embedding_views"] = (replace(original, vector=encoded),)

    with pytest.raises(ServingDatabaseV5Error, match=match):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )


@pytest.mark.parametrize("invalid", (True, "1.0", object()))
def test_v5_export_rejects_non_real_or_boolean_vector_values(
    tmp_path: Path,
    invalid: object,
) -> None:
    records = _records()
    original = records["embedding_views"][0]
    vector: list[object] = [1.0] + [0.0] * (QWEN3_EMBEDDING_DIMENSION - 1)
    vector[0] = invalid
    records["embedding_views"] = (replace(original, vector=vector),)

    with pytest.raises(ServingDatabaseV5Error, match="non-real or boolean"):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )


def test_v5_export_is_byte_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    first_db = tmp_path / "first.sqlite3"
    first_vectors = tmp_path / "first.f32"
    second_db = tmp_path / "second.sqlite3"
    second_vectors = tmp_path / "second.f32"
    first = ServingDatabaseExporterV5().export(first_db, first_vectors, **_records())
    second = ServingDatabaseExporterV5().export(second_db, second_vectors, **_records())
    assert first.database_sha256 == second.database_sha256
    assert first.vector_sha256 == second.vector_sha256


def test_v5_export_seals_document_aggregation_metadata_and_rejects_injection(
    tmp_path: Path,
) -> None:
    records = _records()
    child = records["embedding_views"][0]
    records["embedding_views"] = (
        child,
        replace(
            child,
            row_index=1,
            view_type="CONTRACT",
            embedding_input="전체 계약 요약",
            input_sha256=_sha256("전체 계약 요약"),
        ),
    )
    unsealed = ServingDatabaseExporterV5().export(
        tmp_path / "unsealed.sqlite3",
        tmp_path / "unsealed.f32",
        **records,
    )
    selected = ServingDatabaseExporterV5().export(
        tmp_path / "selected.sqlite3",
        tmp_path / "selected.f32",
        **records,
        document_aggregation_policy="top3_mean",
        sealed_profile_sha256="d" * 64,
        expected_exact_row_corpus_sha256=unsealed.exact_row_corpus_sha256,
    )

    assert selected.exact_row_corpus_sha256 == unsealed.exact_row_corpus_sha256
    with sqlite3.connect(selected.database_path) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    assert metadata["document_aggregation_status"] == "sealed"
    assert metadata["document_aggregation_policy"] == "top3_mean"
    assert metadata["sealed_profile_sha256"] == "d" * 64
    assert metadata["exact_row_corpus_sha256"] == selected.exact_row_corpus_sha256

    with pytest.raises(ServingDatabaseV5Error, match="reserved metadata"):
        ServingDatabaseExporterV5().export(
            tmp_path / "injected.sqlite3",
            tmp_path / "injected.f32",
            **records,
            extra_metadata={"document_aggregation_policy": "contract_plus_child"},
        )


def test_v5_export_preserves_table_header_cell_and_role_contract(tmp_path: Path) -> None:
    records = _records()
    root, paragraph = records["structure_nodes"]
    table_id = "node_table"
    row_id = "node_table_row"
    headers = ("혜택", "조건")
    table = replace(
        paragraph,
        node_id=table_id,
        node_type="TABLE",
        display_text="",
        table_headers=headers,
    )
    row = replace(
        paragraph,
        node_id=row_id,
        parent_id=table_id,
        node_type="TABLE_ROW",
        ordinal=2,
        table_headers=headers,
        table_cells=("공항 라운지", "전월 실적 제외"),
        table_role="BODY",
    )
    records["structure_nodes"] = (root, table, row)
    records["node_spans"] = (replace(records["node_spans"][0], node_id=row_id),)
    records["embedding_views"] = (replace(records["embedding_views"][0], node_id=row_id),)
    database = tmp_path / "index.sqlite3"
    ServingDatabaseExporterV5().export(
        database,
        tmp_path / "vectors.f32",
        **records,
    )

    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            """SELECT table_headers_json,table_cells_json,table_role
                 FROM structure_nodes WHERE node_id=?""",
            (row_id,),
        ).fetchone()
    assert stored == (
        json.dumps(headers, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        json.dumps(
            ("공항 라운지", "전월 실적 제외"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "BODY",
    )


def test_v5_database_constraints_reject_cross_contract_parent_and_link(tmp_path: Path) -> None:
    database, _vectors, _result = _export(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE structure_nodes SET parent_contract_revision_id='revision_other'
                   WHERE node_id='node_paragraph'"""
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO node_links
                   (from_node_id,from_contract_revision_id,to_node_id,to_contract_revision_id,
                    link_type)
                   VALUES('node_root','revision_current','node_paragraph','revision_other',
                          'APPLIES_TO')"""
            )
    finally:
        connection.close()


def test_v5_export_rejects_cross_contract_parent_before_writing(tmp_path: Path) -> None:
    records = _records()
    root, paragraph = records["structure_nodes"]
    records["structure_nodes"] = (
        root,
        replace(paragraph, parent_contract_revision_id="revision_other"),
    )
    with pytest.raises(ServingDatabaseV5Error, match="crosses a contract"):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )
    assert not (tmp_path / "index.sqlite3").exists()
    assert not (tmp_path / "vectors.f32").exists()


def test_v5_export_rejects_cross_contract_link_before_writing(tmp_path: Path) -> None:
    records = _records()
    records["node_links"] = (
        NodeLinkInput(
            from_node_id="node_root",
            from_contract_revision_id="revision_current",
            to_node_id="node_paragraph",
            to_contract_revision_id="revision_other",
            link_type="APPLIES_TO",
            ordinal=0,
        ),
    )
    with pytest.raises(ServingDatabaseV5Error, match="crosses a contract"):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )


def test_v5_export_rejects_source_coverage_gap(tmp_path: Path) -> None:
    records = _records()
    page = records["document_pages"][0]
    root, paragraph = records["structure_nodes"]
    shortened = page.text[:-1]
    records["structure_nodes"] = (root, replace(paragraph, display_text=shortened))
    records["node_spans"] = (
        replace(
            records["node_spans"][0],
            source_end=len(shortened),
            text_sha256=_sha256(shortened),
        ),
    )
    with pytest.raises(ServingDatabaseV5Error, match="do not reconstruct"):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda view: replace(view, row_index=1), "row_index"),
        (lambda view: replace(view, input_sha256="d" * 64), "input_sha256"),
        (lambda view: replace(view, vector=[2.0] + [0.0] * 4095), "normalized"),
    ),
)
def test_v5_export_rejects_unsealed_embedding_rows(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    records = _records()
    records["embedding_views"] = (mutation(records["embedding_views"][0]),)
    with pytest.raises(ServingDatabaseV5Error, match=message):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )


def test_v5_export_rejects_multiple_current_revisions_per_lineage(tmp_path: Path) -> None:
    records = _records()
    revision = records["contract_revisions"][0]
    records["contract_revisions"] = (
        revision,
        replace(
            revision,
            contract_revision_id="revision_duplicate_current",
            document_id="doc_duplicate_current",
            source_id="source_duplicate_current",
        ),
    )
    with pytest.raises(ServingDatabaseV5Error, match="multiple current"):
        ServingDatabaseExporterV5().export(
            tmp_path / "index.sqlite3",
            tmp_path / "vectors.f32",
            **records,
        )


def test_v5_export_preserves_current_and_superseded_revision_chain(tmp_path: Path) -> None:
    records = _records()
    current_revision = records["contract_revisions"][0]
    current_page = records["document_pages"][0]
    current_root, current_paragraph = records["structure_nodes"]
    current_span = records["node_spans"][0]
    current_view = records["embedding_views"][0]
    old_revision_id = "revision_superseded"
    old_root_id = "node_old_root"
    old_paragraph_id = "node_old_paragraph"
    old_revision = replace(
        current_revision,
        contract_revision_id=old_revision_id,
        document_id="doc_superseded",
        source_id="source_superseded",
        source_version="2025-01-01",
        effective_date="2025-01-01",
        temporal_status="superseded",
        supersedes_revision_id=None,
    )
    records["contract_revisions"] = (
        old_revision,
        replace(current_revision, supersedes_revision_id=old_revision_id),
    )
    records["document_pages"] = (
        replace(current_page, contract_revision_id=old_revision_id),
        current_page,
    )
    old_root = replace(
        current_root,
        node_id=old_root_id,
        contract_revision_id=old_revision_id,
    )
    old_paragraph = replace(
        current_paragraph,
        node_id=old_paragraph_id,
        contract_revision_id=old_revision_id,
        parent_id=old_root_id,
        parent_contract_revision_id=old_revision_id,
    )
    records["structure_nodes"] = (old_root, old_paragraph, current_root, current_paragraph)
    records["node_spans"] = (
        replace(
            current_span,
            node_id=old_paragraph_id,
            contract_revision_id=old_revision_id,
        ),
        current_span,
    )
    records["embedding_views"] = (
        replace(
            current_view,
            row_index=0,
            node_id=old_paragraph_id,
            contract_revision_id=old_revision_id,
        ),
        replace(current_view, row_index=1),
    )
    result = ServingDatabaseExporterV5().export(
        tmp_path / "index.sqlite3",
        tmp_path / "vectors.f32",
        **records,
    )
    assert (result.contract_revision_count, result.current_revision_count) == (2, 1)
    assert (result.superseded_revision_count, result.ambiguous_revision_count) == (1, 0)
    assert result.vector_row_count == 2
    connection = sqlite3.connect(tmp_path / "index.sqlite3")
    try:
        assert connection.execute(
            """SELECT supersedes_revision_id FROM contract_revisions
               WHERE temporal_status='current'"""
        ).fetchone() == (old_revision_id,)
    finally:
        connection.close()


def test_v5_coverage_hash_is_bound_to_revision_source(tmp_path: Path) -> None:
    database, _vectors, result = _export(tmp_path)
    connection = sqlite3.connect(database)
    try:
        revision_coverage = connection.execute(
            """SELECT contract_revision_id,source_sha256,source_non_whitespace_count,
                      covered_non_whitespace_count,coverage_sha256
               FROM revision_coverage"""
        ).fetchone()
        assert revision_coverage[2] == revision_coverage[3] == result.source_non_whitespace_count
        assert len(revision_coverage[1]) == len(revision_coverage[4]) == 64
        page_text = connection.execute("SELECT text FROM document_pages").fetchone()[0]
        aggregate = hashlib.sha256(
            "".join(character for character in page_text if not character.isspace()).encode("utf-8")
        ).hexdigest()
        assert aggregate == result.source_coverage_sha256
        assert (
            dict(connection.execute("SELECT key,value FROM metadata"))["source_coverage_sha256"] == aggregate
        )
    finally:
        connection.close()


def test_v5_export_preserves_nonzero_bounded_dispositions_and_hashes(tmp_path: Path) -> None:
    records = _records()
    source_payload = {
        "issuer": "kb",
        "product_code": "CARD-DRM",
        "product_name": "보호 문서 카드",
        "source_url": "https://public.example/protected.pdf",
        "source_version": "2026-08-01",
    }
    source_payload_json = json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    unsupported = UnsupportedProductInput(
        issuer="kb",
        product_code="CARD-DRM",
        name="보호 문서 카드",
        disposition="unsupported_drm",
        source_id="source_" + canonical_sha256(source_payload),
        source_version="2026-08-01",
        source_url="https://public.example/protected.pdf",
        protected_magic="SCDSA002",
        protected_sha256="d" * 64,
        protected_size_bytes=2048,
        source_payload_json=source_payload_json,
    )
    second_source_payload = {
        **source_payload,
        "product_code": "A-CARD-DRM",
        "product_name": "두 번째 보호 문서 카드",
        "source_url": "https://public.example/second-protected.pdf",
    }
    second_source_payload_json = json.dumps(
        second_source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    second_unsupported = UnsupportedProductInput(
        issuer="kb",
        product_code="A-CARD-DRM",
        name="두 번째 보호 문서 카드",
        disposition="unsupported_drm",
        source_id="source_" + canonical_sha256(second_source_payload),
        source_version="2026-08-01",
        source_url="https://public.example/second-protected.pdf",
        protected_magic="SCDSA004",
        protected_sha256="c" * 64,
        protected_size_bytes=1024,
        source_payload_json=second_source_payload_json,
    )
    failed = OCRFailedProductInput(
        issuer="kb",
        product_code="CARD-OCR-FAILED",
        name="OCR 실패 카드",
        document_id="doc_" + "e" * 64,
        title="OCR 실패 카드",
        pdf_sha256="f" * 64,
        pdf_size_bytes=4096,
        page_count=2,
        reason_code="provider_document_rejected",
        reason="The OCR provider could not process this document.",
        attempts=3,
    )
    records["unsupported_products"] = (unsupported, second_unsupported)
    records["ocr_failed_products"] = (failed,)

    database = tmp_path / "index.sqlite3"
    result = ServingDatabaseExporterV5().export(
        database,
        tmp_path / "vectors.f32",
        **records,
    )
    assert (result.unsupported_product_count, result.ocr_failed_product_count) == (2, 1)
    with sqlite3.connect(database) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert metadata["unsupported_document_count"] == "2"
        assert metadata["ocr_failed_document_count"] == "1"
        unsupported_payloads = [
            {
                "disposition": row.disposition,
                "protected_magic": row.protected_magic,
                "protected_sha256": row.protected_sha256,
                "protected_size_bytes": row.protected_size_bytes,
                "source": json.loads(row.source_payload_json),
                "source_id": row.source_id,
            }
            for row in (second_unsupported, unsupported)
        ]
        worker_table_order_sha256 = canonical_sha256(
            {
                "schema_version": "cardrag.unsupported-documents.v1",
                "documents": unsupported_payloads,
            }
        )
        canonical_order_sha256 = canonical_sha256(
            {
                "schema_version": "cardrag.unsupported-documents.v1",
                "documents": sorted(unsupported_payloads, key=canonical_json_bytes),
            }
        )
        assert worker_table_order_sha256 != canonical_order_sha256
        assert metadata["unsupported_documents_sha256"] == canonical_order_sha256
        assert len(metadata["ocr_failed_documents_sha256"]) == 64
        assert connection.execute(
            "SELECT issuer,product_code,disposition FROM unsupported_products ORDER BY issuer,product_code"
        ).fetchall() == [
            ("kb", "A-CARD-DRM", "unsupported_drm"),
            ("kb", "CARD-DRM", "unsupported_drm"),
        ]
        assert connection.execute(
            "SELECT issuer,product_code,reason_code,attempts FROM ocr_failed_products"
        ).fetchone() == ("kb", "CARD-OCR-FAILED", "provider_document_rejected", 3)


def test_v5_verify_database_optimized_detection(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    exporter = ServingDatabaseExporterV5()
    records = _records()
    exporter.export(database, vectors, **records)

    with sqlite3.connect(database) as conn:
        metadata = dict(conn.execute("SELECT key,value FROM metadata"))
        expected_counts = {
            "issuers": len(records.get("issuers", ())),
            "product_lineages": len(records.get("product_lineages", ())),
            "unsupported_products": len(records.get("unsupported_products", ())),
            "ocr_failed_products": len(records.get("ocr_failed_products", ())),
            "contract_revisions": len(records.get("contract_revisions", ())),
            "document_pages": len(records.get("document_pages", ())),
            "structure_nodes": len(records.get("structure_nodes", ())),
            "node_spans": len(records.get("node_spans", ())),
            "node_links": len(records.get("node_links", ())),
            "embedding_profiles": len(records.get("embedding_profiles", ())),
            "embedding_views": len(records.get("embedding_views", ())),
            "embedding_view_spans": sum(len(v.source_spans) for v in records.get("embedding_views", ())),
            "embedding_views_fts": len(records.get("embedding_views", ())),
            "revision_coverage": len(records.get("contract_revisions", ())),
        }

        # 1. Full verify passes
        exporter_module._verify_database(  # noqa: SLF001
            conn,
            expected_metadata=metadata,
            expected_counts=expected_counts,
            run_fts_integrity_check=True,
            is_vacuumed_verify=False,
        )

        # 2. Fast vacuumed verify passes
        exporter_module._verify_database(  # noqa: SLF001
            conn,
            expected_metadata=metadata,
            expected_counts=expected_counts,
            is_vacuumed_verify=True,
        )

        # 3. Gap detection in canonical spans
        conn.execute("SAVEPOINT test_sp")
        # Shrink canonical span source_end to create a gap
        conn.execute(
            """UPDATE node_spans
               SET source_end = source_start + 1
               WHERE is_canonical = 1"""
        )
        with pytest.raises(
            ServingDatabaseV5Error,
            match="stored node span hash does not match|stored node display_text differs|reconstruct the OCR source",
        ):
            exporter_module._verify_database(  # noqa: SLF001
                conn,
                expected_metadata=metadata,
                expected_counts=expected_counts,
                is_vacuumed_verify=False,
            )
        conn.execute("ROLLBACK TO test_sp")

