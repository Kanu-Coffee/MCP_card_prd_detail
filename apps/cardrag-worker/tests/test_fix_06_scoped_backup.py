"""019 FIX_06 regression tests.

Covers: (1) generation-only OCR (seal reuse key null) whose body/manifest/READY
are cryptographically linked to a selected sealed document must be included in
the scoped backup allowance and drained by the formal retry path; (2) bounded
export vector memo and internal export phase instrumentation; (3) issuer
execution mode propagation for manual selected runs and frozen-scope resume
compatibility.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cardrag_core import (
    NativeOCRContract,
    OCRArtifactManifest,
    OCRReady,
    object_path,
    ocr_manifest_path,
    ocr_ready_path,
    ocr_variant_manifest_path,
    ocr_variant_ready_path,
)
from cardrag_core.manifests import ArtifactRef
from cardrag_core.ocr import OCRInput, native_ocr_reuse_key

from cardrag_worker.backup import (
    BackupLedger,
    resolve_ocr_bundle_paths,
    verified_document_ocr_bundles,
)
from cardrag_worker.partial_cli import resolve_partial_execution_mode
from cardrag_worker.partial_execution import PartialExecutionError
from cardrag_worker.pipeline import BoundedVectorFetchMemo


def _contract() -> NativeOCRContract:
    return NativeOCRContract(
        processor_version="cardrag-worker/test-fix-06",
        prompt_version="cardrag-ocr.ko.v2",
        prompt_sha256="1" * 64,
        renderer_id="pypdfium2/test",
        render_scale_milli=6000,
        provider="opencode",
        model="test-model",
        reasoning_effort="medium",
        output_policy="target-pages-only",
        segmentation_strategy_id="cardrag.ocr.windowed-continuity.v1",
        target_pages_per_call=2,
        context_pages_before=1,
        context_pages_after=1,
        whole_document_max_pages=4,
    )


def _settings(state_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        backup_mode="hybrid",
        backup_every_runs=7,
        backup_new_ocr_count=30,
        backup_new_bytes=1024**3,
        backup_max_pending_age_hours=168,
        state_dir=state_dir,
        webdav_base_url=None,
    )


class RecordingClient:
    """put/get-only client so flush exercises remote verification reads."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    async def put(self, path: str, content: bytes) -> None:
        self.puts.append(path)
        self.objects[path] = content

    async def get(self, path: str) -> bytes | None:
        return self.objects.get(path)

    async def close(self) -> None:
        pass


def _make_document(
    state_dir: Path,
    run_id: str,
    *,
    document_id: str,
    issuer: str,
    reuse_key_present: bool = False,
    cache_kind: str | None = None,
    tamper: str | None = None,
) -> dict[str, Any]:
    """Create run-local native OCR artifacts plus the matching seal document."""
    pdf_body = f"%PDF-{document_id}".encode()
    pdf_sha = hashlib.sha256(pdf_body).hexdigest()
    ocr_text = f"## Page 1\n\n{document_id} 혜택\n"
    ocr_bytes = ocr_text.encode("utf-8")
    ocr_sha = hashlib.sha256(ocr_bytes).hexdigest()

    manifest_pdf_sha = "e" * 64 if tamper == "pdf_mismatch" else pdf_sha
    source = OCRInput(pdf_sha256=manifest_pdf_sha, pdf_size_bytes=len(pdf_body), page_count=1)
    manifest = OCRArtifactManifest(
        reuse_key=native_ocr_reuse_key(_contract(), source),
        source=source,
        contract=_contract(),
        output=ArtifactRef(
            media_type="text/markdown; charset=utf-8",
            path=f"v1/objects/sha256/{ocr_sha[:2]}/{ocr_sha}",
            sha256=ocr_sha,
            size_bytes=len(ocr_bytes),
        ),
        ocr_chars=len(ocr_text),
        page_output_sha256=(hashlib.sha256(ocr_bytes).hexdigest(),),
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
    )

    ocr_dir = state_dir / "runs" / run_id / "documents" / document_id / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    manifest_bytes = manifest.canonical_bytes()
    if tamper == "noncanonical":
        manifest_bytes = json.dumps(manifest.model_dump(mode="json"), indent=2).encode()
    if tamper != "orphan_ready":
        (ocr_dir / "native-manifest.json").write_bytes(manifest_bytes)
    if tamper == "orphan_ready":
        ready = OCRReady(
            reuse_key=manifest.reuse_key,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            ocr_sha256=ocr_sha,
        )
        (ocr_dir / "READY.json").write_bytes(ready.canonical_bytes())
        (ocr_dir / "ocr.md").write_bytes(ocr_bytes)
    else:
        (ocr_dir / "ocr.md").write_bytes(ocr_bytes if tamper != "body_hash" else b"tampered body")

    sealed_pdf_path = state_dir / "runs" / run_id / "documents" / f"{document_id}.pdf"
    sealed_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    sealed_pdf_path.write_bytes(pdf_body)
    sealed_ocr_local = state_dir / "runs" / run_id / "objects" / ocr_sha
    sealed_ocr_local.parent.mkdir(parents=True, exist_ok=True)
    sealed_ocr_local.write_bytes(ocr_bytes)

    doc: dict[str, Any] = {
        "document_id": document_id,
        "issuer": issuer,
        "page_count": 1,
        "pdf": {
            "sha256": pdf_sha,
            "size_bytes": len(pdf_body),
            "media_type": "application/pdf",
            "path": f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
        },
        "ocr": {
            "sha256": ocr_sha,
            "size_bytes": len(ocr_bytes),
            "media_type": "text/markdown; charset=utf-8",
            "path": f"v1/objects/sha256/{ocr_sha[:2]}/{ocr_sha}",
        },
    }
    if reuse_key_present:
        doc["ocr_cache_kind"] = cache_kind
        doc["ocr_reuse_key"] = manifest.reuse_key if cache_kind == "native" else "c" * 64
    else:
        doc["ocr_cache_kind"] = None
        doc["ocr_reuse_key"] = None

    return {
        "doc": doc,
        "manifest": manifest,
        "objects": [
            {
                "sha256": pdf_sha,
                "size_bytes": len(pdf_body),
                "media_type": "application/pdf",
                "path": str(sealed_pdf_path),
            },
            {
                "sha256": ocr_sha,
                "size_bytes": len(ocr_bytes),
                "media_type": "text/markdown; charset=utf-8",
                "path": str(sealed_ocr_local),
            },
        ],
        "ocr_sha": ocr_sha,
        "pdf_sha": pdf_sha,
    }


def _write_seal(state_dir: Path, run_id: str, built: list[dict[str, Any]]) -> None:
    sealed_dir = state_dir / "runs" / run_id / "sealed"
    sealed_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "cardrag.publish-seal.v1",
        "run_id": run_id,
        "generation_id": f"g-{run_id}",
        "manifest": {"documents": [item["doc"] for item in built]},
        "objects": [obj for item in built for obj in item["objects"]],
    }
    (sealed_dir / "publish.json").write_text(json.dumps(payload), encoding="utf-8")


def _pending_rows(ledger: BackupLedger) -> list[dict[str, Any]]:
    with ledger._get_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM backup_pending")]


def test_live_like_generation_only_bundle_is_flushed_and_idempotent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-woori"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])

    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    # Mid-run intent recorder, then the scoped completion recorder.
    ledger.record_document_ocr(run_id, "doc-woori-1", state_dir, settings)  # type: ignore[arg-type]
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]

    allowed = ledger.scope_remote_paths(run_id, state_dir, ("woori",))
    bundle_paths = {
        object_path(sel["ocr_sha"]).as_posix(),
        ocr_manifest_path(sel["manifest"].reuse_key, kind="native").as_posix(),
        ocr_ready_path(sel["manifest"].reuse_key, kind="native").as_posix(),
    }
    assert bundle_paths <= allowed
    assert object_path(sel["pdf_sha"]).as_posix() in allowed

    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(settings, client, force=True, allowed_remote_paths=allowed)  # type: ignore[arg-type]
    )
    assert result["status"] == "succeeded", result
    assert result["in_scope_remaining"] == 0
    assert result["pending_out_of_scope"] == 0
    assert result["uploaded_count"] == 4  # pdf CAS, ocr body CAS, manifest, READY

    for rel in bundle_paths:
        assert rel in client.objects, f"{rel} missing from remote transport"
    with ledger._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backup_pending").fetchone()[0] == 0
        receipts = {row[0] for row in conn.execute("SELECT remote_path FROM backup_receipts").fetchall()}
    assert bundle_paths <= receipts

    index_doc = json.loads(client.objects["v1/backup/index.json"])
    entries = {item["remote_path"]: item for item in index_doc["items"]}
    man_rel = ocr_manifest_path(sel["manifest"].reuse_key, kind="native").as_posix()
    ready_rel = ocr_ready_path(sel["manifest"].reuse_key, kind="native").as_posix()
    for rel in (man_rel, ready_rel):
        assert entries[rel]["document_id"] == "doc-woori-1"
        assert entries[rel]["reuse_key"] == sel["manifest"].reuse_key

    # Formal retry is idempotent: receipts exist, nothing retransports.
    client2 = RecordingClient()
    second = asyncio.run(
        ledger.flush(settings, client2, force=True, allowed_remote_paths=allowed)  # type: ignore[arg-type]
    )
    assert second["status"] == "unchanged" and second["requests"] == 0


