from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import cardrag_worker.capacity_v5 as capacity_module
from cardrag_worker.capacity_v5 import (
    BASE_DATABASE_METADATA_ROWS,
    DATABASE_EXPORT_PEAK_MULTIPLIER,
    DATABASE_FTS_INDEXED_TEXT_MULTIPLIER,
    DATABASE_ROW_ENVELOPE_BYTES,
    DEFAULT_MAX_SERVING_DATABASE_BYTES,
    DEFAULT_MAX_STATE_BYTES,
    DEFAULT_MINIMUM_START_FREE_BYTES,
    DEFAULT_RESERVED_FREE_SPACE_BYTES,
    EMBEDDING_CACHE_ROW_ENVELOPE_BYTES,
    EMBEDDING_CACHE_WAL_PEAK_BYTES,
    EMBEDDING_CACHE_WAL_ROW_ENVELOPE_BYTES,
    MAX_SAFE_BYTES,
    VECTOR_ROW_BYTES,
    V5CapacityError,
    V5CapacityPolicy,
    build_v5_database_ledger,
    predict_serving_database_bytes,
    predict_v5_local_artifacts,
    preflight_v5_capacity,
    preflight_v5_remaining_free_capacity,
    preflight_worker_start_capacity,
    revalidate_worker_start_capacity,
    safe_state_usage,
)
from cardrag_worker.exporter_v5 import (
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
from cardrag_worker.state import WorkerState
from cardrag_worker.structure import DerivedView, NodeSpan


def _prediction(**overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "derived_view_count": 2,
        "database_payload_bytes": 1_000,
        "database_row_count": 3,
        "embedding_cache_miss_count": 1,
    }
    arguments.update(overrides)
    return predict_v5_local_artifacts(**arguments)


def _database_ledger_inputs() -> dict[str, Any]:
    revision_id = "revision_" + "1" * 64
    lineage_id = "lineage_" + "2" * 64
    node_id = "node_" + "3" * 64
    source_id = "source_" + "4" * 64
    text_sha256 = "5" * 64
    profile_id = "profile-deepinfra"
    issuer = IssuerInput(code="kb", display_name="KB카드", sort_order=1)
    lineage = ProductLineageInput(
        product_lineage_id=lineage_id,
        issuer="kb",
        product_code="card-1",
        document_type="product_description",
        name="테스트카드",
    )
    unsupported = UnsupportedProductInput(
        issuer="kb",
        product_code="unsupported",
        name="보호카드",
        disposition="unsupported_drm",
        source_id=source_id,
        source_version="v1",
        source_url="https://example.test/u.pdf",
        protected_magic="FASOO_DRMONE",
        protected_sha256="6" * 64,
        protected_size_bytes=10,
        source_payload_json='{"issuer":"kb"}',
    )
    failed = OCRFailedProductInput(
        issuer="kb",
        product_code="failed",
        name="실패카드",
        document_id="doc_" + "7" * 64,
        title="실패카드",
        pdf_sha256="8" * 64,
        pdf_size_bytes=20,
        page_count=1,
        reason_code="invalid_output",
        reason="OCR output was invalid.",
        attempts=2,
    )
    revision = ContractRevisionInput(
        contract_revision_id=revision_id,
        product_lineage_id=lineage_id,
        document_id="doc_" + "9" * 64,
        source_id=source_id,
        source_version="2026-08",
        source_url="https://example.test/card.pdf",
        effective_date="2026-08-01",
        pdf_sha256="a" * 64,
        pdf_size_bytes=100,
        page_count=1,
        temporal_status="current",
    )
    page = DocumentPageInput(
        contract_revision_id=revision_id,
        page=1,
        text="혜택 원문",
        text_sha256=text_sha256,
    )
    node = StructureNodeInput(
        node_id=node_id,
        contract_revision_id=revision_id,
        parent_id=None,
        parent_contract_revision_id=None,
        node_type="ROOT",
        major_class="UNKNOWN",
        raw_heading=None,
        ordinal=0,
        display_text="혜택 원문",
        table_headers=("구분",),
        table_cells=("할인",),
        table_role=None,
    )
    node_span = NodeSpanInput(
        node_id=node_id,
        contract_revision_id=revision_id,
        page=1,
        source_start=0,
        source_end=5,
        text_sha256=text_sha256,
        span_ordinal=0,
        is_canonical=True,
    )
    link = NodeLinkInput(
        from_node_id=node_id,
        from_contract_revision_id=revision_id,
        to_node_id="node_" + "b" * 64,
        to_contract_revision_id=revision_id,
        link_type="NEXT",
        ordinal=0,
    )
    profile = EmbeddingProfileInput(
        profile_id=profile_id,
        provider="openrouter",
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        dimension=4096,
        dtype="float32",
        normalization="l2",
        document_policy="cardrag.structure-views.v1",
        query_policy="cardrag.qwen3-query.v1",
        maximum_tokens=8192,
    )
    span = NodeSpan(
        page=1,
        source_start=0,
        source_end=5,
        text_sha256=text_sha256,
        span_ordinal=0,
        is_canonical=True,
    )
    view = DerivedView(
        view_id="view_" + "c" * 64,
        contract_revision_id=revision_id,
        node_id=node_id,
        parent_item_id=None,
        view_type="DETAIL",
        ordinal=0,
        display_text="혜택 원문",
        embedding_input="혜택 원문",
        spans=(span,),
        context=(),
        input_sha256=text_sha256,
    )
    return {
        "issuers": (issuer,),
        "product_lineages": (lineage,),
        "unsupported_products": (unsupported,),
        "ocr_failed_products": (failed,),
        "contract_revisions": (revision,),
        "document_pages": (page,),
        "structure_nodes": (node,),
        "node_spans": (node_span,),
        "node_links": (link,),
        "embedding_profiles": (profile,),
        "derived_views": (view,),
        "primary_embedding_profile_id": profile_id,
        "extra_metadata": {"parser_policy_sha256": "d" * 64},
        "sealed_profile": False,
    }


def test_prediction_binds_actual_view_count_to_exact_4096d_fp32_artifacts() -> None:
    prediction = _prediction()
    database_bytes = predict_serving_database_bytes(payload_bytes=1_000, row_count=3)

    assert prediction.derived_view_count == 2
    assert prediction.embedding_cache_miss_count == 1
    assert prediction.vector_dimension == 4096
    assert prediction.vector_row_bytes == 4096 * 4 == VECTOR_ROW_BYTES
    assert prediction.vector_sidecar_bytes == 2 * 4096 * 4
    assert prediction.embedding_cache_growth_bytes == EMBEDDING_CACHE_ROW_ENVELOPE_BYTES
    assert prediction.embedding_cache_transaction_bytes == (
        EMBEDDING_CACHE_WAL_PEAK_BYTES + EMBEDDING_CACHE_WAL_ROW_ENVELOPE_BYTES
    )
    assert prediction.serving_database_bytes == database_bytes
    assert prediction.database_export_peak_bytes == (database_bytes * DATABASE_EXPORT_PEAK_MULTIPLIER)
    assert prediction.logical_growth_bytes == (
        prediction.embedding_cache_growth_bytes
        + prediction.embedding_cache_transaction_bytes
        + prediction.vector_sidecar_bytes
        + prediction.serving_database_bytes
    )
    assert prediction.peak_growth_bytes == (
        prediction.embedding_cache_growth_bytes
        + prediction.embedding_cache_transaction_bytes
        + prediction.vector_sidecar_bytes
        + prediction.database_export_peak_bytes
    )


def test_prediction_reserves_existing_wal_for_possible_checkpoint_duplication() -> None:
    prediction = _prediction(embedding_cache_wal_baseline_bytes=123_456)

    assert prediction.embedding_cache_wal_baseline_bytes == 123_456
    assert prediction.embedding_cache_transaction_bytes == (
        123_456 + EMBEDDING_CACHE_WAL_PEAK_BYTES + EMBEDDING_CACHE_WAL_ROW_ENVELOPE_BYTES
    )

    all_hit = _prediction(
        embedding_cache_miss_count=0,
        embedding_cache_wal_baseline_bytes=123_456,
    )
    assert all_hit.embedding_cache_growth_bytes == 0
    assert all_hit.embedding_cache_transaction_bytes == (123_456 + EMBEDDING_CACHE_WAL_PEAK_BYTES)
    assert prediction.peak_growth_bytes >= prediction.logical_growth_bytes


def test_prediction_assumes_every_view_is_a_cache_miss_unless_proven_otherwise() -> None:
    worst_case = predict_v5_local_artifacts(
        derived_view_count=3,
        database_payload_bytes=0,
        database_row_count=0,
    )
    all_hits = predict_v5_local_artifacts(
        derived_view_count=3,
        database_payload_bytes=0,
        database_row_count=0,
        embedding_cache_miss_count=0,
    )

    assert worst_case.embedding_cache_miss_count == 3
    assert worst_case.embedding_cache_growth_bytes == 3 * EMBEDDING_CACHE_ROW_ENVELOPE_BYTES
    assert all_hits.embedding_cache_growth_bytes == 0
    assert all_hits.embedding_cache_transaction_bytes == EMBEDDING_CACHE_WAL_PEAK_BYTES
    assert all_hits.vector_sidecar_bytes == worst_case.vector_sidecar_bytes


def test_database_prediction_is_page_aligned_and_increases_with_payload_and_rows() -> None:
    empty = predict_serving_database_bytes(payload_bytes=0, row_count=0)
    payload = predict_serving_database_bytes(payload_bytes=1, row_count=0)
    row = predict_serving_database_bytes(payload_bytes=0, row_count=1)

    assert empty >= 1024 * 1024
    assert empty % 4096 == payload % 4096 == row % 4096 == 0
    assert payload > empty
    assert row > empty


def test_database_prediction_explicitly_charges_fts_and_secondary_index_text() -> None:
    base = predict_serving_database_bytes(payload_bytes=0, row_count=0)
    fts = predict_serving_database_bytes(
        payload_bytes=0,
        row_count=0,
        fts_indexed_text_bytes=4096,
    )
    indexed = predict_serving_database_bytes(
        payload_bytes=0,
        row_count=0,
        secondary_index_text_bytes=4096,
    )

    assert fts >= base + 4096 * DATABASE_FTS_INDEXED_TEXT_MULTIPLIER
    assert indexed >= base + 4096


def test_database_prediction_is_calibrated_to_observed_partial_candidate_corpus() -> None:
    # Read-only audit of 1,026 partially processed documents. The lower-bound
    # payload excludes additional exact bindings now charged by the ledger.
    observed_rows = 1_954_142
    observed_payload_lower_bound = 85_172_324
    partial = predict_serving_database_bytes(
        payload_bytes=observed_payload_lower_bound,
        row_count=observed_rows,
    )
    representative_exact_bindings = predict_serving_database_bytes(
        payload_bytes=450_000_000,
        row_count=observed_rows,
    )

    assert partial < representative_exact_bindings < DEFAULT_MAX_SERVING_DATABASE_BYTES
    assert (
        predict_serving_database_bytes(
            payload_bytes=450_000_000 * 20,
            row_count=observed_rows * 20,
        )
        > DEFAULT_MAX_SERVING_DATABASE_BYTES
    )


def test_database_prediction_fits_observed_full_v111_candidate_corpus() -> None:
    # Read-only reconstruction of the failed v1.0.11 four-issuer preflight.
    # The former 4 GiB contract rejected this exact corpus before any paid
    # embedding request or publication. The 32 GiB contract admits this sealed
    # prediction and the planned eight-issuer expansion with operating margin.
    observed = predict_serving_database_bytes(
        payload_bytes=1_876_077_491,
        row_count=6_692_163,
        fts_indexed_text_bytes=171_794_956,
        secondary_index_text_bytes=444_565_586,
    )
    planned_eight_issuer = predict_serving_database_bytes(
        payload_bytes=3_752_154_982,
        row_count=13_384_326,
        fts_indexed_text_bytes=343_589_912,
        secondary_index_text_bytes=889_131_172,
    )

    assert observed == 8_148_455_424
    assert planned_eight_issuer == 16_295_858_176
    assert 4 * 1024**3 < observed < planned_eight_issuer < DEFAULT_MAX_SERVING_DATABASE_BYTES == 32 * 1024**3


def test_planned_eight_issuer_projection_fits_linked_capacity_contracts() -> None:
    projection = predict_v5_local_artifacts(
        derived_view_count=630_134,
        database_payload_bytes=3_752_154_982,
        database_row_count=13_384_326,
        database_fts_indexed_text_bytes=343_589_912,
        database_secondary_index_text_bytes=889_131_172,
        embedding_cache_miss_count=443_618,
        embedding_cache_wal_baseline_bytes=0,
    )
    projected_state_bytes = 2 * 4_855_486_381 + projection.logical_growth_bytes
    projected_unique_pdf_bytes = 2 * 2_781_598_165
    projected_generation_download_bytes = (
        projection.serving_database_bytes + projection.vector_sidecar_bytes + projected_unique_pdf_bytes
    )

    assert projection.serving_database_bytes == 16_295_858_176
    assert projection.vector_sidecar_bytes == 10_324_115_456
    assert projection.logical_growth_bytes == 55_701_311_488
    assert projection.peak_growth_bytes == 104_588_886_016
    assert projection.vector_sidecar_bytes < 16 * 1024**3
    assert projected_generation_download_bytes == 32_183_169_962 < 64 * 1024**3
    assert projected_state_bytes == 65_412_284_250 < DEFAULT_MAX_STATE_BYTES == 128 * 1024**3


def test_database_ledger_counts_every_export_table_and_fixed_metadata_allowance() -> None:
    inputs = _database_ledger_inputs()
    ledger = build_v5_database_ledger(**inputs)

    assert ledger.fixed_metadata_allowance_bytes == 1024 * 1024
    assert ledger.fts_indexed_text_bytes == len("혜택 원문".encode())
    assert ledger.secondary_index_text_bytes > 0
    assert ledger.rows == {
        "contract_revisions": 1,
        "document_pages": 1,
        "embedding_profiles": 1,
        "embedding_view_spans": 1,
        "embedding_views": 1,
        "embedding_views_fts": 1,
        "issuers": 1,
        "metadata": BASE_DATABASE_METADATA_ROWS + 1,
        "node_links": 1,
        "node_spans": 1,
        "ocr_failed_products": 1,
        "product_lineages": 1,
        "revision_coverage": 1,
        "structure_nodes": 1,
        "unsupported_products": 1,
    }
    assert ledger.row_count == sum(ledger.rows.values())
    assert ledger.payload_bytes > 0


@pytest.mark.parametrize(
    ("sequence_name", "field_name"),
    [
        ("issuers", "display_name"),
        ("product_lineages", "name"),
        ("unsupported_products", "source_payload_json"),
        ("ocr_failed_products", "reason"),
        ("contract_revisions", "source_version"),
        ("document_pages", "text"),
        ("structure_nodes", "display_text"),
        ("node_spans", "text_sha256"),
        ("node_links", "link_type"),
        ("embedding_profiles", "query_policy"),
    ],
)
def test_database_ledger_explicitly_counts_each_exporter_dto_text_binding(
    sequence_name: str,
    field_name: str,
) -> None:
    inputs = _database_ledger_inputs()
    before = build_v5_database_ledger(**inputs)
    original = inputs[sequence_name][0]
    inputs[sequence_name] = (replace(original, **{field_name: getattr(original, field_name) + "x"}),)

    after = build_v5_database_ledger(**inputs)

    assert after.payload_bytes == before.payload_bytes + 1
    assert after.row_count == before.row_count


def test_database_ledger_counts_view_fts_and_span_bindings_and_row_fanout() -> None:
    inputs = _database_ledger_inputs()
    before = build_v5_database_ledger(**inputs)
    view = inputs["derived_views"][0]
    mutated = replace(view, display_text=view.display_text + "x")
    inputs["derived_views"] = (mutated,)
    text_change = build_v5_database_ledger(**inputs)
    inputs["derived_views"] = (view, replace(view, ordinal=1, view_id="view_" + "e" * 64))
    extra_row = build_v5_database_ledger(**inputs)

    # display_text is bound once to embedding_views and once to its FTS row.
    assert text_change.payload_bytes == before.payload_bytes + 2
    assert text_change.fts_indexed_text_bytes == before.fts_indexed_text_bytes + 1
    assert extra_row.rows["embedding_views"] == 2
    assert extra_row.rows["embedding_views_fts"] == 2
    assert extra_row.rows["embedding_view_spans"] == 2
    assert extra_row.row_count == before.row_count + 3


def test_database_ledger_counts_dynamic_metadata_and_optional_sealed_row() -> None:
    inputs = _database_ledger_inputs()
    before = build_v5_database_ledger(**inputs)
    inputs["extra_metadata"] = {**inputs["extra_metadata"], "new_key": "value"}
    inputs["sealed_profile"] = True
    after = build_v5_database_ledger(**inputs)

    assert after.payload_bytes == before.payload_bytes + len(b"new_keyvalue")
    assert after.rows["metadata"] == before.rows["metadata"] + 2
    assert after.row_count == before.row_count + 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"derived_view_count": True}, "derived view count"),
        ({"derived_view_count": 0}, "derived view count"),
        ({"derived_view_count": -1}, "derived view count"),
        ({"database_payload_bytes": True}, "database payload bytes"),
        ({"database_payload_bytes": -1}, "database payload bytes"),
        ({"database_row_count": True}, "database row count"),
        ({"database_row_count": -1}, "database row count"),
        ({"embedding_cache_miss_count": True}, "embedding cache miss count"),
        ({"embedding_cache_miss_count": -1}, "embedding cache miss count"),
        ({"embedding_cache_miss_count": 3}, "cannot exceed derived view count"),
        ({"embedding_cache_wal_baseline_bytes": True}, "WAL baseline bytes"),
        ({"embedding_cache_wal_baseline_bytes": -1}, "WAL baseline bytes"),
    ],
)
def test_prediction_rejects_boolean_negative_and_inconsistent_counts(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _prediction(**overrides)


def test_prediction_fails_closed_on_all_relevant_arithmetic_overflow() -> None:
    with pytest.raises(V5CapacityError, match="sidecar.*supported byte range"):
        _prediction(derived_view_count=MAX_SAFE_BYTES // VECTOR_ROW_BYTES + 1)
    with pytest.raises(V5CapacityError, match="database.*supported byte range"):
        _prediction(database_payload_bytes=MAX_SAFE_BYTES)
    with pytest.raises(V5CapacityError, match="database.*supported byte range"):
        _prediction(database_row_count=MAX_SAFE_BYTES // DATABASE_ROW_ENVELOPE_BYTES + 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maximum_state_bytes", True),
        ("maximum_state_bytes", 0),
        ("reserved_free_space_bytes", True),
        ("reserved_free_space_bytes", -1),
        ("maximum_vector_sidecar_bytes", True),
        ("maximum_vector_sidecar_bytes", -1),
        ("maximum_serving_database_bytes", True),
        ("maximum_serving_database_bytes", -1),
    ],
)
def test_policy_rejects_boolean_and_out_of_range_values(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        V5CapacityPolicy(**{field: value})


def test_defaults_align_with_mcp_state_and_two_gib_reserved_free_policy() -> None:
    policy = V5CapacityPolicy()

    assert policy.maximum_state_bytes == DEFAULT_MAX_STATE_BYTES == 128 * 1024**3
    assert policy.reserved_free_space_bytes == DEFAULT_RESERVED_FREE_SPACE_BYTES == 2 * 1024**3


def test_prediction_object_cannot_be_resealed_with_another_dimension_or_arithmetic() -> None:
    prediction = _prediction()

    with pytest.raises(ValueError, match="4096D"):
        replace(prediction, vector_dimension=1536)
    with pytest.raises(ValueError, match="arithmetic is inconsistent"):
        replace(prediction, vector_sidecar_bytes=prediction.vector_sidecar_bytes + 1)


def test_preflight_observes_tree_usage_and_exact_boundary_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "runs" / "run-1"
    nested.mkdir(parents=True)
    sentinel = nested / "checkpoint.json"
    sentinel.write_bytes(b"checkpoint")
    prediction = _prediction()
    usage = len(b"checkpoint")
    policy = V5CapacityPolicy(
        maximum_state_bytes=usage + prediction.logical_growth_bytes,
        reserved_free_space_bytes=123,
        maximum_vector_sidecar_bytes=prediction.vector_sidecar_bytes,
        maximum_serving_database_bytes=prediction.serving_database_bytes,
    )
    free = prediction.peak_growth_bytes + policy.reserved_free_space_bytes
    monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", lambda _fd: free)

    snapshot = preflight_v5_capacity(tmp_path, prediction, policy=policy)

    assert snapshot.root == tmp_path.resolve()
    assert snapshot.state_usage_bytes == usage == safe_state_usage(tmp_path)
    assert snapshot.filesystem_free_bytes == free
    assert snapshot.projected_state_bytes == policy.maximum_state_bytes
    assert snapshot.projected_free_bytes == policy.reserved_free_space_bytes
    assert sentinel.read_bytes() == b"checkpoint"


def test_state_quota_rejects_before_any_cleanup_or_artifact_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / "existing.bin"
    sentinel.write_bytes(b"preserve-me")
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    prediction = _prediction()
    policy = V5CapacityPolicy(
        maximum_state_bytes=len(b"preserve-me") + prediction.logical_growth_bytes - 1,
        reserved_free_space_bytes=0,
    )
    monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", lambda _fd: MAX_SAFE_BYTES)

    with pytest.raises(V5CapacityError, match="state quota"):
        preflight_v5_capacity(tmp_path, prediction, policy=policy)

    assert sentinel.read_bytes() == b"preserve-me"
    assert tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))) == before


