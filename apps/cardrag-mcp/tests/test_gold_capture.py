from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import FakeEmbedder, create_database
from v5_fixtures import install_v5_fixture

import cardrag_mcp.gold_capture as capture_module
from cardrag_mcp.aggregation_profile import (
    ArtifactBinding as ScoreArtifactBinding,
)
from cardrag_mcp.aggregation_profile import (
    CorpusInventoryManifest as ScoreCorpusInventoryManifest,
)
from cardrag_mcp.aggregation_profile import (
    CorpusInventoryRow as ScoreCorpusInventoryRow,
)
from cardrag_mcp.aggregation_profile import (
    QueryScoreCoverage,
    ScoreArtifactManifest,
)
from cardrag_mcp.evaluation import (
    EvaluatedAnswer,
    QueryRunResult,
    RetrievedContract,
    RetrievedSpan,
    RunArtifactManifest,
    ShadowObservation,
    V109BaselineObservation,
)
from cardrag_mcp.exact import V5ExactRepository
from cardrag_mcp.gold_capture import (
    AnswerArtifactManifest,
    AnswerEvidenceArtifacts,
    AnswerRecord,
    ArtifactBinding,
    CorpusInventoryManifest,
    CorpusInventoryRow,
    ExternalLexicalRanks,
    ExternalObservationManifest,
    ExternalQueryObservation,
    GoldCaptureError,
    LaneCaptureReceipt,
    NativeV5AttestationManifest,
    NativeV5QueryAttestation,
    PageGenerationManifest,
    capture_native_v5_lanes,
    finalize_native_v5_with_answers,
    seal_external_observation,
    validate_capture_set,
    validate_native_v5_capture,
)
from cardrag_mcp.reranker import (
    RerankerScore,
    RerankerShadowLane,
    RerankerShadowStore,
)
from cardrag_mcp.store import GenerationStore


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@pytest.mark.parametrize("reader_kind", ("whole", "jsonl"))
def test_regular_reader_rejects_same_inode_growth_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_kind: str,
) -> None:
    target = tmp_path / "race.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)
    original_inode = target.stat().st_ino
    original_open = capture_module.os.open
    read_calls = 0
    fdopen_calls = 0
    raced = False

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if not raced and dir_fd is None and Path(os.fspath(path)) == target:
            raced = True
            with target.open("ab") as stream:
                stream.write(payload)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def forbidden_read(descriptor: int, count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        raise AssertionError("read must not run after the opened-size preflight fails")

    def forbidden_fdopen(*args: object, **kwargs: object) -> object:
        nonlocal fdopen_calls
        fdopen_calls += 1
        raise AssertionError("fdopen must not run after the opened-size preflight fails")

    monkeypatch.setattr(capture_module.os, "open", racing_open)
    monkeypatch.setattr(capture_module.os, "read", forbidden_read)
    if reader_kind == "whole":
        with pytest.raises(GoldCaptureError, match="fixture_size_invalid"):
            capture_module._read_regular(
                target,
                maximum_bytes=len(payload),
                code="fixture",
            )
    else:
        monkeypatch.setattr(capture_module.os, "fdopen", forbidden_fdopen)
        with pytest.raises(GoldCaptureError, match="fixture_size_invalid"):
            with capture_module._CanonicalJsonlReader(
                target,
                maximum_bytes=len(payload),
                code="fixture",
            ):
                pass

    assert raced
    assert target.stat().st_ino == original_inode
    assert read_calls == 0
    assert fdopen_calls == 0


def test_regular_reader_rejects_fifo_substitution_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if not nonblocking or not hasattr(os, "mkfifo"):
        pytest.skip("FIFO nonblocking-open semantics require POSIX")
    target = tmp_path / "race.jsonl"
    target.write_bytes(_canonical({}) + b"\n")
    original_open = capture_module.os.open
    swapped = False

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is None and Path(os.fspath(path)) == target:
            assert flags & nonblocking
            target.unlink()
            os.mkfifo(target)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(capture_module.os, "open", racing_open)
    with pytest.raises(GoldCaptureError, match="fixture_not_regular"):
        capture_module._read_regular(
            target,
            maximum_bytes=1024,
            code="fixture",
        )

    assert swapped
    assert target.is_fifo()


def test_canonical_reader_enforces_running_cap_before_parsing_extra_bytes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bounded.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)

    with capture_module._CanonicalJsonlReader(
        target,
        maximum_bytes=len(payload),
        code="fixture",
    ) as reader:
        assert reader.next_record() == {}
        original_stream = reader.stream
        injected = io.BytesIO(payload)
        reader.stream = injected
        with pytest.raises(GoldCaptureError, match="fixture_size_invalid"):
            reader.next_record()
        injected.close()
        reader.stream = original_stream
        assert reader.next_record() is None


@pytest.mark.parametrize("mutation", ("grow", "replace"))
def test_canonical_reader_rejects_mid_read_identity_change(
    tmp_path: Path,
    mutation: str,
) -> None:
    target = tmp_path / "mutable.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)

    with pytest.raises(GoldCaptureError, match="fixture_changed_during_read"):
        with capture_module._CanonicalJsonlReader(
            target,
            maximum_bytes=len(payload) * 2,
            code="fixture",
        ) as reader:
            assert reader.next_record() == {}
            if mutation == "grow":
                with target.open("ab") as stream:
                    stream.write(payload)
            else:
                replacement = tmp_path / "replacement.jsonl"
                replacement.write_bytes(payload)
                replacement.replace(target)


def test_regular_readers_accept_stable_two_link_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "artifact.jsonl"
    linked = tmp_path / "artifact-hardlink.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)
    os.link(target, linked)
    assert target.stat().st_nlink == linked.stat().st_nlink == 2

    assert (
        capture_module._read_regular(
            linked,
            maximum_bytes=len(payload),
            code="fixture",
        )
        == payload
    )
    assert capture_module._hash_regular(
        linked,
        maximum_bytes=len(payload),
        code="fixture",
    ) == ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    with capture_module._CanonicalJsonlReader(
        linked,
        maximum_bytes=len(payload),
        code="fixture",
    ) as reader:
        assert reader.next_record() == {}
        assert reader.next_record() is None
    with capture_module._verified_sidecar(
        linked,
        expected=ArtifactBinding(
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        ),
        code="fixture_sidecar",
    ) as mapping:
        assert mapping[:] == payload


def test_sqlite_reader_rejects_same_inode_growth_before_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "serving.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE fixture(value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    original_size = database.stat().st_size
    original_inode = database.stat().st_ino
    original_open = capture_module.os.open
    raced = False
    connect_calls = 0

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if not raced and dir_fd is None and Path(os.fspath(path)) == database:
            raced = True
            with database.open("ab") as stream:
                stream.write(b"x")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def forbidden_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("SQLite must not open after the fd-size preflight fails")

    monkeypatch.setattr(capture_module, "_MAX_DATABASE_BYTES", original_size)
    monkeypatch.setattr(capture_module.os, "open", racing_open)
    monkeypatch.setattr(capture_module.sqlite3, "connect", forbidden_connect)
    with pytest.raises(GoldCaptureError, match="serving_database_size_invalid"):
        with capture_module._sqlite_readonly(database):
            pass

    assert raced
    assert database.stat().st_ino == original_inode
    assert connect_calls == 0


def test_sqlite_reader_checks_current_path_when_body_raises(tmp_path: Path) -> None:
    database = tmp_path / "serving.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE fixture(value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(database.read_bytes())

    with pytest.raises(GoldCaptureError, match="serving_database_changed_during_read"):
        with capture_module._sqlite_readonly(database):
            replacement.replace(database)
            raise RuntimeError("consumer failed after replacing the pathname")


def test_verified_sidecar_rejects_same_inode_growth_before_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = tmp_path / "sidecar.f32"
    payload = b"\x00\x00\x80?"
    sidecar.write_bytes(payload)
    binding = ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    original_inode = sidecar.stat().st_ino
    original_open = capture_module.os.open
    raced = False

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if not raced and dir_fd is None and Path(os.fspath(path)) == sidecar:
            raced = True
            with sidecar.open("ab") as stream:
                stream.write(b"x")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(capture_module.os, "open", racing_open)
    with pytest.raises(GoldCaptureError, match="sidecar_binding_mismatch"):
        with capture_module._verified_sidecar(sidecar, expected=binding, code="sidecar"):
            pass

    assert raced
    assert sidecar.stat().st_ino == original_inode


def test_verified_sidecar_checks_current_path_when_body_raises(tmp_path: Path) -> None:
    sidecar = tmp_path / "sidecar.f32"
    replacement = tmp_path / "replacement.f32"
    payload = b"\x00\x00\x80?"
    sidecar.write_bytes(payload)
    replacement.write_bytes(payload)
    binding = ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )

    with pytest.raises(GoldCaptureError, match="sidecar_changed_during_validation"):
        with capture_module._verified_sidecar(sidecar, expected=binding, code="sidecar"):
            replacement.replace(sidecar)
            raise RuntimeError("consumer failed after replacing the pathname")


def test_gold_capture_provider_url_is_normalized_before_credentials() -> None:
    assert (
        capture_module._validated_openrouter_base_url("https://openrouter.ai/api/v1/")
        == "https://openrouter.ai/api/v1"
    )


@pytest.mark.parametrize(
    "value",
    (
        "",
        "http://openrouter.ai/api/v1",
        "https://user:secret@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?redirect=1",
        "https://openrouter.ai/api/v1?",
        "https://openrouter.ai/api/v1#fragment",
        "https://openrouter.ai/api/v1#",
        "https://openrouter.ai:invalid/api/v1",
        "//openrouter.ai/api/v1",
        " https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1\n",
        "https://openrouter.ai/api/\x00v1",
        "https://openrouter.ai/api/\x01v1",
        "https://openrouter.ai/api/\x1fv1",
        "https://openrouter.ai/api/\x7fv1",
        "https://openrouter.ai/api/\x85v1",
        "https://openrouter.ai/api/\u200bv1",
        "https:\\evil.example\\api",
    ),
)
def test_gold_capture_rejects_unsafe_provider_url(value: str) -> None:
    with pytest.raises(GoldCaptureError, match="openrouter_base_url_invalid"):
        capture_module._validated_openrouter_base_url(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    (
        "http://openrouter.ai/api/v1",
        "https://user:secret@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?redirect=1",
        "https://openrouter.ai/api/v1?",
        "https://openrouter.ai/api/v1#fragment",
        "https://openrouter.ai/api/v1#",
        "https://openrouter.ai/api/\x00v1",
        "https://openrouter.ai/api/\u200bv1",
    ),
)
async def test_native_cli_rejects_provider_url_before_secret_or_client_construction(
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden_secret(_path: Path) -> str:
        calls.append("secret")
        raise AssertionError("provider URL must be rejected before reading credentials")

    def forbidden_client(*_args: object, **_kwargs: object) -> object:
        calls.append("client")
        raise AssertionError("provider URL must be rejected before client construction")

    monkeypatch.setattr(capture_module, "_read_secret", forbidden_secret)
    monkeypatch.setattr(capture_module, "OpenRouterEmbedder", forbidden_client)
    monkeypatch.setattr(capture_module, "OpenRouterReranker", forbidden_client)
    arguments = SimpleNamespace(
        expected_source_commit="1" * 40,
        fixture_mode=True,
        openrouter_api_key_file=tmp_path / "unused-key",
        openrouter_base_url=value,
        source_commit="1" * 40,
    )

    with pytest.raises(GoldCaptureError, match="openrouter_base_url_invalid"):
        await capture_module._run_native(arguments)
    assert calls == []


def _write_jsonl(path: Path, records: list[object]) -> ArtifactBinding:
    body = b"".join(_canonical(record) + b"\n" for record in records)
    path.write_bytes(body)
    return ArtifactBinding(sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))