def test_scoped_backup_excludes_nonselected_and_forged_pending(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-mixed"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    other = _make_document(state_dir, run_id, document_id="doc-lotte-1", issuer="lotte")
    _write_seal(state_dir, run_id, [sel, other])

    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    # Simulated leftover intent registrations for the non-selected document.
    ledger.record_document_ocr(run_id, "doc-lotte-1", state_dir, settings)  # type: ignore[arg-type]

    forged_rel = ocr_manifest_path("f" * 64, kind="native").as_posix()
    with ledger._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO backup_pending
            (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type,
             created_at, status)
            VALUES ('man-forged', 'ocr_manifest', ?, 10, ?, ?, 'application/json', 0, 'pending')
            """,
            (
                "f" * 64,
                str(
                    state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json"
                ),
                forged_rel,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{forged_rel}", "doc-woori-1"),
        )

    allowed = ledger.scope_remote_paths(run_id, state_dir, ("woori",))
    # The forged row is doc_map-tagged to a selected document: it stays visible
    # in scope (silently dropping it and calling that "complete" is forbidden)
    # but no verified evidence binds it, so it is held, never transported.
    assert forged_rel in allowed, "selected-document rows must stay visible in scope"
    _allowed_full, expected_map = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    assert forged_rel not in expected_map, "a lone doc_map string must not bind allowance"
    lotte_manifest = ocr_manifest_path(other["manifest"].reuse_key, kind="native").as_posix()
    assert lotte_manifest not in allowed
    assert object_path(other["ocr_sha"]).as_posix() not in allowed

    client = RecordingClient()
    _allowed, expected_map2 = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected_map2,
        )
    )
    assert result["in_scope_remaining"] == 1  # the held forged row only
    assert result["in_scope_unbound"] == 1
    assert result["backup_scope_complete"] is False
    assert result["pending_out_of_scope"] == 3  # lotte body/manifest/READY
    assert lotte_manifest not in client.objects
    assert forged_rel not in client.objects
    assert object_path(other["ocr_sha"]).as_posix() not in client.objects
    transported = {p for p in client.puts if not p.startswith("v1/backup/")}
    assert transported <= set(expected_map2)


@pytest.mark.parametrize(
    "tamper",
    ["noncanonical", "pdf_mismatch", "body_hash", "orphan_ready", "page_mismatch"],
)
def test_bundle_linkage_fails_closed_on_inconsistent_evidence(tamper: str, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-tamper"
    if tamper == "orphan_ready":
        built = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
        ocr_dir = state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr"
        (ocr_dir / "native-manifest.json").unlink()
        (ocr_dir / "READY.json").write_bytes(
            OCRReady(
                reuse_key="a" * 64,
                manifest_sha256="b" * 64,
                ocr_sha256=built["ocr_sha"],
            ).canonical_bytes()
        )
    else:
        built = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori", tamper=tamper)
    if tamper == "page_mismatch":
        built["doc"]["page_count"] = 3
    _write_seal(state_dir, run_id, [built])

    assert verified_document_ocr_bundles(state_dir, run_id, built["doc"]) == []

    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    allowed = ledger.scope_remote_paths(run_id, state_dir, ("woori",))
    # The sealed document references themselves remain allowed.
    assert object_path(built["pdf_sha"]).as_posix() in allowed
    assert object_path(built["ocr_sha"]).as_posix() in allowed
    # Unverifiable bundles never grant their metadata paths.
    man_rel = ocr_manifest_path(built["manifest"].reuse_key, kind="native").as_posix()
    ready_rel = ocr_ready_path(built["manifest"].reuse_key, kind="native").as_posix()
    assert man_rel not in allowed
    assert ready_rel not in allowed


def test_pending_bundles_recovered_by_later_scoped_retry(tmp_path: Path) -> None:
    """Existing pending rows drain through the formal retry path, post-verification only."""
    state_dir = tmp_path / "state"
    run_id = "run-retry"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_document_ocr(run_id, "doc-woori-1", state_dir, settings)  # type: ignore[arg-type]
    # Pre-fix behavior left the metadata pair outside the allow set; the verified
    # bundle linkage now includes it and the scoped flush drains it.
    allowed = ledger.scope_remote_paths(run_id, state_dir, ("woori",))
    man_rel = ocr_manifest_path(sel["manifest"].reuse_key, kind="native").as_posix()
    assert man_rel in allowed
    before = {row["remote_path"] for row in _pending_rows(ledger)}
    assert man_rel in before

    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(settings, client, force=True, allowed_remote_paths=allowed)  # type: ignore[arg-type]
    )
    assert result["status"] == "succeeded" and result["in_scope_remaining"] == 0
    assert _pending_rows(ledger) == []


def test_scoped_flush_index_commit_failure_keeps_pending_and_retries_idempotently(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-indexfail"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    allowed = ledger.scope_remote_paths(run_id, state_dir, ("woori",))

    class FailIndexClient(RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        async def put(self, path: str, content: bytes) -> None:
            if path == "v1/backup/index.json" and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("index commit refused")
            await super().put(path, content)

    failing = FailIndexClient()
    result = asyncio.run(
        ledger.flush(settings, failing, force=True, allowed_remote_paths=allowed)  # type: ignore[arg-type]
    )
    assert result["status"] != "succeeded"
    assert result["uploaded_count"] == 0  # completion counted only after index commit
    with ledger._get_connection() as conn:
        pending_after_failure = conn.execute("SELECT COUNT(*) FROM backup_pending").fetchone()[0]
    assert pending_after_failure > 0

    retry = RecordingClient()
    second = asyncio.run(
        ledger.flush(settings, retry, force=True, allowed_remote_paths=allowed)  # type: ignore[arg-type]
    )
    assert second["status"] == "succeeded" and second["in_scope_remaining"] == 0
    assert second["verified_existing_count"] >= 1
    # Idempotent transport: the retry reused receipts instead of re-PUTting bodies.
    assert not [p for p in retry.puts if p.startswith("v1/objects/")]


def test_seal_cache_kind_paths_follow_the_sealed_contract(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-kinds"
    native_doc = _make_document(
        state_dir,
        run_id,
        document_id="doc-native-1",
        issuer="woori",
        reuse_key_present=True,
        cache_kind="native",
    )
    content_doc = _make_document(
        state_dir,
        run_id,
        document_id="doc-content-1",
        issuer="shinhan",
        reuse_key_present=True,
        cache_kind="content",
    )
    _write_seal(state_dir, run_id, [native_doc, content_doc])
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori", "shinhan"))

    # Native cache identity with run evidence binds through the bundle.
    native_key = native_doc["doc"]["ocr_reuse_key"]
    assert ocr_manifest_path(native_key, kind="native").as_posix() in allowed
    assert ocr_manifest_path(native_key, kind="native").as_posix() in expected

    # adopted/content cache identities are shared cache artifacts, not the
    # document-owned backup unit: scoped backup never claims or transports them
    # and they never inflate required-uncomplete counts.
    content_key = content_doc["doc"]["ocr_reuse_key"]
    assert ocr_manifest_path(content_key, kind="content").as_posix() not in allowed
    assert ocr_ready_path(content_key, kind="content").as_posix() not in allowed
    assert ocr_manifest_path(content_key, kind="native").as_posix() not in allowed


def test_full_batch_registration_semantics_unchanged(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-full"
    doc = _make_document(state_dir, run_id, document_id="doc-full-1", issuer="woori")
    _write_seal(state_dir, run_id, [doc])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings)  # type: ignore[arg-type]  # full batch: loose scan
    paths = {row["remote_path"] for row in _pending_rows(ledger)}
    man_rel = ocr_manifest_path(doc["manifest"].reuse_key, kind="native").as_posix()
    assert man_rel in paths
    client = RecordingClient()
    result = asyncio.run(ledger.flush(settings, client, force=True))  # type: ignore[arg-type]
    assert result["status"] == "succeeded"
    # Unscoped flush states nothing about selected-scope completion: the keys
    # are None, never a bare zero that could be misread as "done".
    assert result["in_scope_remaining"] is None and result["pending_out_of_scope"] is None
    assert result["backup_scope_complete"] is None


class TestBoundedVectorFetchMemo:
    def test_identity_isolation_and_fifo_bound(self) -> None:
        memo = BoundedVectorFetchMemo(max_entries=2)
        vector_a = b"a" * 16384
        vector_b = b"b" * 16384
        key = ("k" * 64, "i" * 64, "v" * 64)
        same_key_other_digest = (key[0], key[1], "w" * 64)
        memo.put(key, vector_a)
        assert memo.get(key) is vector_a
        assert memo.get(same_key_other_digest) is None
        memo.put(same_key_other_digest, vector_b)
        assert len(memo) == 2
        third = (key[0], key[1], "x" * 64)
        memo.put(third, b"c")
        assert len(memo) == 2
        assert memo.get(key) is None  # FIFO eviction of the oldest entry
        assert memo.get(same_key_other_digest) is vector_b
        assert memo.get(third) == b"c"

    def test_rejects_invalid_bound(self) -> None:
        with pytest.raises(ValueError):
            BoundedVectorFetchMemo(max_entries=0)


def test_export_profile_reports_internal_phases(tmp_path: Path) -> None:
    import struct

    from test_exporter_v5 import _records

    from cardrag_worker.exporter_v5 import LazyEmbeddingVector, ServingDatabaseExporterV5

    records = _records()
    first = records["embedding_views"][0]
    second = replace(first, row_index=1, view_type="RAW_ITEM")
    payload = struct.pack("<4096f", 1.0, *([0.0] * 4095))
    digest = hashlib.sha256(payload).hexdigest()
    cache_key = "9" * 64

    def lazy(row: Any) -> LazyEmbeddingVector:
        return LazyEmbeddingVector(
            cache_identity=cache_key,
            profile_id=row.profile_id,
            input_sha256=row.input_sha256,
            expected_vector_sha256=digest,
            loader=lambda: payload,
        )

    records["embedding_views"] = (
        replace(first, vector=lazy(first)),
        replace(second, vector=lazy(second)),
    )

    profile: dict[str, float] = {}
    export = ServingDatabaseExporterV5().export(
        tmp_path / "index.sqlite3",
        tmp_path / "vectors.f32",
        profile=profile,
        **records,
    )
    for key in (
        "vector_sidecar_seconds",
        "cache_loader_seconds",
        "vector_digest_seconds",
        "vector_encode_seconds",
        "sqlite_rows_seconds",
        "fts_index_seconds",
        "working_verify_seconds",
        "vacuum_seconds",
        "final_verify_seconds",
        "install_seconds",
    ):
        assert key in profile, f"missing export phase instrumentation: {key}"
        assert profile[key] >= 0.0
    assert export.vector_size_bytes == 2 * 16384

    # Identical outputs with and without instrumentation.
    plain = ServingDatabaseExporterV5().export(
        tmp_path / "plain.sqlite3",
        tmp_path / "plain.f32",
        **records,
    )
    assert plain.database_sha256 == export.database_sha256
    assert plain.vector_sha256 == export.vector_sha256


class TestPartialExecutionModeResolution:
    def test_new_selected_partial_is_mode_selected(self) -> None:
        assert resolve_partial_execution_mode(("woori",), None) == "selected"
        assert resolve_partial_execution_mode(None, None) == "all"

    def test_frozen_historical_scope_is_restored_not_rewritten(self) -> None:
        saved = {"pool": ["woori"], "selected": ["woori"], "mode": "all"}
        assert resolve_partial_execution_mode(("woori",), saved) == "all"

    def test_round_robin_frozen_scope_is_preserved(self) -> None:
        saved = {"pool": ["woori", "hana"], "selected": ["woori"], "mode": "round_robin"}
        assert resolve_partial_execution_mode(("woori",), saved) == "round_robin"

    def test_invalid_frozen_mode_blocks_resume(self) -> None:
        with pytest.raises(PartialExecutionError, match="issuer_scope_changed"):
            resolve_partial_execution_mode(("woori",), {"mode": "bogus"})


# ---------------------------------------------------------------------------
# FIX_07: content binding of the scoped allow list + diagnostic propagation
# ---------------------------------------------------------------------------


def _corrupt_pending_row(
    ledger: BackupLedger,
    rel_path: str,
    *,
    wrong_bytes: bytes,
) -> None:
    bad_file = Path(ledger.db_path).parent / "corrupted-source.bin"
    bad_file.write_bytes(wrong_bytes)
    with ledger._get_connection() as conn:
        conn.execute(
            "UPDATE backup_pending SET sha256 = ?, size_bytes = ?, local_path = ? WHERE remote_path = ?",
            (
                hashlib.sha256(wrong_bytes).hexdigest(),
                len(wrong_bytes),
                str(bad_file),
                rel_path,
            ),
        )


def test_same_path_conflicting_pending_is_repaired_then_verified_bundle_transported(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-repair"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]

    man_rel = ocr_manifest_path(sel["manifest"].reuse_key, kind="native").as_posix()
    _corrupt_pending_row(ledger, man_rel, wrong_bytes=b'{"not":"the verified manifest"}')
    # The formal registration path repairs the ledger row from verified evidence.
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    with ledger._get_connection() as conn:
        row = conn.execute(
            "SELECT sha256, size_bytes FROM backup_pending WHERE remote_path = ?", (man_rel,)
        ).fetchone()
    assert row is not None
    assert row["sha256"] == hashlib.sha256(sel["manifest"].canonical_bytes()).hexdigest()

    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    assert man_rel in allowed
    assert expected[man_rel][0] == hashlib.sha256(sel["manifest"].canonical_bytes()).hexdigest()
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["status"] == "succeeded"
    assert result["in_scope_conflicts"] == 0
    assert result["backup_scope_complete"] is True
    assert client.objects[man_rel] == sel["manifest"].canonical_bytes()


@pytest.mark.parametrize("artifact", ["manifest", "body", "ready"])
def test_conflicting_same_path_pending_blocks_transport_without_repair(artifact: str, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-block"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]

    key = sel["manifest"].reuse_key
    rel = {
        "manifest": ocr_manifest_path(key, kind="native").as_posix(),
        "ready": ocr_ready_path(key, kind="native").as_posix(),
        "body": object_path(sel["ocr_sha"]).as_posix(),
    }[artifact]
    _corrupt_pending_row(ledger, rel, wrong_bytes=b"conflicting bytes for " + artifact.encode())

    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert rel not in client.objects, f"{artifact}: conflicting row must never transport"
    assert result["in_scope_conflicts"] >= 1
    assert result["backup_scope_complete"] is False
    assert result["status"] != "succeeded"
    with ledger._get_connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM backup_pending WHERE remote_path = ?", (rel,)).fetchone()[0]
            == 1
        )  # blocked rows remain for evidence-backed repair


def test_receipt_disagreeing_with_verified_artifact_blocks_remote_overwrite(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-receipt"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    man_rel = ocr_manifest_path(sel["manifest"].reuse_key, kind="native").as_posix()
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
            " VALUES (?, '', ?, ?, 0)",
            (man_rel, "7" * 64, 42),
        )
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert man_rel not in client.objects
    assert result["in_scope_conflicts"] >= 1
    assert result["backup_scope_complete"] is False
    # Unbound artifacts (seal refs) still transport normally.
    assert object_path(sel["pdf_sha"]).as_posix() in client.objects


def test_local_bytes_corruption_beyond_ledger_record_blocks_transport(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-spool"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    man_file = state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json"
    man_file.write_bytes(b'{"tampered":"after registration"}')
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    # The tampered file can no longer verify, so nothing binds that path any
    # more: it stays visible in the allow set but unbound (held), never sent.
    man_rel = ocr_manifest_path(sel["manifest"].reuse_key, kind="native").as_posix()
    assert man_rel in allowed
    assert man_rel not in expected
    # Even a stale expected map for the pre-tamper digest must not transport.
    stale_expected = dict(expected)
    stale_expected[man_rel] = (hashlib.sha256(sel["manifest"].canonical_bytes()).hexdigest(), None)
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed | {man_rel},
            expected_artifacts=stale_expected,
        )
    )
    assert man_rel not in client.objects
    assert result["in_scope_conflicts"] >= 1


def test_lost_source_inside_scope_is_not_completion(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-lost"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    man_file = state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json"
    with ledger._get_connection() as conn:
        row = conn.execute("SELECT sha256 FROM backup_pending WHERE item_id LIKE 'man-%'").fetchone()
    assert row is not None
    # Sources vanish between scope computation and transport: the row becomes
    # lost_source *inside* the scope, which is an incomplete selected artifact.
    man_file.unlink()
    (state_dir / "backup" / "spool" / row["sha256"]).unlink(missing_ok=True)
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_lost_source"] >= 1
    assert result["in_scope_remaining"] >= 1
    assert result["backup_scope_complete"] is False


def test_flush_diagnostic_schema_present_on_deferred_and_config_error(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-schema"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))

    deferred = asyncio.run(
        ledger.flush(  # type: ignore[call-overload]
            settings,  # type: ignore[arg-type]
            force=False,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert deferred["status"] == "deferred"
    for key in (
        "in_scope_remaining",
        "in_scope_lost_source",
        "in_scope_conflicts",
        "pending_out_of_scope",
        "pending_out_of_scope_bytes",
        "backup_scope_complete",
    ):
        assert key in deferred

    config_error = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            None,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert config_error["status"] == "config_error"
    assert config_error["in_scope_remaining"] >= 1
    assert config_error["backup_scope_complete"] is None


def test_scoped_backup_result_fields_maps_flush_diagnostics() -> None:
    from cardrag_worker.backup import scoped_backup_result_fields

    fields = scoped_backup_result_fields(
        {
            "in_scope_remaining": 0,
            "in_scope_lost_source": 0,
            "in_scope_conflicts": 1,
            "in_scope_missing": 2,
            "in_scope_unbound": 3,
            "pending_out_of_scope": 7,
            "pending_out_of_scope_bytes": 2076,
            "backup_scope_complete": False,
        }
    )
    assert fields == {
        "backup_in_scope_remaining": 0,
        "backup_in_scope_lost_source": 0,
        "backup_in_scope_conflicts": 1,
        "backup_in_scope_missing": 2,
        "backup_in_scope_unbound": 3,
        "backup_pending_out_of_scope": 7,
        "backup_pending_out_of_scope_bytes": 2076,
        "backup_scope_complete": False,
    }
    empty = scoped_backup_result_fields({})
    assert all(value is None for value in empty.values())


def _cli_flush_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_dir: Path, client: Any) -> Any:
    from cardrag_worker import backup as backup_module
    from cardrag_worker import cli as cli_module

    fake_settings = SimpleNamespace(
        state_dir=state_dir,
        backup_mode="hybrid",
        backup_every_runs=7,
        backup_new_ocr_count=30,
        backup_new_bytes=1024**3,
        backup_max_pending_age_hours=168,
        webdav_base_url="https://dav.example",
        webdav_username="user",
        webdav_password="pw",  # noqa: S106
        stable_publication_approved=False,
        webdav_upload_chunk_mib=8,
    )
    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", staticmethod(lambda **kw: fake_settings))
    monkeypatch.setattr(backup_module.WebDAVClient, "from_env", staticmethod(lambda **kw: client))
    return fake_settings


def test_cli_backup_flush_run_scopes_to_verified_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    from typer.testing import CliRunner

    from cardrag_worker.cli import app

    state_dir = tmp_path / "state"
    run_id = "run-cli-scope"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    other = _make_document(state_dir, run_id, document_id="doc-lotte-1", issuer="lotte")
    _write_seal(state_dir, run_id, [sel, other])
    (state_dir / "runs" / run_id / "issuer-scope.json").write_text(
        json.dumps({"pool": ["woori", "lotte"], "selected": ["woori"], "mode": "selected"}),
        encoding="utf-8",
    )
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    ledger.record_document_ocr(run_id, "doc-lotte-1", state_dir, settings)  # type: ignore[arg-type]

    client = RecordingClient()
    _cli_flush_env(tmp_path, monkeypatch, state_dir, client)
    res = CliRunner().invoke(app, ["backup", "flush", "--run", run_id])
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    # Legacy global status keeps meaning "whole ledger drained"; the scoped
    # conclusion is carried by the dedicated diagnostic fields instead.
    assert payload["status"] == "degraded"
    assert payload["scoped_run_id"] == run_id
    assert payload["scoped_issuers"] == ["woori"]
    assert payload["in_scope_remaining"] == 0
    assert payload["in_scope_conflicts"] == 0
    assert payload["backup_scope_complete"] is True
    assert payload["pending_out_of_scope"] >= 3
    lotte_manifest = ocr_manifest_path(other["manifest"].reuse_key, kind="native").as_posix()
    assert lotte_manifest not in client.objects


def test_cli_backup_flush_run_without_seal_exits_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from cardrag_worker.cli import app

    state_dir = tmp_path / "state"
    (state_dir / "runs").mkdir(parents=True)
    client = RecordingClient()
    _cli_flush_env(tmp_path, monkeypatch, state_dir, client)
    res = CliRunner().invoke(app, ["backup", "flush", "--run", "missing-run", "--issuer", "woori"])
    assert res.exit_code == 2
    payload = json.loads(res.stdout)
    assert payload["status"] == "config_error"


class TestMemoRealStateFetches:
    """FIX_07 item 3: the memo must cut real WorkerState fetches with identical bytes."""

    UNIQUE_KEYS = 300
    PASSES = 3

    def _seed(self, tmp_path: Path, count: int) -> tuple[Any, Any, list[tuple[str, str, str, bytes]]]:
        import math

        from cardrag_worker.embedding_v5 import (
            OpenRouterEndpointMetadata,
            QwenEmbeddingProfileV5,
        )
        from cardrag_worker.state import WorkerState

        state = WorkerState(tmp_path / "state.sqlite3")
        profile = QwenEmbeddingProfileV5.from_endpoint(
            OpenRouterEndpointMetadata(
                model="qwen/qwen3-embedding-8b",
                provider_id="deepinfra",
                provider_name="DeepInfra",
                endpoint_name="test",
                quantization="BF16",
                maximum_tokens=32768,
                supported_parameters=("encoding_format",),
                metadata_sha256="b" * 64,
            )
        )
        seeded = []
        for i in range(count):
            cache_key = hashlib.sha256(f"cache-{i}".encode()).hexdigest()
            input_sha = hashlib.sha256(f"input-{i}".encode()).hexdigest()
            eps = 0.001 * (i + 1)
            first = math.sqrt(max(0.0, 1.0 - eps * eps))
            values = [first] + [eps] + [0.0] * 4094
            row = state.put_embedding_v5(
                cache_key=cache_key,
                profile_id=profile.profile_id,
                input_sha256=input_sha,
                dimension=4096,
                values=values,
            )
            seeded.append((cache_key, input_sha, hashlib.sha256(row.embedding).hexdigest(), row.embedding))
        return state, profile, seeded

    def _fetch_counter(self, state: Any) -> tuple[list[str], Any]:
        fetched: list[str] = []
        original = state.get_embedding_v5

        def counted(cache_key: str, **kwargs: Any):
            fetched.append(cache_key)
            return original(cache_key, **kwargs)

        state.get_embedding_v5 = counted  # type: ignore[method-assign]
        return fetched, original

    def _run_sequence(
        self,
        state: Any,
        profile: Any,
        seeded: list[tuple[str, str, str, bytes]],
        sequence: list[int],
        memo: BoundedVectorFetchMemo | None,
    ) -> Any:
        from cardrag_worker.performance import WorkerPerformance
        from cardrag_worker.pipeline import make_lazy_cache_vector

        performance = WorkerPerformance()
        for index in sequence:
            cache_key, input_sha, vector_sha, embedding = seeded[index]
            lazy = make_lazy_cache_vector(
                state=state,
                performance=performance,
                memo=memo,
                profile=profile,
                cache_key=cache_key,
                input_sha256=input_sha,
                expected_vector_sha256=vector_sha,
            )
            assert lazy.loader() == embedding
        return performance

    def test_memo_cuts_fetches_and_preserves_bytes(self, tmp_path: Path) -> None:
        import resource
        import time

        state, profile, seeded = self._seed(tmp_path, self.UNIQUE_KEYS)
        fetched, _ = self._fetch_counter(state)
        sequence = list(range(self.UNIQUE_KEYS)) * self.PASSES

        baseline_times: list[float] = []
        started = time.perf_counter()
        perf_off = self._run_sequence(state, profile, seeded, sequence, memo=None)
        baseline_times.append(time.perf_counter() - started)
        assert len(fetched) == len(sequence)
        metrics_off = perf_off.snapshot()["metrics"]
        assert metrics_off["export_cache_lookups"] == len(sequence)
        assert "export_cache_fetch_deduped" not in metrics_off

        fetched.clear()
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        memo_times: list[float] = []
        memo: BoundedVectorFetchMemo | None = None
        perf_on: Any = None
        for _ in range(3):
            memo = BoundedVectorFetchMemo()
            fetched.clear()
            started = time.perf_counter()
            perf_on = self._run_sequence(state, profile, seeded, sequence, memo=memo)
            memo_times.append(time.perf_counter() - started)
        assert memo is not None and perf_on is not None
        assert len(fetched) == self.UNIQUE_KEYS
        metrics_on = perf_on.snapshot()["metrics"]
        assert metrics_on["export_cache_lookups"] == self.UNIQUE_KEYS
        assert metrics_on["export_cache_fetch_deduped"] == len(sequence) - self.UNIQUE_KEYS
        assert len(memo) == min(self.UNIQUE_KEYS, 512)
        rss_growth_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before
        assert rss_growth_kib * 1024 < 24 * 1024 * 1024, "memo must stay bounded, not corpus-sized"
        # Reported in FIX_07_REPORT; soft guard only (shared-host noise tolerated).
        assert min(memo_times) <= max(baseline_times) * 1.5
        print(
            "MEMO-BENCH "
            + json.dumps(
                {
                    "loads": len(sequence),
                    "unique_keys": self.UNIQUE_KEYS,
                    "fetches_disabled": len(sequence),
                    "fetches_enabled": self.UNIQUE_KEYS,
                    "baseline_seconds": [round(t, 4) for t in baseline_times],
                    "memo_seconds": [round(t, 4) for t in memo_times],
                    "peak_rss_growth_kib": rss_growth_kib,
                }
            )
        )
        state.close()

    def test_memo_eviction_and_hit_counts_via_real_state(self, tmp_path: Path) -> None:
        state, profile, seeded = self._seed(tmp_path, 5)
        fetched, _ = self._fetch_counter(state)
        memo = BoundedVectorFetchMemo(max_entries=4)
        performance = self._run_sequence(state, profile, seeded, [0, 1, 2, 3, 4, 1], memo=memo)
        metrics = performance.snapshot()["metrics"]
        assert metrics["export_cache_lookups"] == 5
        assert metrics["export_cache_fetch_deduped"] == 1
        assert len(fetched) == 5
        assert len(memo) == 4
        assert memo.get((*(seeded[0][:3]),)) is None  # key 0 evicted by FIFO bound
        state.close()

    def test_memo_does_not_weaken_mismatch_or_loss_blocking(self, tmp_path: Path) -> None:
        from cardrag_worker.performance import WorkerPerformance
        from cardrag_worker.pipeline import make_lazy_cache_vector

        state, profile, seeded = self._seed(tmp_path, 3)
        fetched, _ = self._fetch_counter(state)
        memo = BoundedVectorFetchMemo()
        cache_key, input_sha, vector_sha, _bytes = seeded[0]

        wrong_digest = make_lazy_cache_vector(
            state=state,
            performance=WorkerPerformance(),
            memo=memo,
            profile=profile,
            cache_key=cache_key,
            input_sha256=input_sha,
            expected_vector_sha256="0" * 64,
        )
        with pytest.raises(RuntimeError, match="changed bytes"):
            wrong_digest.loader()
        assert len(memo) == 0

        wrong_input = make_lazy_cache_vector(
            state=state,
            performance=WorkerPerformance(),
            memo=memo,
            profile=profile,
            cache_key=cache_key,
            input_sha256="1" * 64,
            expected_vector_sha256=vector_sha,
        )
        with pytest.raises(RuntimeError, match="different profile or input"):
            wrong_input.loader()
        assert len(memo) == 0

        missing_key, missing_input, missing_sha, _ = seeded[1]
        state.connection.execute("DELETE FROM embedding_cache_v5 WHERE cache_key=?", (missing_key,))
        gone = make_lazy_cache_vector(
            state=state,
            performance=WorkerPerformance(),
            memo=memo,
            profile=profile,
            cache_key=missing_key,
            input_sha256=missing_input,
            expected_vector_sha256=missing_sha,
        )
        with pytest.raises(RuntimeError, match="disappeared before export"):
            gone.loader()
        assert len(memo) == 0
        state.close()


# ---------------------------------------------------------------------------
# FIX_08: unbound-path holding, receipt/index completeness, deferred view
# ---------------------------------------------------------------------------


def _normal_setup(tmp_path: Path, run_id: str, *, reuse_key: bool = False) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    sel = _make_document(
        state_dir,
        run_id,
        document_id="doc-woori-1",
        issuer="woori",
        reuse_key_present=reuse_key,
        cache_kind="native" if reuse_key else None,
    )
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    return {"state_dir": state_dir, "settings": settings, "ledger": ledger, "sel": sel}


def test_seal_reuse_key_without_any_evidence_held_not_transported(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-hold", reuse_key=True)
    state_dir, ledger, settings, sel = ctx["state_dir"], ctx["ledger"], ctx["settings"], ctx["sel"]
    # Evidence disappears after registration (retention or tamper); no local cache.
    (state_dir / "runs/run-hold/documents/doc-woori-1/ocr/native-manifest.json").unlink()
    key = sel["doc"]["ocr_reuse_key"]
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    allowed, expected = ledger.scoped_backup_scope("run-hold", state_dir, ("woori",))
    assert man_rel in allowed  # visible: never silently dropped from scope
    assert man_rel not in expected  # unbound: no verified bytes exist anywhere local
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert man_rel not in client.objects
    assert result["in_scope_unbound"] >= 1
    assert result["in_scope_remaining"] >= 1
    assert result["backup_scope_complete"] is False
    assert result["status"] != "succeeded"
    # The CAS artifacts still transport normally.
    assert object_path(sel["pdf_sha"]).as_posix() in client.objects


def test_seal_reuse_key_rebinds_from_local_cache_and_spool(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-cache", reuse_key=True)
    state_dir, ledger, settings, sel = ctx["state_dir"], ctx["ledger"], ctx["settings"], ctx["sel"]
    key = sel["doc"]["ocr_reuse_key"]
    manifest = sel["manifest"]
    # Second formal evidence source: the worker local OCR cache holds the pair.
    cache_dir = state_dir / "cache" / "ocr" / key
    cache_dir.mkdir(parents=True)
    (cache_dir / "manifest.json").write_bytes(manifest.canonical_bytes())
    (state_dir / "runs/run-cache/documents/doc-woori-1/ocr/native-manifest.json").unlink()
    allowed, expected = ledger.scoped_backup_scope("run-cache", state_dir, ("woori",))
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    assert man_rel in expected
    assert expected[man_rel][0] == hashlib.sha256(manifest.canonical_bytes()).hexdigest()
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert client.objects[man_rel] == manifest.canonical_bytes()
    assert result["backup_scope_complete"] is True
    assert result["in_scope_unbound"] == 0 and result["in_scope_missing"] == 0


def test_receipt_conflict_detected_without_any_pending_row(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-b", reuse_key=False)
    state_dir, ledger, settings, sel = ctx["state_dir"], ctx["ledger"], ctx["settings"], ctx["sel"]
    key = sel["manifest"].reuse_key
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    with ledger._get_connection() as conn:
        conn.execute("DELETE FROM backup_pending WHERE remote_path = ?", (man_rel,))
        conn.execute(
            "INSERT INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
            " VALUES (?, '', ?, 42, 0)",
            (man_rel, "7" * 64),
        )
    allowed, expected = ledger.scoped_backup_scope("run-b", state_dir, ("woori",))
    assert expected[man_rel][0] == hashlib.sha256(sel["manifest"].canonical_bytes()).hexdigest()
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_conflicts"] >= 1, "receipt conflict must be seen with no pending row"
    assert result["backup_scope_complete"] is False
    index_doc = json.loads(client.objects["v1/backup/index.json"])
    assert all(item["remote_path"] != man_rel for item in index_doc["items"]), (
        "conflicting receipt SHA must not be published into the committed index"
    )
    # No blind remote overwrite of the disputed path.
    assert man_rel not in client.objects


def test_missing_required_artifact_blocks_completion_until_reregistration(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-c", reuse_key=False)
    state_dir, ledger, settings, sel = ctx["state_dir"], ctx["ledger"], ctx["settings"], ctx["sel"]
    key = sel["manifest"].reuse_key
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    with ledger._get_connection() as conn:
        conn.execute("DELETE FROM backup_pending WHERE remote_path = ?", (man_rel,))
    allowed, expected = ledger.scoped_backup_scope("run-c", state_dir, ("woori",))
    client = RecordingClient()
    first = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    # The other items committed, yet the scope is NOT complete: manifest never
    # reached a receipt or the index.
    assert first["in_scope_missing"] == 1
    assert first["backup_scope_complete"] is False
    assert man_rel not in client.objects

    # Formal re-registration from run evidence re-creates the pending row.
    ledger.record_run_success("run-c", state_dir, settings, selected_issuers=("woori",))  # type: ignore[arg-type]
    allowed, expected = ledger.scoped_backup_scope("run-c", state_dir, ("woori",))
    second = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert second["in_scope_missing"] == 0
    assert second["backup_scope_complete"] is True
    assert client.objects[man_rel] == sel["manifest"].canonical_bytes()


def test_receipt_at_other_root_is_not_completion_evidence(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-other-root", reuse_key=False)
    state_dir, ledger, settings, sel = ctx["state_dir"], ctx["ledger"], ctx["settings"], ctx["sel"]
    key = sel["manifest"].reuse_key
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    sha = hashlib.sha256(sel["manifest"].canonical_bytes()).hexdigest()
    body = sel["manifest"].canonical_bytes()
    with ledger._get_connection() as conn:
        conn.execute("DELETE FROM backup_pending WHERE remote_path = ?", (man_rel,))
        conn.execute(
            "INSERT INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
            " VALUES (?, 'https://other.example', ?, ?, 0)",
            (man_rel, sha, len(body)),
        )
    allowed, expected = ledger.scoped_backup_scope("run-other-root", state_dir, ("woori",))
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_missing"] == 1
    assert result["backup_scope_complete"] is False


def test_unchanged_rerun_reports_completion_from_receipts_and_index(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-done", reuse_key=False)
    state_dir, ledger, settings = ctx["state_dir"], ctx["ledger"], ctx["settings"]
    allowed, expected = ledger.scoped_backup_scope("run-done", state_dir, ("woori",))
    client = RecordingClient()
    first = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert first["backup_scope_complete"] is True
    again = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    # Same remote (receipts + committed index) still proves completion even
    # when nothing new transports.
    assert again["status"] == "unchanged"
    assert again["backup_scope_complete"] is True
    assert again["in_scope_remaining"] == 0


def test_scoped_pending_diagnostics_visible_when_threshold_defers(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-defer", reuse_key=False)
    state_dir, ledger, settings = ctx["state_dir"], ctx["ledger"], ctx["settings"]
    # non-selected backlog exists in the same ledger
    _make_document(state_dir, "run-defer", document_id="doc-lotte-1", issuer="lotte")
    ledger.record_document_ocr("run-defer", "doc-lotte-1", state_dir, settings)  # type: ignore[arg-type]
    fields = ledger.scoped_pending_diagnostics(settings, "run-defer", ("woori",))  # type: ignore[arg-type]
    assert fields["backup_diagnostics_mode"] == "read_only"
    assert fields["backup_scope_run_id"] == "run-defer"
    assert fields["backup_in_scope_remaining"] >= 1
    assert fields["backup_in_scope_unbound"] == 0
    assert fields["backup_in_scope_missing"] == 0
    assert fields["backup_pending_out_of_scope"] >= 3
    assert fields["backup_scope_complete"] is None
    # read-only: nothing transported or written
    assert ledger.get_status(settings)["pending_count"] >= 4  # type: ignore[arg-type]


def test_cli_backup_status_run_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    from typer.testing import CliRunner

    from cardrag_worker.cli import app

    ctx = _normal_setup(tmp_path, "run-status", reuse_key=False)
    state_dir, settings = ctx["state_dir"], ctx["settings"]
    (state_dir / "runs/run-status/issuer-scope.json").write_text(
        json.dumps({"pool": ["woori"], "selected": ["woori"], "mode": "selected"}), encoding="utf-8"
    )
    from cardrag_worker import cli as cli_module

    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", staticmethod(lambda **kw: settings))
    res = CliRunner().invoke(app, ["backup", "status", "--run", "run-status"])
    assert res.exit_code == 0, res.stdout
    payload = _json.loads(res.stdout)
    assert payload["backup_diagnostics_mode"] == "read_only"
    assert payload["backup_in_scope_remaining"] >= 1
    assert payload["backup_scope_complete"] is None


# ---------------------------------------------------------------------------
# FIX_09: required unbound paths are pending-independent
# ---------------------------------------------------------------------------


def _strip_metadata_rows_and_evidence(
    tmp_path: Path,
    run_id: str,
    ctx: dict[str, Any],
) -> tuple[str, str]:
    state_dir = ctx["state_dir"]
    ledger = ctx["ledger"]
    key = ctx["sel"]["doc"]["ocr_reuse_key"]
    (state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json").unlink()
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    ready_rel = ocr_ready_path(key, kind="native").as_posix()
    with ledger._get_connection() as conn:
        conn.execute("DELETE FROM backup_pending WHERE remote_path IN (?, ?)", (man_rel, ready_rel))
    return man_rel, ready_rel


def test_required_unbound_paths_block_completion_without_rows(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-r9", reuse_key=True)
    ledger, settings = ctx["ledger"], ctx["settings"]
    man_rel, ready_rel = _strip_metadata_rows_and_evidence(tmp_path, "run-r9", ctx)
    allowed, expected = ledger.scoped_backup_scope("run-r9", ctx["state_dir"], ("woori",))
    assert man_rel in allowed and ready_rel in allowed
    assert man_rel not in expected and ready_rel not in expected
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_unbound"] == 2, "required unbound paths count without any rows"
    assert result["in_scope_missing"] == 0
    assert result["backup_scope_complete"] is False
    assert result["status"] != "succeeded"
    assert man_rel not in client.objects and ready_rel not in client.objects


def test_receipt_present_does_not_satisfy_required_unbound_path(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-r9-receipt", reuse_key=True)
    ledger, settings = ctx["ledger"], ctx["settings"]
    man_rel, _ready_rel = _strip_metadata_rows_and_evidence(tmp_path, "run-r9-receipt", ctx)
    body = ctx["sel"]["manifest"].canonical_bytes()
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
            " VALUES (?, '', ?, ?, 0)",
            (man_rel, hashlib.sha256(body).hexdigest(), len(body)),
        )
    allowed, expected = ledger.scoped_backup_scope("run-r9-receipt", ctx["state_dir"], ("woori",))
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            RecordingClient(),
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_unbound"] == 2
    assert result["backup_scope_complete"] is False


def test_duplicate_rows_on_unbound_path_do_not_change_path_counts(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-r9-dup", reuse_key=True)
    ledger, settings = ctx["ledger"], ctx["settings"]
    man_rel, _ready_rel = _strip_metadata_rows_and_evidence(tmp_path, "run-r9-dup", ctx)
    with ledger._get_connection() as conn:
        for item_id in ("dup-1", "dup-2"):
            conn.execute(
                """
                INSERT OR REPLACE INTO backup_pending
                (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type,
                 created_at, status)
                VALUES (?, 'ocr_manifest', ?, 5, ?, ?, 'application/json', 0, 'pending')
                """,
                (item_id, "0" * 64, str(ctx["state_dir"]), man_rel),
            )
    allowed, expected = ledger.scoped_backup_scope("run-r9-dup", ctx["state_dir"], ("woori",))
    _conflicts, _done, missing = ledger._scoped_integrity(expected, "")
    assert missing == []
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            RecordingClient(),
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    # The verdict is path-based: three rows on one unbound path still count as
    # one unbound path, and duplicate manifest+READY unbound paths stay at two.
    assert result["in_scope_unbound"] == 2
    assert result["backup_scope_complete"] is False


def test_one_sided_bound_metadata_missing_blocks_completion(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-r9-half", reuse_key=False)
    ledger, settings = ctx["ledger"], ctx["settings"]
    key = ctx["sel"]["manifest"].reuse_key
    ready_rel = ocr_ready_path(key, kind="native").as_posix()
    with ledger._get_connection() as conn:
        conn.execute("DELETE FROM backup_pending WHERE remote_path = ?", (ready_rel,))
    allowed, expected = ledger.scoped_backup_scope("run-r9-half", ctx["state_dir"], ("woori",))
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            RecordingClient(),
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_missing"] == 1
    assert result["in_scope_unbound"] == 0
    assert result["backup_scope_complete"] is False


def test_deferred_status_reports_required_unbound_without_rows(tmp_path: Path) -> None:
    ctx = _normal_setup(tmp_path, "run-r9-defer", reuse_key=True)
    ledger, settings = ctx["ledger"], ctx["settings"]
    _strip_metadata_rows_and_evidence(tmp_path, "run-r9-defer", ctx)
    fields = ledger.scoped_pending_diagnostics(settings, "run-r9-defer", ("woori",))  # type: ignore[arg-type]
    assert fields["backup_in_scope_unbound"] == 2
    assert fields["backup_in_scope_missing"] == 0
    assert fields["backup_scope_complete"] is None
    assert fields["backup_diagnostics_mode"] == "read_only"


# ---------------------------------------------------------------------------
# FIX_10: generation-only (null seal reuse key) required metadata preservation
# ---------------------------------------------------------------------------


def _strip_generation_only_evidence_and_pending(
    ctx: dict[str, Any],
    run_id: str,
    *,
    remove_pending: bool = True,
) -> tuple[str, str]:
    state_dir = ctx["state_dir"]
    ledger = ctx["ledger"]
    key = ctx["sel"]["manifest"].reuse_key
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    ready_rel = ocr_ready_path(key, kind="native").as_posix()
    (state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json").unlink()
    if remove_pending:
        with ledger._get_connection() as conn:
            conn.execute("DELETE FROM backup_pending WHERE remote_path IN (?, ?)", (man_rel, ready_rel))
    return man_rel, ready_rel


def test_generation_only_metadata_retains_required_unbound_without_rows(tmp_path: Path) -> None:
    """Reviewer repro: generation-only document retains metadata ownership in doc_map

    Even with run-local native manifest unlinked and pending rows cleared,
    the metadata paths must stay visible in scope and count as required unbound,
    refusing scope completion.
    """
    ctx = _normal_setup(tmp_path, "run-r10-repro", reuse_key=False)
    assert ctx["sel"]["doc"]["ocr_reuse_key"] is None
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    man_rel, ready_rel = _strip_generation_only_evidence_and_pending(
        ctx, "run-r10-repro", remove_pending=True
    )

    allowed, expected = ledger.scoped_backup_scope("run-r10-repro", state_dir, ("woori",))
    assert man_rel in allowed and ready_rel in allowed
    assert man_rel not in expected and ready_rel not in expected

    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert man_rel not in client.objects and ready_rel not in client.objects
    assert result["in_scope_unbound"] == 2
    assert result["in_scope_missing"] == 0
    assert result["backup_scope_complete"] is False
    assert result["status"] != "succeeded"


def test_generation_only_metadata_with_receipt_remains_unbound_without_evidence(tmp_path: Path) -> None:
    """Pre-existing receipts do not grant transport authority without local evidence."""
    ctx = _normal_setup(tmp_path, "run-r10-rcpt", reuse_key=False)
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    man_rel, _ready_rel = _strip_generation_only_evidence_and_pending(
        ctx, "run-r10-rcpt", remove_pending=True
    )
    body = ctx["sel"]["manifest"].canonical_bytes()
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
            " VALUES (?, '', ?, ?, 0)",
            (man_rel, hashlib.sha256(body).hexdigest(), len(body)),
        )

    allowed, expected = ledger.scoped_backup_scope("run-r10-rcpt", state_dir, ("woori",))
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            RecordingClient(),
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_unbound"] == 2
    assert result["backup_scope_complete"] is False


def test_generation_only_metadata_rebinds_from_local_cache(tmp_path: Path) -> None:
    """When run manifest is missing but local cache holds verified evidence, formally rebind."""
    ctx = _normal_setup(tmp_path, "run-r10-rebind", reuse_key=False)
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    manifest = ctx["sel"]["manifest"]
    key = manifest.reuse_key
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    ready_rel = ocr_ready_path(key, kind="native").as_posix()

    cache_dir = state_dir / "cache" / "ocr" / key
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "manifest.json").write_bytes(manifest.canonical_bytes())
    ready = OCRReady(
        reuse_key=key,
        manifest_sha256=hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
        ocr_sha256=manifest.output.sha256,
    )
    (cache_dir / "READY.json").write_bytes(ready.canonical_bytes())

    # Prune run-local native manifest
    (
        state_dir / "runs" / "run-r10-rebind" / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json"
    ).unlink()

    allowed, expected = ledger.scoped_backup_scope("run-r10-rebind", state_dir, ("woori",))
    assert man_rel in expected and ready_rel in expected
    assert expected[man_rel][0] == hashlib.sha256(manifest.canonical_bytes()).hexdigest()

    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert client.objects[man_rel] == manifest.canonical_bytes()
    assert result["in_scope_unbound"] == 0
    assert result["in_scope_missing"] == 0
    assert result["backup_scope_complete"] is True
    assert result["status"] == "succeeded"


def test_generation_only_metadata_tampered_cache_remains_unbound(tmp_path: Path) -> None:
    """Tampered cache evidence cannot rebind and stays unbound."""
    ctx = _normal_setup(tmp_path, "run-r10-tamper", reuse_key=False)
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    key = ctx["sel"]["manifest"].reuse_key
    man_rel = ocr_manifest_path(key, kind="native").as_posix()

    cache_dir = state_dir / "cache" / "ocr" / key
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "manifest.json").write_bytes(b"invalid json")

    (
        state_dir / "runs" / "run-r10-tamper" / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json"
    ).unlink()

    allowed, expected = ledger.scoped_backup_scope("run-r10-tamper", state_dir, ("woori",))
    assert man_rel in allowed
    assert man_rel not in expected

    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            RecordingClient(),
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_unbound"] == 2
    assert result["backup_scope_complete"] is False


def test_generation_only_metadata_unchanged_rerun_completes(tmp_path: Path) -> None:
    """Unchanged rerun after generation-only flush remains complete from receipts + index."""
    ctx = _normal_setup(tmp_path, "run-r10-unchanged", reuse_key=False)
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    allowed, expected = ledger.scoped_backup_scope("run-r10-unchanged", state_dir, ("woori",))

    client = RecordingClient()
    first = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert first["backup_scope_complete"] is True

    again = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert again["status"] == "unchanged"
    assert again["backup_scope_complete"] is True
    assert again["in_scope_remaining"] == 0


def test_generation_only_metadata_deferred_status_reports_unbound(tmp_path: Path) -> None:
    """Read-only deferred status reports unbound generation-only metadata without writes."""
    ctx = _normal_setup(tmp_path, "run-r10-defer", reuse_key=False)
    ledger, settings = ctx["ledger"], ctx["settings"]
    _strip_generation_only_evidence_and_pending(ctx, "run-r10-defer", remove_pending=True)
    fields = ledger.scoped_pending_diagnostics(settings, "run-r10-defer", ("woori",))  # type: ignore[arg-type]
    assert fields["backup_in_scope_unbound"] == 2
    assert fields["backup_in_scope_missing"] == 0
    assert fields["backup_scope_complete"] is None
    assert fields["backup_diagnostics_mode"] == "read_only"


def test_stale_past_revision_metadata_ignored_when_current_bound(tmp_path: Path) -> None:
    """Past revision doc_map metadata for the same document is ignored if current revision is bound."""
    ctx = _normal_setup(tmp_path, "run-r10-stale", reuse_key=False)
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    old_key = "f" * 64
    old_man = ocr_manifest_path(old_key, kind="native").as_posix()
    old_info = {
        "document_id": "doc-woori-1",
        "reuse_key": old_key,
        "pdf_sha256": "0" * 64,
        "ocr_sha256": "9" * 64,  # different from current
        "kind": "native",
    }
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT INTO backup_meta (key, value) VALUES (?, ?)", (f"doc_map_{old_man}", "doc-woori-1")
        )
        conn.execute(
            "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_info_{old_man}", json.dumps(old_info)),
        )

    allowed, expected = ledger.scoped_backup_scope("run-r10-stale", state_dir, ("woori",))
    assert old_man not in allowed

    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["backup_scope_complete"] is True
    assert result["in_scope_unbound"] == 0


def test_stale_past_revision_metadata_unbound_when_current_not_bound(tmp_path: Path) -> None:
    """When current revision is NOT bound and only past-revision record exists, do not claim complete."""
    ctx = _normal_setup(tmp_path, "run-r10-stale-unbound", reuse_key=False)
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    _strip_generation_only_evidence_and_pending(ctx, "run-r10-stale-unbound", remove_pending=True)

    allowed, expected = ledger.scoped_backup_scope("run-r10-stale-unbound", state_dir, ("woori",))
    assert len(allowed) > len(expected)
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_unbound"] >= 1
    assert result["backup_scope_complete"] is False


# ---------------------------------------------------------------------------
# FIX_11: order-independent cache rebinding and stale history exclusion
# ---------------------------------------------------------------------------


def test_stale_history_ordering_does_not_affect_scope_resolution(tmp_path: Path) -> None:
    """Order of doc_map records in SQLite does not change scope/complete resolution.

    Whether past-revision metadata is inserted before or after current-revision
    registration, when current metadata is valid in cache, the past revision is
    excluded and scope completion is achieved.
    """
    for order in ("past_first", "current_first"):
        run_id = f"run-r11-{order}"
        state_dir = tmp_path / order / "state"
        sel = _make_document(
            state_dir, run_id, document_id="doc-woori-1", issuer="woori", reuse_key_present=False
        )
        _write_seal(state_dir, run_id, [sel])
        ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
        settings = _settings(state_dir)

        old_key = "e" * 64
        old_man = ocr_manifest_path(old_key, kind="native").as_posix()
        old_info = {
            "document_id": "doc-woori-1",
            "reuse_key": old_key,
            "pdf_sha256": "0" * 64,
            "ocr_sha256": "8" * 64,
            "kind": "native",
        }

        if order == "past_first":
            with ledger._get_connection() as conn:
                conn.execute(
                    "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
                    (f"doc_map_{old_man}", "doc-woori-1"),
                )
                conn.execute(
                    "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
                    (f"doc_info_{old_man}", json.dumps(old_info)),
                )
            ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))
        else:
            ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))
            with ledger._get_connection() as conn:
                conn.execute(
                    "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
                    (f"doc_map_{old_man}", "doc-woori-1"),
                )
                conn.execute(
                    "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
                    (f"doc_info_{old_man}", json.dumps(old_info)),
                )

        # Place current manifest in local cache and prune run-local manifest
        cache_dir = state_dir / "cache" / "ocr" / sel["manifest"].reuse_key
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "manifest.json").write_bytes(sel["manifest"].canonical_bytes())
        ready = OCRReady(
            reuse_key=sel["manifest"].reuse_key,
            manifest_sha256=hashlib.sha256(sel["manifest"].canonical_bytes()).hexdigest(),
            ocr_sha256=sel["manifest"].output.sha256,
        )
        (cache_dir / "READY.json").write_bytes(ready.canonical_bytes())
        (state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json").unlink()

        allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
        cur_man = ocr_manifest_path(sel["manifest"].reuse_key, kind="native").as_posix()
        assert cur_man in expected
        assert old_man not in allowed, f"Stale history must be excluded regardless of order ({order})"

        client = RecordingClient()
        result = asyncio.run(
            ledger.flush(
                settings,  # type: ignore[arg-type]
                client,
                force=True,
                allowed_remote_paths=allowed,
                expected_artifacts=expected,
            )
        )
        assert result["in_scope_unbound"] == 0
        assert result["backup_scope_complete"] is True


def test_manifest_ready_permutations_order_independent(tmp_path: Path) -> None:
    """Order of manifest vs READY in doc_map yields identical scope and completion."""
    for first_item in ("ready_first", "man_first"):
        run_id = f"run-perm-{first_item}"
        state_dir = tmp_path / first_item / "state"
        sel = _make_document(
            state_dir, run_id, document_id="doc-woori-1", issuer="woori", reuse_key_present=False
        )
        _write_seal(state_dir, run_id, [sel])
        ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
        settings = _settings(state_dir)
        ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))

        key = sel["manifest"].reuse_key
        man_rel = ocr_manifest_path(key, kind="native").as_posix()
        ready_rel = ocr_ready_path(key, kind="native").as_posix()

        # Re-insert in specific order
        with ledger._get_connection() as conn:
            conn.execute(
                "DELETE FROM backup_meta WHERE key IN (?, ?)", (f"doc_map_{man_rel}", f"doc_map_{ready_rel}")
            )
            if first_item == "ready_first":
                conn.execute(
                    "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
                    (f"doc_map_{ready_rel}", "doc-woori-1"),
                )
                conn.execute(
                    "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
                    (f"doc_map_{man_rel}", "doc-woori-1"),
                )
            else:
                conn.execute(
                    "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
                    (f"doc_map_{man_rel}", "doc-woori-1"),
                )
                conn.execute(
                    "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
                    (f"doc_map_{ready_rel}", "doc-woori-1"),
                )

        # Setup cache and prune run-local
        cache_dir = state_dir / "cache" / "ocr" / key
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "manifest.json").write_bytes(sel["manifest"].canonical_bytes())
        ready = OCRReady(
            reuse_key=key,
            manifest_sha256=hashlib.sha256(sel["manifest"].canonical_bytes()).hexdigest(),
            ocr_sha256=sel["manifest"].output.sha256,
        )
        (cache_dir / "READY.json").write_bytes(ready.canonical_bytes())
        (state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json").unlink()

        allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
        assert man_rel in expected and ready_rel in expected
        result = asyncio.run(
            ledger.flush(
                settings,  # type: ignore[arg-type]
                RecordingClient(),
                force=True,
                allowed_remote_paths=allowed,
                expected_artifacts=expected,
            )
        )
        assert result["in_scope_unbound"] == 0
        assert result["backup_scope_complete"] is True


def test_stale_history_retained_when_current_cache_corrupted(tmp_path: Path) -> None:
    """When current cache is corrupted, current rebind fails and stale history blocks completion."""
    run_id = "run-r11-corrupt"
    state_dir = tmp_path / "corrupt" / "state"
    sel = _make_document(
        state_dir, run_id, document_id="doc-woori-1", issuer="woori", reuse_key_present=False
    )
    _write_seal(state_dir, run_id, [sel])
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    settings = _settings(state_dir)

    old_key = "d" * 64
    old_man = ocr_manifest_path(old_key, kind="native").as_posix()
    old_info = {
        "document_id": "doc-woori-1",
        "reuse_key": old_key,
        "pdf_sha256": "0" * 64,
        "ocr_sha256": "7" * 64,
        "kind": "native",
    }
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT INTO backup_meta (key, value) VALUES (?, ?)", (f"doc_map_{old_man}", "doc-woori-1")
        )
        conn.execute(
            "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_info_{old_man}", json.dumps(old_info)),
        )
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))

    # Current cache corrupted
    cache_dir = state_dir / "cache" / "ocr" / sel["manifest"].reuse_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "manifest.json").write_bytes(b"corrupted body")
    (state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr" / "native-manifest.json").unlink()

    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    assert len(allowed) > len(expected)
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            RecordingClient(),
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_unbound"] >= 1
    assert result["backup_scope_complete"] is False


def test_stale_history_with_pending_or_receipt_not_force_transported(tmp_path: Path) -> None:
    """Stale history with pending row or receipt is excluded and not force transported."""
    run_id = "run-r11-stale-pending"
    state_dir = tmp_path / "stale-p" / "state"
    sel = _make_document(
        state_dir, run_id, document_id="doc-woori-1", issuer="woori", reuse_key_present=False
    )
    _write_seal(state_dir, run_id, [sel])
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    settings = _settings(state_dir)

    old_key = "c" * 64
    old_man = ocr_manifest_path(old_key, kind="native").as_posix()
    old_info = {
        "document_id": "doc-woori-1",
        "reuse_key": old_key,
        "pdf_sha256": "0" * 64,
        "ocr_sha256": "6" * 64,
        "kind": "native",
    }
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT INTO backup_meta (key, value) VALUES (?, ?)", (f"doc_map_{old_man}", "doc-woori-1")
        )
        conn.execute(
            "INSERT INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_info_{old_man}", json.dumps(old_info)),
        )
        conn.execute(
            """
            INSERT INTO backup_pending
            (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
            VALUES ('stale-1', 'ocr_manifest', '0'*64, 10, '/tmp/stale', ?, 'application/json', 0, 'pending')
            """,
            (old_man,),
        )
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))

    # Current revision is bound from run-local
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    assert old_man not in allowed
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert old_man not in client.objects
    assert result["backup_scope_complete"] is True


# ---------------------------------------------------------------------------
# FIX_12: generation-only OCR variant backup and index preservation
# ---------------------------------------------------------------------------


def _setup_variant_fixture(
    tmp_path: Path,
    run_id: str,
    *,
    document_id: str = "doc-woori-1",
    issuer: str = "woori",
) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")

    pdf_body = f"%PDF-{document_id}".encode()
    pdf_sha = hashlib.sha256(pdf_body).hexdigest()

    # Variant A (remote canonical pair)
    ocr_text_A = f"## Page 1\n\nVariant A OCR for {document_id}\n"
    ocr_bytes_A = ocr_text_A.encode("utf-8")
    ocr_sha_A = hashlib.sha256(ocr_bytes_A).hexdigest()

    source = OCRInput(pdf_sha256=pdf_sha, pdf_size_bytes=len(pdf_body), page_count=1)
    reuse_key = native_ocr_reuse_key(_contract(), source)

    manifest_A = OCRArtifactManifest(
        reuse_key=reuse_key,
        source=source,
        contract=_contract(),
        output=ArtifactRef(
            media_type="text/markdown; charset=utf-8",
            path=f"v1/objects/sha256/{ocr_sha_A[:2]}/{ocr_sha_A}",
            sha256=ocr_sha_A,
            size_bytes=len(ocr_bytes_A),
        ),
        ocr_chars=len(ocr_text_A),
        page_output_sha256=(hashlib.sha256(ocr_bytes_A).hexdigest(),),
        created_at=datetime(2026, 10, 14, 18, 39, 33, tzinfo=UTC),
    )
    man_bytes_A = manifest_A.canonical_bytes()
    man_sha_A = hashlib.sha256(man_bytes_A).hexdigest()

    ready_A = OCRReady(
        reuse_key=reuse_key,
        manifest_sha256=man_sha_A,
        ocr_sha256=ocr_sha_A,
    )
    ready_bytes_A = ready_A.canonical_bytes()
    ready_sha_A = hashlib.sha256(ready_bytes_A).hexdigest()

    can_man_rel = ocr_manifest_path(reuse_key, kind="native").as_posix()
    can_ready_rel = ocr_ready_path(reuse_key, kind="native").as_posix()
    cas_rel_A = object_path(ocr_sha_A).as_posix()

    # Variant B (local sealed generation-only document)
    ocr_text_B = f"## Page 1\n\nVariant B OCR for {document_id}\n"
    ocr_bytes_B = ocr_text_B.encode("utf-8")
    ocr_sha_B = hashlib.sha256(ocr_bytes_B).hexdigest()

    manifest_B = OCRArtifactManifest(
        reuse_key=reuse_key,
        source=source,
        contract=_contract(),
        output=ArtifactRef(
            media_type="text/markdown; charset=utf-8",
            path=f"v1/objects/sha256/{ocr_sha_B[:2]}/{ocr_sha_B}",
            sha256=ocr_sha_B,
            size_bytes=len(ocr_bytes_B),
        ),
        ocr_chars=len(ocr_text_B),
        page_output_sha256=(hashlib.sha256(ocr_bytes_B).hexdigest(),),
        created_at=datetime(2026, 10, 14, 23, 41, 26, tzinfo=UTC),
    )
    man_bytes_B = manifest_B.canonical_bytes()
    man_sha_B = hashlib.sha256(man_bytes_B).hexdigest()

    ready_B = OCRReady(
        reuse_key=reuse_key,
        manifest_sha256=man_sha_B,
        ocr_sha256=ocr_sha_B,
    )
    ready_bytes_B = ready_B.canonical_bytes()
    ready_sha_B = hashlib.sha256(ready_bytes_B).hexdigest()

    # Materialize local variant B in run directory
    ocr_dir = state_dir / "runs" / run_id / "documents" / document_id / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "native-manifest.json").write_bytes(man_bytes_B)
    (ocr_dir / "ocr.md").write_bytes(ocr_bytes_B)

    sealed_pdf_path = state_dir / "runs" / run_id / "documents" / f"{document_id}.pdf"
    sealed_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    sealed_pdf_path.write_bytes(pdf_body)
    sealed_ocr_local = state_dir / "runs" / run_id / "objects" / ocr_sha_B
    sealed_ocr_local.parent.mkdir(parents=True, exist_ok=True)
    sealed_ocr_local.write_bytes(ocr_bytes_B)

    doc_B: dict[str, Any] = {
        "document_id": document_id,
        "issuer": issuer,
        "page_count": 1,
        "pdf": {
            "sha256": pdf_sha,
            "size_bytes": len(pdf_body),
            "media_type": "application/pdf",
            "path": f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
        },
        "ocr": {
            "sha256": ocr_sha_B,
            "size_bytes": len(ocr_bytes_B),
            "media_type": "text/markdown; charset=utf-8",
            "path": f"v1/objects/sha256/{ocr_sha_B[:2]}/{ocr_sha_B}",
        },
        "ocr_reuse_key": None,
        "ocr_cache_kind": None,
    }

    _write_seal(
        state_dir,
        run_id,
        [
            {
                "doc": doc_B,
                "manifest": manifest_B,
                "objects": [
                    {
                        "sha256": pdf_sha,
                        "size_bytes": len(pdf_body),
                        "media_type": "application/pdf",
                        "path": str(sealed_pdf_path),
                    },
                    {
                        "sha256": ocr_sha_B,
                        "size_bytes": len(ocr_bytes_B),
                        "media_type": "text/markdown; charset=utf-8",
                        "path": str(sealed_ocr_local),
                    },
                ],
            }
        ],
    )

    # Pre-populate remote client with Variant A
    client = RecordingClient()
    client.objects[can_man_rel] = man_bytes_A
    client.objects[can_ready_rel] = ready_bytes_A
    client.objects[cas_rel_A] = ocr_bytes_A

    # Spool variant A locally so offline integrity check succeeds
    spool_dir = state_dir / "backup" / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    (spool_dir / man_sha_A).write_bytes(man_bytes_A)
    (spool_dir / ready_sha_A).write_bytes(ready_bytes_A)

    # Insert receipts for Variant A
    with ledger._get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)
            VALUES (?, '', ?, ?, ?)
            """,
            (can_man_rel, man_sha_A, len(man_bytes_A), 100.0),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)
            VALUES (?, '', ?, ?, ?)
            """,
            (can_ready_rel, ready_sha_A, len(ready_bytes_A), 100.0),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)
            VALUES (?, '', ?, ?, ?)
            """,
            (cas_rel_A, ocr_sha_A, len(ocr_bytes_A), 100.0),
        )

    # Initialize remote backup index with Variant A
    initial_idx = {
        "schema_version": "cardrag.backup-index.v1",
        "updated_at": "2026-10-14T18:40:00Z",
        "items": [
            {"remote_path": can_man_rel, "sha256": man_sha_A, "size_bytes": len(man_bytes_A)},
            {"remote_path": can_ready_rel, "sha256": ready_sha_A, "size_bytes": len(ready_bytes_A)},
            {"remote_path": cas_rel_A, "sha256": ocr_sha_A, "size_bytes": len(ocr_bytes_A)},
        ],
    }
    client.objects["v1/backup/index.json"] = json.dumps(initial_idx, indent=2).encode("utf-8")

    return {
        "state_dir": state_dir,
        "settings": settings,
        "ledger": ledger,
        "client": client,
        "reuse_key": reuse_key,
        "variant_A": {
            "manifest": manifest_A,
            "man_bytes": man_bytes_A,
            "man_sha": man_sha_A,
            "ready_bytes": ready_bytes_A,
            "ready_sha": ready_sha_A,
            "ocr_sha": ocr_sha_A,
            "ocr_bytes": ocr_bytes_A,
            "man_rel": can_man_rel,
            "ready_rel": can_ready_rel,
            "cas_rel": cas_rel_A,
        },
        "variant_B": {
            "doc": doc_B,
            "manifest": manifest_B,
            "man_bytes": man_bytes_B,
            "man_sha": man_sha_B,
            "ready_bytes": ready_bytes_B,
            "ready_sha": ready_sha_B,
            "ocr_sha": ocr_sha_B,
            "ocr_bytes": ocr_bytes_B,
        },
    }