def test_reserved_free_space_rejects_actual_free_shortfall_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / "existing.bin"
    sentinel.write_bytes(b"preserve-me")
    prediction = _prediction()
    policy = V5CapacityPolicy(
        maximum_state_bytes=MAX_SAFE_BYTES,
        reserved_free_space_bytes=77,
    )
    insufficient = prediction.peak_growth_bytes + policy.reserved_free_space_bytes - 1
    monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", lambda _fd: insufficient)

    with pytest.raises(V5CapacityError, match="reserved free-space"):
        preflight_v5_capacity(tmp_path, prediction, policy=policy)

    assert sentinel.read_bytes() == b"preserve-me"


def test_artifact_caps_reject_before_scanning_or_asking_for_free_space(tmp_path: Path) -> None:
    prediction = _prediction()
    vector_policy = V5CapacityPolicy(
        maximum_vector_sidecar_bytes=prediction.vector_sidecar_bytes - 1,
    )
    database_policy = V5CapacityPolicy(
        maximum_serving_database_bytes=prediction.serving_database_bytes - 1,
    )

    with pytest.raises(V5CapacityError, match="vector sidecar"):
        preflight_v5_capacity(tmp_path / "missing", prediction, policy=vector_policy)
    with pytest.raises(V5CapacityError, match="serving database"):
        preflight_v5_capacity(tmp_path / "missing", prediction, policy=database_policy)


