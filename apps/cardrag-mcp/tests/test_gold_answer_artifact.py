from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from cardrag_core import (
    EMBEDDING_VIEW_TYPES,
    ArtifactRef,
    EmbeddingContract,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    IssuerOCRCounts,
    IssuerParserProfile,
    StructureContract,
    StructureMajorClassCounts,
    StructureNodeCounts,
    StructureRevisionCounts,
    StructureSourceCoverage,
    canonical_sha256,
    format_qwen3_document,
    format_qwen3_query,
    generation_database_path,
    generation_vectors_path,
    qwen3_embedding_profile_id,
    sha256_bytes,
)
from conftest import create_database
from pydantic import ValidationError
from v5_fixtures import V5Fixture, build_v5_fixture

import cardrag_mcp.external_gold_producer as external_producer
import cardrag_mcp.gold_answer_artifact as answer_producer
from cardrag_mcp.evaluation import (
    REQUIRED_RELEASE_SLICES,
    V109_BASELINE_COMMIT,
    EvaluatedAnswer,
    GoldDataset,
    GoldQuery,
    QueryRunResult,
    RetrievedContract,
    RetrievedSpan,
    RunArtifactManifest,
    load_gold_jsonl,
)
from cardrag_mcp.external_gold_producer import (
    EMBEDDING_REPLAY_ROW_SCHEMA,
    EMBEDDING_REPLAY_SCHEMA,
    EmbeddingReplayManifest,
    EmbeddingReplayRow,
    ProviderReceipt,
    ProviderResponseArtifact,
    build_qwen_page_corpus,
    produce_external_observation,
)
from cardrag_mcp.gold_answer_artifact import (
    DECISION_MANIFEST_SCHEMA,
    DECISION_SCHEMA,
    INPUT_MANIFEST_SCHEMA,
    INPUT_QUERY_SCHEMA,
    AnswerDecision,
    AnswerEvidence,
    AnswerInputManifest,
    AnswerInputQuery,
    AnswerProducerResult,
    DecisionArtifactManifest,
    DecisionArtifactRecord,
    GoldAnswerProducerError,
    QueryIdentity,
    StateBundleManifest,
    _answer_from_decision,
    _canonical_jsonl,
    _deterministic_decision,
    _query_identities,
    _ranking_projection_sha256,
    _verify_page_evidence,
    build_answer_input_artifact,
    build_answer_request,
    load_answer_producer_receipt,
    produce_answer_artifact,
    verify_answer_input_ranking,
    verify_answer_producer_receipt,
    verify_answer_producer_receipt_portable,
)
from cardrag_mcp.gold_capture import (
    AnswerArtifactManifest,
    AnswerRecord,
    ArtifactBinding,
    LaneCaptureReceipt,
    PageGenerationManifest,
    _load_answers,
    seal_external_observation,
)

SOURCE_COMMIT = "1" * 40
ANSWER_PROFILE = "cardrag.answer.extractive-k8.v1"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@pytest.mark.parametrize("reader_kind", ("whole", "hash"))
def test_answer_readers_reject_same_inode_growth_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_kind: str,
) -> None:
    target = tmp_path / "race.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)
    original_inode = target.stat().st_ino
    original_open = answer_producer.os.open
    read_calls = 0
    raced = False

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if not raced and dir_fd is not None and os.fspath(path) == target.name:
            raced = True
            with target.open("ab") as stream:
                stream.write(payload)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def forbidden_read(descriptor: int, count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        raise AssertionError("read must not run after the opened-size preflight fails")

    monkeypatch.setattr(answer_producer.os, "open", racing_open)
    monkeypatch.setattr(answer_producer.os, "read", forbidden_read)
    with pytest.raises(GoldAnswerProducerError, match="fixture_size_invalid"):
        if reader_kind == "whole":
            answer_producer._read_regular(
                target,
                maximum_bytes=len(payload),
                code="fixture",
            )
        else:
            answer_producer._hash_regular(
                target,
                maximum_bytes=len(payload),
                code="fixture",
            )

    assert raced
    assert target.stat().st_ino == original_inode
    assert read_calls == 0


def test_answer_reader_rejects_fifo_substitution_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if not nonblocking or not hasattr(os, "mkfifo"):
        pytest.skip("FIFO nonblocking-open semantics require POSIX")
    target = tmp_path / "race.jsonl"
    target.write_bytes(_canonical({}) + b"\n")
    original_open = answer_producer.os.open
    swapped = False

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is not None and os.fspath(path) == target.name:
            assert flags & nonblocking
            target.unlink()
            os.mkfifo(target)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(answer_producer.os, "open", racing_open)
    with pytest.raises(GoldAnswerProducerError, match="fixture_not_regular"):
        answer_producer._read_regular(
            target,
            maximum_bytes=1024,
            code="fixture",
        )

    assert swapped
    assert target.is_fifo()


@pytest.mark.parametrize("reader_kind", ("whole", "hash"))
def test_answer_readers_reject_path_replacement_during_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_kind: str,
) -> None:
    target = tmp_path / "mutable.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)
    replacement.write_bytes(payload)
    original_read = answer_producer.os.read
    replaced = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        block = original_read(descriptor, count)
        if block and not replaced:
            replaced = True
            replacement.replace(target)
        return block

    monkeypatch.setattr(answer_producer.os, "read", racing_read)
    with pytest.raises(GoldAnswerProducerError, match="fixture_changed_during_read"):
        if reader_kind == "whole":
            answer_producer._read_regular(
                target,
                maximum_bytes=len(payload),
                code="fixture",
            )
        else:
            answer_producer._hash_regular(
                target,
                maximum_bytes=len(payload),
                code="fixture",
            )
    assert replaced


def test_answer_readers_accept_stable_two_link_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "artifact.jsonl"
    linked = tmp_path / "artifact-hardlink.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)
    os.link(target, linked)
    assert target.stat().st_nlink == linked.stat().st_nlink == 2

    assert (
        answer_producer._read_regular(
            linked,
            maximum_bytes=len(payload),
            code="fixture",
        )
        == payload
    )
    assert answer_producer._hash_regular(
        linked,
        maximum_bytes=len(payload),
        code="fixture",
    ) == ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def test_answer_sqlite_reader_rejects_growth_before_connect_and_accepts_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "serving.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE fixture(value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    payload = database.read_bytes()
    binding = ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    hardlink = tmp_path / "serving-hardlink.sqlite3"
    os.link(database, hardlink)
    assert database.stat().st_nlink == hardlink.stat().st_nlink == 2
    with answer_producer._sqlite_readonly(hardlink, expected=binding) as verified:
        assert verified.execute("SELECT count(*) FROM fixture").fetchone()[0] == 0

    original_inode = database.stat().st_ino
    original_open = answer_producer.os.open
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
        if not raced and dir_fd is not None and os.fspath(path) == database.name:
            raced = True
            with database.open("ab") as stream:
                stream.write(b"x")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def forbidden_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("SQLite must not open after the fd-size preflight fails")

    monkeypatch.setattr(answer_producer, "_MAX_DATABASE_BYTES", len(payload))
    monkeypatch.setattr(answer_producer.os, "open", racing_open)
    monkeypatch.setattr(answer_producer.sqlite3, "connect", forbidden_connect)
    with pytest.raises(GoldAnswerProducerError, match="serving_database_size_invalid"):
        with answer_producer._sqlite_readonly(database, expected=binding):
            pass

    assert raced
    assert database.stat().st_ino == original_inode
    assert connect_calls == 0


def test_answer_sqlite_reader_checks_current_path_when_body_raises(tmp_path: Path) -> None:
    database = tmp_path / "serving.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE fixture(value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    payload = database.read_bytes()
    binding = ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(payload)

    with pytest.raises(GoldAnswerProducerError, match="serving_database_changed_during_read"):
        with answer_producer._sqlite_readonly(database, expected=binding):
            replacement.replace(database)
            raise RuntimeError("consumer failed after replacing the pathname")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> ArtifactBinding:
    payload = b"".join(_canonical(record) + b"\n" for record in records)
    path.write_bytes(payload)
    return ArtifactBinding(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))


def _provider_receipt(
    path: Path,
    *,
    inputs: list[tuple[str, str, np.ndarray[Any, Any]]],
) -> ArtifactBinding:
    artifacts: list[ProviderResponseArtifact] = []
    requests = []
    for ordinal, offset in enumerate(range(0, len(inputs), 128)):
        batch = inputs[offset : offset + 128]
        raw_path = path.with_name(f"{path.stem}-response-{ordinal:03d}.json")
        response_body = _canonical(
            {
                "data": [
                    {"embedding": vector.tolist(), "index": index}
                    for index, (_input_id, _formatted, vector) in enumerate(batch)
                ],
                "model": "qwen/qwen3-embedding-8b",
            }
        )
        envelope = external_producer.EmbeddingRawResponseEnvelope(
            schema_version="cardrag.gold-embedding-provider-response.v1",
            status_code=200,
            provider_header="DeepInfra",
            body_sha256=hashlib.sha256(response_body).hexdigest(),
            body_size_bytes=len(response_body),
            body_base64=base64.b64encode(response_body).decode(),
        )
        raw_path.write_bytes(envelope.canonical_bytes())
        raw_binding = ArtifactBinding(
            sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            size_bytes=raw_path.stat().st_size,
        )
        artifacts.append(ProviderResponseArtifact(file_name=raw_path.name, artifact=raw_binding))
        requests.append(
            external_producer._provider_request_record(
                ordinal=ordinal,
                input_ids=tuple(row[0] for row in batch),
                request_body=external_producer._provider_request_body(
                    model="qwen/qwen3-embedding-8b",
                    provider_id="deepinfra",
                    formatted_inputs=tuple(row[1] for row in batch),
                ),
                response_file_name=raw_path.name,
            )
        )
    artifact_tuple = tuple(artifacts)
    request_tuple = tuple(requests)
    request_contract_sha256 = external_producer._provider_request_contract_sha256(
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        requests=request_tuple,
        input_count=len(inputs),
    )
    receipt = ProviderReceipt(
        schema_version="cardrag.gold-provider-receipt.v1",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        request_contract_sha256=request_contract_sha256,
        requests=request_tuple,
        response_artifact_sha256=canonical_sha256(
            {
                "artifacts": [row.model_dump(mode="json") for row in artifact_tuple],
                "schema_version": "cardrag.gold-provider-responses.v1",
            }
        ),
        response_artifacts=artifact_tuple,
        input_count=len(inputs),
    )
    path.write_bytes(receipt.canonical_bytes())
    return ArtifactBinding(
        sha256=hashlib.sha256(receipt.canonical_bytes()).hexdigest(),
        size_bytes=len(receipt.canonical_bytes()),
    )


def _embedding_replay(
    path: Path,
    *,
    input_kind: str,
    source_commit: str,
    profile_id: str,
    provider_receipt: ArtifactBinding,
    inputs: list[tuple[str, str, np.ndarray[Any, Any]]],
) -> ArtifactBinding:
    manifest = EmbeddingReplayManifest.model_validate(
        {
            "schema_version": EMBEDDING_REPLAY_SCHEMA,
            "lane": "qwen_page",
            "input_kind": input_kind,
            "synthetic": False,
            "source_commit": source_commit,
            "embedding_model": "qwen/qwen3-embedding-8b",
            "embedding_dimension": 4096,
            "embedding_profile_id": profile_id,
            "query_policy": "cardrag.qwen3-query.v1" if input_kind == "query" else None,
            "document_policy": (
                "cardrag.page-window-1600.v1" if input_kind == "document" else None
            ),
            "provider_receipt": provider_receipt.model_dump(mode="json"),
            "record_count": len(inputs),
        }
    )
    records: list[dict[str, Any]] = [manifest.model_dump(mode="json")]
    for ordinal, (input_id, formatted, vector) in enumerate(inputs):
        raw = vector.astype("<f4", copy=False).tobytes()
        records.append(
            EmbeddingReplayRow(
                schema_version=EMBEDDING_REPLAY_ROW_SCHEMA,
                ordinal=ordinal,
                input_id=input_id,
                formatted_input_sha256=hashlib.sha256(formatted.encode()).hexdigest(),
                vector_f32_sha256=hashlib.sha256(raw).hexdigest(),
                vector_f32_base64=base64.b64encode(raw).decode(),
            ).model_dump(mode="json")
        )
    return _write_jsonl(path, records)