def _score_binding(binding: ArtifactBinding) -> ScoreArtifactBinding:
    return ScoreArtifactBinding(sha256=binding.sha256, size_bytes=binding.size_bytes)


def _write_native_score_bundle(
    root: Path,
    *,
    prefix: str,
    gold_sha256: str,
    query_id: str,
    query_sha256: str,
    query_vector: np.ndarray,
    inventory_rows: list[ScoreCorpusInventoryRow],
    scores: list[float],
    active_contracts: int,
    source_commit: str,
    generation_id: str,
    generation_manifest_sha256: str,
    serving_database_sha256: str,
    vector_sidecar_sha256: str,
    exact_row_corpus_sha256: str,
    embedding_profile_id: str,
    runtime_document_aggregation_status: str,
    runtime_document_aggregation_policy: str,
    runtime_sealed_profile_sha256: str | None,
    validation_profile: str = "fixture_only",
) -> SimpleNamespace:
    assert len(inventory_rows) == len(scores)
    inventory_path = root / f"{prefix}-corpus.jsonl"
    inventory_manifest = ScoreCorpusInventoryManifest(
        schema_version="cardrag.document-aggregation-corpus-inventory.v1",
        generation_id=generation_id,
        serving_database_sha256=serving_database_sha256,
        vector_sidecar_sha256=vector_sidecar_sha256,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
        embedding_profile_id=embedding_profile_id,
        corpus_row_count=len(inventory_rows),
    )
    inventory_binding = _write_jsonl(
        inventory_path,
        [
            inventory_manifest.model_dump(mode="json"),
            *(row.model_dump(mode="json") for row in inventory_rows),
        ],
    )
    score_matrix_path = root / f"{prefix}-scores.f32"
    score_payload = np.asarray(scores, dtype="<f4").tobytes()
    score_matrix_path.write_bytes(score_payload)
    score_matrix_binding = ArtifactBinding(
        sha256=hashlib.sha256(score_payload).hexdigest(),
        size_bytes=len(score_payload),
    )
    query_vector_matrix_path = root / f"{prefix}-query-vectors.f32"
    query_vector_payload = np.asarray(query_vector, dtype="<f4").tobytes()
    assert len(query_vector_payload) == 4096 * 4
    query_vector_matrix_path.write_bytes(query_vector_payload)
    query_vector_matrix_binding = ArtifactBinding(
        sha256=hashlib.sha256(query_vector_payload).hexdigest(),
        size_bytes=len(query_vector_payload),
    )
    coverage = QueryScoreCoverage(
        schema_version="cardrag.document-aggregation-query-coverage.v2",
        ordinal=0,
        query_id=query_id,
        query_sha256=query_sha256,
        expected_rows=len(inventory_rows),
        scored_rows=len(inventory_rows),
        active_contracts=active_contracts,
        score_offset_bytes=0,
        score_size_bytes=len(score_payload),
        score_count=len(scores),
        score_sha256=score_matrix_binding.sha256,
        query_vector_offset_bytes=0,
        query_vector_size_bytes=len(query_vector_payload),
        query_vector_count=4096,
        query_vector_sha256=query_vector_matrix_binding.sha256,
    )
    score_manifest = ScoreArtifactManifest(
        schema_version="cardrag.document-aggregation-score-artifact.v2",
        gold_sha256=gold_sha256,
        query_count=1,
        corpus_row_count=len(inventory_rows),
        score_count=len(scores),
        source_commit=source_commit,
        generation_id=generation_id,
        generation_manifest_sha256=generation_manifest_sha256,
        serving_database_sha256=serving_database_sha256,
        vector_sidecar_sha256=vector_sidecar_sha256,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
        embedding_profile_id=embedding_profile_id,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        exact=True,
        approximate=False,
        scoring_contract="cardrag.v5-exact-row-score.v1",
        temporal_scope_policy="gold-query.v1",
        runtime_document_aggregation_status=runtime_document_aggregation_status,
        runtime_document_aggregation_policy=runtime_document_aggregation_policy,
        runtime_sealed_profile_sha256=runtime_sealed_profile_sha256,
        corpus_inventory=_score_binding(inventory_binding),
        score_matrix=_score_binding(score_matrix_binding),
        query_vector_matrix=_score_binding(query_vector_matrix_binding),
        byte_order="little-endian",
        scalar_type="float32",
        matrix_order="row-major",
        validation_profile=validation_profile,
    )
    score_artifact_path = root / f"{prefix}-scores.jsonl"
    score_artifact_binding = _write_jsonl(
        score_artifact_path,
        [score_manifest.model_dump(mode="json"), coverage.model_dump(mode="json")],
    )
    return SimpleNamespace(
        score_artifact_path=score_artifact_path,
        score_artifact_binding=score_artifact_binding,
        corpus_inventory_path=inventory_path,
        corpus_inventory_binding=inventory_binding,
        score_matrix_path=score_matrix_path,
        score_matrix_binding=score_matrix_binding,
        query_vector_matrix_path=query_vector_matrix_path,
        query_vector_matrix_binding=query_vector_matrix_binding,
        manifest=score_manifest,
        coverage=coverage,
    )


def test_native_capture_bounds_live_corpus_rows_before_materialization() -> None:
    assert capture_module._MAX_NATIVE_CAPTURE_CORPUS_ROWS == 66_666
    oversized = SimpleNamespace(
        manifest=SimpleNamespace(
            corpus_row_count=capture_module._MAX_NATIVE_CAPTURE_CORPUS_ROWS + 1
        )
    )
    with pytest.raises(
        GoldCaptureError,
        match="native_score_corpus_row_memory_limit_exceeded",
    ):
        capture_module._native_score_summary(
            oversized,
            query_index=0,
            query_id="gold-001",
            query_sha256="0" * 64,
            expected_provenance=None,
        )


def test_regular_checkpoint_rejects_atomic_same_bytes_replacement(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    database.write_bytes(b"database")
    checkpoint = capture_module._regular_artifact_checkpoint(
        database,
        maximum_bytes=1024,
        code="v5_serving_database",
    )
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(database.read_bytes())
    replacement.replace(database)

    with pytest.raises(GoldCaptureError, match="v5_serving_database_changed_after_use"):
        capture_module._verify_regular_artifact_checkpoint(checkpoint)


def _gold_record(contract_id: str, span_id: str, *, query: str = "혜택") -> dict[str, object]:
    return {
        "condition_groups": [],
        "contracts": [{"contract_revision_id": contract_id, "relevance": 3}],
        "expected_numeric_facts": [],
        "expected_revision_ids": [],
        "high_risk": False,
        "no_answer": False,
        "query_id": "gold-001",
        "question": query,
        "schema_version": "cardrag.gold-query.v1",
        "slices": ["benefit"],
        "spans": [
            {
                "contract_revision_id": contract_id,
                "page": 1,
                "relevance": 3,
                "roles": ["benefit"],
                "source_end": 1,
                "source_start": 0,
                "span_id": span_id,
                "text_sha256": "a" * 64,
            }
        ],
    }


def _qwen_page_fixture(tmp_path: Path) -> dict[str, object]:
    texts = ("페이지 혜택 A", "페이지 혜택 B")
    source_hashes = tuple(hashlib.sha256(text.encode()).hexdigest() for text in texts)
    chunk_ids = tuple(
        "evidence_"
        + capture_module.canonical_sha256(
            {
                "document_id": f"document-{suffix}",
                "page": 1,
                "source_end": len(text),
                "source_start": 0,
                "text_sha256": source_sha256,
            }
        )
        for suffix, text, source_sha256 in zip(("a", "b"), texts, source_hashes, strict=True)
    )
    gold = tmp_path / "gold.jsonl"
    gold_binding = _write_jsonl(gold, [_gold_record("contract-a", chunk_ids[0])])
    dimension = 4096
    matrix = np.zeros((2, dimension), dtype="<f4")
    matrix[0, 0] = 1.0
    matrix[1, 1] = 1.0
    vectors = tmp_path / "vectors.f32"
    vectors.write_bytes(matrix.tobytes())
    vector_binding = ArtifactBinding(
        sha256=hashlib.sha256(vectors.read_bytes()).hexdigest(),
        size_bytes=vectors.stat().st_size,
    )
    database = tmp_path / "page.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT, WITHOUT ROWID;
            CREATE TABLE evaluation_chunks(
              row_index INTEGER PRIMARY KEY,
              chunk_id TEXT NOT NULL UNIQUE,
              contract_revision_id TEXT NOT NULL,
              span_id TEXT NOT NULL UNIQUE,
              document_id TEXT NOT NULL,
              page INTEGER NOT NULL,
              source_start INTEGER NOT NULL,
              source_end INTEGER NOT NULL,
              text TEXT NOT NULL,
              input_sha256 TEXT NOT NULL
            ) STRICT;
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (
                ("schema_id", "cardrag.evaluation-page.v1"),
                ("generation_id", "qwen-page-generation"),
                ("source_commit", "1" * 40),
                ("source_generation_id", "source-generation"),
                ("source_generation_manifest_sha256", "a" * 64),
                ("source_generation_manifest_size_bytes", "1"),
                ("source_serving_database_sha256", "b" * 64),
                ("source_serving_database_size_bytes", "1"),
                ("embedding_model", "qwen/qwen3-embedding-8b"),
                ("embedding_dimension", "4096"),
                ("embedding_profile_id", "qwen-page-profile"),
                ("chunking_policy", "cardrag.page-window-1600.v1"),
                ("maximum_chars", "1600"),
                ("overlap_chars", "160"),
                ("source_text_contract", "cardrag.page-source-text-range.v1"),
                ("column_contract", "cardrag.evaluation-page-columns.v1"),
                ("row_count", "2"),
            ),
        )
        connection.executemany(
            "INSERT INTO evaluation_chunks VALUES(?,?,?,?,?,?,?,?,?,?)",
            tuple(
                (
                    index,
                    chunk_ids[index],
                    f"contract-{suffix}",
                    chunk_ids[index],
                    f"document-{suffix}",
                    1,
                    0,
                    len(texts[index]),
                    texts[index],
                    source_hashes[index],
                )
                for index, suffix in enumerate(("a", "b"))
            ),
        )
        connection.commit()
    finally:
        connection.close()
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
        size_bytes=database.stat().st_size,
    )
    inventory = tmp_path / "inventory.jsonl"
    inventory_manifest = CorpusInventoryManifest(
        schema_version="cardrag.gold-corpus-inventory.v1",
        lane="qwen_page",
        generation_id="qwen-page-generation",
        serving_database_sha256=database_binding.sha256,
        vector_artifact_sha256=vector_binding.sha256,
        embedding_dimension=4096,
        row_count=2,
    )
    inventory_rows = [
        CorpusInventoryRow(
            schema_version="cardrag.gold-corpus-row.v1",
            row_index=index,
            evidence_id=chunk_ids[index],
            contract_revision_id=f"contract-{chr(ord('a') + index)}",
            span_id=chunk_ids[index],
            input_sha256=source_hashes[index],
            embedding_f32_sha256=hashlib.sha256(matrix[index].tobytes()).hexdigest(),
        )
        for index in range(2)
    ]
    inventory_binding = _write_jsonl(
        inventory,
        [
            inventory_manifest.model_dump(mode="json"),
            *(row.model_dump(mode="json") for row in inventory_rows),
        ],
    )
    page_manifest_path = tmp_path / "page-manifest.json"
    page_manifest = PageGenerationManifest(
        schema_version="cardrag.evaluation-page-generation.v2",
        source_commit="1" * 40,
        source_generation_id="source-generation",
        source_generation_manifest=ArtifactBinding(sha256="a" * 64, size_bytes=1),
        source_serving_database=ArtifactBinding(sha256="b" * 64, size_bytes=1),
        generation_id="qwen-page-generation",
        serving_schema="cardrag.evaluation-page.v1",
        serving_database=database_binding,
        vector_artifact=vector_binding,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_profile_id="qwen-page-profile",
        chunking_policy="cardrag.page-window-1600.v1",
        maximum_chars=1600,
        overlap_chars=160,
        source_text_contract="cardrag.page-source-text-range.v1",
        column_contract="cardrag.evaluation-page-columns.v1",
        row_count=2,
        corpus_inventory_sha256=inventory_binding.sha256,
    )
    page_manifest_path.write_bytes(page_manifest.canonical_bytes())
    page_manifest_binding = ArtifactBinding(
        sha256=hashlib.sha256(page_manifest_path.read_bytes()).hexdigest(),
        size_bytes=page_manifest_path.stat().st_size,
    )
    query_vector = matrix[0].tobytes()
    dense_scores = np.asarray((1.0, 0.0), dtype="<f4").tobytes()
    score_matrix = tmp_path / "scores.f32"
    score_matrix.write_bytes(dense_scores)
    score_matrix_binding = ArtifactBinding(
        sha256=hashlib.sha256(dense_scores).hexdigest(),
        size_bytes=len(dense_scores),
    )
    query_vector_matrix = tmp_path / "query-vectors.f32"
    query_vector_matrix.write_bytes(query_vector)
    query_vector_binding = ArtifactBinding(
        sha256=hashlib.sha256(query_vector).hexdigest(),
        size_bytes=len(query_vector),
    )
    result = {
        "answer": {
            "citation_span_ids": [],
            "no_answer": False,
            "numeric_facts": [],
            "selected_revision_ids": [],
            "text": "실제 캡처 답변",
        },
        "contracts": [
            {"contract_revision_id": "contract-a", "rank": 1, "score": 1.0},
            {"contract_revision_id": "contract-b", "rank": 2, "score": 0.0},
        ],
        "lane": "qwen_page",
        "query_id": "gold-001",
        "schema_version": "cardrag.gold-run-result.v1",
        "spans": [
            {
                "contract_revision_id": "contract-a",
                "rank": 1,
                "score": 1.0,
                "span_id": chunk_ids[0],
            },
            {
                "contract_revision_id": "contract-b",
                "rank": 2,
                "score": 0.0,
                "span_id": chunk_ids[1],
            },
        ],
    }
    observation = tmp_path / "observation.jsonl"
    observation_manifest = ExternalObservationManifest(
        schema_version="cardrag.gold-external-observation-artifact.v2",
        lane="qwen_page",
        capture_mode="external_reproducible",
        synthetic=False,
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_version="v1.0.10-candidate",
        source_commit="1" * 40,
        generation_id="qwen-page-generation",
        generation_manifest=page_manifest_binding,
        serving_schema="cardrag.evaluation-page.v1",
        serving_database=database_binding,
        vector_artifact=vector_binding,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_profile_id="qwen-page-profile",
        retrieval_policy="qwen_page_window",
        scoring_contract="cardrag.qwen-page-exact-capture.v1",
        row_count=2,
        corpus_inventory_sha256=inventory_binding.sha256,
        dense_score_matrix=score_matrix_binding,
        query_vector_matrix=query_vector_binding,
        lexical_rank_artifact=None,
        byte_order="little-endian",
        scalar_type="float32",
        matrix_order="row-major",
        maximum_result_contracts=100,
        maximum_result_spans=100,
        maximum_dense_trace_contracts=100,
        maximum_dense_trace_spans=250,
        approximate=False,
    )
    query_observation = ExternalQueryObservation.model_validate_json(
        _canonical(
            {
                "ordinal": 0,
                "lane": "qwen_page",
                "query_id": "gold-001",
                "query_sha256": hashlib.sha256("혜택".encode()).hexdigest(),
                "dense_offset_bytes": 0,
                "dense_size_bytes": len(dense_scores),
                "dense_count": 2,
                "dense_sha256": hashlib.sha256(dense_scores).hexdigest(),
                "vector_offset_bytes": 0,
                "vector_size_bytes": len(query_vector),
                "vector_count": 4096,
                "vector_sha256": hashlib.sha256(query_vector).hexdigest(),
                "result": result,
                "schema_version": "cardrag.gold-external-query-observation.v2",
            }
        )
    )
    observation_binding = _write_jsonl(
        observation,
        [observation_manifest.model_dump(mode="json"), query_observation.model_dump(mode="json")],
    )
    return {
        "database": database,
        "gold": gold,
        "gold_binding": gold_binding,
        "inventory": inventory,
        "inventory_binding": inventory_binding,
        "manifest": page_manifest_path,
        "observation": observation,
        "observation_binding": observation_binding,
        "score_matrix": score_matrix,
        "query_vector_matrix": query_vector_matrix,
        "vectors": vectors,
    }