def test_preflight_fails_closed_when_free_space_cannot_be_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_descriptor: int) -> int:
        raise V5CapacityError("Worker filesystem free space is unavailable")

    monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", unavailable)

    with pytest.raises(V5CapacityError, match="free space is unavailable"):
        preflight_v5_capacity(tmp_path, _prediction())


def test_initial_capacity_rejection_does_not_checkpoint_or_change_existing_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        baseline = state.observe_embedding_cache_v5_wal()
        prediction = _prediction(
            embedding_cache_wal_baseline_bytes=baseline.size_bytes,
        )
        monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", lambda _fd: 0)

        with pytest.raises(V5CapacityError, match="reserved free-space"):
            preflight_v5_capacity(tmp_path, prediction)

        assert state.observe_embedding_cache_v5_wal() == baseline


def test_state_usage_rejects_symlinks_and_non_regular_entries(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"data")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(V5CapacityError, match="contains a symlink"):
        safe_state_usage(tmp_path)
    link.unlink()

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(V5CapacityError, match="non-regular entry"):
        safe_state_usage(tmp_path)


def test_capacity_root_itself_must_be_an_existing_real_directory(tmp_path: Path) -> None:
    root_link = tmp_path / "state-link"
    root_link.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(V5CapacityError, match="contains a symlink"):
        safe_state_usage(root_link)
    with pytest.raises(V5CapacityError, match="unavailable"):
        safe_state_usage(tmp_path / "missing")
    (tmp_path / "target-file").write_bytes(b"file")
    with pytest.raises(V5CapacityError, match="non-directory"):
        safe_state_usage(tmp_path / "target-file")