def _unit_vector(index: int = 0) -> np.ndarray[Any, Any]:
    vector = np.zeros((4096,), dtype="<f4")
    vector[index] = 1.0
    return vector


def _manifest_for_fixture(fixture: V5Fixture) -> GenerationManifest:
    database_body = fixture.database.read_bytes()
    vector_body = fixture.vectors.read_bytes()
    database = ArtifactRef(
        sha256=sha256_bytes(database_body),
        size_bytes=len(database_body),
        media_type="application/vnd.sqlite3",
        path=generation_database_path(fixture.generation_id).as_posix(),
    )
    vector = ArtifactRef(
        sha256=sha256_bytes(vector_body),
        size_bytes=len(vector_body),
        media_type="application/octet-stream",
        path=generation_vectors_path(fixture.generation_id).as_posix(),
    )
    pdf_body = b"%PDF-1.4\nanswer-producer-fixture"
    ocr_body = b"fixture OCR"
    profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    source_hash = sha256_bytes(b"fixture source coverage")
    return GenerationManifest(
        schema_version="cardrag.generation.v5",
        generation_id=fixture.generation_id,
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
        serving_schema="cardrag.serving-db.v5",
        serving_database=database,
        corpus_sha256=sha256_bytes(b"fixture corpus"),
        contract_sha256=sha256_bytes(b"fixture contract"),
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="qwen/qwen3-embedding-8b",
            dimension=4096,
            count=fixture.vector_count,
        ),
        issuer_codes=("kb",),
        counts=GenerationCounts(
            documents=1,
            pdf_objects=1,
            ocr_objects=1,
            chunks=fixture.vector_count,
        ),
        documents=(
            GenerationDocument(
                document_id="doc_answer_fixture",
                issuer="kb",
                pdf=ArtifactRef.for_cas(
                    sha256=sha256_bytes(pdf_body),
                    size_bytes=len(pdf_body),
                    media_type="application/pdf",
                ),
                ocr=ArtifactRef.for_cas(
                    sha256=sha256_bytes(ocr_body),
                    size_bytes=len(ocr_body),
                    media_type="text/markdown; charset=utf-8",
                ),
                page_count=1,
                availability="available",
            ),
        ),
        issuer_ocr_counts=(IssuerOCRCounts(issuer="kb", acquired=1, succeeded=1, failed=0),),
        structure_contract=StructureContract(
            schema_version="cardrag.structure.v2",
            parser_profiles=(
                IssuerParserProfile(
                    issuer="kb",
                    profile_id="cardrag.parser.kb.answer-fixture.v1",
                    profile_sha256=sha256_bytes(b"parser profile"),
                ),
            ),
            node_counts=StructureNodeCounts(
                total=4,
                root=1,
                major_section=1,
                item=1,
                paragraph=1,
                list_item=0,
                table=0,
                table_row=0,
                footnote=0,
                boilerplate=0,
                unclassified=0,
            ),
            major_class_counts=StructureMajorClassCounts(
                total=1,
                benefit=1,
                notice=0,
                mixed=0,
                unknown=0,
            ),
            source_coverage=StructureSourceCoverage(
                source_non_whitespace_characters=10,
                covered_non_whitespace_characters=10,
                source_non_whitespace_sha256=source_hash,
                covered_non_whitespace_sha256=source_hash,
            ),
            revision_counts=StructureRevisionCounts(
                total=1,
                current=1,
                superseded=0,
                ambiguous=0,
            ),
            cross_contract_parent_count=0,
            cross_contract_link_count=0,
            lineages_with_multiple_current_revisions=0,
        ),
        embedding_profiles=(profile,),
        primary_embedding_profile_id=profile.profile_id,
        embedding_view_counts=tuple(
            EmbeddingViewCount(
                view_type=view_type,
                count=fixture.vector_count if view_type == "TITLE" else 0,
            )
            for view_type in EMBEDDING_VIEW_TYPES
        ),
        vector_sidecar=EmbeddingVectorSidecar(
            artifact=vector,
            profile_id=profile.profile_id,
            row_count=fixture.vector_count,
            dimension=4096,
            dtype="float32",
            byte_order="little-endian",
            layout="row-major",
            normalization="l2",
        ),
        parser_policy_sha256=sha256_bytes(b"parser policy"),
        embedding_policy_sha256=sha256_bytes(b"embedding policy"),
        retrieval_policy_sha256=sha256_bytes(b"retrieval policy"),
    )


def _gold_record(
    *,
    revision_id: str,
    span_id: str,
    question: str = "혜택은 무엇인가요?",
) -> dict[str, Any]:
    text = "공항 라운지 혜택\n"
    return {
        "condition_groups": [],
        "contracts": [{"contract_revision_id": revision_id, "relevance": 3}],
        "expected_numeric_facts": [],
        "expected_revision_ids": [],
        "high_risk": False,
        "no_answer": False,
        "query_id": "gold-001",
        "question": question,
        "schema_version": "cardrag.gold-query.v1",
        "slices": ["benefit"],
        "spans": [
            {
                "contract_revision_id": revision_id,
                "page": 1,
                "relevance": 3,
                "roles": ["benefit"],
                "source_end": len(text),
                "source_start": 0,
                "span_id": span_id,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        ],
    }


@dataclass(frozen=True)
class RetrievalFixture:
    run_path: Path
    run_binding: ArtifactBinding
    receipt_path: Path
    receipt_binding: ArtifactBinding
    attestation_path: Path
    attestation_binding: ArtifactBinding
    raw_score_path: Path
    raw_score_binding: ArtifactBinding
    corpus_inventory_path: Path
    corpus_inventory_binding: ArtifactBinding
    dense_score_matrix_path: Path
    dense_score_matrix_binding: ArtifactBinding
    query_vector_matrix_path: Path
    query_vector_matrix_binding: ArtifactBinding


@dataclass(frozen=True)
class ProducerFixture:
    v5: V5Fixture
    manifest: GenerationManifest
    manifest_path: Path
    gold_path: Path
    gold_binding: ArtifactBinding
    input_path: Path
    input_binding: ArtifactBinding
    input_manifest: AnswerInputManifest
    input_query: AnswerInputQuery
    retrieval: RetrievalFixture | None = None


def _producer_fixture(tmp_path: Path) -> ProducerFixture:
    v5 = build_v5_fixture(tmp_path / "generation", generation_id="gen-answer-fixture")
    manifest = _manifest_for_fixture(v5)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(manifest.canonical_bytes())
    node_id = f"{v5.current_revision_id}-paragraph"
    with sqlite3.connect(v5.database) as connection:
        source_text = str(
            connection.execute(
                """SELECT display_text FROM structure_nodes
                    WHERE node_id=? AND contract_revision_id=?""",
                (node_id, v5.current_revision_id),
            ).fetchone()[0]
        )
    gold_path = tmp_path / "gold.jsonl"
    gold_binding = _write_jsonl(
        gold_path,
        [_gold_record(revision_id=v5.current_revision_id, span_id=node_id)],
    )
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(v5.database.read_bytes()).hexdigest(),
        size_bytes=v5.database.stat().st_size,
    )
    input_manifest = AnswerInputManifest(
        schema_version=INPUT_MANIFEST_SCHEMA,
        lane="qwen_structure_exact",
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_commit=SOURCE_COMMIT,
        generation_id=v5.generation_id,
        generation_manifest_sha256=hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
        serving_schema="cardrag.serving-db.v5",
        serving_database=database_binding,
        answer_profile_id=ANSWER_PROFILE,
        maximum_answer_evidence_spans=8,
        rendering_contract="cardrag.extractive-source-blocks.v1",
        synthetic=False,
    )
    evidence = AnswerEvidence(
        span_id=node_id,
        contract_revision_id=v5.current_revision_id,
        rank=1,
        score=0.9,
        source_text=source_text,
        source_text_sha256=hashlib.sha256(source_text.encode()).hexdigest(),
    )
    input_query = AnswerInputQuery(
        schema_version=INPUT_QUERY_SCHEMA,
        query_id="gold-001",
        query_sha256=hashlib.sha256("혜택은 무엇인가요?".encode()).hexdigest(),
        contracts=(
            RetrievedContract(
                contract_revision_id=v5.current_revision_id,
                rank=1,
                score=0.9,
            ),
        ),
        evidence=(evidence,),
    )
    input_path = tmp_path / "retrieval.jsonl"
    input_binding = _write_jsonl(
        input_path,
        [input_manifest.model_dump(mode="json"), input_query.model_dump(mode="json")],
    )
    return ProducerFixture(
        v5=v5,
        manifest=manifest,
        manifest_path=manifest_path,
        gold_path=gold_path,
        gold_binding=gold_binding,
        input_path=input_path,
        input_binding=input_binding,
        input_manifest=input_manifest,
        input_query=input_query,
    )