def test_generation_only_ocr_variant_coexists_with_canonical_remote_pair(tmp_path: Path) -> None:
    """Fixture test: source/contract/native key match, outputs differ.

    Local variant B is generation-only (null reuse key).
    Remote pair A already occupies canonical cache path.
    Scoped backup routes B to variant path; flush uploads B without overwriting A;
    index retains A and adds B; backup_scope_complete is True.
    """
    ctx = _setup_variant_fixture(tmp_path, "run-r12-coexist")
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    client = ctx["client"]
    vA = ctx["variant_A"]
    vB = ctx["variant_B"]
    key = ctx["reuse_key"]

    ledger.record_run_success("run-r12-coexist", state_dir, settings, selected_issuers=("woori",))

    allowed, expected = ledger.scoped_backup_scope("run-r12-coexist", state_dir, ("woori",))

    var_man_rel = ocr_variant_manifest_path(key, vB["ocr_sha"], kind="native").as_posix()
    var_ready_rel = ocr_variant_ready_path(key, vB["ocr_sha"], kind="native").as_posix()

    # Variant B paths are in expected
    assert var_man_rel in expected
    assert var_ready_rel in expected
    assert vA["man_rel"] not in expected

    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )

    assert result["in_scope_conflicts"] == 0
    assert result["in_scope_missing"] == 0
    assert result["backup_scope_complete"] is True
    assert result["status"] == "succeeded"

    # Both Variant A and Variant B exist on remote WebDAV
    assert client.objects[vA["man_rel"]] == vA["man_bytes"]
    assert client.objects[vA["ready_rel"]] == vA["ready_bytes"]
    assert client.objects[var_man_rel] == vB["man_bytes"]
    assert client.objects[var_ready_rel] == vB["ready_bytes"]

    # Both Variant A and Variant B are recorded in remote backup index
    idx = json.loads(client.objects["v1/backup/index.json"].decode("utf-8"))
    idx_paths = {item["remote_path"]: item["sha256"] for item in idx["items"]}
    assert idx_paths[vA["man_rel"]] == vA["man_sha"]
    assert idx_paths[vA["ready_rel"]] == vA["ready_sha"]
    assert idx_paths[var_man_rel] == vB["man_sha"]
    assert idx_paths[var_ready_rel] == vB["ready_sha"]