def test_external_qwen_page_capture_recomputes_every_score_and_is_atomic(tmp_path: Path) -> None:
    fixture = _qwen_page_fixture(tmp_path)
    output = tmp_path / "qwen_page.jsonl"
    receipt_path = tmp_path / "qwen_page.receipt.json"

    receipt = seal_external_observation(
        gold_path=fixture["gold"],
        expected_gold_sha256=fixture["gold_binding"].sha256,
        observation_path=fixture["observation"],
        expected_observation_sha256=fixture["observation_binding"].sha256,
        inventory_path=fixture["inventory"],
        expected_inventory_sha256=fixture["inventory_binding"].sha256,
        generation_manifest_path=fixture["manifest"],
        database_path=fixture["database"],
        vector_path=fixture["vectors"],
        dense_score_matrix_path=fixture["score_matrix"],
        query_vector_matrix_path=fixture["query_vector_matrix"],
        lexical_rank_path=None,
        output_path=output,
        receipt_path=receipt_path,
        expected_source_commit="1" * 40,
        release_gate=False,
    )
    resumed = seal_external_observation(
        gold_path=fixture["gold"],
        expected_gold_sha256=fixture["gold_binding"].sha256,
        observation_path=fixture["observation"],
        expected_observation_sha256=fixture["observation_binding"].sha256,
        inventory_path=fixture["inventory"],
        expected_inventory_sha256=fixture["inventory_binding"].sha256,
        generation_manifest_path=fixture["manifest"],
        database_path=fixture["database"],
        vector_path=fixture["vectors"],
        dense_score_matrix_path=fixture["score_matrix"],
        query_vector_matrix_path=fixture["query_vector_matrix"],
        lexical_rank_path=None,
        output_path=output,
        receipt_path=receipt_path,
        expected_source_commit="1" * 40,
        release_gate=False,
    )

    assert receipt == resumed
    assert receipt.capture_mode == "external_reproducible"
    assert receipt.capture_phase == "bootstrap_retrieval"
    assert receipt.validation_profile == "fixture_only"
    assert not receipt.release_eligible
    assert receipt.answer_evidence is None
    assert receipt.run_artifact.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()

    with pytest.raises(GoldCaptureError, match="candidate_source_commit_mismatch"):
        seal_external_observation(
            gold_path=fixture["gold"],
            expected_gold_sha256=fixture["gold_binding"].sha256,
            observation_path=fixture["observation"],
            expected_observation_sha256=fixture["observation_binding"].sha256,
            inventory_path=fixture["inventory"],
            expected_inventory_sha256=fixture["inventory_binding"].sha256,
            generation_manifest_path=fixture["manifest"],
            database_path=fixture["database"],
            vector_path=fixture["vectors"],
            dense_score_matrix_path=fixture["score_matrix"],
            query_vector_matrix_path=fixture["query_vector_matrix"],
            lexical_rank_path=None,
            output_path=tmp_path / "stale-qwen-page.jsonl",
            receipt_path=tmp_path / "stale-qwen-page.receipt.json",
            expected_source_commit="2" * 40,
            release_gate=False,
        )


def test_external_qwen_page_rejects_vector_growth_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _qwen_page_fixture(tmp_path)
    vector_path = fixture["vectors"]
    assert isinstance(vector_path, Path)
    original_inode = vector_path.stat().st_ino
    original_open = capture_module.os.open
    original_mmap = capture_module.mmap.mmap
    raced = False
    mmap_calls = 0
    vector_descriptor: int | None = None

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced, vector_descriptor
        if not raced and dir_fd is None and Path(os.fspath(path)) == vector_path:
            raced = True
            with vector_path.open("ab") as stream:
                stream.write(b"x")
            vector_descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            return vector_descriptor
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def forbidden_mmap(*args: object, **kwargs: object) -> object:
        nonlocal mmap_calls
        if args and args[0] == vector_descriptor:
            mmap_calls += 1
            raise AssertionError("mmap must not run after the opened-size preflight fails")
        return original_mmap(*args, **kwargs)

    monkeypatch.setattr(capture_module.os, "open", racing_open)
    monkeypatch.setattr(capture_module.mmap, "mmap", forbidden_mmap)
    output = tmp_path / "raced-qwen-page.jsonl"
    receipt = tmp_path / "raced-qwen-page.receipt.json"
    with pytest.raises(GoldCaptureError, match="page_vector_size_mismatch"):
        seal_external_observation(
            gold_path=fixture["gold"],
            expected_gold_sha256=fixture["gold_binding"].sha256,
            observation_path=fixture["observation"],
            expected_observation_sha256=fixture["observation_binding"].sha256,
            inventory_path=fixture["inventory"],
            expected_inventory_sha256=fixture["inventory_binding"].sha256,
            generation_manifest_path=fixture["manifest"],
            database_path=fixture["database"],
            vector_path=vector_path,
            dense_score_matrix_path=fixture["score_matrix"],
            query_vector_matrix_path=fixture["query_vector_matrix"],
            lexical_rank_path=None,
            output_path=output,
            receipt_path=receipt,
            expected_source_commit="1" * 40,
            release_gate=False,
        )

    assert raced
    assert vector_path.stat().st_ino == original_inode
    assert mmap_calls == 0
    assert not output.exists()
    assert not receipt.exists()


def test_qwen_vector_reader_checks_current_path_when_body_raises(tmp_path: Path) -> None:
    fixture = _qwen_page_fixture(tmp_path)
    observation_path = fixture["observation"]
    inventory_path = fixture["inventory"]
    manifest_path = fixture["manifest"]
    vector_path = fixture["vectors"]
    assert isinstance(observation_path, Path)
    assert isinstance(inventory_path, Path)
    assert isinstance(manifest_path, Path)
    assert isinstance(vector_path, Path)
    observation_manifest = ExternalObservationManifest.model_validate_json(
        observation_path.read_bytes().splitlines()[0]
    )
    page_manifest, _binding = capture_module._load_page_generation_manifest(manifest_path)
    _inventory_manifest, inventory, _inventory_binding = capture_module._load_inventory(
        inventory_path,
        expected_manifest=observation_manifest,
        expected_sha256=fixture["inventory_binding"].sha256,
    )
    replacement = tmp_path / "replacement-vectors.f32"
    replacement.write_bytes(vector_path.read_bytes())

    with pytest.raises(GoldCaptureError, match="page_vector_changed_during_validation"):
        with capture_module._verified_corpus_vectors(
            manifest=observation_manifest,
            inventory=inventory,
            database_path=fixture["database"],
            vector_path=vector_path,
            page_manifest=page_manifest,
            release_gate=False,
        ):
            replacement.replace(vector_path)
            raise RuntimeError("consumer failed after replacing the pathname")