def _bind_retrieval_capture(
    fixture: ProducerFixture,
    tmp_path: Path,
    *,
    release_eligible: bool = False,
) -> ProducerFixture:
    raw_inputs = fixture.input_path.read_bytes().splitlines()
    input_queries = tuple(AnswerInputQuery.model_validate_json(row) for row in raw_inputs[1:])
    results = tuple(
        QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id=query.query_id,
            lane="qwen_structure_exact",
            contracts=query.contracts,
            spans=tuple(
                RetrievedSpan(
                    span_id=row.span_id,
                    contract_revision_id=row.contract_revision_id,
                    rank=row.rank,
                    score=row.score,
                )
                for row in query.evidence
            ),
            answer=EvaluatedAnswer(
                text="capture bootstrap",
                no_answer=True,
            ),
        )
        for query in input_queries
    )
    run_manifest = RunArtifactManifest(
        schema_version="cardrag.gold-run-artifact.v1",
        lane="qwen_structure_exact",
        profile_id="cardrag.eval.qwen-structure-exact.v1",
        gold_sha256=fixture.gold_binding.sha256,
        query_count=len(results),
        source_version="v1.0.11-candidate",
        source_commit=SOURCE_COMMIT,
        generation_id=fixture.v5.generation_id,
        generation_manifest_sha256=hashlib.sha256(fixture.manifest.canonical_bytes()).hexdigest(),
        serving_schema="cardrag.serving-db.v5",
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        retrieval_policy="qwen_structure_exact",
        rrf_k=None,
        shadow_only=False,
        primary_lane=None,
        shadow_model=None,
    )
    run_path = tmp_path / "sealed-run.jsonl"
    run_binding = _write_jsonl(
        run_path,
        [
            run_manifest.model_dump(mode="json"),
            *(result.model_dump(mode="json") for result in results),
        ],
    )
    attestation_path = tmp_path / "sealed-attestation.jsonl"
    attestation_binding = _write_jsonl(
        attestation_path,
        [{"schema_version": "fixture-native-attestation.v1"}],
    )
    raw_score_path = tmp_path / "sealed-raw-scores.jsonl"
    raw_score_binding = _write_jsonl(
        raw_score_path,
        [{"schema_version": "fixture-native-score.v1"}],
    )
    corpus_inventory_path = tmp_path / "sealed-corpus-inventory.jsonl"
    corpus_inventory_binding = _write_jsonl(
        corpus_inventory_path,
        [
            {
                "query_count": len(results),
                "row_count": fixture.v5.vector_count,
                "schema_version": "fixture-native-corpus-inventory.v1",
            },
            *(
                {
                    "contract_revision_id": fixture.v5.current_revision_id,
                    "row_index": row_index,
                    "schema_version": "fixture-native-corpus-row.v1",
                }
                for row_index in range(fixture.v5.vector_count)
            ),
        ],
    )
    dense_score_matrix_path = tmp_path / "sealed-dense-scores.f32"
    dense_score_matrix_body = np.ones(
        (len(results), fixture.v5.vector_count),
        dtype="<f4",
    ).tobytes()
    dense_score_matrix_path.write_bytes(dense_score_matrix_body)
    dense_score_matrix_binding = ArtifactBinding(
        sha256=hashlib.sha256(dense_score_matrix_body).hexdigest(),
        size_bytes=len(dense_score_matrix_body),
    )
    query_vector_matrix_path = tmp_path / "sealed-query-vectors.f32"
    query_vector_matrix_body = np.zeros((len(results), 4096), dtype="<f4").tobytes()
    query_vector_matrix_path.write_bytes(query_vector_matrix_body)
    query_vector_matrix_binding = ArtifactBinding(
        sha256=hashlib.sha256(query_vector_matrix_body).hexdigest(),
        size_bytes=len(query_vector_matrix_body),
    )
    assert fixture.manifest.vector_sidecar is not None
    receipt = LaneCaptureReceipt(
        schema_version="cardrag.gold-lane-capture-receipt.v2",
        lane="qwen_structure_exact",
        capture_mode="native_v5",
        capture_phase="bootstrap_retrieval",
        validation_profile="release_grade",
        release_eligible=release_eligible,
        gold_sha256=fixture.gold_binding.sha256,
        query_count=len(results),
        run_artifact=run_binding,
        attestation_artifact=attestation_binding,
        source_generation_id=fixture.v5.generation_id,
        source_generation_manifest_sha256=hashlib.sha256(
            fixture.manifest.canonical_bytes()
        ).hexdigest(),
        source_database_sha256=fixture.input_manifest.serving_database.sha256,
        source_vector_sha256=fixture.manifest.vector_sidecar.artifact.sha256,
        raw_score_artifact_sha256=raw_score_binding.sha256,
        corpus_inventory=corpus_inventory_binding,
        dense_score_matrix=dense_score_matrix_binding,
        query_vector_matrix=query_vector_matrix_binding,
        answer_evidence=None,
    )
    receipt_path = tmp_path / "sealed-capture-receipt.json"
    receipt_path.write_bytes(receipt.canonical_bytes())
    receipt_binding = ArtifactBinding(
        sha256=hashlib.sha256(receipt.canonical_bytes()).hexdigest(),
        size_bytes=len(receipt.canonical_bytes()),
    )
    input_manifest = fixture.input_manifest.model_copy(
        update={
            "retrieval_contract": "cardrag.gold-run-ranking-projection.v1",
            "retrieval_capture_phase": "bootstrap_retrieval",
            "retrieval_run": run_binding,
            "retrieval_capture_receipt": receipt_binding,
            "retrieval_attestation_artifact": attestation_binding,
            "retrieval_raw_score_artifact": raw_score_binding,
            "retrieval_corpus_inventory": corpus_inventory_binding,
            "retrieval_dense_score_matrix": dense_score_matrix_binding,
            "retrieval_query_vector_matrix": query_vector_matrix_binding,
        }
    )
    bound_queries = tuple(
        query.model_copy(update={"retrieval_ranking_sha256": _ranking_projection_sha256(result)})
        for query, result in zip(input_queries, results, strict=True)
    )
    input_binding = _write_jsonl(
        fixture.input_path,
        [
            input_manifest.model_dump(mode="json"),
            *(query.model_dump(mode="json") for query in bound_queries),
        ],
    )
    return replace(
        fixture,
        input_manifest=input_manifest,
        input_query=bound_queries[0],
        input_binding=input_binding,
        retrieval=RetrievalFixture(
            run_path=run_path,
            run_binding=run_binding,
            receipt_path=receipt_path,
            receipt_binding=receipt_binding,
            attestation_path=attestation_path,
            attestation_binding=attestation_binding,
            raw_score_path=raw_score_path,
            raw_score_binding=raw_score_binding,
            corpus_inventory_path=corpus_inventory_path,
            corpus_inventory_binding=corpus_inventory_binding,
            dense_score_matrix_path=dense_score_matrix_path,
            dense_score_matrix_binding=dense_score_matrix_binding,
            query_vector_matrix_path=query_vector_matrix_path,
            query_vector_matrix_binding=query_vector_matrix_binding,
        ),
    )


def _produce(
    fixture: ProducerFixture,
    tmp_path: Path,
    **overrides: Any,
) -> AnswerProducerResult:
    arguments: dict[str, Any] = {
        "gold_path": fixture.gold_path,
        "expected_gold_sha256": fixture.gold_binding.sha256,
        "input_path": fixture.input_path,
        "expected_input_sha256": fixture.input_binding.sha256,
        "generation_manifest_path": fixture.manifest_path,
        "database_path": fixture.v5.database,
        "state_directory": tmp_path / "answer-state",
        "answer_path": tmp_path / "answers.jsonl",
        "ledger_path": tmp_path / "answer-calls.jsonl",
        "receipt_path": tmp_path / "answer-receipt.json",
        "expected_source_commit": SOURCE_COMMIT,
        "expected_answer_profile_id": ANSWER_PROFILE,
        "deterministic": True,
        "maximum_provider_calls": 0,
        "release_gate": False,
    }
    if fixture.retrieval is not None:
        arguments.update(
            {
                "retrieval_run_path": fixture.retrieval.run_path,
                "expected_retrieval_run_sha256": fixture.retrieval.run_binding.sha256,
                "retrieval_capture_receipt_path": fixture.retrieval.receipt_path,
                "expected_retrieval_capture_receipt_sha256": (
                    fixture.retrieval.receipt_binding.sha256
                ),
                "retrieval_attestation_path": fixture.retrieval.attestation_path,
                "expected_retrieval_attestation_sha256": (
                    fixture.retrieval.attestation_binding.sha256
                ),
                "retrieval_raw_score_path": fixture.retrieval.raw_score_path,
                "expected_retrieval_raw_score_sha256": (fixture.retrieval.raw_score_binding.sha256),
                "retrieval_corpus_inventory_path": fixture.retrieval.corpus_inventory_path,
                "expected_retrieval_corpus_inventory_sha256": (
                    fixture.retrieval.corpus_inventory_binding.sha256
                ),
                "retrieval_dense_score_matrix_path": fixture.retrieval.dense_score_matrix_path,
                "expected_retrieval_dense_score_matrix_sha256": (
                    fixture.retrieval.dense_score_matrix_binding.sha256
                ),
                "retrieval_query_vector_matrix_path": fixture.retrieval.query_vector_matrix_path,
                "expected_retrieval_query_vector_matrix_sha256": (
                    fixture.retrieval.query_vector_matrix_binding.sha256
                ),
            }
        )
    arguments.update(overrides)
    return produce_answer_artifact(**arguments)


def _with_queries(
    fixture: ProducerFixture,
    *,
    gold_records: list[dict[str, Any]],
) -> ProducerFixture:
    gold_binding = _write_jsonl(fixture.gold_path, gold_records)
    input_manifest = fixture.input_manifest.model_copy(
        update={"gold_sha256": gold_binding.sha256, "query_count": len(gold_records)}
    )
    input_queries = [
        fixture.input_query.model_copy(
            update={
                "query_id": str(record["query_id"]),
                "query_sha256": hashlib.sha256(str(record["question"]).encode()).hexdigest(),
            }
        )
        for record in gold_records
    ]
    input_binding = _write_jsonl(
        fixture.input_path,
        [
            input_manifest.model_dump(mode="json"),
            *(query.model_dump(mode="json") for query in input_queries),
        ],
    )
    return replace(
        fixture,
        gold_binding=gold_binding,
        input_binding=input_binding,
        input_manifest=input_manifest,
        input_query=input_queries[0],
    )


def _release_gold_records(fixture: ProducerFixture) -> list[dict[str, Any]]:
    required_answer_slices = sorted(REQUIRED_RELEASE_SLICES - {"no_answer"})
    records: list[dict[str, Any]] = []
    for index in range(300):
        record = _gold_record(
            revision_id=fixture.v5.current_revision_id,
            span_id=f"sealed-label-{index}",
            question=f"혜택은 무엇인가요? {index}",
        )
        record["query_id"] = f"gold-{index:03d}"
        if index == 0:
            record["slices"] = required_answer_slices
            record["high_risk"] = True
            record["expected_numeric_facts"] = ["10원"]
            record["expected_revision_ids"] = [fixture.v5.current_revision_id]
            record["spans"] = [
                {
                    **record["spans"][0],
                    "roles": ["benefit", "numeric", "revision"],
                    "span_id": "sealed-benefit",
                },
                {
                    **record["spans"][0],
                    "roles": ["condition"],
                    "span_id": "sealed-condition",
                },
            ]
            record["condition_groups"] = [
                {"at_k": 10, "span_ids": ["sealed-benefit", "sealed-condition"]}
            ]
        elif index == 1:
            record.update(
                {
                    "condition_groups": [],
                    "contracts": [],
                    "expected_numeric_facts": [],
                    "expected_revision_ids": [],
                    "high_risk": False,
                    "no_answer": True,
                    "slices": ["no_answer"],
                    "spans": [],
                }
            )
        records.append(record)
    return records


def _sealed_decisions(
    fixture: ProducerFixture,
    path: Path,
) -> tuple[Path, ArtifactBinding]:
    request = build_answer_request(
        QueryIdentity("gold-001", "혜택은 무엇인가요?"),
        fixture.input_query,
        fixture.input_manifest,
    )
    decision = AnswerDecision(
        schema_version=DECISION_SCHEMA,
        query_id=request.query_id,
        idempotency_key=request.idempotency_key,
        no_answer=False,
        citation_span_ids=(request.evidence[0].span_id,),
        selected_revision_ids=(request.evidence[0].contract_revision_id,),
    )
    manifest = DecisionArtifactManifest(
        schema_version=DECISION_MANIFEST_SCHEMA,
        capture_input_sha256=fixture.input_binding.sha256,
        gold_sha256=fixture.gold_binding.sha256,
        query_count=1,
        source_commit=SOURCE_COMMIT,
        generation_id=fixture.v5.generation_id,
        generation_manifest_sha256=fixture.input_manifest.generation_manifest_sha256,
        answer_profile_id=ANSWER_PROFILE,
        decision_authority="sealed_human",
        release_eligible=True,
        synthetic=False,
    )
    record = DecisionArtifactRecord(
        schema_version="cardrag.gold-answer-decision-record.v1",
        query_id=request.query_id,
        request_sha256=hashlib.sha256(request.canonical_bytes()).hexdigest(),
        decision=decision,
    )
    return path, _write_jsonl(
        path,
        [manifest.model_dump(mode="json"), record.model_dump(mode="json")],
    )


def _rewrite_shard_as_no_answer(path: Path) -> None:
    shard = json.loads(path.read_bytes())
    decision = {
        **shard["decision"],
        "citation_span_ids": [],
        "no_answer": True,
        "numeric_facts": [],
        "selected_revision_ids": [],
    }
    shard["decision"] = decision
    shard["decision_sha256"] = hashlib.sha256(_canonical(decision)).hexdigest()
    shard["record"]["answer"] = {
        "citation_span_ids": [],
        "no_answer": True,
        "numeric_facts": [],
        "selected_revision_ids": [],
        "text": "제공된 검색 근거에서 답을 확인할 수 없습니다.",
    }
    path.chmod(0o600)
    path.write_bytes(_canonical(shard) + b"\n")