def test_unrelated_pending_flush_preserves_canonical_remote_pair_in_index(tmp_path: Path) -> None:
    """Flushing unrelated pending items does not drop existing canonical pair A from index."""
    ctx = _setup_variant_fixture(tmp_path, "run-r12-unrelated")
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    client = ctx["client"]
    vA = ctx["variant_A"]

    # Register an unrelated pending item
    unrelated_bytes = b"unrelated test payload"
    unrelated_sha = hashlib.sha256(unrelated_bytes).hexdigest()
    unrelated_path = f"v1/objects/sha256/{unrelated_sha[:2]}/{unrelated_sha}"
    spool_file = state_dir / "backup" / "spool" / unrelated_sha
    spool_file.write_bytes(unrelated_bytes)
    with ledger._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO backup_pending
            (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
            VALUES ('unrelated-1', 'cas', ?, ?, ?, ?, 'application/octet-stream', 0, 'pending')
            """,
            (unrelated_sha, len(unrelated_bytes), str(spool_file), unrelated_path),
        )

    # Flush without scope restriction (or with unrelated in scope)
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
        )
    )
    assert result["status"] == "succeeded"

    idx = json.loads(client.objects["v1/backup/index.json"].decode("utf-8"))
    idx_paths = {item["remote_path"]: item["sha256"] for item in idx["items"]}
    assert vA["man_rel"] in idx_paths
    assert idx_paths[vA["man_rel"]] == vA["man_sha"]


def test_missing_index_entry_recovered_on_flush(tmp_path: Path) -> None:
    """When a valid receipt exists but was omitted from remote index, flush recovers it."""
    ctx = _setup_variant_fixture(tmp_path, "run-r12-recover")
    ledger, settings = ctx["ledger"], ctx["settings"]
    client = ctx["client"]
    vA = ctx["variant_A"]

    # Drop Variant A from index (simulate previous incomplete flush)
    idx_without_A = {
        "schema_version": "cardrag.backup-index.v1",
        "updated_at": "2026-10-20T00:00:00Z",
        "items": [],
    }
    client.objects["v1/backup/index.json"] = json.dumps(idx_without_A).encode("utf-8")

    # Run flush with needs_index_repair
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
        )
    )
    assert result["status"] == "succeeded"

    idx_after = json.loads(client.objects["v1/backup/index.json"].decode("utf-8"))
    idx_paths = {item["remote_path"]: item["sha256"] for item in idx_after["items"]}
    assert vA["man_rel"] in idx_paths
    assert idx_paths[vA["man_rel"]] == vA["man_sha"]


def test_variant_restored_to_local_cache_and_seed(tmp_path: Path) -> None:
    """Restore correctly materializes both canonical variant A and variant B into local cache."""
    ctx = _setup_variant_fixture(tmp_path, "run-r12-restore")
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    client = ctx["client"]
    vA = ctx["variant_A"]
    vB = ctx["variant_B"]
    key = ctx["reuse_key"]

    ledger.record_run_success("run-r12-restore", state_dir, settings, selected_issuers=("woori",))
    allowed, expected = ledger.scoped_backup_scope("run-r12-restore", state_dir, ("woori",))

    asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )

    # Now restore from client into clean target_dir
    restore_target = tmp_path / "restored"
    restore_result = asyncio.run(
        ledger.restore(
            settings,  # type: ignore[arg-type]
            restore_target,
            webdav_client=client,
        )
    )
    assert restore_result["status"] == "succeeded"

    # Both canonical cache and variant cache exist
    can_cache_man = restore_target / "cache" / "ocr" / key / "manifest.json"
    var_cache_man = restore_target / "cache" / "ocr" / key / "variants" / vB["ocr_sha"] / "manifest.json"
    assert can_cache_man.is_file()
    assert var_cache_man.is_file()
    assert can_cache_man.read_bytes() == vA["man_bytes"]
    assert var_cache_man.read_bytes() == vB["man_bytes"]


def test_corrupted_canonical_receipt_still_blocks_transport_and_reports_conflict(tmp_path: Path) -> None:
    """Contrived corrupt receipt on canonical path is NOT treated as a valid variant."""
    ctx = _setup_variant_fixture(tmp_path, "run-r12-corrupt-rcpt")
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    client = ctx["client"]
    vA = ctx["variant_A"]

    # Corrupt canonical receipt with invalid size and no ready receipt
    with ledger._get_connection() as conn:
        conn.execute(
            "UPDATE backup_receipts SET sha256 = ?, size_bytes = 15 WHERE remote_path = ?",
            ("8" * 64, vA["man_rel"]),
        )
        conn.execute("DELETE FROM backup_receipts WHERE remote_path = ?", (vA["ready_rel"],))

    allowed, expected = ledger.scoped_backup_scope("run-r12-corrupt-rcpt", state_dir, ("woori",))
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_conflicts"] >= 1
    assert result["backup_scope_complete"] is False


# ---------------------------------------------------------------------------
# 019 FIX_13 regression: variant evidence verification and the real retry path
# ---------------------------------------------------------------------------


def _r13_resolve(
    ctx: dict[str, Any], ocr_sha: str, man_sha: str, *, remote_root: str = ""
) -> tuple[str, str, str]:
    vB = ctx["variant_B"]
    with ctx["ledger"]._get_connection() as conn:
        return resolve_ocr_bundle_paths(
            ctx["state_dir"],
            ctx["reuse_key"],
            ocr_sha,
            man_sha,
            is_generation_only=True,
            pdf_sha256=vB["doc"]["pdf"]["sha256"],
            conn=conn,
            remote_root=remote_root,
        )


def _r13_materialize_a_in_cache(ctx: dict[str, Any]) -> Path:
    cache = ctx["state_dir"] / "cache" / "ocr" / ctx["reuse_key"]
    cache.mkdir(parents=True, exist_ok=True)
    vA = ctx["variant_A"]
    (cache / "manifest.json").write_bytes(vA["man_bytes"])
    (cache / "READY.json").write_bytes(vA["ready_bytes"])
    (cache / "ocr.md").write_bytes(vA["ocr_bytes"])
    return cache


def _r13_drop_spool(ctx: dict[str, Any]) -> None:
    spool = ctx["state_dir"] / "backup" / "spool"
    for p in spool.iterdir():
        p.unlink()


class _IndexUnreadableClient(RecordingClient):
    async def get(self, path: str) -> bytes | None:
        if path == "v1/backup/index.json":
            raise RuntimeError("simulated transport failure")
        return await super().get(path)


def test_spool_corroborated_receipt_still_routes_variant(tmp_path: Path) -> None:
    """Positive baseline for the byte-corroboration rewrite."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-baseline")
    vB = ctx["variant_B"]
    var_man = ocr_variant_manifest_path(ctx["reuse_key"], vB["ocr_sha"], kind="native").as_posix()
    paths = _r13_resolve(ctx, vB["ocr_sha"], vB["man_sha"])
    assert paths[1] == var_man
    assert paths[2] == ocr_variant_ready_path(ctx["reuse_key"], vB["ocr_sha"], kind="native").as_posix()
    assert paths[0] == vB["doc"]["ocr"]["path"]