def test_external_compact_v2_caps_predicted_bytes_and_accepts_zero_lexical_hits(
    tmp_path: Path,
) -> None:
    fixture = _qwen_page_fixture(tmp_path)
    manifest_record = json.loads(fixture["observation"].read_bytes().splitlines()[0])
    row_count = capture_module._MAX_EXTERNAL_SIDECAR_BYTES // 4 + 1
    manifest_record["row_count"] = row_count
    manifest_record["dense_score_matrix"] = {
        "sha256": "f" * 64,
        "size_bytes": row_count * 4,
    }
    with pytest.raises(ValueError, match="sidecar size"):
        ExternalObservationManifest.model_validate(manifest_record)

    zero = ExternalLexicalRanks(
        schema_version="cardrag.gold-lexical-ranks.v1",
        ordinal=0,
        query_id="gold-001",
        ranks=(),
    )
    assert zero.canonical_bytes() + b"\n" == _canonical(zero.model_dump(mode="json")) + b"\n"
    original_limit = capture_module._MAX_EXTERNAL_ARTIFACT_BYTES
    capture_module._MAX_EXTERNAL_ARTIFACT_BYTES = 1
    try:
        with pytest.raises(GoldCaptureError, match="external_run_artifact_too_large"):
            capture_module._external_run_bytes((zero,))
    finally:
        capture_module._MAX_EXTERNAL_ARTIFACT_BYTES = original_limit


def test_portable_answer_binding_needs_no_generation_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gold_path = tmp_path / "gold.jsonl"
    gold_binding = _write_jsonl(gold_path, [_gold_record("contract-a", "span-a")])
    gold = capture_module.load_gold_jsonl(gold_path, release_gate=False)
    generation_sha256 = "a" * 64
    serving_database_sha256 = "b" * 64
    answer = EvaluatedAnswer(text="근거 없음", no_answer=True)
    answer_path = tmp_path / "answers.jsonl"
    answer_binding = _write_jsonl(
        answer_path,
        [
            AnswerArtifactManifest(
                schema_version="cardrag.gold-answer-artifact.v1",
                lane="qwen_structure_exact",
                gold_sha256=gold_binding.sha256,
                query_count=1,
                generation_id="generation-a",
                generation_manifest_sha256=generation_sha256,
                answer_profile_id="answer-profile",
                synthetic=False,
            ).model_dump(mode="json"),
            AnswerRecord(
                schema_version="cardrag.gold-answer.v1",
                query_id="gold-001",
                query_sha256=hashlib.sha256("혜택".encode()).hexdigest(),
                answer=answer,
            ).model_dump(mode="json"),
        ],
    )
    authoritative = (
        QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            lane="qwen_structure_exact",
            query_id="gold-001",
            contracts=(),
            spans=(),
            answer=answer,
        ),
    )

    def write_binding(name: str, payload: bytes) -> tuple[Path, ArtifactBinding]:
        path = tmp_path / name
        path.write_bytes(payload)
        return path, ArtifactBinding(
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )

    input_path, input_binding = write_binding("input.jsonl", b"input\n")
    receipt_path, receipt_binding = write_binding("producer-receipt.json", b"receipt\n")
    ledger_path, ledger_binding = write_binding("ledger.jsonl", b"ledger\n")
    identity_path, identity_binding = write_binding("identity.json", b"identity\n")
    bundle_path, bundle_binding = write_binding("state-bundle.jsonl", b"bundle\n")
    retrieval_run, retrieval_run_binding = write_binding("bootstrap-run.jsonl", b"run\n")
    retrieval_receipt, retrieval_receipt_binding = write_binding(
        "bootstrap-receipt.json", b"capture\n"
    )
    retrieval_attestation, retrieval_attestation_binding = write_binding(
        "bootstrap-attestation.jsonl", b"attestation\n"
    )
    retrieval_score, retrieval_score_binding = write_binding("bootstrap-scores.jsonl", b"scores\n")
    retrieval_corpus, retrieval_corpus_binding = write_binding(
        "bootstrap-corpus.jsonl", b"corpus\n"
    )
    retrieval_dense, retrieval_dense_binding = write_binding(
        "bootstrap-scores.f32", b"\x00\x00\x80?"
    )
    retrieval_vectors, retrieval_vectors_binding = write_binding(
        "bootstrap-query-vectors.f32", b"\x00\x00\x80?"
    )
    fake_receipt = SimpleNamespace(
        capture_input=input_binding,
        answer_artifact=answer_binding,
        serving_database=ArtifactBinding(sha256=serving_database_sha256, size_bytes=1),
        call_ledger=ledger_binding,
        state_identity=identity_binding,
        state_bundle=bundle_binding,
        retrieval_run=retrieval_run_binding,
        retrieval_capture_receipt=retrieval_receipt_binding,
        retrieval_attestation_artifact=retrieval_attestation_binding,
        retrieval_raw_score_artifact=retrieval_score_binding,
        retrieval_corpus_inventory=retrieval_corpus_binding,
        retrieval_dense_score_matrix=retrieval_dense_binding,
        retrieval_query_vector_matrix=retrieval_vectors_binding,
        retrieval_lexical_rank_artifact=None,
        decision_artifact=None,
    )
    fake_inputs = SimpleNamespace(
        binding=input_binding,
        manifest=SimpleNamespace(answer_profile_id="answer-profile"),
    )
    calls: list[dict[str, object]] = []

    def portable_verifier(**kwargs: object) -> object:
        calls.append(kwargs)
        assert "generation_manifest_path" not in kwargs
        assert "database_path" not in kwargs
        return fake_receipt

    def forbidden_full_verifier(**_kwargs: object) -> object:
        raise AssertionError("portable validation must not open source artifacts")

    monkeypatch.setattr(
        "cardrag_mcp.gold_answer_artifact.verify_answer_producer_receipt_portable",
        portable_verifier,
    )
    monkeypatch.setattr(
        "cardrag_mcp.gold_answer_artifact.verify_answer_producer_receipt",
        forbidden_full_verifier,
    )
    monkeypatch.setattr(
        "cardrag_mcp.gold_answer_artifact.verify_answer_input_ranking",
        lambda **_kwargs: fake_inputs,
    )
    artifacts = AnswerEvidenceArtifacts(
        generation_manifest_path=None,
        database_path=None,
        input_path=input_path,
        expected_input_sha256=input_binding.sha256,
        producer_receipt_path=receipt_path,
        expected_producer_receipt_sha256=receipt_binding.sha256,
        answer_artifact_path=answer_path,
        expected_answer_artifact_sha256=answer_binding.sha256,
        call_ledger_path=ledger_path,
        state_identity_path=identity_path,
        state_bundle_path=bundle_path,
        answer_profile_id="answer-profile",
        retrieval_run_path=retrieval_run,
        expected_retrieval_run_sha256=retrieval_run_binding.sha256,
        retrieval_capture_receipt_path=retrieval_receipt,
        expected_retrieval_capture_receipt_sha256=retrieval_receipt_binding.sha256,
        retrieval_attestation_path=retrieval_attestation,
        expected_retrieval_attestation_sha256=retrieval_attestation_binding.sha256,
        retrieval_raw_score_path=retrieval_score,
        expected_retrieval_raw_score_sha256=retrieval_score_binding.sha256,
        retrieval_corpus_inventory_path=retrieval_corpus,
        expected_retrieval_corpus_inventory_sha256=retrieval_corpus_binding.sha256,
        retrieval_dense_score_matrix_path=retrieval_dense,
        expected_retrieval_dense_score_matrix_sha256=retrieval_dense_binding.sha256,
        retrieval_query_vector_matrix_path=retrieval_vectors,
        expected_retrieval_query_vector_matrix_sha256=retrieval_vectors_binding.sha256,
    )
    verified = capture_module._verify_answer_evidence(
        artifacts=artifacts,
        authoritative_results=authoritative,
        expected_lane="qwen_structure_exact",
        gold=gold,
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        expected_source_commit="1" * 40,
        expected_generation_id="generation-a",
        expected_generation_manifest_sha256=generation_sha256,
        expected_serving_database_sha256=serving_database_sha256,
        release_gate=True,
        source_replay=False,
    )
    assert verified is not None
    assert verified.answer_state_bundle == bundle_binding
    assert len(calls) == 1

    input_path.write_bytes(b"tampered-input\n")
    with pytest.raises(
        GoldCaptureError,
        match="answer_evidence_binding_changed_after_verification",
    ):
        capture_module._verify_answer_evidence(
            artifacts=artifacts,
            authoritative_results=authoritative,
            expected_lane="qwen_structure_exact",
            gold=gold,
            gold_path=gold_path,
            expected_gold_sha256=gold_binding.sha256,
            expected_source_commit="1" * 40,
            expected_generation_id="generation-a",
            expected_generation_manifest_sha256=generation_sha256,
            expected_serving_database_sha256=serving_database_sha256,
            release_gate=True,
            source_replay=False,
        )


@pytest.mark.parametrize(
    "tampered_score",
    (0.5, float(np.nextafter(np.float32(1.0), np.float32(0.0)))),
)
def test_external_qwen_page_capture_rejects_raw_score_tamper_and_symlink(
    tmp_path: Path,
    tampered_score: float,
) -> None:
    fixture = _qwen_page_fixture(tmp_path)
    records = [json.loads(line) for line in fixture["observation"].read_bytes().splitlines()]
    tampered_scores = np.asarray((tampered_score, 0.0), dtype="<f4").tobytes()
    tampered_score_path = tmp_path / "tampered-scores.f32"
    tampered_score_path.write_bytes(tampered_scores)
    records[0]["dense_score_matrix"] = {
        "sha256": hashlib.sha256(tampered_scores).hexdigest(),
        "size_bytes": len(tampered_scores),
    }
    records[1]["dense_sha256"] = hashlib.sha256(tampered_scores).hexdigest()
    tampered = tmp_path / "tampered.jsonl"
    tampered_binding = _write_jsonl(tampered, records)

    with pytest.raises(GoldCaptureError, match="external_raw_score_provenance_mismatch"):
        seal_external_observation(
            gold_path=fixture["gold"],
            expected_gold_sha256=fixture["gold_binding"].sha256,
            observation_path=tampered,
            expected_observation_sha256=tampered_binding.sha256,
            inventory_path=fixture["inventory"],
            expected_inventory_sha256=fixture["inventory_binding"].sha256,
            generation_manifest_path=fixture["manifest"],
            database_path=fixture["database"],
            vector_path=fixture["vectors"],
            dense_score_matrix_path=tampered_score_path,
            query_vector_matrix_path=fixture["query_vector_matrix"],
            lexical_rank_path=None,
            output_path=tmp_path / "bad.jsonl",
            receipt_path=tmp_path / "bad.receipt.json",
            release_gate=False,
        )

    symlink = tmp_path / "observation-link.jsonl"
    symlink.symlink_to(fixture["observation"])
    with pytest.raises(GoldCaptureError, match="external_observation_not_regular"):
        seal_external_observation(
            gold_path=fixture["gold"],
            expected_gold_sha256=fixture["gold_binding"].sha256,
            observation_path=symlink,
            expected_observation_sha256=fixture["observation_binding"].sha256,
            inventory_path=fixture["inventory"],
            expected_inventory_sha256=fixture["inventory_binding"].sha256,
            generation_manifest_path=fixture["manifest"],
            database_path=fixture["database"],
            vector_path=fixture["vectors"],
            dense_score_matrix_path=fixture["score_matrix"],
            query_vector_matrix_path=fixture["query_vector_matrix"],
            lexical_rank_path=None,
            output_path=tmp_path / "link.jsonl",
            receipt_path=tmp_path / "link.receipt.json",
            release_gate=False,
        )


