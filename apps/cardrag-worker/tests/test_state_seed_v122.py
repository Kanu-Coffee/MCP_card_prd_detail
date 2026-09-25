"""Tests for fail-closed v1.0.28 state seed and lineage/OCR cache recovery (v122)."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cardrag_core.canonical import canonical_json_bytes, canonical_sha256
from cardrag_core.domain import ArtifactRef
from cardrag_core.manifests import OCRArtifactManifest
from cardrag_core.ocr import NativeOCRContract, OCRInput, native_ocr_reuse_key, verify_ocr_bytes
from helpers import pdf_bytes
from typer.testing import CliRunner

from cardrag_worker.cli import app
from cardrag_worker.contracts import SourceRecord
from cardrag_worker.ocr import (
    OCRResolver,
    OCRSeedPreflightError,
)
from cardrag_worker.pdf_cache import PDFCache, PDFSourceIdentity
from cardrag_worker.pipeline import _known_snapshot_sources
from cardrag_worker.providers import DocumentOCRProvider
from cardrag_worker.revision_history_v5 import plan_revision_history_v5
from cardrag_worker.state import WorkerState
from cardrag_worker.state_seed_v122 import (
    StateSeedError,
    StateSeedLedger,
    apply_state_seed_v122,
    build_state_seed_v122_plan,
    load_state_seed_ledger,
)

runner = CliRunner()


def _tree_fingerprint(root: Path) -> tuple[tuple[str, int, int, str | None], ...]:
    result: list[tuple[str, int, int, str | None]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root))
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
        result.append((relative, stat.S_IFMT(info.st_mode), info.st_mtime_ns, digest))
    return tuple(result)


def _checkpoint_and_close(state: WorkerState) -> None:
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    state.close()


def _make_source_identity(label: str = "card-1") -> PDFSourceIdentity:
    discovery_sha256 = hashlib.sha256(label.encode()).hexdigest()
    return PDFSourceIdentity(
        source_id=f"source_{discovery_sha256}",
        issuer="kb",
        product_code=label.upper(),
        document_type="product-manual",
        source_url=f"https://cards.example/{label}",
        source_version="2026-01",
        source_post_id=f"post-{label}",
        discovery_sha256=discovery_sha256,
    )


@dataclass(frozen=True)
class V122Fixture:
    root: Path
    generation_id: str
    run_id: str
    pdf_hashes: tuple[str, ...]
    doc_ids: tuple[str, ...]
    native_doc_id: str
    adopted_doc_id: str
    native_ocr_hash: str
    adopted_ocr_hash: str


def _create_v122_source_fixture(tmp_path: Path) -> V122Fixture:
    root = tmp_path / "v1028-source"
    state_db = root / "worker-state.sqlite3"
    state = WorkerState(state_db)
    run_id = "bd9a4c513041462886d347afd5c46c47"
    generation_id = "g-bd9a4c513041462886d347af-9b84c2e38c14"

    # Start run
    state.start_run(run_id=run_id)
    cache = PDFCache(root, state)

    # 1. Native document
    pdf_content1 = pdf_bytes(pages=1, width=612)
    pdf_file1 = tmp_path / "native.pdf"
    pdf_file1.write_bytes(pdf_content1)
    pdf_sha1 = hashlib.sha256(pdf_content1).hexdigest()

    source_rec1 = SourceRecord(
        issuer="kb",
        product_code="CARD-NATIVE",
        product_name="Card Native",
        document_type="product-manual",
        source_url="https://cdn.example/native.pdf",
        source_version="2026-01",
        effective_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        source_post_id="post-native",
        file_name="native.pdf",
        category="credit",
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    doc_id1 = source_rec1.document_id(pdf_sha1)
    identity1 = PDFSourceIdentity(
        source_id=source_rec1.source_id,
        issuer=source_rec1.issuer,
        product_code=source_rec1.product_code,
        document_type=source_rec1.document_type,
        source_url=source_rec1.source_url,
        source_version=source_rec1.source_version,
        source_post_id=source_rec1.source_post_id,
        discovery_sha256=canonical_sha256(source_rec1.discovery_payload),
    )

    cache.ingest_and_bind(
        identity1,
        pdf_file1,
        final_url="https://cdn.example/native.pdf",
        expected_sha256=pdf_sha1,
        expected_size_bytes=len(pdf_content1),
        expected_page_count=1,
        etag='"etag1"',
        last_modified="Thu, 01 Jan 2026 00:00:00 GMT",
        replace_validators=True,
    )

    # 2. Adopted document
    pdf_content2 = pdf_bytes(pages=2, width=613)
    pdf_file2 = tmp_path / "adopted.pdf"
    pdf_file2.write_bytes(pdf_content2)
    pdf_sha2 = hashlib.sha256(pdf_content2).hexdigest()

    source_rec2 = SourceRecord(
        issuer="kb",
        product_code="CARD-ADOPTED",
        product_name="Card Adopted",
        document_type="product-manual",
        source_url="https://cdn.example/adopted.pdf",
        source_version="2026-01",
        effective_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        source_post_id="post-adopted",
        file_name="adopted.pdf",
        category="credit",
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    doc_id2 = source_rec2.document_id(pdf_sha2)
    identity2 = PDFSourceIdentity(
        source_id=source_rec2.source_id,
        issuer=source_rec2.issuer,
        product_code=source_rec2.product_code,
        document_type=source_rec2.document_type,
        source_url=source_rec2.source_url,
        source_version=source_rec2.source_version,
        source_post_id=source_rec2.source_post_id,
        discovery_sha256=canonical_sha256(source_rec2.discovery_payload),
    )

    cache.ingest_and_bind(
        identity2,
        pdf_file2,
        final_url="https://cdn.example/adopted.pdf",
        expected_sha256=pdf_sha2,
        expected_size_bytes=len(pdf_content2),
        expected_page_count=2,
        etag='"etag2"',
        last_modified="Thu, 01 Jan 2026 00:00:00 GMT",
        replace_validators=True,
    )

    # OCR artifacts
    ocr_body1 = b"## Page 1\n\nNative OCR content\n"
    ocr_sha1 = hashlib.sha256(ocr_body1).hexdigest()
    ocr_dir1 = root / "runs" / run_id / "documents" / doc_id1 / "ocr"
    ocr_dir1.mkdir(parents=True, exist_ok=True)
    (ocr_dir1 / "ocr.md").write_bytes(ocr_body1)

    contract = NativeOCRContract(
        processor_version="cardrag-worker/paddleocr-vl/1.6",
        cache_epoch=0,
        prompt_version="cardrag-ocr.ko.v2",
        prompt_sha256=hashlib.sha256(b"prompt").hexdigest(),
        renderer_id="paddlex-pdfium/300dpi",
        render_scale_milli=4167,
        provider="paddleocr",
        model="PaddleOCR-VL-1.6",
        segmentation_strategy_id="document",
        whole_document_max_pages=100,
        target_pages_per_call=100,
        context_pages_before=0,
        context_pages_after=0,
        output_policy="target-pages-only",
    )
    source_inp1 = OCRInput(pdf_sha256=pdf_sha1, pdf_size_bytes=len(pdf_content1), page_count=1)
    reuse_key1 = native_ocr_reuse_key(contract, source_inp1)
    verified1 = verify_ocr_bytes(ocr_body1, expected_page_count=1)
    manifest1 = OCRArtifactManifest(
        reuse_key=reuse_key1,
        source=source_inp1,
        contract=contract,
        output=ArtifactRef.for_cas(
            sha256=ocr_sha1,
            size_bytes=len(ocr_body1),
            media_type="text/markdown; charset=utf-8",
        ),
        ocr_chars=verified1.char_count,
        page_output_sha256=verified1.page_sha256,
        created_at=datetime.now(UTC),
    )
    (ocr_dir1 / "native-manifest.json").write_bytes(manifest1.canonical_bytes())

    ocr_body2 = b"## Page 1\n\nAdopted Page 1\n\n## Page 2\n\nAdopted Page 2\n"
    ocr_sha2 = hashlib.sha256(ocr_body2).hexdigest()
    ocr_dir2 = root / "runs" / run_id / "documents" / doc_id2 / "ocr"
    ocr_dir2.mkdir(parents=True, exist_ok=True)
    (ocr_dir2 / "ocr.md").write_bytes(ocr_body2)
    reuse_key2 = "adopted-rk-2"

    # Sealed publication
    contract_sha = "873a628ea7a4a91d217cebf2fb94489c4bc93fb7055607099a9e7933991c83ed"
    corpus_sha = hashlib.sha256(b"corpus").hexdigest()

    publish_payload = {
        "schema_version": "cardrag.worker-seal.v1",
        "channel": "candidate-v1.0.11",
        "contract_sha256": contract_sha,
        "corpus_sha256": corpus_sha,
        "generation_id": generation_id,
        "run_id": run_id,
        "manifest": {
            "schema_version": "cardrag.worker-manifest.v1",
            "channel": "candidate-v1.0.11",
            "contract_sha256": contract_sha,
            "corpus_sha256": corpus_sha,
            "generation_id": generation_id,
            "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
            "created_at": datetime.now(UTC).isoformat(),
            "documents": [
                {
                    "availability": "available",
                    "document_id": doc_id1,
                    "issuer": "kb",
                    "product_code": "CARD-NATIVE",
                    "page_count": 1,
                    "pdf": {
                        "path": f"cas/pdf/{pdf_sha1[:2]}/{pdf_sha1}",
                        "sha256": pdf_sha1,
                        "size_bytes": len(pdf_content1),
                        "media_type": "application/pdf",
                    },
                    "ocr": {
                        "path": f"cas/ocr/{ocr_sha1[:2]}/{ocr_sha1}",
                        "sha256": ocr_sha1,
                        "size_bytes": len(ocr_body1),
                        "media_type": "text/markdown; charset=utf-8",
                    },
                    "ocr_cache_kind": "native",
                    "ocr_reuse_key": reuse_key1,
                },
                {
                    "availability": "available",
                    "document_id": doc_id2,
                    "issuer": "kb",
                    "product_code": "CARD-ADOPTED",
                    "page_count": 2,
                    "pdf": {
                        "path": f"cas/pdf/{pdf_sha2[:2]}/{pdf_sha2}",
                        "sha256": pdf_sha2,
                        "size_bytes": len(pdf_content2),
                        "media_type": "application/pdf",
                    },
                    "ocr": {
                        "path": f"cas/ocr/{ocr_sha2[:2]}/{ocr_sha2}",
                        "sha256": ocr_sha2,
                        "size_bytes": len(ocr_body2),
                        "media_type": "text/markdown; charset=utf-8",
                    },
                    "ocr_cache_kind": "adopted",
                    "ocr_reuse_key": reuse_key2,
                    "is_historical": True,
                },
            ],
            "objects": [
                {
                    "path": f"runs/{run_id}/documents/{doc_id1}/ocr/ocr.md",
                    "sha256": ocr_sha1,
                    "size_bytes": len(ocr_body1),
                    "media_type": "text/markdown; charset=utf-8",
                },
                {
                    "path": f"runs/{run_id}/documents/{doc_id2}/ocr/ocr.md",
                    "sha256": ocr_sha2,
                    "size_bytes": len(ocr_body2),
                    "media_type": "text/markdown; charset=utf-8",
                },
            ],
        },
    }

    seal_dir = root / "runs" / run_id / "sealed"
    seal_dir.mkdir(parents=True, exist_ok=True)
    (seal_dir / "publish.json").write_bytes(canonical_json_bytes(publish_payload))

    # Record snapshot in state
    snapshot_payload = {
        "contract_version": "cardrag.source-snapshot.v1",
        "issuer": "kb",
        "parser_version": "kb-v1",
        "records": [
            source_rec1.discovery_payload,
            source_rec2.discovery_payload,
        ],
    }
    state.record_snapshot(
        run_id=run_id,
        snapshot_id=f"snap-{run_id}-kb",
        issuer="kb",
        source_sha256=hashlib.sha256(b"source_kb").hexdigest(),
        record_count=2,
        payload=snapshot_payload,
    )

    # Write acquisition.v1.json checkpoint
    acq_payload = {
        "schema_version": "cardrag.pdf-acquisition.v1",
        "run_id": run_id,
        "contract_sha256": contract_sha,
        "inputs": [
            {"source_id": source_rec1.source_id, "status": "succeeded", "pdf_sha256": pdf_sha1},
            {"source_id": source_rec2.source_id, "status": "succeeded", "pdf_sha256": pdf_sha2},
        ],
        "documents": [
            {
                "document_id": doc_id1,
                "source_id": source_rec1.source_id,
                "pdf_sha256": pdf_sha1,
                "pdf_size_bytes": len(pdf_content1),
                "page_count": 1,
                "is_historical": False,
            },
            {
                "document_id": doc_id2,
                "source_id": source_rec2.source_id,
                "pdf_sha256": pdf_sha2,
                "pdf_size_bytes": len(pdf_content2),
                "page_count": 2,
                "is_historical": True,
            },
        ],
    }
    acq_dir = root / "runs" / run_id / "checkpoints"
    acq_dir.mkdir(parents=True, exist_ok=True)
    (acq_dir / "acquisition.v1.json").write_bytes(canonical_json_bytes(acq_payload))

    # Finish run in DB
    state.finish_run(run_id, "succeeded", corpus_sha256=corpus_sha, contract_sha256=contract_sha)
    state.record_publish(
        generation_id=generation_id,
        run_id=run_id,
        corpus_sha256=corpus_sha,
        contract_sha256=contract_sha,
        serving_sha256=hashlib.sha256(b"serving").hexdigest(),
        status="ready",
        details={"manifest_sha256": hashlib.sha256(b"manifest").hexdigest()},
    )
    _checkpoint_and_close(state)

    assert not Path(f"{state_db}-wal").exists()
    assert not Path(f"{state_db}-shm").exists()

    return V122Fixture(
        root=root,
        generation_id=generation_id,
        run_id=run_id,
        pdf_hashes=(pdf_sha1, pdf_sha2),
        doc_ids=(doc_id1, doc_id2),
        native_doc_id=doc_id1,
        adopted_doc_id=doc_id2,
        native_ocr_hash=ocr_sha1,
        adopted_ocr_hash=ocr_sha2,
    )


def test_state_seed_plan_and_apply_idempotent(tmp_path: Path) -> None:
    fixture = _create_v122_source_fixture(tmp_path)
    before_fingerprint = _tree_fingerprint(fixture.root)

    # 1. Build Plan
    plan = build_state_seed_v122_plan(fixture.root, generation_id=fixture.generation_id)
    assert plan.generation_id == fixture.generation_id
    assert plan.run_id == fixture.run_id
    assert len(plan.cas_objects) == 2
    assert len(plan.sources) == 2
    assert len(plan.revisions) == 2
    assert len(plan.ocr_entries) == 2
    assert len(plan.prior_current_doc_ids) == 1
    assert len(plan.prior_historical_doc_ids) == 1
    assert plan.prior_current_doc_ids == (fixture.native_doc_id,)
    assert plan.prior_historical_doc_ids == (fixture.adopted_doc_id,)
    assert _tree_fingerprint(fixture.root) == before_fingerprint

    # 2. Apply Plan to Destination
    destination = tmp_path / "v122-candidate-destination"
    destination_db = destination / "worker-state.sqlite3"
    with WorkerState(destination_db) as dest_state:
        first_report = apply_state_seed_v122(plan, dest_state, destination)
        assert first_report["applied"] is True
        assert first_report["imported_pdf_objects"] == 2
        assert first_report["imported_revisions"] == 2
        assert first_report["imported_ocr_files"] == 3  # 2 ocr.md + 1 native-manifest.json
        assert first_report["reused_pdf_objects"] == 0

        # Check ledger created
        ledger = load_state_seed_ledger(destination)
        assert ledger is not None
        assert ledger.generation_id == fixture.generation_id
        assert len(ledger.entries_by_doc_id) == 2
        assert ledger.prior_current_doc_ids == frozenset({fixture.native_doc_id})
        assert ledger.prior_historical_doc_ids == frozenset({fixture.adopted_doc_id})

        # Check OCR files materialized
        native_dir = destination / "ocr-seed" / fixture.native_doc_id
        assert (native_dir / "ocr.md").is_file()
        assert (native_dir / "manifest.json").is_file()
        adopted_dir = destination / "ocr-seed" / fixture.adopted_doc_id
        assert (adopted_dir / "ocr.md").is_file()

        # 3. Idempotent re-apply
        second_report = apply_state_seed_v122(plan, dest_state, destination)
        assert second_report["imported_pdf_objects"] == 0
        assert second_report["imported_revisions"] == 0
        assert second_report["imported_ocr_files"] == 0
        assert second_report["reused_pdf_objects"] == 2
        assert second_report["reused_revisions"] == 2
        assert second_report["reused_ocr_files"] == 3


def test_active_writer_sidecars_blocked(tmp_path: Path) -> None:
    fixture = _create_v122_source_fixture(tmp_path)
    wal_file = fixture.root / "worker-state.sqlite3-wal"
    wal_file.write_bytes(b"dummy-wal")

    with pytest.raises(StateSeedError) as exc_info:
        build_state_seed_v122_plan(fixture.root, generation_id=fixture.generation_id)
    assert exc_info.value.code == "source_has_sidecars"


def test_corrupted_ocr_body_blocked(tmp_path: Path) -> None:
    fixture = _create_v122_source_fixture(tmp_path)
    ocr_file = (
        fixture.root
        / "runs"
        / fixture.run_id
        / "documents"
        / fixture.native_doc_id
        / "ocr"
        / "ocr.md"
    )
    ocr_file.write_bytes(b"tampered content")

    with pytest.raises(StateSeedError) as exc_info:
        build_state_seed_v122_plan(fixture.root, generation_id=fixture.generation_id)
    assert exc_info.value.code == "source_ocr_corrupt"


@pytest.mark.asyncio
async def test_ocr_resolver_uses_seed_ledger_and_fails_on_seed_miss(tmp_path: Path) -> None:
    fixture = _create_v122_source_fixture(tmp_path)
    destination = tmp_path / "v122-candidate-destination"
    destination_db = destination / "worker-state.sqlite3"
    plan = build_state_seed_v122_plan(fixture.root, generation_id=fixture.generation_id)
    with WorkerState(destination_db) as dest_state:
        apply_state_seed_v122(plan, dest_state, destination)

    ledger = load_state_seed_ledger(destination)
    assert ledger is not None

    mock_provider = MagicMock(spec=DocumentOCRProvider)
    mock_provider.provider = "mock-provider"
    mock_provider.model = "mock-model"
    mock_provider.render_scale_milli = 4167
    mock_provider.renderer_id = "paddlex-pdfium/300dpi"
    mock_provider.recognize_document = AsyncMock(return_value=["Page 1 content"])

    with WorkerState(destination_db) as dest_state:
        resolver = OCRResolver(
            provider=mock_provider,
            state=dest_state,
            webdav=None,
            seed_ledger=ledger,
        )

        # 1. Native seed entry hit: must resolve from seed ledger, provider not called!
        out_dir1 = destination / "test_out_1"
        native_entry = ledger.entries_by_doc_id[fixture.native_doc_id]
        res1 = await resolver.resolve(
            run_id="run-new",
            document_id=fixture.native_doc_id,
            pdf_path=destination / f"cas/pdf/{native_entry.pdf_sha256[:2]}/{native_entry.pdf_sha256}",
            pdf_sha256=native_entry.pdf_sha256,
            pdf_size_bytes=native_entry.pdf_size_bytes,
            page_count=native_entry.page_count,
            output_dir=out_dir1,
        )
        assert res1.cache_reused is True
        assert res1.provider_called is False
        assert res1.ocr_sha256 == fixture.native_ocr_hash
        assert (out_dir1 / "ocr.md").is_file()
        assert (out_dir1 / "native-manifest.json").is_file()
        mock_provider.recognize_document.assert_not_called()

        # 2. Adopted seed entry hit: must resolve from seed ledger, provider not called!
        out_dir2 = destination / "test_out_2"
        adopted_entry = ledger.entries_by_doc_id[fixture.adopted_doc_id]
        res2 = await resolver.resolve(
            run_id="run-new",
            document_id=fixture.adopted_doc_id,
            pdf_path=destination / f"cas/pdf/{adopted_entry.pdf_sha256[:2]}/{adopted_entry.pdf_sha256}",
            pdf_sha256=adopted_entry.pdf_sha256,
            pdf_size_bytes=adopted_entry.pdf_size_bytes,
            page_count=adopted_entry.page_count,
            output_dir=out_dir2,
        )
        assert res2.cache_reused is True
        assert res2.provider_called is False
        assert res2.ocr_sha256 == fixture.adopted_ocr_hash
        assert (out_dir2 / "ocr.md").is_file()
        mock_provider.recognize_document.assert_not_called()

        # 3. Seed document miss: if document was in seed generation but missing from cache,
        # fail-closed with OCRSeedPreflightError before provider invocation!
        fake_seed_doc_id = "doc_" + hashlib.sha256(b"fake-missing").hexdigest()
        # Add to seed_doc_ids without an entry
        tampered_ledger = StateSeedLedger(
            generation_id=ledger.generation_id,
            run_id=ledger.run_id,
            corpus_sha256=ledger.corpus_sha256,
            contract_sha256=ledger.contract_sha256,
            entries_by_doc_id=ledger.entries_by_doc_id,
            seed_pdf_shas=ledger.seed_pdf_shas,
            seed_doc_ids=ledger.seed_doc_ids | {fake_seed_doc_id},
            prior_current_doc_ids=ledger.prior_current_doc_ids,
            prior_historical_doc_ids=ledger.prior_historical_doc_ids,
        )
        resolver_tampered = OCRResolver(
            provider=mock_provider,
            state=dest_state,
            webdav=None,
            seed_ledger=tampered_ledger,
        )
        with pytest.raises(OCRSeedPreflightError, match="failing closed to prevent mass OCR re-processing"):
            await resolver_tampered.resolve(
                run_id="run-new",
                document_id=fake_seed_doc_id,
                pdf_path=destination / f"cas/pdf/{native_entry.pdf_sha256[:2]}/{native_entry.pdf_sha256}",
                pdf_sha256="1" * 64,
                pdf_size_bytes=100,
                page_count=1,
                output_dir=destination / "out_miss",
            )
        mock_provider.recognize_document.assert_not_called()

        # 4. Genuinely new document (not in seed): proceeds to provider!
        new_pdf_sha = "9" * 64
        new_doc_id = "doc_" + hashlib.sha256(b"brand-new").hexdigest()
        dummy_pdf_path = tmp_path / "brand_new.pdf"
        dummy_pdf_path.write_bytes(pdf_bytes(pages=1, width=612))
        res_new = await resolver.resolve(
            run_id="run-new",
            document_id=new_doc_id,
            pdf_path=dummy_pdf_path,
            pdf_sha256=new_pdf_sha,
            pdf_size_bytes=len(dummy_pdf_path.read_bytes()),
            page_count=1,
            output_dir=destination / "out_new",
        )
        assert res_new.provider_called is True
        mock_provider.recognize_document.assert_called_once()


def test_cli_seed_state_v122(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _create_v122_source_fixture(tmp_path)
    dest_dir = tmp_path / "candidate-state"
    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(dest_dir))
    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-v1.0.11")

    # 1. Dry run
    dry_result = runner.invoke(
        app,
        ["seed-state-v122", str(fixture.root), "--generation-id", fixture.generation_id],
    )
    assert dry_result.exit_code == 0, dry_result.output
    dry_json = json.loads(dry_result.output)
    assert dry_json["applied"] is False
    assert dry_json["dry_run"] is True
    assert dry_json["accepted_pdf_objects"] == 2
    assert dry_json["accepted_ocr_documents"] == 2

    # 2. Apply
    apply_result = runner.invoke(
        app,
        ["seed-state-v122", str(fixture.root), "--generation-id", fixture.generation_id, "--apply"],
    )
    assert apply_result.exit_code == 0, apply_result.output
    apply_json = json.loads(apply_result.output)
    assert apply_json["applied"] is True
    assert apply_json["idempotence_verified"] is True
    assert apply_json["imported_pdf_objects"] == 2
    assert apply_json["idempotence_imported_pdf_objects"] == 0


def test_historical_document_source_record_from_earlier_snapshot_materializes(tmp_path: Path) -> None:
    # 1. Create a fixture where source_rec2 exists ONLY in an earlier run's snapshot (run-old),
    # and the terminal publish run (run-122) only has source_rec1 in its snapshot.
    root = tmp_path / "v122-earlier-snapshot-source"
    root.mkdir(parents=True, exist_ok=True)
    state_db = root / "worker-state.sqlite3"
    generation_id = "g-test-earlier-snap"
    run_id_old = "run-old"
    run_id_term = "run-122"

    pdf_content1 = pdf_bytes(pages=1, width=612)
    pdf_content2 = pdf_bytes(pages=2, width=612)
    pdf_sha1 = hashlib.sha256(pdf_content1).hexdigest()
    pdf_sha2 = hashlib.sha256(pdf_content2).hexdigest()

    state = WorkerState(state_db)
    cache = PDFCache(root, state)

    pdf_file1 = tmp_path / "pdf1.pdf"
    pdf_file1.write_bytes(pdf_content1)
    pdf_file2 = tmp_path / "pdf2.pdf"
    pdf_file2.write_bytes(pdf_content2)

    source_rec1 = SourceRecord(
        issuer="kb",
        product_code="CARD-CURRENT",
        product_name="Card Current",
        document_type="product-manual",
        source_url="https://example.com/current.pdf",
        source_version="2026-01",
        effective_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        source_post_id="post-current",
        file_name="current.pdf",
        category="credit",
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    doc_id1 = source_rec1.document_id(pdf_sha1)
    identity1 = PDFSourceIdentity(
        source_id=source_rec1.source_id,
        issuer=source_rec1.issuer,
        product_code=source_rec1.product_code,
        document_type=source_rec1.document_type,
        source_url=source_rec1.source_url,
        source_version=source_rec1.source_version,
        source_post_id=source_rec1.source_post_id,
        discovery_sha256=canonical_sha256(source_rec1.discovery_payload),
    )
    cache.ingest_and_bind(
        identity1,
        pdf_file1,
        final_url="https://cdn.example/current.pdf",
        expected_sha256=pdf_sha1,
        expected_size_bytes=len(pdf_content1),
        expected_page_count=1,
    )

    source_rec2 = SourceRecord(
        issuer="kb",
        product_code="CARD-HISTORICAL-ONLY",
        product_name="Card Historical",
        document_type="product-manual",
        source_url="https://example.com/hist.pdf",
        source_version="2025-01",
        effective_date=datetime(2025, 1, 1, tzinfo=UTC).date(),
        source_post_id="post-hist",
        file_name="hist.pdf",
        category="credit",
        discovered_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    doc_id2 = source_rec2.document_id(pdf_sha2)
    identity2 = PDFSourceIdentity(
        source_id=source_rec2.source_id,
        issuer=source_rec2.issuer,
        product_code=source_rec2.product_code,
        document_type=source_rec2.document_type,
        source_url=source_rec2.source_url,
        source_version=source_rec2.source_version,
        source_post_id=source_rec2.source_post_id,
        discovery_sha256=canonical_sha256(source_rec2.discovery_payload),
    )
    cache.ingest_and_bind(
        identity2,
        pdf_file2,
        final_url="https://cdn.example/hist.pdf",
        expected_sha256=pdf_sha2,
        expected_size_bytes=len(pdf_content2),
        expected_page_count=2,
    )

    # In earlier run, record snapshot with source_rec2
    state.start_run(run_id=run_id_old)
    state.record_snapshot(
        run_id=run_id_old,
        snapshot_id=f"snap-{run_id_old}-kb",
        issuer="kb",
        source_sha256=hashlib.sha256(b"source_kb_old").hexdigest(),
        record_count=1,
        payload={
            "contract_version": "cardrag.source-snapshot.v1",
            "issuer": "kb",
            "parser_version": "kb-v1",
            "records": [source_rec2.discovery_payload],
        },
    )
    state.finish_run(run_id_old, "succeeded", corpus_sha256="c" * 64, contract_sha256="d" * 64)

    # In terminal run, record snapshot with ONLY source_rec1
    state.start_run(run_id=run_id_term)
    state.record_snapshot(
        run_id=run_id_term,
        snapshot_id=f"snap-{run_id_term}-kb",
        issuer="kb",
        source_sha256=hashlib.sha256(b"source_kb_term").hexdigest(),
        record_count=1,
        payload={
            "contract_version": "cardrag.source-snapshot.v1",
            "issuer": "kb",
            "parser_version": "kb-v1",
            "records": [source_rec1.discovery_payload],
        },
    )

    ocr_body1 = b"## Page 1\n\nCurrent\n"
    ocr_sha1 = hashlib.sha256(ocr_body1).hexdigest()
    ocr_dir1 = root / "runs" / run_id_term / "documents" / doc_id1 / "ocr"
    ocr_dir1.mkdir(parents=True, exist_ok=True)
    (ocr_dir1 / "ocr.md").write_bytes(ocr_body1)

    ocr_body2 = b"## Page 1\n\nHist P1\n\n## Page 2\n\nHist P2\n"
    ocr_sha2 = hashlib.sha256(ocr_body2).hexdigest()
    ocr_dir2 = root / "runs" / run_id_term / "documents" / doc_id2 / "ocr"
    ocr_dir2.mkdir(parents=True, exist_ok=True)
    (ocr_dir2 / "ocr.md").write_bytes(ocr_body2)

    contract_sha = "873a628ea7a4a91d217cebf2fb94489c4bc93fb7055607099a9e7933991c83ed"
    corpus_sha = hashlib.sha256(b"corpus-earlier").hexdigest()
    publish_payload = {
        "schema_version": "cardrag.worker-seal.v1",
        "channel": "candidate-v1.0.11",
        "contract_sha256": contract_sha,
        "corpus_sha256": corpus_sha,
        "generation_id": generation_id,
        "run_id": run_id_term,
        "manifest": {
            "schema_version": "cardrag.worker-manifest.v1",
            "channel": "candidate-v1.0.11",
            "contract_sha256": contract_sha,
            "corpus_sha256": corpus_sha,
            "generation_id": generation_id,
            "manifest_sha256": hashlib.sha256(b"man").hexdigest(),
            "created_at": datetime.now(UTC).isoformat(),
            "documents": [
                {
                    "availability": "available",
                    "document_id": doc_id1,
                    "issuer": "kb",
                    "product_code": "CARD-CURRENT",
                    "page_count": 1,
                    "pdf": {"path": f"cas/pdf/{pdf_sha1[:2]}/{pdf_sha1}", "sha256": pdf_sha1, "size_bytes": len(pdf_content1), "media_type": "application/pdf"},
                    "ocr": {"path": f"cas/ocr/{ocr_sha1[:2]}/{ocr_sha1}", "sha256": ocr_sha1, "size_bytes": len(ocr_body1), "media_type": "text/markdown; charset=utf-8"},
                    "ocr_cache_kind": "adopted",
                    "ocr_reuse_key": "rk-1",
                },
                {
                    "availability": "available",
                    "document_id": doc_id2,
                    "issuer": "kb",
                    "product_code": "CARD-HISTORICAL-ONLY",
                    "page_count": 2,
                    "pdf": {"path": f"cas/pdf/{pdf_sha2[:2]}/{pdf_sha2}", "sha256": pdf_sha2, "size_bytes": len(pdf_content2), "media_type": "application/pdf"},
                    "ocr": {"path": f"cas/ocr/{ocr_sha2[:2]}/{ocr_sha2}", "sha256": ocr_sha2, "size_bytes": len(ocr_body2), "media_type": "text/markdown; charset=utf-8"},
                    "ocr_cache_kind": "adopted",
                    "ocr_reuse_key": "rk-2",
                    "is_historical": True,
                },
            ],
            "objects": [
                {"path": f"runs/{run_id_term}/documents/{doc_id1}/ocr/ocr.md", "sha256": ocr_sha1, "size_bytes": len(ocr_body1), "media_type": "text/markdown; charset=utf-8"},
                {"path": f"runs/{run_id_term}/documents/{doc_id2}/ocr/ocr.md", "sha256": ocr_sha2, "size_bytes": len(ocr_body2), "media_type": "text/markdown; charset=utf-8"},
            ],
        },
    }
    seal_dir = root / "runs" / run_id_term / "sealed"
    seal_dir.mkdir(parents=True, exist_ok=True)
    (seal_dir / "publish.json").write_bytes(canonical_json_bytes(publish_payload))

    acq_payload = {
        "schema_version": "cardrag.pdf-acquisition.v1",
        "run_id": run_id_term,
        "contract_sha256": contract_sha,
        "inputs": [
            {"source_id": source_rec1.source_id, "status": "succeeded", "pdf_sha256": pdf_sha1},
            {"source_id": source_rec2.source_id, "status": "succeeded", "pdf_sha256": pdf_sha2},
        ],
        "documents": [
            {"document_id": doc_id1, "source_id": source_rec1.source_id, "pdf_sha256": pdf_sha1, "pdf_size_bytes": len(pdf_content1), "page_count": 1, "is_historical": False},
            {"document_id": doc_id2, "source_id": source_rec2.source_id, "pdf_sha256": pdf_sha2, "pdf_size_bytes": len(pdf_content2), "page_count": 2, "is_historical": True},
        ],
    }
    acq_dir = root / "runs" / run_id_term / "checkpoints"
    acq_dir.mkdir(parents=True, exist_ok=True)
    (acq_dir / "acquisition.v1.json").write_bytes(canonical_json_bytes(acq_payload))

    state.finish_run(run_id_term, "succeeded", corpus_sha256=corpus_sha, contract_sha256=contract_sha)
    state.record_publish(
        generation_id=generation_id,
        run_id=run_id_term,
        corpus_sha256=corpus_sha,
        contract_sha256=contract_sha,
        serving_sha256=hashlib.sha256(b"srv").hexdigest(),
        status="ready",
        details={"manifest_sha256": hashlib.sha256(b"man").hexdigest()},
    )
    _checkpoint_and_close(state)

    # 2. Plan and apply seed
    plan = build_state_seed_v122_plan(root, generation_id=generation_id)
    plan_sources = {s.source_id: s for s in plan.source_records}
    assert source_rec2.source_id in plan_sources
    assert plan_sources[source_rec2.source_id].product_code == "CARD-HISTORICAL-ONLY"

    destination = tmp_path / "dest-earlier"
    dest_db = destination / "worker-state.sqlite3"
    with WorkerState(dest_db) as dest_state:
        apply_state_seed_v122(plan, dest_state, destination)

    ledger = load_state_seed_ledger(destination)
    assert ledger is not None
    assert source_rec2.source_id in ledger.source_records

    # 3. Verify revision expansion in new run resolves source_rec2 via seed_ledger
    with WorkerState(dest_db) as dest_state:
        known = _known_snapshot_sources(dest_state, (), [source_rec1], seed_ledger=ledger)
        assert source_rec2.source_id in known

        lineage = dest_state.pdf_cache_lineage_history(
            issuer=source_rec2.issuer,
            product_code=source_rec2.product_code,
            document_type=source_rec2.document_type,
        )
        assert len(lineage) >= 1
        history_plan = plan_revision_history_v5(
            current_source=source_rec2,
            current_pdf_sha256=pdf_sha2,
            rows=lineage,
            known_sources=known,
        )
        assert len(history_plan.unresolved_revisions) == 0
        assert len(history_plan.candidates) == 1
        assert history_plan.candidates[0].source.source_id == source_rec2.source_id


def test_two_sources_sharing_same_pdf_sha_bound_exactly(tmp_path: Path) -> None:
    # Two distinct products sharing the EXACT SAME pdf bytes / sha256
    root = tmp_path / "v122-shared-pdf-source"
    root.mkdir(parents=True, exist_ok=True)
    state_db = root / "worker-state.sqlite3"
    generation_id = "g-test-shared-pdf"
    run_id = "run-shared"

    pdf_content = pdf_bytes(pages=1, width=612)
    shared_pdf_sha = hashlib.sha256(pdf_content).hexdigest()

    state = WorkerState(state_db)
    cache = PDFCache(root, state)

    pdf_file = tmp_path / "shared.pdf"
    pdf_file.write_bytes(pdf_content)

    source_A = SourceRecord(
        issuer="kb",
        product_code="CARD-PROD-A",
        product_name="Product A",
        document_type="product-manual",
        source_url="https://example.com/shared.pdf",
        source_version="2026-01",
        effective_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        source_post_id="post-A",
        file_name="shared.pdf",
        category="credit",
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    doc_id_A = source_A.document_id(shared_pdf_sha)
    identity_A = PDFSourceIdentity(
        source_id=source_A.source_id,
        issuer=source_A.issuer,
        product_code=source_A.product_code,
        document_type=source_A.document_type,
        source_url=source_A.source_url,
        source_version=source_A.source_version,
        source_post_id=source_A.source_post_id,
        discovery_sha256=canonical_sha256(source_A.discovery_payload),
    )
    cache.ingest_and_bind(
        identity_A,
        pdf_file,
        final_url="https://cdn.example/shared-A.pdf",
        expected_sha256=shared_pdf_sha,
        expected_size_bytes=len(pdf_content),
        expected_page_count=1,
    )

    source_B = SourceRecord(
        issuer="kb",
        product_code="CARD-PROD-B",
        product_name="Product B",
        document_type="product-manual",
        source_url="https://example.com/shared.pdf",
        source_version="2026-01",
        effective_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        source_post_id="post-B",
        file_name="shared.pdf",
        category="credit",
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    doc_id_B = source_B.document_id(shared_pdf_sha)
    identity_B = PDFSourceIdentity(
        source_id=source_B.source_id,
        issuer=source_B.issuer,
        product_code=source_B.product_code,
        document_type=source_B.document_type,
        source_url=source_B.source_url,
        source_version=source_B.source_version,
        source_post_id=source_B.source_post_id,
        discovery_sha256=canonical_sha256(source_B.discovery_payload),
    )
    cache.ingest_and_bind(
        identity_B,
        pdf_file,
        final_url="https://cdn.example/shared-B.pdf",
        expected_sha256=shared_pdf_sha,
        expected_size_bytes=len(pdf_content),
        expected_page_count=1,
    )

    assert doc_id_A != doc_id_B

    state.start_run(run_id=run_id)
    state.record_snapshot(
        run_id=run_id,
        snapshot_id=f"snap-{run_id}-kb",
        issuer="kb",
        source_sha256=hashlib.sha256(b"src-shared").hexdigest(),
        record_count=2,
        payload={
            "contract_version": "cardrag.source-snapshot.v1",
            "issuer": "kb",
            "parser_version": "kb-v1",
            "records": [source_A.discovery_payload, source_B.discovery_payload],
        },
    )

    ocr_body_A = b"## Page 1\n\nProd A\n"
    ocr_sha_A = hashlib.sha256(ocr_body_A).hexdigest()
    ocr_dir_A = root / "runs" / run_id / "documents" / doc_id_A / "ocr"
    ocr_dir_A.mkdir(parents=True, exist_ok=True)
    (ocr_dir_A / "ocr.md").write_bytes(ocr_body_A)

    ocr_body_B = b"## Page 1\n\nProd B\n"
    ocr_sha_B = hashlib.sha256(ocr_body_B).hexdigest()
    ocr_dir_B = root / "runs" / run_id / "documents" / doc_id_B / "ocr"
    ocr_dir_B.mkdir(parents=True, exist_ok=True)
    (ocr_dir_B / "ocr.md").write_bytes(ocr_body_B)

    contract_sha = "873a628ea7a4a91d217cebf2fb94489c4bc93fb7055607099a9e7933991c83ed"
    corpus_sha = hashlib.sha256(b"corpus-shared").hexdigest()
    publish_payload = {
        "schema_version": "cardrag.worker-seal.v1",
        "channel": "candidate-v1.0.11",
        "contract_sha256": contract_sha,
        "corpus_sha256": corpus_sha,
        "generation_id": generation_id,
        "run_id": run_id,
        "manifest": {
            "schema_version": "cardrag.worker-manifest.v1",
            "channel": "candidate-v1.0.11",
            "contract_sha256": contract_sha,
            "corpus_sha256": corpus_sha,
            "generation_id": generation_id,
            "manifest_sha256": hashlib.sha256(b"man").hexdigest(),
            "created_at": datetime.now(UTC).isoformat(),
            "documents": [
                {
                    "availability": "available",
                    "document_id": doc_id_A,
                    "issuer": "kb",
                    "product_code": "CARD-PROD-A",
                    "page_count": 1,
                    "pdf": {"path": f"cas/pdf/{shared_pdf_sha[:2]}/{shared_pdf_sha}", "sha256": shared_pdf_sha, "size_bytes": len(pdf_content), "media_type": "application/pdf"},
                    "ocr": {"path": f"cas/ocr/{ocr_sha_A[:2]}/{ocr_sha_A}", "sha256": ocr_sha_A, "size_bytes": len(ocr_body_A), "media_type": "text/markdown; charset=utf-8"},
                    "ocr_cache_kind": "adopted",
                    "ocr_reuse_key": "rk-A",
                },
                {
                    "availability": "available",
                    "document_id": doc_id_B,
                    "issuer": "kb",
                    "product_code": "CARD-PROD-B",
                    "page_count": 1,
                    "pdf": {"path": f"cas/pdf/{shared_pdf_sha[:2]}/{shared_pdf_sha}", "sha256": shared_pdf_sha, "size_bytes": len(pdf_content), "media_type": "application/pdf"},
                    "ocr": {"path": f"cas/ocr/{ocr_sha_B[:2]}/{ocr_sha_B}", "sha256": ocr_sha_B, "size_bytes": len(ocr_body_B), "media_type": "text/markdown; charset=utf-8"},
                    "ocr_cache_kind": "adopted",
                    "ocr_reuse_key": "rk-B",
                },
            ],
            "objects": [
                {"path": f"runs/{run_id}/documents/{doc_id_A}/ocr/ocr.md", "sha256": ocr_sha_A, "size_bytes": len(ocr_body_A), "media_type": "text/markdown; charset=utf-8"},
                {"path": f"runs/{run_id}/documents/{doc_id_B}/ocr/ocr.md", "sha256": ocr_sha_B, "size_bytes": len(ocr_body_B), "media_type": "text/markdown; charset=utf-8"},
            ],
        },
    }
    seal_dir = root / "runs" / run_id / "sealed"
    seal_dir.mkdir(parents=True, exist_ok=True)
    (seal_dir / "publish.json").write_bytes(canonical_json_bytes(publish_payload))

    acq_payload = {
        "schema_version": "cardrag.pdf-acquisition.v1",
        "run_id": run_id,
        "contract_sha256": contract_sha,
        "inputs": [
            {"source_id": source_A.source_id, "status": "succeeded", "pdf_sha256": shared_pdf_sha},
            {"source_id": source_B.source_id, "status": "succeeded", "pdf_sha256": shared_pdf_sha},
        ],
        "documents": [
            {"document_id": doc_id_A, "source_id": source_A.source_id, "pdf_sha256": shared_pdf_sha, "pdf_size_bytes": len(pdf_content), "page_count": 1, "is_historical": False},
            {"document_id": doc_id_B, "source_id": source_B.source_id, "pdf_sha256": shared_pdf_sha, "pdf_size_bytes": len(pdf_content), "page_count": 1, "is_historical": False},
        ],
    }
    acq_dir = root / "runs" / run_id / "checkpoints"
    acq_dir.mkdir(parents=True, exist_ok=True)
    (acq_dir / "acquisition.v1.json").write_bytes(canonical_json_bytes(acq_payload))

    state.finish_run(run_id, "succeeded", corpus_sha256=corpus_sha, contract_sha256=contract_sha)
    state.record_publish(
        generation_id=generation_id,
        run_id=run_id,
        corpus_sha256=corpus_sha,
        contract_sha256=contract_sha,
        serving_sha256=hashlib.sha256(b"srv-shared").hexdigest(),
        status="ready",
        details={"manifest_sha256": hashlib.sha256(b"man").hexdigest()},
    )
    _checkpoint_and_close(state)

    # Build plan
    plan = build_state_seed_v122_plan(root, generation_id=generation_id)
    assert len(plan.cas_objects) == 1
    assert len(plan.ocr_entries) == 2
    assert len(plan.source_records) == 2
    plan_sources = {s.source_id: s for s in plan.source_records}
    assert plan_sources[source_A.source_id].product_code == "CARD-PROD-A"
    assert plan_sources[source_B.source_id].product_code == "CARD-PROD-B"

    # Apply to destination
    destination = tmp_path / "dest-shared"
    dest_db = destination / "worker-state.sqlite3"
    with WorkerState(dest_db) as dest_state:
        report = apply_state_seed_v122(plan, dest_state, destination)
        assert report["imported_pdf_objects"] == 1
        assert report["imported_ocr_files"] == 2

    ledger = load_state_seed_ledger(destination)
    assert ledger is not None
    assert ledger.entries_by_doc_id[doc_id_A].source_id == source_A.source_id
    assert ledger.entries_by_doc_id[doc_id_B].source_id == source_B.source_id
    assert ledger.source_records[source_A.source_id].product_code == "CARD-PROD-A"
    assert ledger.source_records[source_B.source_id].product_code == "CARD-PROD-B"


def test_tampered_or_conflicting_source_records_fail_closed(tmp_path: Path) -> None:
    # Sub-case a: missing source_id in snapshot
    fixture_a = _create_v122_source_fixture(tmp_path / "case_a")
    acq_path_a = fixture_a.root / "runs" / fixture_a.run_id / "checkpoints" / "acquisition.v1.json"
    acq_data_a = json.loads(acq_path_a.read_text())
    acq_data_a["documents"][0]["source_id"] = "source_" + "0" * 64
    acq_path_a.write_bytes(canonical_json_bytes(acq_data_a))

    with pytest.raises(StateSeedError) as exc_a:
        build_state_seed_v122_plan(fixture_a.root, generation_id=fixture_a.generation_id)
    assert "source_record_missing" in exc_a.value.code or "source_metadata_unresolved" in exc_a.value.code

    # Sub-case b: tampered source binding in acquisition (source_id does not compute to document_id)
    fixture_b = _create_v122_source_fixture(tmp_path / "case_b")
    acq_path_b = fixture_b.root / "runs" / fixture_b.run_id / "checkpoints" / "acquisition.v1.json"
    acq_data_b = json.loads(acq_path_b.read_text())
    # doc1 now points to source_rec2's source_id, which computes to doc_id2 != doc_id1
    acq_data_b["documents"][0]["source_id"] = acq_data_b["documents"][1]["source_id"]
    acq_path_b.write_bytes(canonical_json_bytes(acq_data_b))

    with pytest.raises(StateSeedError) as exc_b:
        build_state_seed_v122_plan(fixture_b.root, generation_id=fixture_b.generation_id)
    assert "document_identity_mismatch" in exc_b.value.code or "source_record_mismatch" in exc_b.value.code

    # Sub-case c: conflicting snapshots for same source_id
    fixture_c = _create_v122_source_fixture(tmp_path / "case_c")
    state_c = WorkerState(fixture_c.root / "worker-state.sqlite3")
    tampered_rec = {
        "issuer": "kb",
        "product_code": "CARD-NATIVE",
        "product_name": "TAMPERED DIFFERENT NAME",
        "document_type": "product-manual",
        "source_url": "https://example.com/tampered.pdf",
        "source_version": "2026-01",
        "effective_date": "INVALID-DATE",
        "source_post_id": "post-native",
        "file_name": "native.pdf",
        "category": "credit",
        "discovered_at": "2026-01-01T00:00:00+00:00",
    }
    state_c.start_run(run_id="run-conflict")
    state_c.record_snapshot(
        run_id="run-conflict",
        snapshot_id="snap-conflict",
        issuer="kb",
        source_sha256=hashlib.sha256(b"conflict").hexdigest(),
        record_count=1,
        payload={
            "contract_version": "cardrag.source-snapshot.v1",
            "issuer": "kb",
            "parser_version": "kb-v1",
            "records": [tampered_rec],
        },
    )
    state_c.finish_run(run_id="run-conflict", status="succeeded", corpus_sha256="c" * 64, contract_sha256="d" * 64)
    _checkpoint_and_close(state_c)

    with pytest.raises(StateSeedError) as exc_c:
        build_state_seed_v122_plan(fixture_c.root, generation_id=fixture_c.generation_id)
    assert "invalid_source_metadata" in exc_c.value.code


def test_schema_compatibility_rejects_v1_ledger_and_invalid_ledger(tmp_path: Path) -> None:
    dest = tmp_path / "compat-test"
    dest.mkdir(parents=True, exist_ok=True)
    ledger_dir = dest / "audit-reports" / "state-seed"
    ledger_dir.mkdir(parents=True, exist_ok=True)

    # 1. v1 ledger rejected
    v1_payload = {
        "schema_version": "cardrag.state-seed-ledger.v1",
        "status": "applied",
        "generation_id": "g-1",
        "run_id": "r-1",
        "corpus_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
        "ocr_documents": [],
    }
    v1_bytes = canonical_json_bytes(v1_payload)
    v1_sha = hashlib.sha256(v1_bytes).hexdigest()
    ledger_file_1 = ledger_dir / f"{v1_sha}.json"
    ledger_file_1.write_bytes(v1_bytes)

    with pytest.raises(StateSeedError) as exc_v1:
        load_state_seed_ledger(dest)
    assert exc_v1.value.code == "seed_ledger_schema_mismatch"
    ledger_file_1.unlink()

    # 2. v2 ledger missing source_records rejected
    v2_missing = {
        "schema_version": "cardrag.state-seed-ledger.v2",
        "status": "applied",
        "generation_id": "g-1",
        "run_id": "r-1",
        "corpus_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
        "ocr_documents": [],
    }
    v2_bytes = canonical_json_bytes(v2_missing)
    v2_sha = hashlib.sha256(v2_bytes).hexdigest()
    ledger_file_2 = ledger_dir / f"{v2_sha}.json"
    ledger_file_2.write_bytes(v2_bytes)

    with pytest.raises(StateSeedError) as exc_v2:
        load_state_seed_ledger(dest)
    assert exc_v2.value.code == "seed_ledger_source_records_missing"
    ledger_file_2.unlink()

    # 3. Checksum tampering rejected (filename hash does not match content)
    bad_sha = "0" * 64
    bad_file = ledger_dir / f"{bad_sha}.json"
    bad_file.write_bytes(b'{"dummy": true}')
    with pytest.raises(StateSeedError) as exc_hash:
        load_state_seed_ledger(dest)
    assert exc_hash.value.code == "seed_ledger_hash_mismatch"


def test_destination_collision_and_safety_checks(tmp_path: Path) -> None:
    fixture = _create_v122_source_fixture(tmp_path / "coll_src")
    plan = build_state_seed_v122_plan(fixture.root, generation_id=fixture.generation_id)

    # 1. Source and destination overlap rejected
    with pytest.raises(StateSeedError) as exc_overlap, WorkerState(fixture.root / "worker-state.sqlite3") as s:
        apply_state_seed_v122(plan, s, fixture.root)
    assert exc_overlap.value.code == "source_destination_overlap"

    # 2. Conflicting destination revision rejected
    dest = tmp_path / "coll_dest"
    dest.mkdir(parents=True, exist_ok=True)
    dest_db = dest / "worker-state.sqlite3"
    with WorkerState(dest_db) as dest_state:
        cache = PDFCache(dest, dest_state)
        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(pdf_bytes(pages=1, width=612))
        cache.ingest_and_bind(
            plan.sources[0].identity,
            dummy_pdf,
            final_url="https://example.com/conflicting.pdf",
            expected_sha256=hashlib.sha256(dummy_pdf.read_bytes()).hexdigest(),
            expected_size_bytes=len(dummy_pdf.read_bytes()),
            expected_page_count=1,
        )

        with pytest.raises(StateSeedError) as exc_rev:
            apply_state_seed_v122(plan, dest_state, dest)
        assert exc_rev.value.code == "destination_revision_conflict"