def test_forged_doc_info_alone_does_not_route_past_corrupt_canonical_receipts(tmp_path: Path) -> None:
    """FIX_13 repro A: metadata strings carry no verification authority."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-forged")
    ledger, state_dir = ctx["ledger"], ctx["state_dir"]
    vA, vB, key = ctx["variant_A"], ctx["variant_B"], ctx["reuse_key"]

    # A spool evidence is pruned, canonical receipts are forged to arbitrary
    # digests, and doc_info alone still "describes" a plausible variant.
    _r13_drop_spool(ctx)
    with ledger._get_connection() as conn:
        conn.execute(
            "UPDATE backup_receipts SET sha256 = ?, size_bytes = 4 WHERE remote_path = ?",
            ("7" * 64, vA["man_rel"]),
        )
        conn.execute(
            "UPDATE backup_receipts SET sha256 = ?, size_bytes = 5 WHERE remote_path = ?",
            ("8" * 64, vA["ready_rel"]),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (
                f"doc_info_{vA['man_rel']}",
                json.dumps(
                    {
                        "reuse_key": key,
                        "pdf_sha256": vB["doc"]["pdf"]["sha256"],
                        "ocr_sha256": vA["ocr_sha"],
                        "kind": "native",
                    }
                ),
            ),
        )

    var_man = ocr_variant_manifest_path(key, vB["ocr_sha"], kind="native").as_posix()
    allowed, expected = ledger.scoped_backup_scope("run-r13-forged", state_dir, ("woori",))
    assert var_man not in expected
    assert expected[vA["man_rel"]][0] == vB["man_sha"]

    client = ctx["client"]
    result = asyncio.run(
        ledger.flush(
            ctx["settings"],  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_conflicts"] >= 1
    assert result["backup_scope_complete"] is False
    assert var_man not in client.objects
    assert client.objects[vA["man_rel"]] == vA["man_bytes"]
    idx = json.loads(client.objects["v1/backup/index.json"].decode("utf-8"))
    idx_paths = {item["remote_path"]: item["sha256"] for item in idx["items"]}
    # Forged receipt claims are never re-issued into the committed index.
    assert vA["man_rel"] not in idx_paths
    assert vA["ready_rel"] not in idx_paths


def test_receipt_size_mismatch_is_not_byte_corroborated(tmp_path: Path) -> None:
    ctx = _setup_variant_fixture(tmp_path, "run-r13-size")
    vA, vB = ctx["variant_A"], ctx["variant_B"]
    with ctx["ledger"]._get_connection() as conn:
        conn.execute(
            "UPDATE backup_receipts SET size_bytes = size_bytes + 1 WHERE remote_path = ?",
            (vA["man_rel"],),
        )
    paths = _r13_resolve(ctx, vB["ocr_sha"], vB["man_sha"])
    assert paths[1] == vA["man_rel"]  # held on canonical, not routed to a variant


def test_ready_receipt_pair_disagreement_blocks_variant_routing(tmp_path: Path) -> None:
    ctx = _setup_variant_fixture(tmp_path, "run-r13-ready")
    vA, vB = ctx["variant_A"], ctx["variant_B"]
    with ctx["ledger"]._get_connection() as conn:
        conn.execute(
            "UPDATE backup_receipts SET sha256 = ? WHERE remote_path = ?",
            ("9" * 64, vA["ready_rel"]),
        )
    paths = _r13_resolve(ctx, vB["ocr_sha"], vB["man_sha"])
    assert paths[1] == vA["man_rel"]


def test_multiple_receipt_rows_resolved_by_byte_corroborated_claim(tmp_path: Path) -> None:
    """Ambiguous same-root rows are corroborated individually, never by fetch order."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-multi")
    vA, vB = ctx["variant_A"], ctx["variant_B"]
    with ctx["ledger"]._get_connection() as conn:
        conn.execute(
            "INSERT INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
            " VALUES (?, '', ?, 12345, 0)",
            (vA["man_rel"], "f" * 64),
        )
    var_man = ocr_variant_manifest_path(ctx["reuse_key"], vB["ocr_sha"], kind="native").as_posix()
    paths = _r13_resolve(ctx, vB["ocr_sha"], vB["man_sha"])
    assert paths[1] == var_man  # the A row is corroborated by the spool bytes