def test_deterministic_v5_artifact_is_resumable_and_accepted_by_existing_consumer(
    tmp_path: Path,
) -> None:
    fixture = _producer_fixture(tmp_path)

    first = _produce(fixture, tmp_path)
    second = _produce(fixture, tmp_path)

    assert first.resumed_queries == 0
    assert second.resumed_queries == 1
    assert first.provider_calls_this_process == second.provider_calls_this_process == 0
    assert first.receipt.capture_mode == "deterministic_extractive"
    assert first.receipt.logical_provider_call_count == 0
    assert first.answer_path.read_bytes() == second.answer_path.read_bytes()
    answer_lines = [json.loads(line) for line in first.answer_path.read_bytes().splitlines()]
    assert answer_lines[1]["schema_version"] == "cardrag.gold-answer.v1"
    assert answer_lines[1]["answer"]["text"] == "공항 라운지 혜택"
    assert answer_lines[1]["answer"]["citation_span_ids"] == [
        f"{fixture.v5.current_revision_id}-paragraph"
    ]
    gold = load_gold_jsonl(fixture.gold_path)
    loaded_manifest, answers, binding = _load_answers(
        first.answer_path,
        gold=gold,
        lane="qwen_structure_exact",
        generation_id=fixture.v5.generation_id,
        generation_manifest_sha256=hashlib.sha256(fixture.manifest.canonical_bytes()).hexdigest(),
    )
    assert loaded_manifest.answer_profile_id == ANSWER_PROFILE
    assert answers["gold-001"].text == "공항 라운지 혜택"
    assert binding == first.receipt.answer_artifact


def test_public_receipt_and_final_ranking_verifiers_replay_complete_chain(
    tmp_path: Path,
) -> None:
    fixture = _bind_retrieval_capture(_producer_fixture(tmp_path), tmp_path)
    assert fixture.retrieval is not None
    result = _produce(fixture, tmp_path)
    receipt_binding = ArtifactBinding(
        sha256=hashlib.sha256(result.receipt_path.read_bytes()).hexdigest(),
        size_bytes=result.receipt_path.stat().st_size,
    )

    loaded_receipt = load_answer_producer_receipt(
        result.receipt_path,
        expected_sha256=receipt_binding.sha256,
    )
    verified = verify_answer_producer_receipt(
        receipt_path=result.receipt_path,
        expected_receipt_sha256=receipt_binding.sha256,
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        input_path=fixture.input_path,
        expected_input_sha256=fixture.input_binding.sha256,
        generation_manifest_path=fixture.manifest_path,
        database_path=fixture.v5.database,
        answer_path=result.answer_path,
        expected_answer_sha256=result.receipt.answer_artifact.sha256,
        ledger_path=result.ledger_path,
        state_identity_path=tmp_path / "answer-state" / "identity.json",
        state_bundle_path=result.state_bundle_path,
        expected_lane="qwen_structure_exact",
        expected_source_commit=SOURCE_COMMIT,
        expected_generation_id=fixture.v5.generation_id,
        expected_generation_manifest_sha256=fixture.input_manifest.generation_manifest_sha256,
        expected_answer_profile_id=ANSWER_PROFILE,
        retrieval_run_path=fixture.retrieval.run_path,
        expected_retrieval_run_sha256=fixture.retrieval.run_binding.sha256,
        retrieval_capture_receipt_path=fixture.retrieval.receipt_path,
        expected_retrieval_capture_receipt_sha256=fixture.retrieval.receipt_binding.sha256,
        retrieval_attestation_path=fixture.retrieval.attestation_path,
        expected_retrieval_attestation_sha256=fixture.retrieval.attestation_binding.sha256,
        retrieval_raw_score_path=fixture.retrieval.raw_score_path,
        expected_retrieval_raw_score_sha256=fixture.retrieval.raw_score_binding.sha256,
        retrieval_corpus_inventory_path=fixture.retrieval.corpus_inventory_path,
        expected_retrieval_corpus_inventory_sha256=(
            fixture.retrieval.corpus_inventory_binding.sha256
        ),
        retrieval_dense_score_matrix_path=fixture.retrieval.dense_score_matrix_path,
        expected_retrieval_dense_score_matrix_sha256=(
            fixture.retrieval.dense_score_matrix_binding.sha256
        ),
        retrieval_query_vector_matrix_path=fixture.retrieval.query_vector_matrix_path,
        expected_retrieval_query_vector_matrix_sha256=(
            fixture.retrieval.query_vector_matrix_binding.sha256
        ),
        release_gate=False,
    )
    assert verified == loaded_receipt == result.receipt
    portable = verify_answer_producer_receipt_portable(
        receipt_path=result.receipt_path,
        expected_receipt_sha256=receipt_binding.sha256,
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        input_path=fixture.input_path,
        expected_input_sha256=fixture.input_binding.sha256,
        answer_path=result.answer_path,
        expected_answer_sha256=result.receipt.answer_artifact.sha256,
        ledger_path=result.ledger_path,
        state_identity_path=tmp_path / "answer-state" / "identity.json",
        state_bundle_path=result.state_bundle_path,
        expected_lane="qwen_structure_exact",
        expected_source_commit=SOURCE_COMMIT,
        expected_generation_id=fixture.v5.generation_id,
        expected_generation_manifest_sha256=(fixture.input_manifest.generation_manifest_sha256),
        expected_answer_profile_id=ANSWER_PROFILE,
        retrieval_corpus_inventory_path=fixture.retrieval.corpus_inventory_path,
        expected_retrieval_corpus_inventory_sha256=(
            fixture.retrieval.corpus_inventory_binding.sha256
        ),
        release_gate=False,
    )
    assert portable == verified
    assert verified.state_identity.size_bytes > 0
    assert verified.state_bundle == ArtifactBinding(
        sha256=hashlib.sha256(result.state_bundle_path.read_bytes()).hexdigest(),
        size_bytes=result.state_bundle_path.stat().st_size,
    )
    assert verified.maximum_provider_calls == 0

    run_lines = fixture.retrieval.run_path.read_bytes().splitlines()
    authoritative = (QueryRunResult.model_validate_json(run_lines[1]),)
    input_dataset = verify_answer_input_ranking(
        input_path=fixture.input_path,
        expected_input_sha256=fixture.input_binding.sha256,
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        authoritative_results=authoritative,
        expected_lane="qwen_structure_exact",
        release_gate=False,
    )
    assert input_dataset.binding == fixture.input_binding
    tampered_query = fixture.input_query.model_copy(update={"evidence": ()})
    tampered_path = tmp_path / "tampered-ranking-input.jsonl"
    tampered_binding = _write_jsonl(
        tampered_path,
        [
            fixture.input_manifest.model_dump(mode="json"),
            tampered_query.model_dump(mode="json"),
        ],
    )
    with pytest.raises(
        GoldAnswerProducerError,
        match="retrieval_ranking_projection_mismatch",
    ):
        verify_answer_input_ranking(
            input_path=tampered_path,
            expected_input_sha256=tampered_binding.sha256,
            gold_path=fixture.gold_path,
            expected_gold_sha256=fixture.gold_binding.sha256,
            authoritative_results=authoritative,
            expected_lane="qwen_structure_exact",
            release_gate=False,
        )


def test_public_verifier_rejects_fully_repinned_deterministic_no_answer_tamper(
    tmp_path: Path,
) -> None:
    fixture = _producer_fixture(tmp_path)
    result = _produce(fixture, tmp_path)

    bundle_records = [
        json.loads(line) for line in result.state_bundle_path.read_bytes().splitlines()
    ]
    shard = bundle_records[1]["shard"]
    decision = {
        **shard["decision"],
        "citation_span_ids": [],
        "no_answer": True,
        "numeric_facts": [],
        "selected_revision_ids": [],
    }
    no_answer = {
        "citation_span_ids": [],
        "no_answer": True,
        "numeric_facts": [],
        "selected_revision_ids": [],
        "text": "제공된 검색 근거에서 답을 확인할 수 없습니다.",
    }
    shard["decision"] = decision
    shard["decision_sha256"] = hashlib.sha256(_canonical(decision)).hexdigest()
    shard["record"]["answer"] = no_answer
    result.state_bundle_path.chmod(0o600)
    state_bundle_binding = _write_jsonl(result.state_bundle_path, bundle_records)

    answer_records = [json.loads(line) for line in result.answer_path.read_bytes().splitlines()]
    answer_records[1]["answer"] = no_answer
    result.answer_path.chmod(0o600)
    answer_binding = _write_jsonl(result.answer_path, answer_records)

    receipt_record = json.loads(result.receipt_path.read_bytes())
    receipt_record["state_bundle"] = state_bundle_binding.model_dump(mode="json")
    receipt_record["answer_artifact"] = answer_binding.model_dump(mode="json")
    result.receipt_path.chmod(0o600)
    receipt_payload = _canonical(receipt_record) + b"\n"
    result.receipt_path.write_bytes(receipt_payload)

    with pytest.raises(
        GoldAnswerProducerError,
        match="answer_state_decision_provenance_mismatch",
    ):
        verify_answer_producer_receipt_portable(
            receipt_path=result.receipt_path,
            expected_receipt_sha256=hashlib.sha256(receipt_payload).hexdigest(),
            gold_path=fixture.gold_path,
            expected_gold_sha256=fixture.gold_binding.sha256,
            input_path=fixture.input_path,
            expected_input_sha256=fixture.input_binding.sha256,
            answer_path=result.answer_path,
            expected_answer_sha256=answer_binding.sha256,
            ledger_path=result.ledger_path,
            state_identity_path=tmp_path / "answer-state" / "identity.json",
            state_bundle_path=result.state_bundle_path,
            expected_lane="qwen_structure_exact",
            expected_source_commit=SOURCE_COMMIT,
            expected_generation_id=fixture.v5.generation_id,
            expected_generation_manifest_sha256=(fixture.input_manifest.generation_manifest_sha256),
            expected_answer_profile_id=ANSWER_PROFILE,
            release_gate=False,
        )


def test_answer_producer_rejects_fixture_only_bootstrap_trust_root(tmp_path: Path) -> None:
    fixture = _bind_retrieval_capture(_producer_fixture(tmp_path), tmp_path)
    assert fixture.retrieval is not None
    receipt = LaneCaptureReceipt.model_validate_json(fixture.retrieval.receipt_path.read_bytes())
    downgraded = receipt.model_copy(update={"validation_profile": "fixture_only"})
    fixture.retrieval.receipt_path.write_bytes(downgraded.canonical_bytes())
    receipt_binding = ArtifactBinding(
        sha256=hashlib.sha256(downgraded.canonical_bytes()).hexdigest(),
        size_bytes=len(downgraded.canonical_bytes()),
    )
    input_manifest = fixture.input_manifest.model_copy(
        update={"retrieval_capture_receipt": receipt_binding}
    )
    input_lines = fixture.input_path.read_bytes().splitlines()
    input_binding = _write_jsonl(
        fixture.input_path,
        [
            input_manifest.model_dump(mode="json"),
            *(json.loads(line) for line in input_lines[1:]),
        ],
    )
    fixture = replace(
        fixture,
        input_manifest=input_manifest,
        input_binding=input_binding,
        retrieval=replace(fixture.retrieval, receipt_binding=receipt_binding),
    )

    with pytest.raises(GoldAnswerProducerError, match="retrieval_capture_contract_mismatch"):
        _produce(fixture, tmp_path)