def test_external_v109_capture_recomputes_dense_and_lexical_rrf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = create_database(
        tmp_path / "generation-v109" / "index.sqlite3",
        "generation-v109",
        schema_id="cardrag.serving-db.v4",
    )
    database = fixture.database
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
        size_bytes=database.stat().st_size,
    )
    connection = sqlite3.connect(database)
    try:
        source_rows = connection.execute(
            """SELECT e.evidence_id,e.document_id,e.text,e.embedding
                 FROM evidence AS e ORDER BY e.evidence_id"""
        ).fetchall()
    finally:
        connection.close()
    inventory_path = tmp_path / "v109-inventory.jsonl"
    inventory_manifest = CorpusInventoryManifest(
        schema_version="cardrag.gold-corpus-inventory.v1",
        lane="v109_baseline",
        generation_id="generation-v109",
        serving_database_sha256=database_binding.sha256,
        vector_artifact_sha256=None,
        embedding_dimension=1536,
        row_count=len(source_rows),
    )
    inventory_rows = [
        CorpusInventoryRow(
            schema_version="cardrag.gold-corpus-row.v1",
            row_index=index,
            evidence_id=str(row[0]),
            contract_revision_id=str(row[1]),
            span_id=str(row[0]),
            input_sha256=hashlib.sha256(str(row[2]).encode()).hexdigest(),
            embedding_f32_sha256=hashlib.sha256(bytes(row[3])).hexdigest(),
        )
        for index, row in enumerate(source_rows)
    ]
    inventory_binding = _write_jsonl(
        inventory_path,
        [
            inventory_manifest.model_dump(mode="json"),
            *(row.model_dump(mode="json") for row in inventory_rows),
        ],
    )
    gold_path = tmp_path / "v109-gold.jsonl"
    gold_binding = _write_jsonl(
        gold_path,
        [_gold_record(str(source_rows[0][1]), str(source_rows[0][0]), query="airport lounge")],
    )
    generation_binding = ArtifactBinding(sha256="f" * 64, size_bytes=123)
    query_vector = np.zeros((1536,), dtype="<f4")
    query_vector[0] = 1.0
    scored = [
        (float(np.frombuffer(bytes(row[3]), dtype="<f4") @ query_vector), index, row)
        for index, row in enumerate(source_rows)
    ]
    dense_order = sorted(scored, key=lambda item: (-item[0], str(item[2][0])))
    dense_rank = {index: rank for rank, (_score, index, _row) in enumerate(dense_order, start=1)}
    lexical = capture_module._v109_lexical_ranks(database, "airport lounge", limit=250)
    score_payload = np.asarray(
        [score for score, _index, _row in sorted(scored, key=lambda item: item[1])],
        dtype="<f4",
    ).tobytes()
    score_matrix_path = tmp_path / "v109-scores.f32"
    score_matrix_path.write_bytes(score_payload)
    score_matrix_binding = ArtifactBinding(
        sha256=hashlib.sha256(score_payload).hexdigest(),
        size_bytes=len(score_payload),
    )
    vector_payload = query_vector.tobytes()
    query_vector_matrix_path = tmp_path / "v109-query-vectors.f32"
    query_vector_matrix_path.write_bytes(vector_payload)
    query_vector_matrix_binding = ArtifactBinding(
        sha256=hashlib.sha256(vector_payload).hexdigest(),
        size_bytes=len(vector_payload),
    )
    row_index_by_evidence = {row.evidence_id: row.row_index for row in inventory_rows}
    lexical_ranks = sorted(lexical.items(), key=lambda item: item[1])
    lexical_payload = (
        _canonical(
            {
                "ordinal": 0,
                "query_id": "gold-001",
                "ranks": [
                    [row_index_by_evidence[evidence_id], rank]
                    for evidence_id, rank in lexical_ranks
                ],
                "schema_version": "cardrag.gold-lexical-ranks.v1",
            }
        )
        + b"\n"
    )
    lexical_rank_path = tmp_path / "v109-lexical.jsonl"
    lexical_rank_path.write_bytes(lexical_payload)
    lexical_binding = ArtifactBinding(
        sha256=hashlib.sha256(lexical_payload).hexdigest(),
        size_bytes=len(lexical_payload),
    )
    external_manifest = ExternalObservationManifest(
        schema_version="cardrag.gold-external-observation-artifact.v2",
        lane="v109_baseline",
        capture_mode="external_reproducible",
        synthetic=False,
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_version="v1.0.9",
        source_commit="fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113",
        generation_id="generation-v109",
        generation_manifest=generation_binding,
        serving_schema="cardrag.serving-db.v4",
        serving_database=database_binding,
        vector_artifact=None,
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
        embedding_profile_id="cardrag.embedding.v109-small.v1",
        retrieval_policy="small_rrf",
        maximum_candidates=250,
        scoring_contract="cardrag.v109-small-dense-rrf-capture.v1",
        row_count=len(source_rows),
        corpus_inventory_sha256=inventory_binding.sha256,
        dense_score_matrix=score_matrix_binding,
        query_vector_matrix=query_vector_matrix_binding,
        lexical_rank_artifact=lexical_binding,
        byte_order="little-endian",
        scalar_type="float32",
        matrix_order="row-major",
        maximum_result_contracts=100,
        maximum_result_spans=100,
        maximum_dense_trace_contracts=100,
        maximum_dense_trace_spans=250,
        approximate=False,
    )
    raw_rows = [
        {
            "contract_revision_id": str(row[1]),
            "dense_rank": dense_rank[index],
            "dense_score": score,
            "evidence_id": str(row[0]),
            "input_sha256": inventory_rows[index].input_sha256,
            "lexical_rank": lexical.get(str(row[0])),
            "row_index": index,
            "span_id": str(row[0]),
        }
        for score, index, row in scored
    ]
    dense_spans = [
        {
            "contract_revision_id": str(row[1]),
            "rank": rank,
            "score": score,
            "span_id": str(row[0]),
        }
        for rank, (score, _index, row) in enumerate(dense_order, start=1)
    ]

    def contracts(spans: list[dict[str, object]]) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        seen: set[str] = set()
        for span in spans:
            contract_id = str(span["contract_revision_id"])
            if contract_id not in seen:
                seen.add(contract_id)
                output.append(
                    {
                        "contract_revision_id": contract_id,
                        "rank": len(output) + 1,
                        "score": span["score"],
                    }
                )
        return output

    fused = sorted(
        (
            (
                (0.0 if row["lexical_rank"] is None else 1 / (60 + int(row["lexical_rank"])))
                + 1 / (60 + int(row["dense_rank"])),
                row,
            )
            for row in raw_rows
        ),
        key=lambda item: (-item[0], str(item[1]["evidence_id"])),
    )
    primary_spans = [
        {
            "contract_revision_id": row["contract_revision_id"],
            "rank": rank,
            "score": score,
            "span_id": row["span_id"],
        }
        for rank, (score, row) in enumerate(fused, start=1)
    ]
    query_observation = ExternalQueryObservation.model_validate_json(
        _canonical(
            {
                "ordinal": 0,
                "lane": "v109_baseline",
                "query_id": "gold-001",
                "query_sha256": hashlib.sha256(b"airport lounge").hexdigest(),
                "dense_offset_bytes": 0,
                "dense_size_bytes": len(score_payload),
                "dense_count": len(source_rows),
                "dense_sha256": hashlib.sha256(score_payload).hexdigest(),
                "vector_offset_bytes": 0,
                "vector_size_bytes": len(vector_payload),
                "vector_count": 1536,
                "vector_sha256": hashlib.sha256(vector_payload).hexdigest(),
                "lexical_offset_bytes": 0,
                "lexical_size_bytes": len(lexical_payload),
                "lexical_count": len(lexical_ranks),
                "lexical_sha256": hashlib.sha256(lexical_payload).hexdigest(),
                "result": {
                    "answer": {
                        "citation_span_ids": [],
                        "no_answer": False,
                        "numeric_facts": [],
                        "selected_revision_ids": [],
                        "text": "봉인된 v1.0.9 답변",
                    },
                    "contracts": contracts(primary_spans)[:100],
                    "lane": "v109_baseline",
                    "query_id": "gold-001",
                    "schema_version": "cardrag.gold-run-result.v1",
                    "spans": primary_spans[:100],
                    "v109_baseline": {
                        "dense_contracts": contracts(dense_spans)[:100],
                        "dense_spans": dense_spans[:250],
                        "kind": "v109_small_rrf",
                        "rrf_k": 60,
                    },
                },
                "schema_version": "cardrag.gold-external-query-observation.v2",
            }
        )
    )
    observation_path = tmp_path / "v109-observation.jsonl"
    observation_binding = _write_jsonl(
        observation_path,
        [external_manifest.model_dump(mode="json"), query_observation.model_dump(mode="json")],
    )
    fake_generation = SimpleNamespace(
        schema_version="cardrag.generation.v4",
        serving_schema="cardrag.serving-db.v4",
        generation_id="generation-v109",
        serving_database=database_binding,
    )
    monkeypatch.setattr(
        capture_module,
        "_load_generation_manifest",
        lambda _path: (fake_generation, generation_binding),
    )

    receipt = seal_external_observation(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        observation_path=observation_path,
        expected_observation_sha256=observation_binding.sha256,
        inventory_path=inventory_path,
        expected_inventory_sha256=inventory_binding.sha256,
        generation_manifest_path=tmp_path / "v109-manifest.json",
        database_path=database,
        vector_path=None,
        dense_score_matrix_path=score_matrix_path,
        query_vector_matrix_path=query_vector_matrix_path,
        lexical_rank_path=lexical_rank_path,
        output_path=tmp_path / "v109.jsonl",
        receipt_path=tmp_path / "v109.receipt.json",
        release_gate=False,
    )

    assert receipt.lane == "v109_baseline"
    assert receipt.capture_mode == "external_reproducible"
    assert receipt.validation_profile == "fixture_only"

    original_gold_loader = capture_module.load_gold_jsonl
    monkeypatch.setattr(
        capture_module,
        "load_gold_jsonl",
        lambda path, *, release_gate: original_gold_loader(path, release_gate=False),
    )
    with pytest.raises(GoldCaptureError, match="v109_preserved_source_anchor_mismatch"):
        seal_external_observation(
            gold_path=gold_path,
            expected_gold_sha256=gold_binding.sha256,
            observation_path=observation_path,
            expected_observation_sha256=observation_binding.sha256,
            inventory_path=inventory_path,
            expected_inventory_sha256=inventory_binding.sha256,
            generation_manifest_path=tmp_path / "v109-manifest.json",
            database_path=database,
            vector_path=None,
            dense_score_matrix_path=score_matrix_path,
            query_vector_matrix_path=query_vector_matrix_path,
            lexical_rank_path=lexical_rank_path,
            output_path=tmp_path / "alternate-v109.jsonl",
            receipt_path=tmp_path / "alternate-v109.receipt.json",
            expected_source_commit=capture_module.V109_BASELINE_COMMIT,
            release_gate=True,
        )