def test_same_output_under_different_manifest_is_held_not_routed(tmp_path: Path) -> None:
    """Identical OCR body under a different canonical manifest: explicit hold, no reuse."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-sameout")
    vA = ctx["variant_A"]
    m2 = vA["manifest"].model_copy(update={"created_at": datetime(2026, 10, 20, 3, 0, tzinfo=UTC)})
    body2 = m2.canonical_bytes()
    assert body2 != vA["man_bytes"]
    sha2 = hashlib.sha256(body2).hexdigest()
    paths = _r13_resolve(ctx, m2.output.sha256, sha2)
    assert paths[1] == vA["man_rel"]  # held for operator review, not a coexisting variant


def test_cache_only_evidence_routes_variant_without_spool(tmp_path: Path) -> None:
    """Spool pruned but the formal restore cache holds A: corroboration succeeds."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-cacheonly")
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    vA, vB, key = ctx["variant_A"], ctx["variant_B"], ctx["reuse_key"]
    _r13_drop_spool(ctx)
    _r13_materialize_a_in_cache(ctx)

    ledger.record_run_success("run-r13-cacheonly", state_dir, settings, selected_issuers=("woori",))
    allowed, expected = ledger.scoped_backup_scope("run-r13-cacheonly", state_dir, ("woori",))
    var_man = ocr_variant_manifest_path(key, vB["ocr_sha"], kind="native").as_posix()
    assert var_man in expected
    assert vA["man_rel"] not in expected

    client = ctx["client"]
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["backup_scope_complete"] is True
    assert result["in_scope_conflicts"] == 0
    assert client.objects[var_man] == vB["man_bytes"]
    assert client.objects[vA["man_rel"]] == vA["man_bytes"]


def test_only_current_revision_in_cache_does_not_authenticate_old_receipt(tmp_path: Path) -> None:
    """Cache holding only the incoming B bytes proves nothing about the A receipt claim."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-curconly")
    ledger, state_dir = ctx["ledger"], ctx["state_dir"]
    vA, vB = ctx["variant_A"], ctx["variant_B"]
    _r13_drop_spool(ctx)
    cache = state_dir / "cache" / "ocr" / ctx["reuse_key"]
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "manifest.json").write_bytes(vB["man_bytes"])
    (cache / "READY.json").write_bytes(vB["ready_bytes"])

    allowed, expected = ledger.scoped_backup_scope("run-r13-curconly", state_dir, ("woori",))
    assert expected[vA["man_rel"]][0] == vB["man_sha"]
    result = asyncio.run(
        ledger.flush(
            ctx["settings"],  # type: ignore[arg-type]
            ctx["client"],
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_conflicts"] >= 1
    assert result["backup_scope_complete"] is False


def test_foreign_root_receipt_claim_conflicts_and_blocks_overwrite(tmp_path: Path) -> None:
    """A receipt bound to another root authenticates no variant and blocks the overwrite."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-foreign")
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    vA, vB = ctx["variant_A"], ctx["variant_B"]
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_receipts SET remote_root = ?", ("https://other.example",))

    paths = _r13_resolve(ctx, vB["ocr_sha"], vB["man_sha"], remote_root="")
    assert paths[1] == vA["man_rel"]

    ledger.record_run_success("run-r13-foreign", state_dir, settings, selected_issuers=("woori",))
    allowed, expected = ledger.scoped_backup_scope("run-r13-foreign", state_dir, ("woori",))
    assert expected[vA["man_rel"]][0] == vB["man_sha"]
    client = ctx["client"]
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["in_scope_conflicts"] >= 1
    assert result["backup_scope_complete"] is False
    # The previously verified remote bytes are never overwritten.
    assert client.objects[vA["man_rel"]] == vA["man_bytes"]
    assert client.objects[vA["ready_rel"]] == vA["ready_bytes"]


def test_force_flush_repairs_index_drift_through_standard_factory_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX_13 repro B: without an injected client or hook, pending 0 still repairs."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-repair")
    ledger, state_dir, vA = ctx["ledger"], ctx["state_dir"], ctx["variant_A"]
    settings = _cli_flush_env(tmp_path, monkeypatch, state_dir, ctx["client"])
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_receipts SET remote_root = ?", (settings.webdav_base_url,))
    # The committed index exists but has lost every verified item.
    ctx["client"].objects["v1/backup/index.json"] = json.dumps(
        {"schema_version": "cardrag.backup-index.v1", "updated_at": "2026-10-20T00:00:00Z", "items": []}
    ).encode("utf-8")

    result = asyncio.run(ledger.flush(settings, force=True))  # type: ignore[arg-type]
    assert result["status"] == "succeeded"
    idx = json.loads(ctx["client"].objects["v1/backup/index.json"].decode("utf-8"))
    idx_paths = {item["remote_path"]: item["sha256"] for item in idx["items"]}
    assert idx_paths[vA["man_rel"]] == vA["man_sha"]
    assert idx_paths[vA["ready_rel"]] == vA["ready_sha"]
    assert idx_paths[vA["cas_rel"]] == vA["ocr_sha"]

    before_puts = len(ctx["client"].puts)
    again = asyncio.run(ledger.flush(settings, force=True))  # type: ignore[arg-type]
    assert again["status"] == "unchanged"
    assert len(ctx["client"].puts) == before_puts


def test_force_flush_never_fabricates_or_blindly_replaces_unreadable_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent, corrupt, and read-failed index states are all refused by the probe."""
    ctx = _setup_variant_fixture(tmp_path, "run-r13-absent")
    ledger, state_dir, client = ctx["ledger"], ctx["state_dir"], ctx["client"]
    settings = _cli_flush_env(tmp_path, monkeypatch, state_dir, client)
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_receipts SET remote_root = ?", (settings.webdav_base_url,))

    # 1. No index document at all: a brand-new remote gets its first index
    #    from a transport batch, not from a pending-less probe.
    del client.objects["v1/backup/index.json"]
    result = asyncio.run(ledger.flush(settings, force=True))  # type: ignore[arg-type]
    assert result["status"] == "unchanged"
    assert client.puts == []
    assert "v1/backup/index.json" not in client.objects

    # 2. Unparseable committed index: never blindly replaced.
    client.objects["v1/backup/index.json"] = b"{ not json"
    result = asyncio.run(ledger.flush(settings, force=True))  # type: ignore[arg-type]
    assert result["status"] == "unchanged"
    assert client.puts == []
    assert client.objects["v1/backup/index.json"] == b"{ not json"

    # 3. Transport failure reading the index: no writes at all.
    from cardrag_worker import backup as backup_module

    failing = _IndexUnreadableClient()
    failing.objects.update(client.objects)
    monkeypatch.setattr(backup_module.WebDAVClient, "from_env", staticmethod(lambda **kw: failing))
    result = asyncio.run(ledger.flush(settings, force=True))  # type: ignore[arg-type]
    assert result["status"] == "unchanged"
    assert failing.puts == []


def test_cli_backup_flush_run_registers_and_transports_variant_live_like(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX_13 repro C end-to-end: the shipped CLI retry itself closes the live gap.

    Reproduces the actual production ledger shape found by the reviewer:
    pending 0, no A spool, doc_info describing B, only the verified A pair
    remotely (receipts at the current root, restored cache bytes), and the
    committed index missing A.  No recorder pre-call and no factory hook —
    only WebDAVClient.from_env's transport is substituted.
    """
    from typer.testing import CliRunner

    from cardrag_worker.cli import app

    ctx = _setup_variant_fixture(tmp_path, "run-r13-cli")
    ledger, state_dir, client = ctx["ledger"], ctx["state_dir"], ctx["client"]
    vA, vB, key = ctx["variant_A"], ctx["variant_B"], ctx["reuse_key"]
    settings = _cli_flush_env(tmp_path, monkeypatch, state_dir, client)

    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_receipts SET remote_root = ?", (settings.webdav_base_url,))
        conn.execute("DELETE FROM backup_pending")
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (
                f"doc_info_{vA['man_rel']}",
                json.dumps(
                    {
                        "document_id": "doc-woori-1",
                        "reuse_key": key,
                        "pdf_sha256": vB["doc"]["pdf"]["sha256"],
                        "ocr_sha256": vB["ocr_sha"],
                        "kind": "native",
                    }
                ),
            ),
        )
    assert conn_check_pending_zero(ledger)
    _r13_drop_spool(ctx)
    _r13_materialize_a_in_cache(ctx)
    client.objects["v1/backup/index.json"] = json.dumps(
        {"schema_version": "cardrag.backup-index.v1", "updated_at": "2026-10-20T00:00:00Z", "items": []}
    ).encode("utf-8")

    var_man = ocr_variant_manifest_path(key, vB["ocr_sha"], kind="native").as_posix()
    var_ready = ocr_variant_ready_path(key, vB["ocr_sha"], kind="native").as_posix()

    res = CliRunner().invoke(app, ["backup", "flush", "--run", "run-r13-cli", "--issuer", "woori"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["status"] == "succeeded", payload
    assert payload["in_scope_conflicts"] == 0
    assert payload["in_scope_missing"] == 0
    assert payload["backup_scope_complete"] is True

    # B variant metadata + bodies transported; A canonical pair untouched.
    assert client.objects[var_man] == vB["man_bytes"]
    assert client.objects[var_ready] == vB["ready_bytes"]
    assert client.objects[vB["doc"]["ocr"]["path"]] == vB["ocr_bytes"]
    assert client.objects[vA["man_rel"]] == vA["man_bytes"]
    assert client.objects[vA["ready_rel"]] == vA["ready_bytes"]
    assert client.objects[vA["cas_rel"]] == vA["ocr_bytes"]

    idx = json.loads(client.objects["v1/backup/index.json"].decode("utf-8"))
    idx_paths = {item["remote_path"]: item["sha256"] for item in idx["items"]}
    assert idx_paths[vA["man_rel"]] == vA["man_sha"]
    assert idx_paths[vA["ready_rel"]] == vA["ready_sha"]
    assert idx_paths[var_man] == vB["man_sha"]
    assert idx_paths[var_ready] == hashlib.sha256(vB["ready_bytes"]).hexdigest()

    # Second CLI retry is idempotent: nothing re-registers or retransports.
    before_puts = len(client.puts)
    res2 = CliRunner().invoke(app, ["backup", "flush", "--run", "run-r13-cli", "--issuer", "woori"])
    assert res2.exit_code == 0, res2.output
    payload2 = json.loads(res2.stdout)
    assert payload2["status"] == "unchanged"
    assert payload2["uploaded_count"] == 0
    assert payload2["backup_scope_complete"] is True
    assert len(client.puts) == before_puts


def conn_check_pending_zero(ledger: BackupLedger) -> bool:
    with ledger._get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM backup_pending WHERE status = 'pending'").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 019 FIX_18: stale past-revision CAS scope and native metadata recovery
# ---------------------------------------------------------------------------


def _r18_content_doc(tmp_path: Path, run_id: str) -> dict[str, Any]:
    """A current content-kind document that owns no native metadata (live shape)."""
    state_dir = tmp_path / "state"
    sel = _make_document(
        state_dir,
        run_id,
        document_id="doc-woori-1",
        issuer="woori",
        reuse_key_present=True,
        cache_kind="content",
    )
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    # content OCR artifacts are shared cache objects, not document-owned native
    # metadata; production runs retain no native manifest for them.
    for item in (state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr").iterdir():
        item.unlink()
    return {"state_dir": state_dir, "sel": sel, "settings": settings, "ledger": ledger}


def _r18_tag_past_cas(
    ledger: BackupLedger,
    *,
    old_ocr: str,
    pdf_sha: str,
    pending: bool = False,
    receipt: bool = True,
) -> str:
    old_path = object_path(old_ocr).as_posix()
    info = {
        "document_id": "doc-woori-1",
        "reuse_key": "e" * 64,
        "pdf_sha256": pdf_sha,
        "ocr_sha256": old_ocr,
        "kind": "native",
    }
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{old_path}", "doc-woori-1"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_info_{old_path}", json.dumps(info)),
        )
        if receipt:
            conn.execute(
                "INSERT INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
                " VALUES (?, '', ?, 16456, 0)",
                (old_path, old_ocr),
            )
        if pending:
            conn.execute(
                "INSERT INTO backup_pending"
                " (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type,"
                "  created_at, status) VALUES ('past-cas', 'ocr_cas', ?, 16456, '/tmp/none', ?,"
                "  'text/markdown; charset=utf-8', 0, 'pending')",
                (old_ocr, old_path),
            )
    return old_path


def test_past_revision_cas_is_not_current_required_scope(tmp_path: Path) -> None:
    """Live FIX_18 shape: content document + leftover doc_map/doc_info for an old OCR CAS."""
    ctx = _r18_content_doc(tmp_path, "run-r18-stale-cas")
    ledger, state_dir, sel = ctx["ledger"], ctx["state_dir"], ctx["sel"]
    old_ocr = "75" * 32
    old_path = _r18_tag_past_cas(ledger, old_ocr=old_ocr, pdf_sha=sel["doc"]["pdf"]["sha256"])
    cur_path = object_path(sel["ocr_sha"]).as_posix()

    allowed, expected = ledger.scoped_backup_scope("run-r18-stale-cas", state_dir, ("woori",))
    assert old_path not in allowed, "past-revision CAS must not be claimed as current required"
    assert old_path not in expected
    assert cur_path in allowed and cur_path in expected
    assert sel["doc"]["pdf"]["path"] in allowed and sel["doc"]["pdf"]["path"] in expected

    # Retained receipt and metadata tag are evidence history, not deleted.
    with ledger._get_connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM backup_receipts WHERE remote_path = ?", (old_path,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM backup_meta WHERE key = ?", (f"doc_map_{old_path}",)
            ).fetchone()[0]
            == 1
        )

    client = RecordingClient()
    asyncio.run(
        ledger.flush(
            ctx["settings"],  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert old_path not in client.objects
    assert ledger._last_excluded_stale_object == 1


def test_past_revision_cas_stays_excluded_with_pending_row(tmp_path: Path) -> None:
    """A stale CAS pending row is left in the ledger but never transported as current."""
    ctx = _r18_content_doc(tmp_path, "run-r18-stale-pending")
    ledger, state_dir, sel = ctx["ledger"], ctx["state_dir"], ctx["sel"]
    old_path = _r18_tag_past_cas(
        ledger,
        old_ocr="76" * 32,
        pdf_sha=sel["doc"]["pdf"]["sha256"],
        pending=True,
    )
    allowed, _expected = ledger.scoped_backup_scope("run-r18-stale-pending", state_dir, ("woori",))
    assert old_path not in allowed
    client = RecordingClient()
    flush = asyncio.run(
        ledger.flush(
            ctx["settings"],  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
        )
    )
    assert old_path not in client.objects
    assert flush["pending_out_of_scope"] >= 1
    with ledger._get_connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM backup_pending WHERE remote_path = ?", (old_path,)).fetchone()[
                0
            ]
            == 1
        )


def test_past_cas_still_required_when_another_current_document_references_it(tmp_path: Path) -> None:
    """Protection: a current selected document's seal reference keeps a shared CAS in scope."""
    ctx = _r18_content_doc(tmp_path, "run-r18-shared-cas")
    ledger, state_dir, sel = ctx["ledger"], ctx["state_dir"], ctx["sel"]
    old_ocr = "77" * 32
    old_path = _r18_tag_past_cas(ledger, old_ocr=old_ocr, pdf_sha=sel["doc"]["pdf"]["sha256"])

    other = _make_document(
        state_dir,
        "run-r18-shared-cas",
        document_id="doc-woori-2",
        issuer="woori",
        reuse_key_present=True,
        cache_kind="content",
    )
    other["doc"]["ocr"]["sha256"] = old_ocr
    other["doc"]["ocr"]["path"] = old_path
    other["doc"]["ocr"]["size_bytes"] = 16456
    _write_seal(state_dir, "run-r18-shared-cas", [sel, other])

    allowed, expected = ledger.scoped_backup_scope("run-r18-shared-cas", state_dir, ("woori",))
    assert old_path in allowed, "a current seal reference must stay protected"
    assert old_path in expected


def test_native_tagged_cas_without_current_reference_stays_unbound(tmp_path: Path) -> None:
    """Fail-closed: native/unknown provenance is never blanket-excluded."""
    ctx = _normal_setup(tmp_path, "run-r18-native-tag", reuse_key=True)
    ledger, state_dir = ctx["ledger"], ctx["state_dir"]
    (state_dir / "runs/run-r18-native-tag/documents/doc-woori-1/ocr/native-manifest.json").unlink()
    unknown = object_path("78" * 32).as_posix()
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{unknown}", "doc-woori-1"),
        )
    allowed, expected = ledger.scoped_backup_scope("run-r18-native-tag", state_dir, ("woori",))
    assert unknown in allowed
    assert unknown not in expected


def _r18_recovery_ctx(tmp_path: Path, run_id: str) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    sel = _make_document(
        state_dir,
        run_id,
        document_id="doc-woori-1",
        issuer="woori",
        reuse_key_present=True,
        cache_kind="native",
    )
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    # Retention pruned the run-local native evidence; only seal + remote pair remain.
    for item in (state_dir / "runs" / run_id / "documents" / "doc-woori-1" / "ocr").iterdir():
        item.unlink()
    key = sel["manifest"].reuse_key
    man_rel = ocr_manifest_path(key, kind="native").as_posix()
    ready_rel = ocr_ready_path(key, kind="native").as_posix()
    man_bytes = sel["manifest"].canonical_bytes()
    man_sha = hashlib.sha256(man_bytes).hexdigest()
    ready = OCRReady(reuse_key=key, manifest_sha256=man_sha, ocr_sha256=sel["ocr_sha"])
    ready_bytes = ready.canonical_bytes()
    client = RecordingClient()
    client.objects[man_rel] = man_bytes
    client.objects[ready_rel] = ready_bytes
    with ledger._get_connection() as conn:
        for rel, sha, size in (
            (man_rel, man_sha, len(man_bytes)),
            (ready_rel, hashlib.sha256(ready_bytes).hexdigest(), len(ready_bytes)),
        ):
            conn.execute(
                "INSERT INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
                " VALUES (?, '', ?, ?, 0)",
                (rel, sha, size),
            )
    return {
        "state_dir": state_dir,
        "sel": sel,
        "settings": settings,
        "ledger": ledger,
        "client": client,
        "key": key,
        "man_rel": man_rel,
        "ready_rel": ready_rel,
        "man_bytes": man_bytes,
        "ready_bytes": ready_bytes,
    }