def test_portable_bundle_cap_and_query_count_fail_without_large_allocation(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    result = _produce(fixture, tmp_path)
    receipt_sha256 = hashlib.sha256(result.receipt_path.read_bytes()).hexdigest()
    oversized_bundle = tmp_path / "oversized-state-bundle.jsonl"
    with oversized_bundle.open("wb") as output:
        output.truncate(answer_producer._MAX_PORTABLE_ARTIFACT_BYTES + 1)

    with pytest.raises(GoldAnswerProducerError, match="answer_state_bundle_size_invalid"):
        verify_answer_producer_receipt_portable(
            receipt_path=result.receipt_path,
            expected_receipt_sha256=receipt_sha256,
            gold_path=fixture.gold_path,
            expected_gold_sha256=fixture.gold_binding.sha256,
            input_path=fixture.input_path,
            expected_input_sha256=fixture.input_binding.sha256,
            answer_path=result.answer_path,
            expected_answer_sha256=result.receipt.answer_artifact.sha256,
            ledger_path=result.ledger_path,
            state_identity_path=tmp_path / "answer-state" / "identity.json",
            state_bundle_path=oversized_bundle,
            expected_lane="qwen_structure_exact",
            expected_source_commit=SOURCE_COMMIT,
            expected_generation_id=fixture.v5.generation_id,
            expected_generation_manifest_sha256=(fixture.input_manifest.generation_manifest_sha256),
            expected_answer_profile_id=ANSWER_PROFILE,
            release_gate=False,
        )

    with pytest.raises(ValidationError):
        StateBundleManifest(
            schema_version="cardrag.gold-answer-state-bundle.v1",
            state_identity=result.receipt.state_identity,
            decision_mode="deterministic_extractive",
            retrieval_corpus_inventory=None,
            query_count=501,
            reservation_count=0,
        )


def test_sealed_decision_artifact_is_zero_call_and_cannot_supply_prose(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    request = build_answer_request(
        QueryIdentity("gold-001", "혜택은 무엇인가요?"),
        fixture.input_query,
        fixture.input_manifest,
    )
    decision = AnswerDecision(
        schema_version=DECISION_SCHEMA,
        query_id=request.query_id,
        idempotency_key=request.idempotency_key,
        no_answer=False,
        citation_span_ids=(request.evidence[0].span_id,),
        numeric_facts=(),
        selected_revision_ids=(request.evidence[0].contract_revision_id,),
    )
    manifest = DecisionArtifactManifest(
        schema_version=DECISION_MANIFEST_SCHEMA,
        capture_input_sha256=fixture.input_binding.sha256,
        gold_sha256=fixture.gold_binding.sha256,
        query_count=1,
        source_commit=SOURCE_COMMIT,
        generation_id=fixture.v5.generation_id,
        generation_manifest_sha256=fixture.input_manifest.generation_manifest_sha256,
        answer_profile_id=ANSWER_PROFILE,
        decision_authority="sealed_human",
        release_eligible=True,
        synthetic=False,
    )
    record = DecisionArtifactRecord(
        schema_version="cardrag.gold-answer-decision-record.v1",
        query_id=request.query_id,
        request_sha256=hashlib.sha256(request.canonical_bytes()).hexdigest(),
        decision=decision,
    )
    decisions_path = tmp_path / "decisions.jsonl"
    decisions_binding = _write_jsonl(
        decisions_path,
        [manifest.model_dump(mode="json"), record.model_dump(mode="json")],
    )

    result = _produce(
        fixture,
        tmp_path,
        deterministic=False,
        decision_path=decisions_path,
        expected_decision_sha256=decisions_binding.sha256,
        maximum_provider_calls=0,
    )

    assert result.receipt.capture_mode == "sealed_decisions"
    assert result.receipt.logical_provider_call_count == 0
    assert result.receipt.decision_artifact == decisions_binding
    assert result.receipt.provider_id == f"sealed_human:{decisions_binding.sha256}"
    assert result.receipt.maximum_provider_calls == 0
    assert result.receipt.state_identity.size_bytes > 0
    assert len(result.ledger_path.read_bytes().splitlines()) == 1
    portable = verify_answer_producer_receipt_portable(
        receipt_path=result.receipt_path,
        expected_receipt_sha256=hashlib.sha256(result.receipt_path.read_bytes()).hexdigest(),
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        input_path=fixture.input_path,
        expected_input_sha256=fixture.input_binding.sha256,
        answer_path=result.answer_path,
        expected_answer_sha256=result.receipt.answer_artifact.sha256,
        ledger_path=result.ledger_path,
        state_identity_path=tmp_path / "answer-state" / "identity.json",
        state_bundle_path=result.state_bundle_path,
        expected_lane="qwen_structure_exact",
        expected_source_commit=SOURCE_COMMIT,
        expected_generation_id=fixture.v5.generation_id,
        expected_generation_manifest_sha256=(fixture.input_manifest.generation_manifest_sha256),
        expected_answer_profile_id=ANSWER_PROFILE,
        decision_path=decisions_path,
        expected_decision_sha256=decisions_binding.sha256,
        release_gate=False,
    )
    assert portable == result.receipt
    with pytest.raises(ValidationError):
        DecisionArtifactRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "text": "provider가 작성한 paraphrase는 계약상 입력할 수 없음",
            }
        )


def test_qwen_page_external_generation_produces_existing_answer_schema(tmp_path: Path) -> None:
    source_text = "페이지 방식 혜택 10,000원"
    source_sha256 = hashlib.sha256(source_text.encode()).hexdigest()
    document_id = "document-a"
    chunk_id = "evidence_" + canonical_sha256(
        {
            "document_id": document_id,
            "page": 1,
            "source_end": len(source_text),
            "source_start": 0,
            "text_sha256": source_sha256,
        }
    )
    database = tmp_path / "page.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT, WITHOUT ROWID;
            CREATE TABLE evaluation_chunks(
              row_index INTEGER PRIMARY KEY CHECK(row_index >= 0),
              chunk_id TEXT NOT NULL UNIQUE,
              contract_revision_id TEXT NOT NULL,
              span_id TEXT NOT NULL UNIQUE,
              document_id TEXT NOT NULL,
              page INTEGER NOT NULL CHECK(page > 0),
              source_start INTEGER NOT NULL CHECK(source_start >= 0),
              source_end INTEGER NOT NULL CHECK(source_end > source_start),
              text TEXT NOT NULL CHECK(length(text) > 0),
              input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64)
            ) STRICT;
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (
                ("schema_id", "cardrag.evaluation-page.v1"),
                ("generation_id", "page-answer-generation"),
                ("source_commit", SOURCE_COMMIT),
                ("source_generation_id", "source-generation"),
                ("source_generation_manifest_sha256", "d" * 64),
                ("source_generation_manifest_size_bytes", "1"),
                ("source_serving_database_sha256", "e" * 64),
                ("source_serving_database_size_bytes", "1"),
                ("embedding_model", "qwen/qwen3-embedding-8b"),
                ("embedding_dimension", "4096"),
                ("embedding_profile_id", "page-profile"),
                ("chunking_policy", "cardrag.page-window-1600.v1"),
                ("maximum_chars", "1600"),
                ("overlap_chars", "160"),
                ("source_text_contract", "cardrag.page-source-text-range.v1"),
                ("column_contract", "cardrag.evaluation-page-columns.v1"),
                ("row_count", "1"),
            ),
        )
        connection.execute(
            "INSERT INTO evaluation_chunks VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                0,
                chunk_id,
                "contract-a",
                chunk_id,
                document_id,
                1,
                0,
                len(source_text),
                source_text,
                source_sha256,
            ),
        )
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
        size_bytes=database.stat().st_size,
    )
    vector_body = b"page-vector-fixture"
    vector_binding = ArtifactBinding(
        sha256=hashlib.sha256(vector_body).hexdigest(),
        size_bytes=len(vector_body),
    )
    page_manifest = PageGenerationManifest(
        schema_version="cardrag.evaluation-page-generation.v2",
        source_commit=SOURCE_COMMIT,
        source_generation_id="source-generation",
        source_generation_manifest=ArtifactBinding(sha256="d" * 64, size_bytes=1),
        source_serving_database=ArtifactBinding(sha256="e" * 64, size_bytes=1),
        generation_id="page-answer-generation",
        serving_schema="cardrag.evaluation-page.v1",
        serving_database=database_binding,
        vector_artifact=vector_binding,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_profile_id="page-profile",
        chunking_policy="cardrag.page-window-1600.v1",
        maximum_chars=1600,
        overlap_chars=160,
        source_text_contract="cardrag.page-source-text-range.v1",
        column_contract="cardrag.evaluation-page-columns.v1",
        row_count=1,
        corpus_inventory_sha256="a" * 64,
    )
    manifest_path = tmp_path / "page-manifest.json"
    manifest_path.write_bytes(page_manifest.canonical_bytes())
    gold_path = tmp_path / "page-gold.jsonl"
    gold_binding = _write_jsonl(
        gold_path,
        [_gold_record(revision_id="contract-a", span_id=chunk_id)],
    )
    input_manifest = AnswerInputManifest(
        schema_version=INPUT_MANIFEST_SCHEMA,
        lane="qwen_page",
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_commit=SOURCE_COMMIT,
        generation_id="page-answer-generation",
        generation_manifest_sha256=hashlib.sha256(page_manifest.canonical_bytes()).hexdigest(),
        serving_schema="cardrag.evaluation-page.v1",
        serving_database=database_binding,
        answer_profile_id=ANSWER_PROFILE,
        maximum_answer_evidence_spans=8,
        rendering_contract="cardrag.extractive-source-blocks.v1",
        synthetic=False,
    )
    input_query = AnswerInputQuery(
        schema_version=INPUT_QUERY_SCHEMA,
        query_id="gold-001",
        query_sha256=hashlib.sha256("혜택은 무엇인가요?".encode()).hexdigest(),
        contracts=(RetrievedContract(contract_revision_id="contract-a", rank=1, score=0.9),),
        evidence=(
            AnswerEvidence(
                span_id=chunk_id,
                contract_revision_id="contract-a",
                rank=1,
                score=0.9,
                source_text=source_text,
                source_text_sha256=source_sha256,
            ),
        ),
    )
    input_path = tmp_path / "page-input.jsonl"
    input_binding = _write_jsonl(
        input_path,
        [input_manifest.model_dump(mode="json"), input_query.model_dump(mode="json")],
    )

    result = produce_answer_artifact(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        input_path=input_path,
        expected_input_sha256=input_binding.sha256,
        generation_manifest_path=manifest_path,
        database_path=database,
        state_directory=tmp_path / "page-state",
        answer_path=tmp_path / "page-answers.jsonl",
        ledger_path=tmp_path / "page-ledger.jsonl",
        receipt_path=tmp_path / "page-receipt.json",
        expected_source_commit=SOURCE_COMMIT,
        expected_answer_profile_id=ANSWER_PROFILE,
        deterministic=True,
        maximum_provider_calls=0,
        release_gate=False,
    )

    gold = load_gold_jsonl(gold_path)
    loaded, answers, binding = _load_answers(
        result.answer_path,
        gold=gold,
        lane="qwen_page",
        generation_id="page-answer-generation",
        generation_manifest_sha256=input_manifest.generation_manifest_sha256,
    )
    assert loaded.lane == "qwen_page"
    assert answers["gold-001"].numeric_facts == ("10,000원",)
    assert binding == result.receipt.answer_artifact