def test_startup_preflight_uses_existing_ancestor_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "not-created" / "worker-state"
    observed: list[int] = []

    def free_bytes(descriptor: int) -> int:
        observed.append(descriptor)
        return DEFAULT_MINIMUM_START_FREE_BYTES

    monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", free_bytes)
    snapshot = preflight_worker_start_capacity(
        state_root,
        minimum_free_bytes=DEFAULT_MINIMUM_START_FREE_BYTES,
    )

    assert snapshot.requested_state_root == state_root.absolute()
    assert snapshot.filesystem_probe_path == tmp_path.resolve()
    assert snapshot.filesystem_free_bytes == DEFAULT_MINIMUM_START_FREE_BYTES
    assert len(observed) == 1
    assert not state_root.exists()
    assert not state_root.parent.exists()


def test_startup_preflight_rejects_shortfall_and_invalid_limits_without_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "missing" / "state"
    monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", lambda _fd: 99)

    with pytest.raises(V5CapacityError, match="minimum startup free-space"):
        preflight_worker_start_capacity(state_root, minimum_free_bytes=100)
    for invalid in (True, -1, MAX_SAFE_BYTES + 1):
        with pytest.raises(ValueError, match="minimum Worker start free bytes"):
            preflight_worker_start_capacity(state_root, minimum_free_bytes=invalid)

    assert not state_root.exists()
    assert not state_root.parent.exists()