def test_recover_metadata_restores_verified_native_pair(tmp_path: Path) -> None:
    ctx = _r18_recovery_ctx(tmp_path, "run-r18-recover")
    ledger, settings, state_dir = ctx["ledger"], ctx["settings"], ctx["state_dir"]
    client, key = ctx["client"], ctx["key"]
    man_rel, ready_rel = ctx["man_rel"], ctx["ready_rel"]

    allowed, expected = ledger.scoped_backup_scope("run-r18-recover", state_dir, ("woori",))
    assert man_rel in allowed and man_rel not in expected
    assert ready_rel in allowed and ready_rel not in expected

    before_puts = len(client.puts)
    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, "run-r18-recover", ("woori",), client)
    )
    assert result["status"] == "succeeded", result
    assert result["verified_count"] == 1
    assert result["pairs"][0]["action"] == "restored"
    assert len(client.puts) == before_puts, "recovery must not upload anything"
    with ledger._get_connection() as conn:
        n_receipts = conn.execute("SELECT COUNT(*) FROM backup_receipts").fetchone()[0]
    assert n_receipts == 2

    cache_dir = state_dir / "cache" / "ocr" / key
    assert (cache_dir / "manifest.json").read_bytes() == ctx["man_bytes"]
    assert (cache_dir / "READY.json").read_bytes() == ctx["ready_bytes"]

    allowed, expected = ledger.scoped_backup_scope("run-r18-recover", state_dir, ("woori",))
    assert man_rel in expected and ready_rel in expected
    assert result["in_scope_unbound_after"] < result["in_scope_unbound_before"]
    # The shipped scoped CLI retry re-registers the run before flushing; emulate
    # that formal entry here so only the CAS bodies need transporting.
    ledger.record_run_success("run-r18-recover", state_dir, settings, selected_issuers=("woori",))
    allowed, expected = ledger.scoped_backup_scope("run-r18-recover", state_dir, ("woori",))

    flush = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert flush["backup_scope_complete"] is True, flush
    assert man_rel not in client.puts and ready_rel not in client.puts

    # Idempotent: already bound and already present, still no remote writes.
    before_puts = len(client.puts)
    again = asyncio.run(ledger.recover_verified_ocr_metadata(settings, "run-r18-recover", ("woori",), client))
    assert again["status"] == "succeeded"
    assert again["pairs"][0]["action"] == "already_bound"
    assert len(client.puts) == before_puts


@pytest.mark.parametrize("tamper", ["manifest_bytes", "ready_bytes", "no_receipt", "missing_manifest"])
def test_recover_metadata_blocks_unverified_pairs(tamper: str, tmp_path: Path) -> None:
    ctx = _r18_recovery_ctx(tmp_path, f"run-r18-bad-{tamper}")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    man_rel, ready_rel = ctx["man_rel"], ctx["ready_rel"]
    if tamper == "manifest_bytes":
        client.objects[man_rel] = b'{"corrupted": true}'
    elif tamper == "ready_bytes":
        client.objects[ready_rel] = b'{"corrupted": true}'
    elif tamper == "missing_manifest":
        del client.objects[man_rel]
    else:
        with ledger._get_connection() as conn:
            conn.execute("DELETE FROM backup_receipts WHERE remote_path = ?", (man_rel,))

    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, f"run-r18-bad-{tamper}", ("woori",), client)
    )
    assert result["status"] == "partial", result
    assert result["verified_count"] == 0
    assert result["pairs"][0]["error"]
    cache_dir = state_dir / "cache" / "ocr" / ctx["key"]
    assert not (cache_dir / "manifest.json").exists()
    assert not (cache_dir / "READY.json").exists()

    allowed, expected = ledger.scoped_backup_scope(f"run-r18-bad-{tamper}", state_dir, ("woori",))
    assert man_rel not in expected
    flush = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert flush["backup_scope_complete"] is False


def test_recover_metadata_never_clobbers_other_variant_cache(tmp_path: Path) -> None:
    """A differing canonical cache manifest is preserved; verified pair goes to variants/."""
    ctx = _r18_recovery_ctx(tmp_path, "run-r18-conflict")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    key = ctx["key"]
    cache_dir = state_dir / "cache" / "ocr" / key
    cache_dir.mkdir(parents=True, exist_ok=True)
    foreign = b'{"other":"variant"}'
    (cache_dir / "manifest.json").write_bytes(foreign)
    (cache_dir / "READY.json").write_bytes(b'{"other":"ready"}')

    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, "run-r18-conflict", ("woori",), client)
    )
    assert result["status"] == "succeeded", result
    assert (cache_dir / "manifest.json").read_bytes() == foreign
    # No clobber and no unverifiable claim: the differing local pair still wins
    # nothing, so scope stays unbound until the local evidence is reconciled.
    variance = cache_dir / "variants" / ctx["sel"]["ocr_sha"] / "manifest.json"
    assert variance.read_bytes() == ctx["man_bytes"]
    # The verified remote pair still binds the canonical scope; the foreign local
    # canonical bytes were neither destroyed nor granted authority.
    _allowed, expected = ledger.scoped_backup_scope("run-r18-conflict", state_dir, ("woori",))
    assert ctx["man_rel"] in expected


def test_cli_recover_metadata_targets_exact_keys_and_unblocks_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from cardrag_worker.cli import app

    ctx = _r18_recovery_ctx(tmp_path, "run-r18-cli")
    ledger, state_dir, client = ctx["ledger"], ctx["state_dir"], ctx["client"]
    settings = _cli_flush_env(tmp_path, monkeypatch, state_dir, client)
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_receipts SET remote_root = ?", (settings.webdav_base_url,))

    res = CliRunner().invoke(
        app,
        [
            "backup",
            "recover-metadata",
            "--run",
            "run-r18-cli",
            "--issuer",
            "woori",
            "--key",
            ctx["key"],
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["status"] == "succeeded"
    assert payload["verified_count"] == 1
    assert payload["scoped_run_id"] == "run-r18-cli"
    assert payload["in_scope_unbound_after"] == 0

    again = CliRunner().invoke(
        app,
        ["backup", "recover-metadata", "--run", "run-r18-cli", "--issuer", "woori"],
    )
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["pairs"][0]["action"] == "already_bound"

    status = CliRunner().invoke(app, ["backup", "status", "--run", "run-r18-cli", "--issuer", "woori"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["backup_in_scope_unbound"] == 0

    flush = CliRunner().invoke(app, ["backup", "flush", "--run", "run-r18-cli", "--issuer", "woori"])
    assert flush.exit_code == 0, flush.output
    flush_payload = json.loads(flush.stdout)
    assert flush_payload["backup_scope_complete"] is True
    assert ctx["man_rel"] not in client.puts
    assert ctx["ready_rel"] not in client.puts


# ---------------------------------------------------------------------------
# 019 FIX_19: full-pair candidate selection and cache write boundaries
# ---------------------------------------------------------------------------


def _r19_variant_paths(ctx: dict[str, Any]) -> tuple[str, str]:
    return (
        ocr_variant_manifest_path(ctx["key"], ctx["sel"]["ocr_sha"], kind="native").as_posix(),
        ocr_variant_ready_path(ctx["key"], ctx["sel"]["ocr_sha"], kind="native").as_posix(),
    )


def test_recover_metadata_skips_partial_or_other_output_variant(tmp_path: Path) -> None:
    """A partial/different-output variant must not block a healthy canonical pair."""
    ctx = _r18_recovery_ctx(tmp_path, "run-r19-partial-variant")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    var_man, _var_ready = _r19_variant_paths(ctx)
    foreign_output = ArtifactRef(
        media_type="text/markdown; charset=utf-8",
        path=f"v1/objects/sha256/aa/{'aa' * 32}",
        sha256="aa" * 32,
        size_bytes=123,
    )
    variant_manifest = ctx["sel"]["manifest"].model_copy(update={"output": foreign_output})
    client.objects[var_man] = variant_manifest.canonical_bytes()  # no variant READY/receipt

    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, "run-r19-partial-variant", ("woori",), client)
    )
    assert result["status"] == "succeeded", result
    assert result["verified_count"] == 1
    assert result["pairs"][0]["action"] == "restored"
    assert result["pairs"][0]["manifest_path"] == ctx["man_rel"]
    cache_dir = state_dir / "cache" / "ocr" / ctx["key"]
    assert (cache_dir / "manifest.json").read_bytes() == ctx["man_bytes"]
    assert not (cache_dir / "variants").exists()


def test_recover_metadata_uses_healthy_variant_when_canonical_invalid(tmp_path: Path) -> None:
    """A healthy variant pair is recovered when the canonical claim is invalid."""
    ctx = _r18_recovery_ctx(tmp_path, "run-r19-healthy-variant")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    var_man, var_ready = _r19_variant_paths(ctx)
    client.objects[var_man] = client.objects.pop(ctx["man_rel"])
    client.objects[var_ready] = client.objects.pop(ctx["ready_rel"])
    with ledger._get_connection() as conn:
        conn.execute(
            "UPDATE backup_receipts SET remote_path = ? WHERE remote_path = ?",
            (var_man, ctx["man_rel"]),
        )
        conn.execute(
            "UPDATE backup_receipts SET remote_path = ? WHERE remote_path = ?",
            (var_ready, ctx["ready_rel"]),
        )
    # Canonical address remains, but corrupt and without receipts.
    client.objects[ctx["man_rel"]] = b'{"corrupt": true}'

    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, "run-r19-healthy-variant", ("woori",), client)
    )
    assert result["status"] == "succeeded", result
    assert result["pairs"][0]["manifest_path"] == var_man
    assert result["pairs"][0]["ready_path"] == var_ready
    target = state_dir / "cache" / "ocr" / ctx["key"] / "variants" / ctx["sel"]["ocr_sha"]
    assert (target / "manifest.json").read_bytes() == ctx["man_bytes"]
    assert (target / "READY.json").read_bytes() == ctx["ready_bytes"]
    allowed, expected = ledger.scoped_backup_scope("run-r19-healthy-variant", state_dir, ("woori",))
    assert var_man in expected and var_ready in expected
    assert ctx["man_rel"] not in expected


def test_recover_metadata_reports_all_invalid_candidates(tmp_path: Path) -> None:
    ctx = _r18_recovery_ctx(tmp_path, "run-r19-all-invalid")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    var_man, _ = _r19_variant_paths(ctx)
    client.objects[ctx["man_rel"]] = b'{"corrupt": true}'
    client.objects[var_man] = b'{"also": "corrupt"}'
    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, "run-r19-all-invalid", ("woori",), client)
    )
    assert result["status"] == "partial"
    assert result["verified_count"] == 0
    error = result["pairs"][0]["error"]
    assert "variant" in error and "canonical" in error
    assert not (state_dir / "cache" / "ocr" / ctx["key"]).exists()


def test_recover_metadata_rejects_dangling_leaf_symlink(tmp_path: Path) -> None:
    import os as _os

    ctx = _r18_recovery_ctx(tmp_path, "run-r19-leaf-symlink")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    cache_dir = state_dir / "cache" / "ocr" / ctx["key"]
    cache_dir.mkdir(parents=True)
    outside = tmp_path / "outside-manifest.json"
    _os.symlink(outside, cache_dir / "manifest.json")

    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, "run-r19-leaf-symlink", ("woori",), client)
    )
    assert result["status"] == "partial"
    assert "unsafe" in (result["pairs"][0]["error"] or "")
    assert not outside.exists()
    assert not (cache_dir / "READY.json").exists()


def test_recover_metadata_rejects_key_and_variants_directory_symlinks(tmp_path: Path) -> None:
    import os as _os

    # Key directory symlink: no read or write may pass through it.
    ctx = _r18_recovery_ctx(tmp_path, "run-r19-key-symlink")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    cache_root = state_dir / "cache" / "ocr"
    cache_root.mkdir(parents=True)
    outside_dir = tmp_path / "outside-key"
    outside_dir.mkdir()
    _os.symlink(outside_dir, cache_root / ctx["key"])
    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, "run-r19-key-symlink", ("woori",), client)
    )
    assert result["status"] == "partial"
    assert "unsafe" in (result["pairs"][0]["error"] or "")
    assert not (outside_dir / "manifest.json").exists()

    # Variants directory symlink: canonical is a different regular variant, so
    # the verified pair would need the variant directory.
    ctx2 = _r18_recovery_ctx(tmp_path / "second", "run-r19-variants-symlink")
    ledger2, settings2, state_dir2, client2 = (
        ctx2["ledger"],
        ctx2["settings"],
        ctx2["state_dir"],
        ctx2["client"],
    )
    cache2 = state_dir2 / "cache" / "ocr" / ctx2["key"]
    cache2.mkdir(parents=True)
    (cache2 / "manifest.json").write_bytes(b"other variant manifest")
    (cache2 / "READY.json").write_bytes(b"other variant ready")
    outside2 = tmp_path / "outside-variants"
    outside2.mkdir()
    _os.symlink(outside2, cache2 / "variants")
    result2 = asyncio.run(
        ledger2.recover_verified_ocr_metadata(settings2, "run-r19-variants-symlink", ("woori",), client2)
    )
    assert result2["status"] == "partial"
    assert "unsafe" in (result2["pairs"][0]["error"] or "")
    assert not (outside2 / ctx2["sel"]["ocr_sha"]).exists()
    assert (cache2 / "manifest.json").read_bytes() == b"other variant manifest"


def test_recover_metadata_conflicting_variant_target_is_not_overwritten(tmp_path: Path) -> None:
    ctx = _r18_recovery_ctx(tmp_path, "run-r19-target-conflict")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    cache_dir = state_dir / "cache" / "ocr" / ctx["key"]
    variant_dir = cache_dir / "variants" / ctx["sel"]["ocr_sha"]
    variant_dir.mkdir(parents=True)
    (cache_dir / "manifest.json").write_bytes(b"canonical other variant")
    (cache_dir / "READY.json").write_bytes(b"canonical other ready")
    (variant_dir / "manifest.json").write_bytes(b"conflicting target manifest")

    result = asyncio.run(
        ledger.recover_verified_ocr_metadata(settings, "run-r19-target-conflict", ("woori",), client)
    )
    assert result["status"] == "partial"
    assert "conflict" in (result["pairs"][0]["error"] or "")
    assert (cache_dir / "manifest.json").read_bytes() == b"canonical other variant"
    assert (variant_dir / "manifest.json").read_bytes() == b"conflicting target manifest"
    assert not (variant_dir / "READY.json").exists()


def test_recover_metadata_write_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    import os as _os
    import stat as _stat

    ctx = _r18_recovery_ctx(tmp_path, "run-r19-write-deny")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    cache_dir = state_dir / "cache" / "ocr" / ctx["key"]
    cache_dir.mkdir(parents=True)
    _os.chmod(cache_dir, _stat.S_IRUSR | _stat.S_IXUSR)
    try:
        result = asyncio.run(
            ledger.recover_verified_ocr_metadata(settings, "run-r19-write-deny", ("woori",), client)
        )
    finally:
        _os.chmod(cache_dir, 0o700)
    assert result["status"] == "partial"
    assert result["verified_count"] == 0
    assert not (cache_dir / "manifest.json").exists()
    assert not (cache_dir / "READY.json").exists()


def test_recover_metadata_cache_write_lock_serializes_writers(tmp_path: Path) -> None:
    import fcntl as _fcntl
    import os as _os

    ctx = _r18_recovery_ctx(tmp_path, "run-r19-locked")
    ledger, settings, state_dir, client = ctx["ledger"], ctx["settings"], ctx["state_dir"], ctx["client"]
    lock_path = state_dir / "ocr-cache-recovery.lock"
    descriptor = _os.open(lock_path, _os.O_CREAT | _os.O_RDWR, 0o600)
    _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    try:
        result = asyncio.run(
            ledger.recover_verified_ocr_metadata(settings, "run-r19-locked", ("woori",), client)
        )
    finally:
        _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        _os.close(descriptor)
    assert result["status"] == "locked"
    assert not (state_dir / "cache" / "ocr" / ctx["key"]).exists()