def test_actual_external_page_corpus_capture_to_answer_and_consumer(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    fixture = _with_queries(fixture, gold_records=_release_gold_records(fixture))
    gold = load_gold_jsonl(fixture.gold_path, release_gate=True)
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    chunks = external_producer._source_page_chunks(
        generation_manifest=fixture.manifest,
        database_path=fixture.v5.database,
    )
    document_inputs = [
        (
            chunk.chunk_id,
            format_qwen3_document(chunk.text),
            _unit_vector(index),
        )
        for index, chunk in enumerate(chunks)
    ]
    document_receipt_path = tmp_path / "page-document-provider-receipt.json"
    document_receipt = _provider_receipt(
        document_receipt_path,
        inputs=document_inputs,
    )
    document_replay_path = tmp_path / "page-document-replay.jsonl"
    _embedding_replay(
        document_replay_path,
        input_kind="document",
        source_commit=SOURCE_COMMIT,
        profile_id=profile_id,
        provider_receipt=document_receipt,
        inputs=document_inputs,
    )
    page_database = tmp_path / "actual-page.sqlite3"
    page_vectors = tmp_path / "actual-page-vectors.f32"
    page_inventory = tmp_path / "actual-page-inventory.jsonl"
    page_manifest_path = tmp_path / "actual-page-manifest.json"
    corpus = build_qwen_page_corpus(
        source_generation_manifest_path=fixture.manifest_path,
        source_database_path=fixture.v5.database,
        source_commit=SOURCE_COMMIT,
        embedding_profile_id=profile_id,
        document_embedding_replay_path=document_replay_path,
        provider_receipt_path=document_receipt_path,
        database_output_path=page_database,
        vector_output_path=page_vectors,
        inventory_output_path=page_inventory,
        generation_manifest_output_path=page_manifest_path,
    )
    bootstrap_manifest = AnswerArtifactManifest(
        schema_version="cardrag.gold-answer-artifact.v1",
        lane="qwen_page",
        gold_sha256=fixture.gold_binding.sha256,
        query_count=len(gold.queries),
        generation_id=corpus.generation_id,
        generation_manifest_sha256=corpus.generation_manifest.sha256,
        answer_profile_id="capture-bootstrap-no-answer",
        synthetic=False,
    )
    bootstrap_path = tmp_path / "page-bootstrap-answers.jsonl"
    bootstrap_binding = _write_jsonl(
        bootstrap_path,
        [
            bootstrap_manifest.model_dump(mode="json"),
            *(
                AnswerRecord(
                    schema_version="cardrag.gold-answer.v1",
                    query_id=query.query_id,
                    query_sha256=hashlib.sha256(query.question.encode()).hexdigest(),
                    answer=EvaluatedAnswer(text="검색 전 bootstrap", no_answer=True),
                ).model_dump(mode="json")
                for query in gold.queries
            ),
        ],
    )
    query_inputs = [
        (
            query.query_id,
            format_qwen3_query(query.question),
            _unit_vector(index),
        )
        for index, query in enumerate(gold.queries)
    ]
    query_receipt_path = tmp_path / "page-query-provider-receipt.json"
    query_receipt = _provider_receipt(query_receipt_path, inputs=query_inputs)
    query_replay_path = tmp_path / "page-query-replay.jsonl"
    _embedding_replay(
        query_replay_path,
        input_kind="query",
        source_commit=SOURCE_COMMIT,
        profile_id=profile_id,
        provider_receipt=query_receipt,
        inputs=query_inputs,
    )
    observation_path = tmp_path / "actual-page-observation.jsonl"
    dense_score_matrix_path = tmp_path / "actual-page-dense-scores.f32"
    query_vector_matrix_path = tmp_path / "actual-page-query-vectors.f32"
    observation_binding = produce_external_observation(
        lane="qwen_page",
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        answer_artifact_path=bootstrap_path,
        expected_answer_artifact_sha256=bootstrap_binding.sha256,
        query_embedding_replay_path=query_replay_path,
        provider_receipt_path=query_receipt_path,
        generation_manifest_path=page_manifest_path,
        database_path=page_database,
        vector_path=page_vectors,
        inventory_path=page_inventory,
        source_commit=SOURCE_COMMIT,
        output_path=observation_path,
        dense_score_matrix_path=dense_score_matrix_path,
        query_vector_matrix_path=query_vector_matrix_path,
        lexical_rank_path=None,
        release_gate=True,
    )
    run_path = tmp_path / "actual-page-run.jsonl"
    capture_receipt_path = tmp_path / "actual-page-capture-receipt.json"
    capture_receipt = seal_external_observation(
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        observation_path=observation_path,
        expected_observation_sha256=observation_binding.sha256,
        inventory_path=page_inventory,
        expected_inventory_sha256=corpus.inventory.sha256,
        generation_manifest_path=page_manifest_path,
        database_path=page_database,
        vector_path=page_vectors,
        dense_score_matrix_path=dense_score_matrix_path,
        query_vector_matrix_path=query_vector_matrix_path,
        lexical_rank_path=None,
        output_path=run_path,
        receipt_path=capture_receipt_path,
        source_generation_manifest_path=fixture.manifest_path,
        source_database_path=fixture.v5.database,
        expected_source_commit=SOURCE_COMMIT,
        release_gate=True,
    )
    assert capture_receipt.corpus_inventory == corpus.inventory
    assert capture_receipt.dense_score_matrix is not None
    assert capture_receipt.query_vector_matrix is not None
    capture_receipt_binding = ArtifactBinding(
        sha256=hashlib.sha256(capture_receipt_path.read_bytes()).hexdigest(),
        size_bytes=capture_receipt_path.stat().st_size,
    )
    input_path = tmp_path / "actual-page-answer-input.jsonl"
    input_binding = build_answer_input_artifact(
        lane="qwen_page",
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        generation_manifest_path=page_manifest_path,
        database_path=page_database,
        retrieval_run_path=run_path,
        expected_retrieval_run_sha256=capture_receipt.run_artifact.sha256,
        retrieval_capture_receipt_path=capture_receipt_path,
        expected_retrieval_capture_receipt_sha256=capture_receipt_binding.sha256,
        retrieval_attestation_path=observation_path,
        expected_retrieval_attestation_sha256=observation_binding.sha256,
        retrieval_raw_score_path=observation_path,
        expected_retrieval_raw_score_sha256=observation_binding.sha256,
        retrieval_corpus_inventory_path=page_inventory,
        expected_retrieval_corpus_inventory_sha256=corpus.inventory.sha256,
        retrieval_dense_score_matrix_path=dense_score_matrix_path,
        expected_retrieval_dense_score_matrix_sha256=(capture_receipt.dense_score_matrix.sha256),
        retrieval_query_vector_matrix_path=query_vector_matrix_path,
        expected_retrieval_query_vector_matrix_sha256=(capture_receipt.query_vector_matrix.sha256),
        output_path=input_path,
        expected_source_commit=SOURCE_COMMIT,
        answer_profile_id=ANSWER_PROFILE,
        release_gate=True,
    )
    result = produce_answer_artifact(
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        input_path=input_path,
        expected_input_sha256=input_binding.sha256,
        generation_manifest_path=page_manifest_path,
        database_path=page_database,
        retrieval_run_path=run_path,
        expected_retrieval_run_sha256=capture_receipt.run_artifact.sha256,
        retrieval_capture_receipt_path=capture_receipt_path,
        expected_retrieval_capture_receipt_sha256=capture_receipt_binding.sha256,
        retrieval_attestation_path=observation_path,
        expected_retrieval_attestation_sha256=observation_binding.sha256,
        retrieval_raw_score_path=observation_path,
        expected_retrieval_raw_score_sha256=observation_binding.sha256,
        retrieval_corpus_inventory_path=page_inventory,
        expected_retrieval_corpus_inventory_sha256=corpus.inventory.sha256,
        retrieval_dense_score_matrix_path=dense_score_matrix_path,
        expected_retrieval_dense_score_matrix_sha256=(capture_receipt.dense_score_matrix.sha256),
        retrieval_query_vector_matrix_path=query_vector_matrix_path,
        expected_retrieval_query_vector_matrix_sha256=(capture_receipt.query_vector_matrix.sha256),
        state_directory=tmp_path / "actual-page-answer-state",
        answer_path=tmp_path / "actual-page-answers.jsonl",
        ledger_path=tmp_path / "actual-page-answer-ledger.jsonl",
        receipt_path=tmp_path / "actual-page-answer-receipt.json",
        expected_source_commit=SOURCE_COMMIT,
        expected_answer_profile_id=ANSWER_PROFILE,
        deterministic=True,
        maximum_provider_calls=0,
        release_gate=True,
    )
    loaded, answers, binding = _load_answers(
        result.answer_path,
        gold=gold,
        lane="qwen_page",
        generation_id=corpus.generation_id,
        generation_manifest_sha256=corpus.generation_manifest.sha256,
    )
    assert loaded.lane == "qwen_page"
    assert answers["gold-000"].citation_span_ids[0] == chunks[0].chunk_id
    assert binding == result.receipt.answer_artifact
    portable = verify_answer_producer_receipt_portable(
        receipt_path=result.receipt_path,
        expected_receipt_sha256=hashlib.sha256(result.receipt_path.read_bytes()).hexdigest(),
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        input_path=input_path,
        expected_input_sha256=input_binding.sha256,
        answer_path=result.answer_path,
        expected_answer_sha256=result.receipt.answer_artifact.sha256,
        ledger_path=result.ledger_path,
        state_identity_path=tmp_path / "actual-page-answer-state" / "identity.json",
        state_bundle_path=result.state_bundle_path,
        expected_lane="qwen_page",
        expected_source_commit=SOURCE_COMMIT,
        expected_generation_id=corpus.generation_id,
        expected_generation_manifest_sha256=corpus.generation_manifest.sha256,
        expected_answer_profile_id=ANSWER_PROFILE,
        retrieval_corpus_inventory_path=page_inventory,
        expected_retrieval_corpus_inventory_sha256=corpus.inventory.sha256,
        release_gate=True,
    )
    assert portable == result.receipt

    input_lines = input_path.read_bytes().splitlines()
    input_query = AnswerInputQuery.model_validate_json(input_lines[1])
    for index, statement in enumerate(
        (
            "UPDATE evaluation_chunks SET text=text || 'X' WHERE row_index=0",
            "UPDATE evaluation_chunks SET source_end=source_end + 1 WHERE row_index=0",
            (
                "UPDATE evaluation_chunks SET input_sha256="
                "'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' "
                "WHERE row_index=0"
            ),
        )
    ):
        tampered = tmp_path / f"tampered-page-{index}.sqlite3"
        shutil.copyfile(page_database, tampered)
        with sqlite3.connect(tampered) as connection:
            connection.execute(statement)
            connection.commit()
            metadata = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            with pytest.raises(
                GoldAnswerProducerError,
                match="page_database_source_contract_invalid",
            ):
                _verify_page_evidence(
                    connection,
                    (input_query,),
                    metadata=metadata,
                    expected_row_count=corpus.row_count,
                    expected_embedding_profile_id=profile_id,
                    expected_source_commit=SOURCE_COMMIT,
                    expected_source_generation_id=fixture.v5.generation_id,
                    expected_source_generation_manifest=ArtifactBinding(
                        sha256=hashlib.sha256(fixture.manifest.canonical_bytes()).hexdigest(),
                        size_bytes=len(fixture.manifest.canonical_bytes()),
                    ),
                    expected_source_serving_database=fixture.input_manifest.serving_database,
                )


def test_v109_external_generation_produces_existing_answer_schema(tmp_path: Path) -> None:
    legacy = create_database(
        tmp_path / "v109" / "index.sqlite3",
        "v109-answer-generation",
        two_documents=False,
        schema_id="cardrag.serving-db.v4",
    )
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(legacy.database.read_bytes()).hexdigest(),
        size_bytes=legacy.database.stat().st_size,
    )
    document_id, pdf_sha256, pdf_size, pdf_body = legacy.documents[0]
    ocr_body = b"legacy OCR fixture"
    generation = GenerationManifest(
        schema_version="cardrag.generation.v4",
        generation_id=legacy.generation_id,
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
        serving_schema="cardrag.serving-db.v4",
        serving_database=ArtifactRef(
            sha256=database_binding.sha256,
            size_bytes=database_binding.size_bytes,
            media_type="application/vnd.sqlite3",
            path=generation_database_path(legacy.generation_id).as_posix(),
        ),
        corpus_sha256=legacy.corpus_sha256,
        contract_sha256=legacy.contract_sha256,
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="openai/text-embedding-3-small",
            dimension=1536,
            count=2,
        ),
        issuer_codes=legacy.issuer_codes,
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=2),
        documents=(
            GenerationDocument(
                document_id=document_id,
                issuer="woori",
                pdf=ArtifactRef.for_cas(
                    sha256=pdf_sha256,
                    size_bytes=pdf_size,
                    media_type="application/pdf",
                ),
                ocr=ArtifactRef.for_cas(
                    sha256=hashlib.sha256(ocr_body).hexdigest(),
                    size_bytes=len(ocr_body),
                    media_type="text/markdown; charset=utf-8",
                ),
                page_count=1,
                availability="available",
            ),
        ),
        issuer_ocr_counts=(IssuerOCRCounts(issuer="woori", acquired=1, succeeded=1, failed=0),),
    )
    manifest_path = tmp_path / "v109-manifest.json"
    manifest_path.write_bytes(generation.canonical_bytes())
    gold_path = tmp_path / "v109-gold.jsonl"
    gold_binding = _write_jsonl(
        gold_path,
        [_gold_record(revision_id=document_id, span_id="ev-a")],
    )
    input_manifest = AnswerInputManifest(
        schema_version=INPUT_MANIFEST_SCHEMA,
        lane="v109_baseline",
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_commit=V109_BASELINE_COMMIT,
        generation_id=legacy.generation_id,
        generation_manifest_sha256=hashlib.sha256(generation.canonical_bytes()).hexdigest(),
        serving_schema="cardrag.serving-db.v4",
        serving_database=database_binding,
        answer_profile_id=ANSWER_PROFILE,
        maximum_answer_evidence_spans=8,
        rendering_contract="cardrag.extractive-source-blocks.v1",
        synthetic=False,
    )
    source_text = "airport lounge benefit"
    input_query = AnswerInputQuery(
        schema_version=INPUT_QUERY_SCHEMA,
        query_id="gold-001",
        query_sha256=hashlib.sha256("혜택은 무엇인가요?".encode()).hexdigest(),
        contracts=(RetrievedContract(contract_revision_id=document_id, rank=1, score=0.9),),
        evidence=(
            AnswerEvidence(
                span_id="ev-a",
                contract_revision_id=document_id,
                rank=1,
                score=0.9,
                source_text=source_text,
                source_text_sha256=hashlib.sha256(source_text.encode()).hexdigest(),
            ),
        ),
    )
    input_path = tmp_path / "v109-input.jsonl"
    input_binding = _write_jsonl(
        input_path,
        [input_manifest.model_dump(mode="json"), input_query.model_dump(mode="json")],
    )

    result = produce_answer_artifact(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        input_path=input_path,
        expected_input_sha256=input_binding.sha256,
        generation_manifest_path=manifest_path,
        database_path=legacy.database,
        state_directory=tmp_path / "v109-state",
        answer_path=tmp_path / "v109-answers.jsonl",
        ledger_path=tmp_path / "v109-ledger.jsonl",
        receipt_path=tmp_path / "v109-receipt.json",
        expected_source_commit=V109_BASELINE_COMMIT,
        expected_answer_profile_id=ANSWER_PROFILE,
        deterministic=True,
        maximum_provider_calls=0,
        release_gate=False,
    )

    gold = load_gold_jsonl(gold_path)
    loaded, answers, binding = _load_answers(
        result.answer_path,
        gold=gold,
        lane="v109_baseline",
        generation_id=legacy.generation_id,
        generation_manifest_sha256=input_manifest.generation_manifest_sha256,
    )
    assert loaded.lane == "v109_baseline"
    assert answers["gold-001"].text == source_text
    assert binding == result.receipt.answer_artifact