class _FakeRerankerClient:
    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, _query: str, documents: list[str]) -> tuple[RerankerScore, ...]:
        self.calls += 1
        return tuple(
            RerankerScore(index=index, relevance_score=1.0 - index / 100)
            for index in range(len(documents))
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_native_v5_capture_calls_actual_apis_resumes_and_revalidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "runtime", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, handle = install_v5_fixture(store)
    gold_path = tmp_path / "gold.jsonl"
    span_id = f"{fixture.current_revision_id}-item"
    query_text = "알파 카드 혜택"
    gold_binding = _write_jsonl(
        gold_path,
        [_gold_record(fixture.current_revision_id, span_id, query=query_text)],
    )
    query_vector = np.zeros((4096,), dtype=np.float32)
    query_vector[0] = 1.0
    embedder = FakeEmbedder(query_vector)
    capture = await V5ExactRepository(store, embedder).capture_unscoped_current_scores(
        query_text,
        handle=handle,
    )
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(fixture.database.read_bytes()).hexdigest(),
        size_bytes=fixture.database.stat().st_size,
    )
    sidecar_path = fixture.database.parent / "vectors.f32"
    sidecar_binding = ArtifactBinding(
        sha256=hashlib.sha256(sidecar_path.read_bytes()).hexdigest(),
        size_bytes=sidecar_path.stat().st_size,
    )
    generation_sha256 = "e" * 64
    source_commit = "1" * 40
    score_bundle = _write_native_score_bundle(
        tmp_path,
        prefix="native",
        gold_sha256=gold_binding.sha256,
        query_id="gold-001",
        query_sha256=capture.query_sha256,
        query_vector=np.frombuffer(capture.query_vector_f32, dtype="<f4"),
        inventory_rows=[
            ScoreCorpusInventoryRow(
                schema_version="cardrag.document-aggregation-corpus-row.v1",
                ordinal=ordinal,
                row_index=row.row_index,
                contract_revision_id=row.contract_revision_id,
                node_id=row.node_id,
                view_type=row.view_type,
                input_sha256=row.input_sha256,
                embedding_profile_id=row.embedding_profile_id,
            )
            for ordinal, row in enumerate(capture.rows)
        ],
        scores=[row.score for row in capture.rows],
        active_contracts=capture.expected_active_contracts,
        source_commit=source_commit,
        generation_id=fixture.generation_id,
        generation_manifest_sha256=generation_sha256,
        serving_database_sha256=database_binding.sha256,
        vector_sidecar_sha256=sidecar_binding.sha256,
        exact_row_corpus_sha256=handle.metadata.exact_row_corpus_sha256,
        embedding_profile_id=handle.metadata.primary_embedding_profile_id,
        runtime_document_aggregation_status=handle.metadata.document_aggregation_status,
        runtime_document_aggregation_policy=handle.metadata.document_aggregation_policy,
        runtime_sealed_profile_sha256=handle.metadata.sealed_profile_sha256,
        validation_profile="release_grade",
    )
    answer_path = tmp_path / "answers.jsonl"
    answer_manifest = AnswerArtifactManifest(
        schema_version="cardrag.gold-answer-artifact.v1",
        lane="qwen_structure_exact",
        gold_sha256=gold_binding.sha256,
        query_count=1,
        generation_id=fixture.generation_id,
        generation_manifest_sha256=generation_sha256,
        answer_profile_id="sealed-answer-profile",
        synthetic=False,
    )
    answer_record = AnswerRecord(
        schema_version="cardrag.gold-answer.v1",
        query_id="gold-001",
        query_sha256=hashlib.sha256(query_text.encode()).hexdigest(),
        answer=EvaluatedAnswer(
            text="실제 캡처 답변",
            no_answer=False,
            citation_span_ids=(),
            numeric_facts=(),
            selected_revision_ids=(),
        ),
    )
    answer_binding = _write_jsonl(
        answer_path,
        [answer_manifest.model_dump(mode="json"), answer_record.model_dump(mode="json")],
    )
    fake_manifest = SimpleNamespace(
        schema_version="cardrag.generation.v5",
        generation_id=fixture.generation_id,
        serving_database=database_binding,
        vector_sidecar=SimpleNamespace(artifact=sidecar_binding),
        exact_row_corpus_sha256=handle.metadata.exact_row_corpus_sha256,
        primary_embedding_profile_id=handle.metadata.primary_embedding_profile_id,
        embedding_contract=SimpleNamespace(count=handle.metadata.embedding_count),
    )
    monkeypatch.setattr(
        capture_module,
        "_load_generation_manifest",
        lambda _path: (fake_manifest, ArtifactBinding(sha256=generation_sha256, size_bytes=123)),
    )
    monkeypatch.setattr(capture_module, "load_generation_handle", lambda *_args, **_kwargs: handle)
    real_load_gold = capture_module.load_gold_jsonl
    monkeypatch.setattr(
        capture_module,
        "load_gold_jsonl",
        lambda path, *, release_gate: real_load_gold(path, release_gate=False),
    )
    state = tmp_path / "capture-state"
    state.mkdir()
    reranker_client = _FakeRerankerClient()
    reranker_lane = RerankerShadowLane(
        reranker_client,  # type: ignore[arg-type]
        RerankerShadowStore(
            state,
            maximum_jobs=10,
            maximum_total_bytes=8 * 1024 * 1024,
            maximum_artifact_bytes=1024 * 1024,
        ),
        maximum_candidates=64,
    )
    output = tmp_path / "output"

    first = await capture_native_v5_lanes(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        score_artifact_path=score_bundle.score_artifact_path,
        score_corpus_inventory_path=score_bundle.corpus_inventory_path,
        score_matrix_path=score_bundle.score_matrix_path,
        score_query_vector_matrix_path=score_bundle.query_vector_matrix_path,
        expected_score_artifact_sha256=score_bundle.score_artifact_binding.sha256,
        answer_artifact_path=None,
        expected_answer_artifact_sha256=None,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=handle.object_root,
        output_directory=output,
        state_directory=state,
        source_commit=source_commit,
        embedder=embedder,
        reranker_lane=reranker_lane,
        expected_source_commit=source_commit,
        release_gate=True,
    )
    calls_after_first = len(embedder.calls)
    second = await capture_native_v5_lanes(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        score_artifact_path=score_bundle.score_artifact_path,
        score_corpus_inventory_path=score_bundle.corpus_inventory_path,
        score_matrix_path=score_bundle.score_matrix_path,
        score_query_vector_matrix_path=score_bundle.query_vector_matrix_path,
        expected_score_artifact_sha256=score_bundle.score_artifact_binding.sha256,
        answer_artifact_path=None,
        expected_answer_artifact_sha256=None,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=handle.object_root,
        output_directory=output,
        state_directory=state,
        source_commit=source_commit,
        embedder=embedder,
        reranker_lane=reranker_lane,
        expected_source_commit=source_commit,
        release_gate=True,
    )

    assert first.resumed_queries == 0
    assert second.resumed_queries == 1
    assert len(embedder.calls) == calls_after_first
    assert reranker_client.calls == 1
    native_attestation = [
        json.loads(line) for line in first.attestation_path.read_bytes().splitlines()
    ]
    assert (
        native_attestation[1]["raw_expected_embedding_rows"]
        > native_attestation[1]["expected_embedding_rows"]
    )
    receipts = validate_native_v5_capture(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        score_artifact_path=score_bundle.score_artifact_path,
        score_corpus_inventory_path=score_bundle.corpus_inventory_path,
        score_matrix_path=score_bundle.score_matrix_path,
        score_query_vector_matrix_path=score_bundle.query_vector_matrix_path,
        answer_artifact_path=None,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=handle.object_root,
        attestation_path=first.attestation_path,
        run_paths=first.run_paths,
        receipt_paths=first.receipt_paths,
        reranker_state_root=state,
        expected_source_commit=source_commit,
        release_gate=True,
    )
    assert set(receipts) == {"qwen_structure_exact", "lexical_shadow", "reranker_shadow"}
    assert all(not receipt.release_eligible for receipt in receipts.values())

    dummy_binding = ArtifactBinding(sha256="f" * 64, size_bytes=1)
    answer_evidence = capture_module.AnswerEvidenceBindings(
        schema_version="cardrag.gold-answer-evidence-bindings.v1",
        lane="qwen_structure_exact",
        answer_profile_id="sealed-answer-profile",
        capture_input=dummy_binding,
        producer_receipt=dummy_binding,
        answer_artifact=answer_binding,
        call_ledger=dummy_binding,
        state_identity=dummy_binding,
        answer_state_bundle=dummy_binding,
        decision_artifact=None,
        retrieval_run=dummy_binding,
        retrieval_capture_receipt=dummy_binding,
        retrieval_attestation_artifact=dummy_binding,
        retrieval_raw_score_artifact=dummy_binding,
        retrieval_corpus_inventory=dummy_binding,
        retrieval_dense_score_matrix=dummy_binding,
        retrieval_query_vector_matrix=dummy_binding,
        retrieval_lexical_rank_artifact=None,
    )
    answer_artifacts = AnswerEvidenceArtifacts(
        generation_manifest_path=tmp_path / "manifest.json",
        database_path=fixture.database,
        input_path=answer_path,
        expected_input_sha256=answer_binding.sha256,
        producer_receipt_path=answer_path,
        expected_producer_receipt_sha256=answer_binding.sha256,
        answer_artifact_path=answer_path,
        expected_answer_artifact_sha256=answer_binding.sha256,
        call_ledger_path=answer_path,
        state_identity_path=answer_path,
        state_bundle_path=answer_path,
        answer_profile_id="sealed-answer-profile",
        retrieval_run_path=answer_path,
        expected_retrieval_run_sha256=answer_binding.sha256,
        retrieval_capture_receipt_path=answer_path,
        expected_retrieval_capture_receipt_sha256=answer_binding.sha256,
        retrieval_attestation_path=answer_path,
        expected_retrieval_attestation_sha256=answer_binding.sha256,
        retrieval_raw_score_path=answer_path,
        expected_retrieval_raw_score_sha256=answer_binding.sha256,
        retrieval_corpus_inventory_path=answer_path,
        expected_retrieval_corpus_inventory_sha256=answer_binding.sha256,
        retrieval_dense_score_matrix_path=answer_path,
        expected_retrieval_dense_score_matrix_sha256=answer_binding.sha256,
        retrieval_query_vector_matrix_path=answer_path,
        expected_retrieval_query_vector_matrix_sha256=answer_binding.sha256,
    )
    monkeypatch.setattr(
        capture_module,
        "_verify_answer_evidence",
        lambda **_kwargs: answer_evidence,
    )

    def forbidden_provider(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("native finalization must not construct a provider client")

    monkeypatch.setattr(capture_module, "OpenRouterEmbedder", forbidden_provider)
    monkeypatch.setattr(capture_module, "OpenRouterReranker", forbidden_provider)
    embedder_calls_before_finalize = len(embedder.calls)
    reranker_calls_before_finalize = reranker_client.calls
    finalized = finalize_native_v5_with_answers(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        score_artifact_path=score_bundle.score_artifact_path,
        score_corpus_inventory_path=score_bundle.corpus_inventory_path,
        score_matrix_path=score_bundle.score_matrix_path,
        score_query_vector_matrix_path=score_bundle.query_vector_matrix_path,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=handle.object_root,
        bootstrap_attestation_path=first.attestation_path,
        bootstrap_run_paths=first.run_paths,
        bootstrap_receipt_paths=first.receipt_paths,
        expected_bootstrap_receipt_sha256={
            lane: hashlib.sha256(path.read_bytes()).hexdigest()
            for lane, path in first.receipt_paths.items()
        },
        reranker_state_root=state,
        answer_evidence_artifacts=answer_artifacts,
        output_directory=tmp_path / "finalized-output",
        expected_source_commit=source_commit,
    )
    assert len(embedder.calls) == embedder_calls_before_finalize
    assert reranker_client.calls == reranker_calls_before_finalize
    assert all(
        json.loads(path.read_bytes())["capture_phase"] == "final_release"
        for path in finalized.receipt_paths.values()
    )

    with pytest.raises(GoldCaptureError, match="candidate_source_commit_mismatch"):
        validate_native_v5_capture(
            gold_path=gold_path,
            expected_gold_sha256=gold_binding.sha256,
            score_artifact_path=score_bundle.score_artifact_path,
            score_corpus_inventory_path=score_bundle.corpus_inventory_path,
            score_matrix_path=score_bundle.score_matrix_path,
            score_query_vector_matrix_path=score_bundle.query_vector_matrix_path,
            answer_artifact_path=None,
            generation_manifest_path=tmp_path / "manifest.json",
            generation_directory=fixture.database.parent,
            object_root=handle.object_root,
            attestation_path=first.attestation_path,
            run_paths=first.run_paths,
            receipt_paths=first.receipt_paths,
            reranker_state_root=state,
            expected_source_commit="2" * 40,
            release_gate=True,
        )

    shard = state / "query-000.json"
    raw = json.loads(shard.read_bytes())
    raw["attestation"]["raw_score_query_binding_sha256"] = "0" * 64
    shard.chmod(0o600)
    shard.write_bytes(_canonical(raw))
    with pytest.raises(GoldCaptureError, match="native_capture_resume_shard_mismatch"):
        await capture_native_v5_lanes(
            gold_path=gold_path,
            expected_gold_sha256=gold_binding.sha256,
            score_artifact_path=score_bundle.score_artifact_path,
            score_corpus_inventory_path=score_bundle.corpus_inventory_path,
            score_matrix_path=score_bundle.score_matrix_path,
            score_query_vector_matrix_path=score_bundle.query_vector_matrix_path,
            expected_score_artifact_sha256=score_bundle.score_artifact_binding.sha256,
            answer_artifact_path=None,
            expected_answer_artifact_sha256=None,
            generation_manifest_path=tmp_path / "manifest.json",
            generation_directory=fixture.database.parent,
            object_root=handle.object_root,
            output_directory=output,
            state_directory=state,
            source_commit=source_commit,
            embedder=embedder,
            reranker_lane=reranker_lane,
            expected_source_commit=source_commit,
            release_gate=True,
        )


def test_capture_set_revalidates_all_attestations_and_complete_query_coverage(
    tmp_path: Path,
) -> None:
    gold_path = tmp_path / "set-gold.jsonl"
    gold_binding = _write_jsonl(gold_path, [_gold_record("contract-a", "span-a")])
    query_sha256 = hashlib.sha256("혜택".encode()).hexdigest()
    answer = EvaluatedAnswer(
        text="봉인 답변",
        no_answer=False,
        citation_span_ids=("span-a",),
        numeric_facts=(),
        selected_revision_ids=("contract-a",),
    )
    contract = RetrievedContract(contract_revision_id="contract-a", rank=1, score=1.0)
    span = RetrievedSpan(
        span_id="span-a",
        contract_revision_id="contract-a",
        rank=1,
        score=1.0,
    )
    dense_trace = V109BaselineObservation(
        kind="v109_small_rrf",
        rrf_k=60,
        dense_contracts=(contract,),
        dense_spans=(span,),
    )
    results = {
        "v109_baseline": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="v109_baseline",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
            v109_baseline=dense_trace,
        ),
        "qwen_page": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="qwen_page",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
        ),
        "qwen_structure_exact": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="qwen_structure_exact",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
        ),
        "lexical_shadow": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="lexical_shadow",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
            shadow=ShadowObservation(
                kind="lexical",
                influenced_primary_ordering=False,
                contracts=(contract,),
                spans=(span,),
            ),
        ),
        "reranker_shadow": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="reranker_shadow",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
            shadow=ShadowObservation(
                kind="reranker",
                influenced_primary_ordering=False,
                contracts=(contract,),
                spans=(span,),
            ),
        ),
    }
    external_bindings = {
        "v109_baseline": {
            "generation": ArtifactBinding(sha256="1" * 64, size_bytes=1),
            "database": ArtifactBinding(sha256="2" * 64, size_bytes=1),
            "vector": None,
            "generation_id": "v109-generation",
        },
        "qwen_page": {
            "generation": ArtifactBinding(sha256="3" * 64, size_bytes=1),
            "database": ArtifactBinding(sha256="4" * 64, size_bytes=1),
            "vector": ArtifactBinding(sha256="5" * 64, size_bytes=1),
            "generation_id": "qwen-page-generation",
        },
    }
    attestation_paths: dict[str, Path] = {}
    attestation_bindings: dict[str, ArtifactBinding] = {}
    external_inventory_paths: dict[str, Path] = {}
    external_inventory_bindings: dict[str, ArtifactBinding] = {}
    external_score_paths: dict[str, Path] = {}
    external_query_vector_paths: dict[str, Path] = {}
    external_lexical_paths: dict[str, Path] = {}
    external_sidecars: dict[
        str,
        tuple[ArtifactBinding, ArtifactBinding, ArtifactBinding | None],
    ] = {}
    for lane in ("v109_baseline", "qwen_page"):
        binding = external_bindings[lane]
        dimension = 1536 if lane == "v109_baseline" else 4096
        query_vector = np.zeros((dimension,), dtype="<f4")
        query_vector[0] = 1.0
        dense_payload = np.asarray((1.0,), dtype="<f4").tobytes()
        dense_path = tmp_path / f"{lane}.scores.f32"
        dense_path.write_bytes(dense_payload)
        dense_binding = ArtifactBinding(
            sha256=hashlib.sha256(dense_payload).hexdigest(),
            size_bytes=len(dense_payload),
        )
        vector_payload = query_vector.tobytes()
        vector_path = tmp_path / f"{lane}.query-vectors.f32"
        vector_path.write_bytes(vector_payload)
        vector_binding = ArtifactBinding(
            sha256=hashlib.sha256(vector_payload).hexdigest(),
            size_bytes=len(vector_payload),
        )
        lexical_payload = (
            _canonical(
                {
                    "ordinal": 0,
                    "query_id": "gold-001",
                    "ranks": [[0, 1]],
                    "schema_version": "cardrag.gold-lexical-ranks.v1",
                }
            )
            + b"\n"
            if lane == "v109_baseline"
            else None
        )
        lexical_path = tmp_path / f"{lane}.lexical.jsonl"
        lexical_binding = None
        if lexical_payload is not None:
            lexical_path.write_bytes(lexical_payload)
            lexical_binding = ArtifactBinding(
                sha256=hashlib.sha256(lexical_payload).hexdigest(),
                size_bytes=len(lexical_payload),
            )
            external_lexical_paths[lane] = lexical_path
        external_score_paths[lane] = dense_path
        external_query_vector_paths[lane] = vector_path
        external_sidecars[lane] = (dense_binding, vector_binding, lexical_binding)
        inventory_path = tmp_path / f"{lane}.corpus.jsonl"
        inventory_binding = _write_jsonl(
            inventory_path,
            [
                CorpusInventoryManifest(
                    schema_version="cardrag.gold-corpus-inventory.v1",
                    lane=lane,  # type: ignore[arg-type]
                    generation_id=str(binding["generation_id"]),
                    serving_database_sha256=binding["database"].sha256,
                    vector_artifact_sha256=(
                        None if binding["vector"] is None else binding["vector"].sha256
                    ),
                    embedding_dimension=dimension,  # type: ignore[arg-type]
                    row_count=1,
                ).model_dump(mode="json"),
                CorpusInventoryRow(
                    schema_version="cardrag.gold-corpus-row.v1",
                    row_index=0,
                    evidence_id="span-a",
                    contract_revision_id="contract-a",
                    span_id="span-a",
                    input_sha256="8" * 64,
                    embedding_f32_sha256=hashlib.sha256(
                        np.asarray((1.0, *([0.0] * (dimension - 1))), dtype="<f4").tobytes()
                    ).hexdigest(),
                ).model_dump(mode="json"),
            ],
        )
        external_inventory_paths[lane] = inventory_path
        external_inventory_bindings[lane] = inventory_binding
        manifest = ExternalObservationManifest(
            schema_version="cardrag.gold-external-observation-artifact.v2",
            lane=lane,  # type: ignore[arg-type]
            capture_mode="external_reproducible",
            synthetic=False,
            gold_sha256=gold_binding.sha256,
            query_count=1,
            source_version="v1.0.9" if lane == "v109_baseline" else "v1.0.10-candidate",
            source_commit=(
                "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113" if lane == "v109_baseline" else "a" * 40
            ),
            generation_id=str(binding["generation_id"]),
            generation_manifest=binding["generation"],  # type: ignore[arg-type]
            serving_schema=(
                "cardrag.serving-db.v4" if lane == "v109_baseline" else "cardrag.evaluation-page.v1"
            ),
            serving_database=binding["database"],  # type: ignore[arg-type]
            vector_artifact=binding["vector"],  # type: ignore[arg-type]
            embedding_model=(
                "openai/text-embedding-3-small"
                if lane == "v109_baseline"
                else "qwen/qwen3-embedding-8b"
            ),
            embedding_dimension=dimension,  # type: ignore[arg-type]
            embedding_profile_id=f"{lane}-profile",
            retrieval_policy="small_rrf" if lane == "v109_baseline" else "qwen_page_window",
            maximum_candidates=250 if lane == "v109_baseline" else None,
            scoring_contract=(
                "cardrag.v109-small-dense-rrf-capture.v1"
                if lane == "v109_baseline"
                else "cardrag.qwen-page-exact-capture.v1"
            ),
            row_count=1,
            corpus_inventory_sha256=inventory_binding.sha256,
            dense_score_matrix=dense_binding,
            query_vector_matrix=vector_binding,
            lexical_rank_artifact=lexical_binding,
            byte_order="little-endian",
            scalar_type="float32",
            matrix_order="row-major",
            maximum_result_contracts=100,
            maximum_result_spans=100,
            maximum_dense_trace_contracts=100,
            maximum_dense_trace_spans=250,
            approximate=False,
        )
        observation = ExternalQueryObservation(
            schema_version="cardrag.gold-external-query-observation.v2",
            ordinal=0,
            lane=lane,  # type: ignore[arg-type]
            query_id="gold-001",
            query_sha256=query_sha256,
            dense_offset_bytes=0,
            dense_size_bytes=len(dense_payload),
            dense_count=1,
            dense_sha256=hashlib.sha256(dense_payload).hexdigest(),
            vector_offset_bytes=0,
            vector_size_bytes=len(vector_payload),
            vector_count=dimension,  # type: ignore[arg-type]
            vector_sha256=hashlib.sha256(vector_payload).hexdigest(),
            lexical_offset_bytes=0 if lexical_payload is not None else None,
            lexical_size_bytes=None if lexical_payload is None else len(lexical_payload),
            lexical_count=1 if lexical_payload is not None else None,
            lexical_sha256=(
                None if lexical_payload is None else hashlib.sha256(lexical_payload).hexdigest()
            ),
            result=results[lane],
        )
        path = tmp_path / f"{lane}.capture-attestation.jsonl"
        attestation_paths[lane] = path
        attestation_bindings[lane] = _write_jsonl(
            path,
            [manifest.model_dump(mode="json"), observation.model_dump(mode="json")],
        )

    native_query_vector = np.zeros((4096,), dtype="<f4")
    native_query_vector[0] = 1.0
    native_score_bundle = _write_native_score_bundle(
        tmp_path,
        prefix="capture-set-native",
        gold_sha256=gold_binding.sha256,
        query_id="gold-001",
        query_sha256=query_sha256,
        query_vector=native_query_vector,
        inventory_rows=[
            ScoreCorpusInventoryRow(
                schema_version="cardrag.document-aggregation-corpus-row.v1",
                ordinal=0,
                row_index=0,
                contract_revision_id="contract-a",
                node_id="span-a",
                view_type="RAW_ITEM",
                input_sha256="8" * 64,
                embedding_profile_id="qwen-structure-profile",
            )
        ],
        scores=[1.0],
        active_contracts=1,
        source_commit="a" * 40,
        generation_id="native-generation",
        generation_manifest_sha256="b" * 64,
        serving_database_sha256="c" * 64,
        vector_sidecar_sha256="d" * 64,
        exact_row_corpus_sha256="e" * 64,
        embedding_profile_id="qwen-structure-profile",
        runtime_document_aggregation_status="candidate_default",
        runtime_document_aggregation_policy="max_child",
        runtime_sealed_profile_sha256=None,
        validation_profile="fixture_only",
    )
    raw_score_query_binding_sha256 = capture_module.canonical_sha256(
        {
            "corpus_inventory_sha256": native_score_bundle.corpus_inventory_binding.sha256,
            "score_sha256": native_score_bundle.coverage.score_sha256,
        }
    )
    native_manifest = NativeV5AttestationManifest(
        schema_version="cardrag.gold-native-v5-attestation.v2",
        capture_phase="bootstrap_retrieval",
        validation_profile="fixture_only",
        capture_mode="native_v5",
        synthetic=False,
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_commit="a" * 40,
        generation_id="native-generation",
        generation_manifest=ArtifactBinding(sha256="b" * 64, size_bytes=1),
        serving_database=ArtifactBinding(sha256="c" * 64, size_bytes=1),
        vector_sidecar=ArtifactBinding(sha256="d" * 64, size_bytes=1),
        exact_row_corpus_sha256="e" * 64,
        embedding_profile_id="qwen-structure-profile",
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        score_artifact=native_score_bundle.score_artifact_binding,
        score_corpus_inventory=native_score_bundle.corpus_inventory_binding,
        score_matrix=native_score_bundle.score_matrix_binding,
        score_query_vector_matrix=native_score_bundle.query_vector_matrix_binding,
        answer_artifact=None,
        answer_evidence=None,
        raw_score_api="cardrag_mcp.exact.V5ExactRepository.capture_unscoped_current_scores",
        exact_api="cardrag_mcp.exact.V5ExactRepository.search",
        lexical_api="cardrag_mcp.exact.V5ExactRepository.search.lexical_shadow",
        reranker_api="cardrag_mcp.reranker.RerankerShadowLane.observe",
        reranker_model="qwen/qwen3-reranker-8b",
    )
    native_query = NativeV5QueryAttestation(
        schema_version="cardrag.gold-native-v5-query-attestation.v2",
        query_id="gold-001",
        query_sha256=query_sha256,
        query_vector_sha256=native_score_bundle.coverage.query_vector_sha256,
        raw_score_query_binding_sha256=raw_score_query_binding_sha256,
        raw_expected_embedding_rows=1,
        raw_scored_embedding_rows=1,
        raw_active_contracts=1,
        expected_embedding_rows=1,
        scored_embedding_rows=1,
        expected_active_contracts=1,
        scored_contracts=1,
        exact_blocks=1,
        exact_response_sha256="2" * 64,
        lexical_status="succeeded",
        lexical_additional_evidence_count=0,
        reranker_artifact_sha256="3" * 64,
        qwen_structure_exact_result_sha256=capture_module.canonical_sha256(
            results["qwen_structure_exact"]
        ),
        lexical_shadow_result_sha256=capture_module.canonical_sha256(results["lexical_shadow"]),
        reranker_shadow_result_sha256=capture_module.canonical_sha256(results["reranker_shadow"]),
    )
    native_attestation_path = tmp_path / "native-v5-attestation.jsonl"
    native_attestation_binding = _write_jsonl(
        native_attestation_path,
        [native_manifest.model_dump(mode="json"), native_query.model_dump(mode="json")],
    )
    for lane in ("qwen_structure_exact", "lexical_shadow", "reranker_shadow"):
        attestation_paths[lane] = native_attestation_path
        attestation_bindings[lane] = native_attestation_binding

    run_paths: dict[str, Path] = {}
    receipt_paths: dict[str, Path] = {}
    expected_receipts: dict[str, str] = {}
    profile = {
        "v109_baseline": ("cardrag.eval.v109-small-rrf.v1", "small_rrf"),
        "qwen_page": ("cardrag.eval.qwen-page.v1", "qwen_page_window"),
        "qwen_structure_exact": (
            "cardrag.eval.qwen-structure-exact.v1",
            "qwen_structure_exact",
        ),
        "lexical_shadow": (
            "cardrag.eval.lexical-shadow.v1",
            "qwen_structure_exact_lexical_shadow",
        ),
        "reranker_shadow": (
            "cardrag.eval.reranker-shadow.v1",
            "qwen_structure_exact_reranker_shadow",
        ),
    }
    for lane in capture_module.LANES:
        native = lane in capture_module.NATIVE_V5_LANES
        external = external_bindings.get(lane)
        generation_id = "native-generation" if native else str(external["generation_id"])
        generation_sha256 = "b" * 64 if native else external["generation"].sha256
        manifest = RunArtifactManifest(
            schema_version="cardrag.gold-run-artifact.v1",
            lane=lane,
            profile_id=profile[lane][0],  # type: ignore[arg-type]
            gold_sha256=gold_binding.sha256,
            query_count=1,
            source_version="v1.0.10-candidate" if lane != "v109_baseline" else "v1.0.9",
            source_commit=(
                "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113" if lane == "v109_baseline" else "a" * 40
            ),
            generation_id=generation_id,
            generation_manifest_sha256=generation_sha256,
            serving_schema=(
                "cardrag.serving-db.v5"
                if native
                else (
                    "cardrag.serving-db.v4"
                    if lane == "v109_baseline"
                    else "cardrag.evaluation-page.v1"
                )
            ),
            embedding_model=(
                "openai/text-embedding-3-small"
                if lane == "v109_baseline"
                else "qwen/qwen3-embedding-8b"
            ),
            embedding_dimension=1536 if lane == "v109_baseline" else 4096,
            retrieval_policy=profile[lane][1],  # type: ignore[arg-type]
            rrf_k=60 if lane == "v109_baseline" else None,
            shadow_only=lane in {"lexical_shadow", "reranker_shadow"},
            primary_lane=(
                "qwen_structure_exact" if lane in {"lexical_shadow", "reranker_shadow"} else None
            ),
            shadow_model="qwen/qwen3-reranker-8b" if lane == "reranker_shadow" else None,
        )
        run_path = tmp_path / f"{lane}.jsonl"
        run_binding = _write_jsonl(
            run_path,
            [manifest.model_dump(mode="json"), results[lane].model_dump(mode="json")],
        )
        receipt = LaneCaptureReceipt(
            schema_version="cardrag.gold-lane-capture-receipt.v2",
            lane=lane,
            capture_mode="native_v5" if native else "external_reproducible",
            capture_phase="bootstrap_retrieval",
            validation_profile="fixture_only",
            release_eligible=False,
            gold_sha256=gold_binding.sha256,
            query_count=1,
            run_artifact=run_binding,
            attestation_artifact=attestation_bindings[lane],
            source_generation_id=generation_id,
            source_generation_manifest_sha256=generation_sha256,
            source_database_sha256=("c" * 64 if native else external["database"].sha256),
            source_vector_sha256=(
                "d" * 64
                if native
                else (None if external["vector"] is None else external["vector"].sha256)
            ),
            raw_score_artifact_sha256=(
                native_score_bundle.score_artifact_binding.sha256
                if native
                else attestation_bindings[lane].sha256
            ),
            corpus_inventory=(
                native_score_bundle.corpus_inventory_binding
                if native
                else external_inventory_bindings[lane]
            ),
            dense_score_matrix=(
                native_score_bundle.score_matrix_binding if native else external_sidecars[lane][0]
            ),
            query_vector_matrix=(
                native_score_bundle.query_vector_matrix_binding
                if native
                else external_sidecars[lane][1]
            ),
            lexical_rank_artifact=(None if native else external_sidecars[lane][2]),
            answer_evidence=None,
        )
        receipt_path = tmp_path / f"{lane}.capture-receipt.json"
        receipt_path.write_bytes(receipt.canonical_bytes())
        run_paths[lane] = run_path
        receipt_paths[lane] = receipt_path
        expected_receipts[lane] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()

    set_receipt = validate_capture_set(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        run_paths=run_paths,  # type: ignore[arg-type]
        receipt_paths=receipt_paths,  # type: ignore[arg-type]
        attestation_paths=attestation_paths,  # type: ignore[arg-type]
        native_score_artifact_path=native_score_bundle.score_artifact_path,
        native_score_corpus_inventory_path=native_score_bundle.corpus_inventory_path,
        native_score_matrix_path=native_score_bundle.score_matrix_path,
        native_score_query_vector_matrix_path=native_score_bundle.query_vector_matrix_path,
        external_inventory_paths=external_inventory_paths,  # type: ignore[arg-type]
        external_dense_score_matrix_paths=external_score_paths,  # type: ignore[arg-type]
        external_query_vector_matrix_paths=external_query_vector_paths,  # type: ignore[arg-type]
        external_lexical_rank_paths=external_lexical_paths,  # type: ignore[arg-type]
        expected_receipt_sha256=expected_receipts,  # type: ignore[arg-type]
        output_path=tmp_path / "capture-set.json",
        expected_source_commit="a" * 40,
        release_gate=False,
    )
    assert tuple(receipt.lane for receipt in set_receipt.lanes) == capture_module.LANES

    with pytest.raises(GoldCaptureError, match="candidate_source_commit_mismatch"):
        validate_capture_set(
            gold_path=gold_path,
            expected_gold_sha256=gold_binding.sha256,
            run_paths=run_paths,  # type: ignore[arg-type]
            receipt_paths=receipt_paths,  # type: ignore[arg-type]
            attestation_paths=attestation_paths,  # type: ignore[arg-type]
            native_score_artifact_path=native_score_bundle.score_artifact_path,
            native_score_corpus_inventory_path=native_score_bundle.corpus_inventory_path,
            native_score_matrix_path=native_score_bundle.score_matrix_path,
            native_score_query_vector_matrix_path=native_score_bundle.query_vector_matrix_path,
            external_inventory_paths=external_inventory_paths,  # type: ignore[arg-type]
            external_dense_score_matrix_paths=external_score_paths,  # type: ignore[arg-type]
            external_query_vector_matrix_paths=external_query_vector_paths,  # type: ignore[arg-type]
            external_lexical_rank_paths=external_lexical_paths,  # type: ignore[arg-type]
            expected_receipt_sha256=expected_receipts,  # type: ignore[arg-type]
            expected_source_commit="b" * 40,
            release_gate=False,
        )

    unsafe_attestation = tmp_path / "qwen-page-attestation-link.jsonl"
    unsafe_attestation.symlink_to(attestation_paths["qwen_page"])
    attestation_paths["qwen_page"] = unsafe_attestation
    with pytest.raises(GoldCaptureError, match="external_release_attestation_not_regular"):
        validate_capture_set(
            gold_path=gold_path,
            expected_gold_sha256=gold_binding.sha256,
            run_paths=run_paths,  # type: ignore[arg-type]
            receipt_paths=receipt_paths,  # type: ignore[arg-type]
            attestation_paths=attestation_paths,  # type: ignore[arg-type]
            native_score_artifact_path=native_score_bundle.score_artifact_path,
            native_score_corpus_inventory_path=native_score_bundle.corpus_inventory_path,
            native_score_matrix_path=native_score_bundle.score_matrix_path,
            native_score_query_vector_matrix_path=native_score_bundle.query_vector_matrix_path,
            external_inventory_paths=external_inventory_paths,  # type: ignore[arg-type]
            external_dense_score_matrix_paths=external_score_paths,  # type: ignore[arg-type]
            external_query_vector_matrix_paths=external_query_vector_paths,  # type: ignore[arg-type]
            external_lexical_rank_paths=external_lexical_paths,  # type: ignore[arg-type]
            expected_receipt_sha256=expected_receipts,  # type: ignore[arg-type]
            expected_source_commit="a" * 40,
            release_gate=False,
        )