def test_cli_recover_metadata_full_pair_with_partial_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from cardrag_worker.cli import app

    ctx = _r18_recovery_ctx(tmp_path, "run-r19-cli")
    ledger, state_dir, client = ctx["ledger"], ctx["state_dir"], ctx["client"]
    settings = _cli_flush_env(tmp_path, monkeypatch, state_dir, client)
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_receipts SET remote_root = ?", (settings.webdav_base_url,))
    var_man, _ = _r19_variant_paths(ctx)
    client.objects[var_man] = ctx["man_bytes"]  # partial variant: no READY/receipt

    res = CliRunner().invoke(
        app,
        [
            "backup",
            "recover-metadata",
            "--run",
            "run-r19-cli",
            "--issuer",
            "woori",
            "--key",
            ctx["key"],
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["status"] == "succeeded"
    assert payload["pairs"][0]["manifest_path"] == ctx["man_rel"]
    assert payload["in_scope_unbound_after"] == 0

    flush = CliRunner().invoke(app, ["backup", "flush", "--run", "run-r19-cli", "--issuer", "woori"])
    assert flush.exit_code == 0, flush.output
    assert json.loads(flush.stdout)["backup_scope_complete"] is True


# ---------------------------------------------------------------------------
# 019 FIX_20: growing backup index bounded reads and safe commit merge
# ---------------------------------------------------------------------------


class _LimitedIndexClient(RecordingClient):
    """get/put client that enforces max_bytes like the production WebDAVClient."""

    def __init__(self, limit: int = 8 * 1024 * 1024) -> None:
        super().__init__()
        self.limit = limit
        self.index_max_bytes: list[int | None] = []

    async def get_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes | None:
        if path == "v1/backup/index.json":
            self.index_max_bytes.append(max_bytes)
        body = self.objects.get(path)
        if body is not None and max_bytes is not None and len(body) > max_bytes:
            from cardrag_core.webdav import WebDAVIntegrityError

            raise WebDAVIntegrityError(f"WebDAV object {path} exceeds the read limit")
        return body


def _r20_index(
    items: list[dict[str, Any]],
    *,
    padding: int = 0,
    extra: dict[str, Any] | None = None,
) -> bytes:
    merged = [dict(item) for item in items]
    if extra is not None:
        merged.append(extra)
    if padding:
        # Keep every item real (restore treats index entries as artifacts); the
        # index grows through retained per-item metadata fields.
        assert merged
        merged[0] = {**merged[0], "padding": "x" * padding}
    return json.dumps(
        {
            "schema_version": "cardrag.backup-index.v1",
            "updated_at": "2026-10-21T00:00:00Z",
            "items": merged,
        },
        indent=2,
    ).encode("utf-8")


def _r20_scoped_receipts(tmp_path: Path, run_id: str) -> dict[str, Any]:
    """A retained generation-only run with bound receipts and no pending rows."""
    state_dir = tmp_path / "state"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    with ledger._get_connection() as conn:
        for rel, (sha, size) in expected.items():
            assert isinstance(size, int)
            conn.execute(
                "INSERT INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)"
                " VALUES (?, '', ?, ?, 0)",
                (rel, sha, size),
            )
    return {
        "state_dir": state_dir,
        "sel": sel,
        "settings": settings,
        "ledger": ledger,
        "allowed": allowed,
        "expected": expected,
    }


def test_scoped_completion_reads_growing_index_with_dedicated_bound(tmp_path: Path) -> None:
    """The production-shaped >8MiB index still proves completion via the dedicated limit."""
    from cardrag_worker.backup import BACKUP_INDEX_MAX_BYTES

    ctx = _r20_scoped_receipts(tmp_path, "run-r20-large")
    ledger, settings = ctx["ledger"], ctx["settings"]
    allowed, expected = ctx["allowed"], ctx["expected"]
    items = [{"remote_path": rel, "sha256": sha, "size_bytes": size} for rel, (sha, size) in expected.items()]
    index = _r20_index(items, padding=8_500_000)
    assert len(index) > 8 * 1024 * 1024
    assert len(index) < BACKUP_INDEX_MAX_BYTES
    client = _LimitedIndexClient()
    client.objects["v1/backup/index.json"] = index

    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["status"] == "unchanged", result
    assert result["backup_scope_complete"] is True
    assert result["index_read_error"] is None
    assert client.index_max_bytes
    assert all(value == BACKUP_INDEX_MAX_BYTES for value in client.index_max_bytes)


def test_index_over_dedicated_bound_reports_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cardrag_worker import backup as backup_module

    # 1. The real client raises its integrity error at the dedicated bound.
    ctx = _r20_scoped_receipts(tmp_path / "a", "run-r20-too-large")

    class _AlwaysTooLarge(RecordingClient):
        async def get_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes | None:
            if path == "v1/backup/index.json":
                from cardrag_core.webdav import WebDAVIntegrityError

                raise WebDAVIntegrityError("exceeds")
            return await super().get(path)

    client = _AlwaysTooLarge()
    result = asyncio.run(
        ctx["ledger"].flush(
            ctx["settings"],  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=ctx["allowed"],
            expected_artifacts=ctx["expected"],
        )
    )
    assert result["status"] == "unchanged"
    assert result["backup_scope_complete"] is None
    assert result["index_read_error"] == "size_limit"

    # 2. A get-only client returning a document physically larger than the
    #    dedicated bound is rejected by the reader's own finite length check.
    ctx2 = _r20_scoped_receipts(tmp_path / "b", "run-r20-over-bound")
    client2 = RecordingClient()
    client2.objects["v1/backup/index.json"] = b"x" * (backup_module.BACKUP_INDEX_MAX_BYTES + 1)
    result2 = asyncio.run(
        ctx2["ledger"].flush(
            ctx2["settings"],  # type: ignore[arg-type]
            client2,
            force=True,
            allowed_remote_paths=ctx2["allowed"],
            expected_artifacts=ctx2["expected"],
        )
    )
    assert result2["backup_scope_complete"] is None
    assert result2["index_read_error"] == "size_limit"

    # 3. A get-only client returning an oversized valid document is bounded by
    #    the same constant (monkeypatched to keep the fixture small).
    monkeypatch.setattr(backup_module, "BACKUP_INDEX_MAX_BYTES", 1024)
    ctx3 = _r20_scoped_receipts(tmp_path / "c", "run-r20-small-limit")
    items = [
        {"remote_path": rel, "sha256": sha, "size_bytes": size}
        for rel, (sha, size) in ctx3["expected"].items()
    ]
    client3 = RecordingClient()
    client3.objects["v1/backup/index.json"] = _r20_index(items, padding=2048)
    result3 = asyncio.run(
        ctx3["ledger"].flush(
            ctx3["settings"],  # type: ignore[arg-type]
            client3,
            force=True,
            allowed_remote_paths=ctx3["allowed"],
            expected_artifacts=ctx3["expected"],
        )
    )
    assert result3["backup_scope_complete"] is None
    assert result3["index_read_error"] == "size_limit"


def test_commit_refuses_to_overwrite_unreadable_index(tmp_path: Path) -> None:
    """Invalid JSON and read failures never merge into an empty index."""
    for failure in ("invalid_json", "transport"):
        state_dir = tmp_path / failure / "state"
        run_id = f"run-r20-commit-{failure}"
        sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
        _write_seal(state_dir, run_id, [sel])
        settings = _settings(state_dir)
        ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
        ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))
        allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))

        if failure == "invalid_json":
            client: RecordingClient = RecordingClient()
            original = b'{"broken":'
            client.objects["v1/backup/index.json"] = original
        else:
            client = _IndexUnreadableClient()
            client.objects["v1/backup/index.json"] = b'{"unreadable": true}'
            original = client.objects["v1/backup/index.json"]

        result = asyncio.run(
            ledger.flush(
                settings,  # type: ignore[arg-type]
                client,
                force=True,
                allowed_remote_paths=allowed,
                expected_artifacts=expected,
            )
        )
        assert result["status"] == "pending_commit", result
        assert "refusing to overwrite" in (result["error"] or "")
        assert client.objects["v1/backup/index.json"] == original
        assert "v1/backup/index.json" not in client.puts
        assert not any(path.startswith("v1/backup/batches/") for path in client.puts)
        with ledger._get_connection() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM backup_pending WHERE status = 'pending'").fetchone()[0] > 0
            )


def test_commit_merge_preserves_remote_only_items(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-r20-merge"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))

    remote_only = {
        "remote_path": "v1/objects/sha256/ab/" + "a" * 64,
        "sha256": "a" * 64,
        "size_bytes": 7,
        "document_id": "previous-generation",
    }
    client = RecordingClient()
    client.objects["v1/backup/index.json"] = _r20_index([], extra=remote_only)

    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["status"] == "succeeded", result
    assert result["backup_scope_complete"] is True
    published = json.loads(client.objects["v1/backup/index.json"].decode("utf-8"))
    paths = {item["remote_path"]: item["sha256"] for item in published["items"]}
    assert paths[remote_only["remote_path"]] == remote_only["sha256"]
    for rel, (sha, _size) in expected.items():
        assert paths[rel] == sha


def test_no_pending_repair_merges_large_index_and_keeps_remote_only_items(tmp_path: Path) -> None:
    from cardrag_worker.backup import BACKUP_INDEX_MAX_BYTES

    ctx = _r20_scoped_receipts(tmp_path, "run-r20-repair")
    ledger, settings = ctx["ledger"], ctx["settings"]
    expected = ctx["expected"]
    missing_rel = ocr_ready_path(ctx["sel"]["manifest"].reuse_key, kind="native").as_posix()
    items = [
        {"remote_path": rel, "sha256": sha, "size_bytes": size}
        for rel, (sha, size) in expected.items()
        if rel != missing_rel
    ]
    remote_only = {
        "remote_path": "v1/objects/sha256/cd/" + "c" * 64,
        "sha256": "c" * 64,
        "size_bytes": 3,
        "document_id": "other-generation",
    }
    index = _r20_index(items, padding=8_500_000, extra=remote_only)
    assert len(index) > 8 * 1024 * 1024
    client = _LimitedIndexClient()
    client.objects["v1/backup/index.json"] = index

    result = asyncio.run(
        ledger.flush(settings, client, force=True)  # type: ignore[arg-type]
    )
    assert result["status"] == "succeeded", result
    assert client.index_max_bytes and client.index_max_bytes[0] == BACKUP_INDEX_MAX_BYTES
    published = json.loads(client.objects["v1/backup/index.json"].decode("utf-8"))
    paths = {item["remote_path"]: item["sha256"] for item in published["items"]}
    assert paths[missing_rel] == expected[missing_rel][0]
    assert paths[remote_only["remote_path"]] == remote_only["sha256"]
    with ledger._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backup_pending").fetchone()[0] == 0


def test_restore_discovers_receipts_from_growing_index(tmp_path: Path) -> None:
    from cardrag_worker.backup import BACKUP_INDEX_MAX_BYTES

    body = b"restored payload"
    sha = hashlib.sha256(body).hexdigest()
    remote_path = object_path(sha).as_posix()
    item = {"remote_path": remote_path, "sha256": sha, "size_bytes": len(body)}
    index = _r20_index([item], padding=8_500_000)
    client = _LimitedIndexClient()
    client.objects["v1/backup/index.json"] = index
    client.objects[remote_path] = body

    state_dir = tmp_path / "fresh-state"
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    settings = _settings(state_dir)
    target = tmp_path / "restored"
    result = asyncio.run(ledger.restore(settings, target, webdav_client=client))  # type: ignore[arg-type]
    assert result["status"] == "succeeded", result
    assert result["restored_count"] == 1
    assert (target / remote_path).read_bytes() == body
    assert client.index_max_bytes and client.index_max_bytes[0] == BACKUP_INDEX_MAX_BYTES


# ---------------------------------------------------------------------------
# 019 FIX_21: empty-body index is unreadable; restore index failures surface
# ---------------------------------------------------------------------------


def test_index_reader_distinguishes_http200_empty_from_404(tmp_path: Path) -> None:
    from pathlib import PurePosixPath
    from types import SimpleNamespace as _Namespace

    from cardrag_core.webdav import WebDAVHTTPError

    from cardrag_worker.backup import BACKUP_INDEX_MAX_BYTES, BackupIndexRead
    from cardrag_worker.webdav import WebDAVClient

    class _Core:
        def __init__(self, *, body: bytes | None = None, error: BaseException | None = None) -> None:
            self.body = body
            self.error = error
            self.max_bytes: list[int | None] = []

        def get(self, path: str, *, max_bytes: int | None = None) -> Any:
            self.max_bytes.append(max_bytes)
            if self.error is not None:
                raise self.error
            return _Namespace(content=self.body)

    state_dir = tmp_path / "state"
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")

    empty_core = _Core(body=b"")
    empty_read = asyncio.run(ledger._read_backup_index(WebDAVClient(empty_core)))
    assert empty_read == BackupIndexRead("unreadable", None, "invalid_json")
    assert empty_core.max_bytes == [BACKUP_INDEX_MAX_BYTES]

    missing_core = _Core(error=WebDAVHTTPError("GET", PurePosixPath("v1/backup/index.json"), 404))
    assert asyncio.run(ledger._read_backup_index(WebDAVClient(missing_core))) == BackupIndexRead("absent")
    assert missing_core.max_bytes == [BACKUP_INDEX_MAX_BYTES]

    valid_core = _Core(body=_r20_index([]))
    present = asyncio.run(ledger._read_backup_index(WebDAVClient(valid_core)))
    assert present.state == "present" and present.payload is not None
    assert present.payload["items"] == []


def test_empty_body_index_is_not_treated_as_absent_in_flush(tmp_path: Path) -> None:
    # 1. No pending: completion must stay unverified, never true.
    ctx = _r20_scoped_receipts(tmp_path / "a", "run-r21-empty")
    client = RecordingClient()
    client.objects["v1/backup/index.json"] = b""
    result = asyncio.run(
        ctx["ledger"].flush(
            ctx["settings"],  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=ctx["allowed"],
            expected_artifacts=ctx["expected"],
        )
    )
    assert result["status"] == "unchanged"
    assert result["backup_scope_complete"] is None
    assert result["index_read_error"] == "invalid_json"
    assert client.objects["v1/backup/index.json"] == b""

    # 2. With pending: commit refuses to overwrite and keeps the rows.
    state_dir = tmp_path / "b" / "state"
    run_id = "run-r21-empty-commit"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    commit_client = RecordingClient()
    commit_client.objects["v1/backup/index.json"] = b""
    commit = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            commit_client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert commit["status"] == "pending_commit"
    assert commit["index_read_error"] == "invalid_json"
    assert "refusing to overwrite" in (commit["error"] or "")
    assert commit_client.objects["v1/backup/index.json"] == b""
    assert "v1/backup/index.json" not in commit_client.puts
    assert not any(path.startswith("v1/backup/batches/") for path in commit_client.puts)
    with ledger._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backup_pending WHERE status = 'pending'").fetchone()[0] > 0


def test_absent_index_still_created_on_first_commit(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    run_id = "run-r21-create"
    sel = _make_document(state_dir, run_id, document_id="doc-woori-1", issuer="woori")
    _write_seal(state_dir, run_id, [sel])
    settings = _settings(state_dir)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("woori",))
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("woori",))
    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            settings,  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    assert result["status"] == "succeeded", result
    assert result["backup_scope_complete"] is True
    assert "v1/backup/index.json" in client.objects


@pytest.mark.parametrize(
    ("failure", "expected_kind"),
    [
        ("http_403", "http_403"),
        ("transport", "transport"),
        ("size_limit", "size_limit"),
        ("empty_body", "invalid_json"),
        ("invalid_json", "invalid_json"),
        ("client_contract", "client_contract"),
    ],
)
def test_restore_index_read_failure_is_reported_not_empty(
    failure: str, expected_kind: str, tmp_path: Path
) -> None:
    from pathlib import PurePosixPath

    from cardrag_core.webdav import WebDAVHTTPError, WebDAVIntegrityError

    class _FailingClient(RecordingClient):
        async def get_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes | None:
            if path != "v1/backup/index.json":
                return await super().get(path)
            if failure == "http_403":
                raise WebDAVHTTPError("GET", PurePosixPath(path), 403)
            if failure == "transport":
                raise RuntimeError("connection reset")
            if failure == "size_limit":
                raise WebDAVIntegrityError("exceeds")
            if failure == "empty_body":
                return b""
            if failure == "invalid_json":
                return b"{not json"
            return 42  # type: ignore[return-value]

    state_dir = tmp_path / "state"
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    settings = _settings(state_dir)
    target = tmp_path / "restored"
    result = asyncio.run(ledger.restore(settings, target, webdav_client=_FailingClient()))  # type: ignore[arg-type]
    assert result["status"] == "failed", result
    assert result["index_read_error"] == expected_kind
    assert result["restored_count"] == 0
    assert not target.exists() or not any(target.iterdir())
    with ledger._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backup_receipts").fetchone()[0] == 0

    # The failure must release the shared cache-writer lock for the next retry.
    retry_client = RecordingClient()  # no index object → HTTP404/absent
    retry = asyncio.run(ledger.restore(settings, target, webdav_client=retry_client))  # type: ignore[arg-type]
    assert retry["status"] == "empty"


def test_restore_owned_client_closed_and_cli_nonzero_on_index_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import PurePosixPath

    from cardrag_core.webdav import WebDAVHTTPError
    from typer.testing import CliRunner

    from cardrag_worker import backup as backup_module
    from cardrag_worker.cli import app

    class _FailingOwned(RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        async def get_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes | None:
            if path == "v1/backup/index.json":
                raise WebDAVHTTPError("GET", PurePosixPath(path), 403)
            return await super().get(path)

        async def close(self) -> None:
            self.closed = True

    client = _FailingOwned()
    state_dir = tmp_path / "state"
    settings = _cli_flush_env(tmp_path, monkeypatch, state_dir, client)
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    result = asyncio.run(ledger.restore(settings, tmp_path / "owned-target"))  # type: ignore[arg-type]
    assert result["status"] == "failed"
    assert result["index_read_error"] == "http_403"
    assert client.closed is True

    res = CliRunner().invoke(app, ["backup", "restore", "--target-dir", str(tmp_path / "cli-target")])
    assert res.exit_code == 1, res.output
    payload = json.loads(res.stdout)
    assert payload["status"] == "failed"
    assert payload["index_read_error"] == "http_403"
    assert client.closed is True

    # 404 is a normal empty backup and must exit 0.
    ok_client = RecordingClient()
    monkeypatch.setattr(backup_module.WebDAVClient, "from_env", staticmethod(lambda **kw: ok_client))
    res_ok = CliRunner().invoke(app, ["backup", "restore", "--target-dir", str(tmp_path / "ok-target")])
    assert res_ok.exit_code == 0, res_ok.output
    assert json.loads(res_ok.stdout)["status"] == "empty"