def test_deterministic_extractive_is_release_capable_without_gold_label_access(
    tmp_path: Path,
) -> None:
    fixture = _producer_fixture(tmp_path)
    fixture = _with_queries(fixture, gold_records=_release_gold_records(fixture))
    fixture = _bind_retrieval_capture(fixture, tmp_path)

    result = _produce(
        fixture,
        tmp_path,
        state_directory=tmp_path / "release-state",
        answer_path=tmp_path / "release-answers.jsonl",
        ledger_path=tmp_path / "release-ledger.jsonl",
        receipt_path=tmp_path / "release-receipt.json",
        release_gate=True,
    )

    assert result.receipt.release_eligible is True
    assert result.receipt.capture_mode == "deterministic_extractive"
    assert result.receipt.query_count == 300
    assert result.receipt.logical_provider_call_count == 0
    assert fixture.retrieval is not None
    assert result.receipt.retrieval_corpus_inventory == (fixture.retrieval.corpus_inventory_binding)
    assert result.receipt.retrieval_dense_score_matrix == (
        fixture.retrieval.dense_score_matrix_binding
    )
    assert result.receipt.retrieval_query_vector_matrix == (
        fixture.retrieval.query_vector_matrix_binding
    )
    bundled_identity = StateBundleManifest.model_validate_json(
        result.state_bundle_path.read_bytes().splitlines()[0]
    )
    assert bundled_identity.retrieval_corpus_inventory == (
        fixture.retrieval.corpus_inventory_binding
    )
    receipt_sha256 = hashlib.sha256(result.receipt_path.read_bytes()).hexdigest()
    verified = verify_answer_producer_receipt_portable(
        receipt_path=result.receipt_path,
        expected_receipt_sha256=receipt_sha256,
        gold_path=fixture.gold_path,
        expected_gold_sha256=fixture.gold_binding.sha256,
        input_path=fixture.input_path,
        expected_input_sha256=fixture.input_binding.sha256,
        answer_path=result.answer_path,
        expected_answer_sha256=result.receipt.answer_artifact.sha256,
        ledger_path=result.ledger_path,
        state_identity_path=tmp_path / "release-state" / "identity.json",
        state_bundle_path=result.state_bundle_path,
        expected_lane="qwen_structure_exact",
        expected_source_commit=SOURCE_COMMIT,
        expected_generation_id=fixture.v5.generation_id,
        expected_generation_manifest_sha256=(fixture.input_manifest.generation_manifest_sha256),
        expected_answer_profile_id=ANSWER_PROFILE,
        retrieval_corpus_inventory_path=fixture.retrieval.corpus_inventory_path,
        expected_retrieval_corpus_inventory_sha256=(
            fixture.retrieval.corpus_inventory_binding.sha256
        ),
        release_gate=True,
    )
    assert verified == result.receipt

    repinned_inventory_path = tmp_path / "repinned-native-inventory.jsonl"
    repinned_inventory = _write_jsonl(
        repinned_inventory_path,
        [{"schema_version": "repinned-native-corpus-inventory.v1"}],
    )
    with pytest.raises(
        GoldAnswerProducerError,
        match="retrieval_capture_artifact_binding_mismatch",
    ):
        verify_answer_producer_receipt_portable(
            receipt_path=result.receipt_path,
            expected_receipt_sha256=receipt_sha256,
            gold_path=fixture.gold_path,
            expected_gold_sha256=fixture.gold_binding.sha256,
            input_path=fixture.input_path,
            expected_input_sha256=fixture.input_binding.sha256,
            answer_path=result.answer_path,
            expected_answer_sha256=result.receipt.answer_artifact.sha256,
            ledger_path=result.ledger_path,
            state_identity_path=tmp_path / "release-state" / "identity.json",
            state_bundle_path=result.state_bundle_path,
            expected_lane="qwen_structure_exact",
            expected_source_commit=SOURCE_COMMIT,
            expected_generation_id=fixture.v5.generation_id,
            expected_generation_manifest_sha256=(fixture.input_manifest.generation_manifest_sha256),
            expected_answer_profile_id=ANSWER_PROFILE,
            retrieval_corpus_inventory_path=repinned_inventory_path,
            expected_retrieval_corpus_inventory_sha256=repinned_inventory.sha256,
            release_gate=True,
        )


def test_answer_selection_boundary_never_receives_gold_labels(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    original = load_gold_jsonl(fixture.gold_path)
    no_answer_query = GoldQuery(
        schema_version="cardrag.gold-query.v1",
        query_id="gold-001",
        question="혜택은 무엇인가요?",
        slices=("no_answer",),
        contracts=(),
        spans=(),
        condition_groups=(),
        expected_numeric_facts=(),
        expected_revision_ids=(),
        no_answer=True,
        high_risk=False,
    )
    relabeled = GoldDataset(
        queries=(no_answer_query,),
        sha256="f" * 64,
        size_bytes=1,
    )

    original_identity = _query_identities(original)[0]
    relabeled_identity = _query_identities(relabeled)[0]
    original_request = build_answer_request(
        original_identity,
        fixture.input_query,
        fixture.input_manifest,
    )
    relabeled_request = build_answer_request(
        relabeled_identity,
        fixture.input_query,
        fixture.input_manifest,
    )

    assert original_request == relabeled_request
    assert _deterministic_decision(original_request) == _deterministic_decision(relabeled_request)
    assert set(type(original_request).model_fields) == {
        "schema_version",
        "idempotency_key",
        "lane",
        "source_commit",
        "generation_id",
        "generation_manifest_sha256",
        "answer_profile_id",
        "query_id",
        "query_sha256",
        "question",
        "evidence",
    }


class _FakeProvider:
    provider_id = "fixture-provider"
    answer_profile_id = ANSWER_PROFILE

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, request: Any) -> AnswerDecision:
        self.calls += 1
        evidence = request.evidence[0]
        return AnswerDecision(
            schema_version="cardrag.gold-answer-decision.v1",
            query_id=request.query_id,
            idempotency_key=request.idempotency_key,
            no_answer=False,
            citation_span_ids=(evidence.span_id,),
            numeric_facts=(),
            selected_revision_ids=(evidence.contract_revision_id,),
        )


def test_provider_call_ledger_is_bounded_and_completed_shard_is_zero_call_on_resume(
    tmp_path: Path,
) -> None:
    fixture = _producer_fixture(tmp_path)
    provider = _FakeProvider()

    first = _produce(
        fixture,
        tmp_path,
        deterministic=False,
        provider=provider,
        provider_release_eligible=True,
        maximum_provider_calls=1,
    )
    second = _produce(
        fixture,
        tmp_path,
        deterministic=False,
        provider=provider,
        provider_release_eligible=True,
        maximum_provider_calls=1,
    )

    assert provider.calls == 1
    assert first.provider_calls_this_process == 1
    assert second.provider_calls_this_process == 0
    assert second.resumed_queries == 1
    assert first.receipt.logical_provider_call_count == 1
    ledger = [json.loads(line) for line in first.ledger_path.read_bytes().splitlines()]
    assert ledger[0]["logical_provider_call_count"] == 1
    assert len(ledger) == 2

    other = tmp_path / "too-small"
    with pytest.raises(GoldAnswerProducerError, match="maximum_provider_calls_insufficient"):
        _produce(
            fixture,
            other,
            deterministic=False,
            provider=_FakeProvider(),
            maximum_provider_calls=0,
            state_directory=other / "state",
            answer_path=other / "answers.jsonl",
            ledger_path=other / "ledger.jsonl",
            receipt_path=other / "receipt.json",
        )