def test_startup_preflight_rejects_symlink_ancestor_without_following_it(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(V5CapacityError, match="contains a symlink"):
        preflight_worker_start_capacity(linked / "state", minimum_free_bytes=0)

    assert not (real / "state").exists()


def test_startup_preflight_rejects_nested_state_symlink_before_state_open(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    victim = tmp_path / "victim.sqlite3"
    victim.write_bytes(b"do-not-open")
    (state_root / "worker-state.sqlite3").symlink_to(victim)

    with pytest.raises(V5CapacityError, match="contains a symlink"):
        preflight_worker_start_capacity(state_root, minimum_free_bytes=0)

    assert victim.read_bytes() == b"do-not-open"


def test_startup_revalidation_binds_existing_ancestry_and_new_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "missing" / "state"
    snapshot = preflight_worker_start_capacity(state_root, minimum_free_bytes=0)
    state_root.mkdir(parents=True)

    revalidated = revalidate_worker_start_capacity(snapshot)
    revalidated_again = revalidate_worker_start_capacity(revalidated)

    assert revalidated.requested_state_root == state_root
    assert revalidated.state_root_existed
    assert revalidated.filesystem_device == snapshot.filesystem_device
    assert revalidated.filesystem_id == snapshot.filesystem_id
    assert revalidated.state_root_device is not None
    assert revalidated.state_root_inode is not None
    assert revalidated_again.state_root_device == revalidated.state_root_device
    assert revalidated_again.state_root_inode == revalidated.state_root_inode


def test_startup_revalidation_rejects_replaced_existing_root(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    snapshot = preflight_worker_start_capacity(state_root, minimum_free_bytes=0)
    state_root.rename(tmp_path / "original-state")
    state_root.mkdir()

    with pytest.raises(V5CapacityError, match="changed after"):
        revalidate_worker_start_capacity(snapshot)


def test_startup_revalidation_rejects_half_populated_state_root_identity(tmp_path: Path) -> None:
    state_root = tmp_path / "missing-state"
    snapshot = preflight_worker_start_capacity(state_root, minimum_free_bytes=0)

    for forged in (
        replace(snapshot, state_root_device=snapshot.filesystem_device),
        replace(snapshot, state_root_inode=snapshot.probe_inode),
        replace(
            snapshot,
            state_root_existed=True,
            state_root_device=None,
            state_root_inode=None,
        ),
    ):
        with pytest.raises(V5CapacityError, match="identity evidence is inconsistent"):
            revalidate_worker_start_capacity(forged)

    assert not state_root.exists()


def test_remaining_free_preflight_is_o1_and_rechecks_exact_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prediction = _prediction()
    policy = V5CapacityPolicy(reserved_free_space_bytes=123)
    required = (
        prediction.embedding_cache_growth_bytes
        + prediction.embedding_cache_transaction_bytes
        + prediction.vector_sidecar_bytes
        + prediction.database_export_peak_bytes
        + policy.reserved_free_space_bytes
    )

    def must_not_scan(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("remaining-free check rescanned the state tree")

    monkeypatch.setattr(capacity_module, "_tree_usage_fd", must_not_scan)
    monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", lambda _fd: required)
    snapshot = preflight_v5_remaining_free_capacity(
        tmp_path,
        prediction,
        remaining_embedding_cache_miss_count=1,
        policy=policy,
    )

    assert snapshot.required_free_bytes == required
    assert snapshot.filesystem_free_bytes == required

    monkeypatch.setattr(capacity_module, "_filesystem_free_bytes", lambda _fd: required - 1)
    with pytest.raises(V5CapacityError, match="remaining free-space"):
        preflight_v5_remaining_free_capacity(
            tmp_path,
            prediction,
            remaining_embedding_cache_miss_count=1,
            policy=policy,
        )


@pytest.mark.parametrize("remaining", (True, -1, 2))
def test_remaining_free_preflight_rejects_invalid_counts(
    tmp_path: Path,
    remaining: int,
) -> None:
    with pytest.raises(ValueError, match="remaining embedding cache"):
        preflight_v5_remaining_free_capacity(
            tmp_path,
            _prediction(),
            remaining_embedding_cache_miss_count=remaining,
        )


def test_descriptor_walk_rejects_deep_symlink_and_component_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "outer" / "state"
    state_root.mkdir(parents=True)
    (state_root / "sentinel").write_bytes(b"original")
    replacement_target = tmp_path / "replacement-target"
    replacement_target.mkdir()
    (replacement_target / "large").write_bytes(b"do-not-follow")
    replacement_link = tmp_path / "replacement-link"
    replacement_link.symlink_to(replacement_target, target_is_directory=True)
    original_open = capacity_module.os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "state" and dir_fd is not None and not swapped:
            swapped = True
            state_root.rename(state_root.with_name("original-state"))
            replacement_link.rename(state_root)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(capacity_module.os, "open", swap_before_open)
    with pytest.raises(V5CapacityError, match="changed during traversal"):
        safe_state_usage(state_root)

    assert swapped
    assert (state_root.with_name("original-state") / "sentinel").read_bytes() == b"original"


def test_descriptor_tree_scan_rejects_nested_symlink(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (nested / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(V5CapacityError, match="contains a symlink"):
        safe_state_usage(tmp_path)


def test_descriptor_tree_scan_rejects_entry_replacement_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"small")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"larger replacement")
    original_stat = capacity_module.os.stat
    payload_stats = 0

    def swap_before_final_stat(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal payload_stats
        if path == "payload" and dir_fd is not None:
            payload_stats += 1
            if payload_stats == 2:
                payload.rename(tmp_path / "original-payload")
                replacement.rename(payload)
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(capacity_module.os, "stat", swap_before_final_stat)

    with pytest.raises(V5CapacityError, match="changed during traversal"):
        safe_state_usage(tmp_path)

    assert payload_stats == 2


def test_descriptor_tree_scan_rejects_new_entry_after_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "existing").write_bytes(b"existing")
    original_listdir = capacity_module.os.listdir

    def mutate_after_enumeration(descriptor: int) -> list[str]:
        names = original_listdir(descriptor)
        (tmp_path / "late").write_bytes(b"late")
        return names

    monkeypatch.setattr(capacity_module.os, "listdir", mutate_after_enumeration)

    with pytest.raises(V5CapacityError, match="changed during traversal"):
        safe_state_usage(tmp_path)