def test_public_verifier_rejects_repinned_provider_ledger_mapping(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    result = _produce(
        fixture,
        tmp_path,
        deterministic=False,
        provider=_FakeProvider(),
        provider_release_eligible=True,
        maximum_provider_calls=1,
    )
    ledger_records = [json.loads(line) for line in result.ledger_path.read_bytes().splitlines()]
    ledger_records[1]["provider_id"] = "repinned-provider"
    result.ledger_path.chmod(0o600)
    ledger_binding = _write_jsonl(result.ledger_path, ledger_records)
    receipt_record = json.loads(result.receipt_path.read_bytes())
    receipt_record["call_ledger"] = ledger_binding.model_dump(mode="json")
    result.receipt_path.chmod(0o600)
    receipt_payload = _canonical(receipt_record) + b"\n"
    result.receipt_path.write_bytes(receipt_payload)

    with pytest.raises(GoldAnswerProducerError, match="answer_call_ledger_semantic_mismatch"):
        verify_answer_producer_receipt_portable(
            receipt_path=result.receipt_path,
            expected_receipt_sha256=hashlib.sha256(receipt_payload).hexdigest(),
            gold_path=fixture.gold_path,
            expected_gold_sha256=fixture.gold_binding.sha256,
            input_path=fixture.input_path,
            expected_input_sha256=fixture.input_binding.sha256,
            answer_path=result.answer_path,
            expected_answer_sha256=result.receipt.answer_artifact.sha256,
            ledger_path=result.ledger_path,
            state_identity_path=tmp_path / "answer-state" / "identity.json",
            state_bundle_path=result.state_bundle_path,
            expected_lane="qwen_structure_exact",
            expected_source_commit=SOURCE_COMMIT,
            expected_generation_id=fixture.v5.generation_id,
            expected_generation_manifest_sha256=(fixture.input_manifest.generation_manifest_sha256),
            expected_answer_profile_id=ANSWER_PROFILE,
            release_gate=False,
        )


def test_resume_recomputes_deterministic_and_sealed_decision_provenance(
    tmp_path: Path,
) -> None:
    deterministic_root = tmp_path / "deterministic"
    fixture = _producer_fixture(deterministic_root)
    _produce(fixture, deterministic_root)
    deterministic_shard = deterministic_root / "answer-state" / "shards" / "query-000.json"
    _rewrite_shard_as_no_answer(deterministic_shard)
    with pytest.raises(
        GoldAnswerProducerError,
        match="answer_state_decision_provenance_mismatch",
    ):
        _produce(fixture, deterministic_root)

    sealed_root = tmp_path / "sealed"
    sealed_fixture = _producer_fixture(sealed_root)
    decision_path, decision_binding = _sealed_decisions(
        sealed_fixture,
        sealed_root / "decisions.jsonl",
    )
    _produce(
        sealed_fixture,
        sealed_root,
        deterministic=False,
        decision_path=decision_path,
        expected_decision_sha256=decision_binding.sha256,
    )
    sealed_shard = sealed_root / "answer-state" / "shards" / "query-000.json"
    _rewrite_shard_as_no_answer(sealed_shard)
    with pytest.raises(
        GoldAnswerProducerError,
        match="answer_state_decision_provenance_mismatch",
    ):
        _produce(
            sealed_fixture,
            sealed_root,
            deterministic=False,
            decision_path=decision_path,
            expected_decision_sha256=decision_binding.sha256,
        )


def test_provider_resume_requires_logical_call_and_reservation_binding(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    provider = _FakeProvider()
    _produce(
        fixture,
        tmp_path,
        deterministic=False,
        provider=provider,
        provider_release_eligible=True,
        maximum_provider_calls=1,
    )
    shard_path = tmp_path / "answer-state" / "shards" / "query-000.json"
    shard = json.loads(shard_path.read_bytes())
    shard["logical_call_index"] = None
    shard_path.chmod(0o600)
    shard_path.write_bytes(_canonical(shard) + b"\n")
    with pytest.raises(
        GoldAnswerProducerError,
        match="answer_state_provider_call_missing",
    ):
        _produce(
            fixture,
            tmp_path,
            deterministic=False,
            provider=provider,
            provider_release_eligible=True,
            maximum_provider_calls=1,
        )
    assert provider.calls == 1


@pytest.mark.parametrize(
    ("decision", "code"),
    (
        (
            {
                "citation_span_ids": ("fabricated",),
                "numeric_facts": (),
                "selected_revision_ids": (),
            },
            "answer_citation_not_retrieved",
        ),
        (
            {
                "citation_span_ids": (),
                "numeric_facts": ("99,999원",),
            },
            "answer_numeric_fact_not_in_cited_source",
        ),
    ),
)
def test_decision_cannot_fabricate_citations_or_numeric_facts(
    tmp_path: Path,
    decision: dict[str, tuple[str, ...]],
    code: str,
) -> None:
    fixture = _producer_fixture(tmp_path)
    request = build_answer_request(
        QueryIdentity("gold-001", "혜택은 무엇인가요?"),
        fixture.input_query,
        fixture.input_manifest,
    )
    values = {
        "schema_version": "cardrag.gold-answer-decision.v1",
        "query_id": request.query_id,
        "idempotency_key": request.idempotency_key,
        "no_answer": False,
        "citation_span_ids": (fixture.input_query.evidence[0].span_id,),
        "numeric_facts": (),
        "selected_revision_ids": (fixture.v5.current_revision_id,),
    }
    values.update(decision)
    if not values["citation_span_ids"]:
        values["citation_span_ids"] = (fixture.input_query.evidence[0].span_id,)
    sealed = AnswerDecision.model_validate(values)
    with pytest.raises(GoldAnswerProducerError, match=code):
        _answer_from_decision(request, sealed)


def test_evidence_rejects_credentials_controls_and_non_finite_scores() -> None:
    common = {
        "span_id": "span-1",
        "contract_revision_id": "revision-1",
        "rank": 1,
        "score": 0.5,
    }
    for text in (
        "api_key=supersecretvalue123456",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "normal\x00text",
    ):
        with pytest.raises((GoldAnswerProducerError, ValidationError, ValueError)):
            AnswerEvidence.model_validate(
                {
                    **common,
                    "source_text": text,
                    "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
    with pytest.raises(ValidationError):
        AnswerEvidence.model_validate(
            {
                **common,
                "score": float("nan"),
                "source_text": "정상 근거",
                "source_text_sha256": hashlib.sha256("정상 근거".encode()).hexdigest(),
            }
        )
    with pytest.raises(ValidationError):
        AnswerDecision(
            schema_version=DECISION_SCHEMA,
            query_id="gold-001",
            idempotency_key="answer-" + "1" * 64,
            no_answer=False,
            citation_span_ids=("span-1",),
            numeric_facts=("0",),
            selected_revision_ids=("revision-1",),
        )


@pytest.mark.parametrize(
    ("payload", "code"),
    (
        (b'{"a":1,"a":2}\n', "json_duplicate_key"),
        (b'{"score":NaN}\n', "json_non_finite_number"),
        (b'{"a":1}', "fixture_not_canonical_lines"),
        (b' {"a":1}\n', "fixture_not_canonical_bytes"),
    ),
)
def test_canonical_jsonl_rejects_duplicates_nan_and_noncanonical_bytes(
    payload: bytes,
    code: str,
) -> None:
    with pytest.raises(GoldAnswerProducerError, match=code):
        _canonical_jsonl(payload, code="fixture")


def test_generation_source_text_mismatch_fails_before_any_provider_call(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    changed = "조작된 근거"
    changed_evidence = fixture.input_query.evidence[0].model_copy(
        update={
            "source_text": changed,
            "source_text_sha256": hashlib.sha256(changed.encode()).hexdigest(),
        }
    )
    changed_query = fixture.input_query.model_copy(update={"evidence": (changed_evidence,)})
    changed_binding = _write_jsonl(
        fixture.input_path,
        [
            fixture.input_manifest.model_dump(mode="json"),
            changed_query.model_dump(mode="json"),
        ],
    )
    fixture = replace(fixture, input_binding=changed_binding, input_query=changed_query)
    provider = _FakeProvider()

    with pytest.raises(GoldAnswerProducerError, match="answer_evidence_generation_mismatch"):
        _produce(
            fixture,
            tmp_path,
            deterministic=False,
            provider=provider,
            maximum_provider_calls=1,
        )
    assert provider.calls == 0


def test_outputs_are_create_only_and_symlinks_are_rejected(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    result = _produce(fixture, tmp_path)
    result.answer_path.chmod(0o600)
    result.answer_path.write_bytes(b"{}\n")

    with pytest.raises(GoldAnswerProducerError, match="answer_artifact_already_differs"):
        _produce(fixture, tmp_path)

    other = tmp_path / "symlink-case"
    other.mkdir()
    target = other / "target.jsonl"
    target.write_bytes(b"{}\n")
    output = other / "answers.jsonl"
    output.symlink_to(target)
    with pytest.raises(GoldAnswerProducerError, match="answer_artifact_symlink"):
        _produce(
            fixture,
            other,
            state_directory=other / "state",
            answer_path=output,
            ledger_path=other / "ledger.jsonl",
            receipt_path=other / "receipt.json",
        )


def test_state_ancestor_symlink_and_non_private_owner_directory_fail_closed(
    tmp_path: Path,
) -> None:
    fixture = _producer_fixture(tmp_path)
    real_parent = tmp_path / "real-state-parent"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-state-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(GoldAnswerProducerError, match="answer_state_symlink"):
        _produce(
            fixture,
            tmp_path / "symlink-state-case",
            state_directory=linked_parent / "state",
        )

    public_state = tmp_path / "public-state"
    public_state.mkdir(mode=0o700)
    public_state.chmod(0o755)
    with pytest.raises(GoldAnswerProducerError, match="answer_state_not_private"):
        _produce(
            fixture,
            tmp_path / "public-state-case",
            state_directory=public_state,
        )


def test_no_answer_and_revision_contracts_are_fail_closed(tmp_path: Path) -> None:
    fixture = _producer_fixture(tmp_path)
    request = build_answer_request(
        QueryIdentity("gold-001", "혜택은 무엇인가요?"),
        fixture.input_query,
        fixture.input_manifest,
    )
    no_answer = AnswerDecision(
        schema_version=DECISION_SCHEMA,
        query_id=request.query_id,
        idempotency_key=request.idempotency_key,
        no_answer=True,
    )
    rendered = _answer_from_decision(request, no_answer)
    assert rendered.no_answer is True
    assert rendered.citation_span_ids == ()

    with pytest.raises(ValidationError):
        AnswerDecision(
            schema_version=DECISION_SCHEMA,
            query_id=request.query_id,
            idempotency_key=request.idempotency_key,
            no_answer=True,
            citation_span_ids=(request.evidence[0].span_id,),
        )
    wrong_revision = AnswerDecision(
        schema_version=DECISION_SCHEMA,
        query_id=request.query_id,
        idempotency_key=request.idempotency_key,
        no_answer=False,
        citation_span_ids=(request.evidence[0].span_id,),
        selected_revision_ids=("fabricated-revision",),
    )
    with pytest.raises(GoldAnswerProducerError, match="answer_revision_binding_mismatch"):
        _answer_from_decision(request, wrong_revision)


@pytest.mark.parametrize(
    ("override", "code"),
    (
        ({"expected_gold_sha256": "0" * 64}, "gold_sha256_mismatch"),
        ({"expected_input_sha256": "0" * 64}, "answer_input_sha256_mismatch"),
        ({"expected_source_commit": "2" * 40}, "candidate_source_commit_mismatch"),
        ({"expected_answer_profile_id": "other-profile"}, "answer_profile_id_mismatch"),
    ),
)
def test_exact_bindings_reject_stale_gold_input_source_and_profile(
    tmp_path: Path,
    override: dict[str, str],
    code: str,
) -> None:
    fixture = _producer_fixture(tmp_path)
    with pytest.raises(GoldAnswerProducerError, match=code):
        _produce(fixture, tmp_path, **override)
