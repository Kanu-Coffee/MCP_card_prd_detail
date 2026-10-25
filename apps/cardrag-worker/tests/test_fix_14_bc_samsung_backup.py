"""Tests for FIX_14: BC sealed OCR reuse, Samsung backup scope policy, and recovery criteria."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cardrag_core import (
    ArtifactRef,
    NativeOCRContract,
    OCRArtifactManifest,
    OCRReady,
    object_path,
    ocr_manifest_path,
    ocr_ready_path,
)
from cardrag_core.ocr import (
    OCRInput,
    content_addressed_ocr_reuse_key,
    native_ocr_reuse_key,
)
from typer.testing import CliRunner

from cardrag_worker.backup import BackupLedger
from cardrag_worker.cli import app
from cardrag_worker.ocr import (
    OCRResolver,
    OCRResult,
    PriorLocalNativeSource,
)
from cardrag_worker.state import WorkerState


def _contract() -> NativeOCRContract:
    return NativeOCRContract(
        processor_version="cardrag-worker/test-fix-14",
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
    """Mock WebDAV client recording puts and serving gets."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    async def put(self, path: str, content: bytes) -> None:
        self.puts.append(path)
        self.objects[path] = content

    async def put_bytes(self, path: str, content: bytes, content_type: str | None = None) -> None:
        self.puts.append(path)
        self.objects[path] = content

    async def get_bytes(self, path: str) -> bytes | None:
        return self.objects.get(path)

    async def get(self, path: str) -> bytes | None:
        return self.objects.get(path)

    async def atomic_replace_bytes(self, path: str, content: bytes, content_type: str | None = None) -> None:
        self.puts.append(path)
        self.objects[path] = content

    async def close(self) -> None:
        pass


def _setup_sealed_prior(
    state_dir: Path,
    prior_run_id: str,
    *,
    document_id: str,
    issuer: str,
    cache_kind: str,
    reuse_key: str,
    pdf_bytes: bytes,
    ocr_bytes: bytes,
) -> PriorLocalNativeSource:
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    ocr_sha = hashlib.sha256(ocr_bytes).hexdigest()

    prior_run_dir = state_dir / "runs" / prior_run_id
    doc_dir = prior_run_dir / "documents" / document_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / f"{document_id}.pdf").write_bytes(pdf_bytes)

    # Put OCR in documents/{document_id}/ocr/ocr.md for _lookup_prior_local_sealed_ocr
    ocr_dir = doc_dir / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "ocr.md").write_bytes(ocr_bytes)

    # Put OCR object in runs/{run_id}/objects/{ocr_sha}
    obj_dir = prior_run_dir / "objects"
    obj_dir.mkdir(parents=True, exist_ok=True)
    (obj_dir / ocr_sha).write_bytes(ocr_bytes)

    # Sealed publish.json
    sealed_dir = prior_run_dir / "sealed"
    sealed_dir.mkdir(parents=True, exist_ok=True)
    publish_doc = {
        "schema_version": "cardrag.publish-seal.v1",
        "run_id": prior_run_id,
        "generation_id": f"gen-{prior_run_id}",
        "manifest": {
            "corpus_sha256": "c" * 64,
            "contract_sha256": "k" * 64,
            "documents": [
                {
                    "document_id": document_id,
                    "issuer": issuer,
                    "page_count": 1,
                    "pdf": {
                        "sha256": pdf_sha,
                        "size_bytes": len(pdf_bytes),
                        "media_type": "application/pdf",
                        "path": f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
                    },
                    "ocr": {
                        "sha256": ocr_sha,
                        "size_bytes": len(ocr_bytes),
                        "media_type": "text/markdown; charset=utf-8",
                        "path": f"v1/objects/sha256/{ocr_sha[:2]}/{ocr_sha}",
                    },
                    "ocr_reuse_key": reuse_key,
                    "ocr_cache_kind": cache_kind,
                }
            ],
        },
        "objects": [
            {
                "sha256": pdf_sha,
                "size_bytes": len(pdf_bytes),
                "media_type": "application/pdf",
                "path": str(doc_dir / f"{document_id}.pdf"),
            },
            {
                "sha256": ocr_sha,
                "size_bytes": len(ocr_bytes),
                "media_type": "text/markdown; charset=utf-8",
                "path": str(obj_dir / ocr_sha),
            },
        ],
    }
    (sealed_dir / "publish.json").write_text(json.dumps(publish_doc), encoding="utf-8")

    return PriorLocalNativeSource(
        runs_root=state_dir / "runs",
        run_id=prior_run_id,
        generation_id=f"gen-{prior_run_id}",
        corpus_sha256="c" * 64,
        contract_sha256="k" * 64,
        document_id=document_id,
        pdf_sha256=pdf_sha,
        pdf_size_bytes=len(pdf_bytes),
        page_count=1,
        ocr_sha256=ocr_sha,
        ocr_size_bytes=len(ocr_bytes),
        cache_kind=cache_kind,
        reuse_key=reuse_key,
    )


# -----------------------------------------------------------------------------
# P1: BC sealed OCR variant ID fixes
# -----------------------------------------------------------------------------


def test_lookup_prior_local_sealed_ocr_native_has_none_variant_id(tmp_path: Path) -> None:
    """When cache_kind is 'native', cache_variant_id must be None according to OCRResult contract."""
    state_dir = tmp_path / "state"
    pdf_bytes = b"%PDF-1.4 test bc native"
    ocr_bytes = b"## Page 1\n\nBenefit text for native\n"
    source = OCRInput(
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(), pdf_size_bytes=len(pdf_bytes), page_count=1
    )
    contract = _contract()
    native_key = native_ocr_reuse_key(contract, source)

    prior = _setup_sealed_prior(
        state_dir,
        "run-prior-samsung",
        document_id="doc_prior_samsung_1",
        issuer="samsung",
        cache_kind="native",
        reuse_key=native_key,
        pdf_bytes=pdf_bytes,
        ocr_bytes=ocr_bytes,
    )

    resolver_stub = SimpleNamespace(cache_epoch=0, contract=contract)
    output_dir = tmp_path / "out" / "doc_new_bc_1"
    output_dir.mkdir(parents=True, exist_ok=True)

    result = OCRResolver._lookup_prior_local_sealed_ocr(
        resolver_stub,  # type: ignore[arg-type]
        prior=prior,
        source=source,
        document_id="doc_new_bc_1",
        output_dir=output_dir,
    )

    assert result is not None
    assert isinstance(result, OCRResult)
    assert result.cache_kind == "native"
    assert result.cache_reuse_key == native_key
    assert result.cache_variant_id is None
    assert result.cache_reused is True
    assert result.ocr_sha256 == hashlib.sha256(ocr_bytes).hexdigest()
    assert result.ocr_bytes == ocr_bytes


def test_lookup_prior_local_sealed_ocr_adopted_has_none_variant_id(tmp_path: Path) -> None:
    """When cache_kind is 'adopted', cache_variant_id must be None."""
    state_dir = tmp_path / "state"
    pdf_bytes = b"%PDF-1.4 test adopted"
    ocr_bytes = b"## Page 1\n\nBenefit text for adopted\n"
    source = OCRInput(
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(), pdf_size_bytes=len(pdf_bytes), page_count=1
    )
    contract = _contract()
    adopted_key = "a" * 64

    prior = _setup_sealed_prior(
        state_dir,
        "run-prior-kb",
        document_id="doc_prior_kb_1",
        issuer="kb",
        cache_kind="adopted",
        reuse_key=adopted_key,
        pdf_bytes=pdf_bytes,
        ocr_bytes=ocr_bytes,
    )

    resolver_stub = SimpleNamespace(cache_epoch=0, contract=contract)
    output_dir = tmp_path / "out" / "doc_new_bc_adopted"
    output_dir.mkdir(parents=True, exist_ok=True)

    result = OCRResolver._lookup_prior_local_sealed_ocr(
        resolver_stub,  # type: ignore[arg-type]
        prior=prior,
        source=source,
        document_id="doc_new_bc_adopted",
        output_dir=output_dir,
    )

    assert result is not None
    assert result.cache_kind == "adopted"
    assert result.cache_reuse_key == adopted_key
    assert result.cache_variant_id is None
    assert result.cache_reused is True


def test_lookup_prior_local_sealed_ocr_content_has_variant_id(tmp_path: Path) -> None:
    """When cache_kind is 'content', cache_variant_id must be a 64-char hex string."""
    state_dir = tmp_path / "state"
    pdf_bytes = b"%PDF-1.4 test content"
    ocr_bytes = b"## Page 1\n\nBenefit text for content\n"
    source = OCRInput(
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(), pdf_size_bytes=len(pdf_bytes), page_count=1
    )
    contract = _contract()
    content_key = content_addressed_ocr_reuse_key(source, cache_epoch=0)

    prior = _setup_sealed_prior(
        state_dir,
        "run-prior-content",
        document_id="doc_prior_content_1",
        issuer="woori",
        cache_kind="content",
        reuse_key=content_key,
        pdf_bytes=pdf_bytes,
        ocr_bytes=ocr_bytes,
    )

    resolver_stub = SimpleNamespace(cache_epoch=0, contract=contract)
    output_dir = tmp_path / "out" / "doc_new_content"
    output_dir.mkdir(parents=True, exist_ok=True)

    result = OCRResolver._lookup_prior_local_sealed_ocr(
        resolver_stub,  # type: ignore[arg-type]
        prior=prior,
        source=source,
        document_id="doc_new_content",
        output_dir=output_dir,
    )

    assert result is not None
    assert result.cache_kind == "content"
    assert result.cache_reuse_key == content_key
    assert result.cache_variant_id is not None
    assert len(result.cache_variant_id) == 64
    assert result.cache_reused is True


@pytest.mark.asyncio
async def test_resolver_prior_native_without_manifest_reused_without_provider(tmp_path: Path) -> None:
    """BC repro condition: prior native sealed OCR exists without native manifest;

    resolver must succeed via _lookup_prior_local_sealed_ocr fallback without provider call.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    worker_state = WorkerState(state_dir / "worker-state.sqlite3")

    pdf_bytes = b"%PDF-1.4 bc exact pdf"
    ocr_bytes = b"## Page 1\n\nbc exact ocr content\n"
    source = OCRInput(
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(), pdf_size_bytes=len(pdf_bytes), page_count=1
    )
    contract = _contract()
    native_key = native_ocr_reuse_key(contract, source)

    prior = _setup_sealed_prior(
        state_dir,
        "run-samsung-live",
        document_id="doc_samsung_prior_1",
        issuer="samsung",
        cache_kind="native",
        reuse_key=native_key,
        pdf_bytes=pdf_bytes,
        ocr_bytes=ocr_bytes,
    )
    # Ensure there is NO native-manifest.json in the prior doc directory
    # so _materialize_prior_local_native raises OCRValidationError and triggers sealed OCR fallback
    prior_ocr_dir = state_dir / "runs" / "run-samsung-live" / "documents" / "doc_samsung_prior_1" / "ocr"
    assert not (prior_ocr_dir / "native-manifest.json").exists()

    mock_provider = AsyncMock()
    mock_provider.provider = "opencode"
    mock_provider.model = "test-model"
    mock_provider.reasoning_effort = "medium"
    mock_provider.recognize = AsyncMock(
        side_effect=AssertionError("Provider must NOT be called for sealed prior reuse")
    )

    resolver = OCRResolver(
        provider=mock_provider,
        state=worker_state,
        webdav=None,
    )

    run_new = worker_state.start_run(run_id="run-bc-new")
    new_doc_dir = state_dir / "runs" / run_new / "documents" / "doc_bc_new_1"
    new_doc_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = new_doc_dir / "doc_bc_new_1.pdf"
    pdf_path.write_bytes(pdf_bytes)

    result = await resolver.resolve(
        run_id=run_new,
        document_id="doc_bc_new_1",
        pdf_path=pdf_path,
        pdf_sha256=source.pdf_sha256,
        pdf_size_bytes=source.pdf_size_bytes,
        page_count=source.page_count,
        output_dir=new_doc_dir / "ocr",
        prior_local_native=prior,
        retained_ocr_identity=(prior.ocr_sha256, prior.ocr_size_bytes),
    )

    assert result.cache_reused is True
    assert result.provider_called is False
    assert result.cache_kind == "native"
    assert result.cache_variant_id is None
    assert result.ocr_sha256 == prior.ocr_sha256
    assert result.ocr_bytes == ocr_bytes
    assert mock_provider.recognize.call_count == 0


# -----------------------------------------------------------------------------
# P2: Samsung backup policy & scope diagnostics tests
# -----------------------------------------------------------------------------


def _create_samsung_like_run(
    state_dir: Path,
    run_id: str,
    *,
    doc_id: str = "doc_samsung_transition_1",
    issuer: str = "samsung",
    pdf_bytes: bytes = b"%PDF-1.4 samsung transition",
    ocr_bytes: bytes = b"## Page 1\n\nsamsung content ocr\n",
    past_native_key: str = "a7745ec9619c6016f7ddffea3423378c9718b74ff0f6c9aa9be2ac90fc55d206",
) -> dict[str, Any]:
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    ocr_sha = hashlib.sha256(ocr_bytes).hexdigest()
    source = OCRInput(pdf_sha256=pdf_sha, pdf_size_bytes=len(pdf_bytes), page_count=1)
    content_key = content_addressed_ocr_reuse_key(source, cache_epoch=0)

    run_dir = state_dir / "runs" / run_id
    doc_dir = run_dir / "documents" / doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = doc_dir / f"{doc_id}.pdf"
    pdf_path.write_bytes(pdf_bytes)

    obj_dir = run_dir / "objects"
    obj_dir.mkdir(parents=True, exist_ok=True)
    ocr_path = obj_dir / ocr_sha
    ocr_path.write_bytes(ocr_bytes)

    doc: dict[str, Any] = {
        "document_id": doc_id,
        "issuer": issuer,
        "page_count": 1,
        "pdf": {
            "sha256": pdf_sha,
            "size_bytes": len(pdf_bytes),
            "media_type": "application/pdf",
            "path": f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
        },
        "ocr": {
            "sha256": ocr_sha,
            "size_bytes": len(ocr_bytes),
            "media_type": "text/markdown; charset=utf-8",
            "path": f"v1/objects/sha256/{ocr_sha[:2]}/{ocr_sha}",
        },
        "ocr_reuse_key": content_key,
        "ocr_cache_kind": "content",
    }

    sealed_dir = run_dir / "sealed"
    sealed_dir.mkdir(parents=True, exist_ok=True)
    publish_doc = {
        "schema_version": "cardrag.publish-seal.v1",
        "run_id": run_id,
        "generation_id": f"gen-{run_id}",
        "manifest": {
            "corpus_sha256": "c" * 64,
            "contract_sha256": "k" * 64,
            "documents": [doc],
        },
        "objects": [
            {
                "sha256": pdf_sha,
                "size_bytes": len(pdf_bytes),
                "media_type": "application/pdf",
                "path": str(pdf_path),
            },
            {
                "sha256": ocr_sha,
                "size_bytes": len(ocr_bytes),
                "media_type": "text/markdown; charset=utf-8",
                "path": str(ocr_path),
            },
        ],
    }
    (sealed_dir / "publish.json").write_text(json.dumps(publish_doc), encoding="utf-8")
    (run_dir / "issuer-scope.json").write_text(json.dumps({"selected": [issuer]}), encoding="utf-8")

    # Setup ledger with old native doc_map / doc_info from past run
    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    past_man_rel = ocr_manifest_path(past_native_key, kind="native").as_posix()
    past_ready_rel = ocr_ready_path(past_native_key, kind="native").as_posix()

    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{past_man_rel}", doc_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{past_ready_rel}", doc_id),
        )
        info_json = json.dumps(
            {
                "document_id": doc_id,
                "reuse_key": past_native_key,
                "pdf_sha256": pdf_sha,
                "ocr_sha256": ocr_sha,
                "kind": "native",
            }
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_info_{past_man_rel}", info_json),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_info_{past_ready_rel}", info_json),
        )

    return {
        "state_dir": state_dir,
        "run_id": run_id,
        "doc": doc,
        "doc_id": doc_id,
        "pdf_bytes": pdf_bytes,
        "pdf_sha": pdf_sha,
        "ocr_bytes": ocr_bytes,
        "ocr_sha": ocr_sha,
        "content_key": content_key,
        "past_native_key": past_native_key,
        "past_man_rel": past_man_rel,
        "past_ready_rel": past_ready_rel,
        "ledger": ledger,
    }


def test_samsung_native_to_content_transition_excludes_old_doc_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Samsung case: document transitioned from native to content.

    Past native doc_map paths must be excluded under shared cache policy;
    in_scope_unbound must be 0, backup_excluded_shared_cache must be 2,
    and flush must succeed with backup_scope_complete=True.
    """
    state_dir = tmp_path / "state"
    run_id = "run-samsung-r14"
    ctx = _create_samsung_like_run(state_dir, run_id)
    ledger = ctx["ledger"]
    settings = _settings(state_dir)

    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("samsung",))

    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("samsung",))

    # Past native manifest/READY must NOT be in allowed scope
    assert ctx["past_man_rel"] not in allowed
    assert ctx["past_ready_rel"] not in allowed

    # Only the document's PDF and OCR CAS objects are expected
    expected_paths = {
        object_path(ctx["pdf_sha"]).as_posix(),
        object_path(ctx["ocr_sha"]).as_posix(),
    }
    assert allowed == expected_paths
    assert set(expected.keys()) == expected_paths

    # Check read-only diagnostics
    diag = ledger.scoped_pending_diagnostics(settings, run_id, ("samsung",))
    assert diag["backup_in_scope_unbound"] == 0
    assert diag["backup_excluded_shared_cache"] == 2
    assert diag["backup_in_scope_conflicts"] == 0
    assert diag["backup_in_scope_remaining"] == 2
    assert diag["backup_in_scope_missing"] == 0

    # Flush with client
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
    assert result["backup_scope_complete"] is True
    assert result["in_scope_unbound"] == 0
    assert result["excluded_shared_cache"] == 2

    # CLI status output check
    from cardrag_worker import cli as cli_module

    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", staticmethod(lambda **kw: settings))
    res = CliRunner().invoke(app, ["backup", "status", "--run", run_id, "--issuer", "samsung"])
    assert res.exit_code == 0
    status_out = json.loads(res.stdout)
    assert status_out["backup_in_scope_unbound"] == 0
    assert status_out["backup_excluded_shared_cache"] == 2


def test_samsung_content_run_with_verified_native_bundle_preserves_bundle(tmp_path: Path) -> None:
    """If a content document also generated a verified native bundle in this run,

    that bundle must be protected and included in expected artifacts.
    """
    state_dir = tmp_path / "state"
    run_id = "run-content-with-native-bundle"
    ctx = _create_samsung_like_run(state_dir, run_id)
    ledger = ctx["ledger"]
    settings = _settings(state_dir)
    contract = _contract()
    source = OCRInput(
        pdf_sha256=ctx["pdf_sha"],
        pdf_size_bytes=len(ctx["pdf_bytes"]),
        page_count=1,
    )
    reuse_key = native_ocr_reuse_key(contract, source)

    # Materialize a verified native manifest in runs/{run_id}/documents/{doc_id}/ocr/
    doc_ocr_dir = state_dir / "runs" / run_id / "documents" / ctx["doc_id"] / "ocr"
    doc_ocr_dir.mkdir(parents=True, exist_ok=True)
    manifest = OCRArtifactManifest(
        reuse_key=reuse_key,
        source=source,
        contract=contract,
        output=ArtifactRef(
            media_type="text/markdown; charset=utf-8",
            path=f"v1/objects/sha256/{ctx['ocr_sha'][:2]}/{ctx['ocr_sha']}",
            sha256=ctx["ocr_sha"],
            size_bytes=len(ctx["ocr_bytes"]),
        ),
        ocr_chars=10,
        page_output_sha256=(hashlib.sha256(ctx["ocr_bytes"]).hexdigest(),),
        created_at=datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC),
    )
    (doc_ocr_dir / "native-manifest.json").write_bytes(manifest.canonical_bytes())
    (doc_ocr_dir / "ocr.md").write_bytes(ctx["ocr_bytes"])
    ready = OCRReady(
        reuse_key=reuse_key,
        manifest_sha256=hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
        ocr_sha256=ctx["ocr_sha"],
    )
    (doc_ocr_dir / "READY.json").write_bytes(ready.canonical_bytes())

    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("samsung",))
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("samsung",))

    bundle_man = ocr_manifest_path(reuse_key, kind="native").as_posix()
    bundle_ready = ocr_ready_path(reuse_key, kind="native").as_posix()

    # Because a verified native bundle exists, its paths MUST be in expected!
    assert bundle_man in expected
    assert bundle_ready in expected
    assert bundle_man in allowed
    assert bundle_ready in allowed
    assert len(allowed) == 4
    assert len(expected) == 4
    assert len(allowed - set(expected)) == 0
    assert ledger._last_excluded_shared_cache == 2

    # Flush should complete successfully
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
    assert result["backup_scope_complete"] is True
    assert result["in_scope_unbound"] == 0
    assert result["excluded_shared_cache"] == 2


def test_samsung_content_run_with_lost_native_bundle_blocks_completion(tmp_path: Path) -> None:
    """If a content document had an owned bundle registered, but its manifest was lost,

    it must NOT be pushed out of scope as 'shared cache'. It must remain in paths as
    UNBOUND, blocking completion (backup_scope_complete=False).
    """
    state_dir = tmp_path / "state"
    run_id = "run-content-lost-native-bundle"
    ctx = _create_samsung_like_run(state_dir, run_id)
    ledger = ctx["ledger"]
    settings = _settings(state_dir)
    contract = _contract()
    source = OCRInput(
        pdf_sha256=ctx["pdf_sha"],
        pdf_size_bytes=len(ctx["pdf_bytes"]),
        page_count=1,
    )
    reuse_key = native_ocr_reuse_key(contract, source)

    doc_ocr_dir = state_dir / "runs" / run_id / "documents" / ctx["doc_id"] / "ocr"
    doc_ocr_dir.mkdir(parents=True, exist_ok=True)
    manifest = OCRArtifactManifest(
        reuse_key=reuse_key,
        source=source,
        contract=contract,
        output=ArtifactRef(
            media_type="text/markdown; charset=utf-8",
            path=f"v1/objects/sha256/{ctx['ocr_sha'][:2]}/{ctx['ocr_sha']}",
            sha256=ctx["ocr_sha"],
            size_bytes=len(ctx["ocr_bytes"]),
        ),
        ocr_chars=10,
        page_output_sha256=(hashlib.sha256(ctx["ocr_bytes"]).hexdigest(),),
        created_at=datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC),
    )
    (doc_ocr_dir / "native-manifest.json").write_bytes(manifest.canonical_bytes())
    (doc_ocr_dir / "ocr.md").write_bytes(ctx["ocr_bytes"])
    ready = OCRReady(
        reuse_key=reuse_key,
        manifest_sha256=hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
        ocr_sha256=ctx["ocr_sha"],
    )
    (doc_ocr_dir / "READY.json").write_bytes(ready.canonical_bytes())

    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("samsung",))

    # Now simulate loss of the owned manifest
    (doc_ocr_dir / "native-manifest.json").unlink()

    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("samsung",))
    unbound = sorted(allowed - set(expected))

    bundle_man = ocr_manifest_path(reuse_key, kind="native").as_posix()
    bundle_ready = ocr_ready_path(reuse_key, kind="native").as_posix()

    # The lost owned bundle must remain in scope as UNBOUND
    assert bundle_man in allowed
    assert bundle_ready in allowed
    assert bundle_man not in expected
    assert bundle_ready not in expected
    assert set(unbound) == {bundle_man, bundle_ready}
    assert ledger._last_excluded_shared_cache == 2

    # Flush must fail closed: backup_scope_complete=False
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
    assert result["backup_scope_complete"] is False
    assert result["in_scope_unbound"] == 2
    assert result["in_scope_remaining"] == 2
    assert result["pending_out_of_scope"] == 0
    assert result["excluded_shared_cache"] == 2


def test_samsung_content_run_rebinds_from_local_cache(tmp_path: Path) -> None:
    """When run directory manifest is lost, but the local OCR cache has the valid

    cryptographically verified pair matching the document identity, Phase 1 rebinds it.
    """
    state_dir = tmp_path / "state"
    run_id = "run-content-cache-rebind"
    ctx = _create_samsung_like_run(state_dir, run_id)
    ledger = ctx["ledger"]
    settings = _settings(state_dir)
    contract = _contract()
    source = OCRInput(
        pdf_sha256=ctx["pdf_sha"],
        pdf_size_bytes=len(ctx["pdf_bytes"]),
        page_count=1,
    )
    reuse_key = native_ocr_reuse_key(contract, source)

    doc_ocr_dir = state_dir / "runs" / run_id / "documents" / ctx["doc_id"] / "ocr"
    doc_ocr_dir.mkdir(parents=True, exist_ok=True)
    manifest = OCRArtifactManifest(
        reuse_key=reuse_key,
        source=source,
        contract=contract,
        output=ArtifactRef(
            media_type="text/markdown; charset=utf-8",
            path=f"v1/objects/sha256/{ctx['ocr_sha'][:2]}/{ctx['ocr_sha']}",
            sha256=ctx["ocr_sha"],
            size_bytes=len(ctx["ocr_bytes"]),
        ),
        ocr_chars=10,
        page_output_sha256=(hashlib.sha256(ctx["ocr_bytes"]).hexdigest(),),
        created_at=datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC),
    )
    (doc_ocr_dir / "native-manifest.json").write_bytes(manifest.canonical_bytes())
    (doc_ocr_dir / "ocr.md").write_bytes(ctx["ocr_bytes"])
    ready = OCRReady(
        reuse_key=reuse_key,
        manifest_sha256=hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
        ocr_sha256=ctx["ocr_sha"],
    )
    (doc_ocr_dir / "READY.json").write_bytes(ready.canonical_bytes())

    # Pre-populate local cache at cache/ocr/{reuse_key}/
    cache_dir = state_dir / "cache" / "ocr" / reuse_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "manifest.json").write_bytes(manifest.canonical_bytes())
    (cache_dir / "READY.json").write_bytes(ready.canonical_bytes())

    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("samsung",))

    # Unlink run directory manifest to trigger Phase 1 cache rebind
    (doc_ocr_dir / "native-manifest.json").unlink()

    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("samsung",))
    bundle_man = ocr_manifest_path(reuse_key, kind="native").as_posix()
    bundle_ready = ocr_ready_path(reuse_key, kind="native").as_posix()

    assert bundle_man in expected
    assert bundle_ready in expected
    assert len(allowed) == 4
    assert len(expected) == 4
    assert len(allowed - set(expected)) == 0
    assert ledger._last_excluded_shared_cache == 2


def test_true_native_evidence_loss_remains_unbound_and_blocks_completion(tmp_path: Path) -> None:
    """When a document's sealed contract is native and its cache evidence is missing,

    it must NOT be excluded; it must remain in paths, reported as unbound, blocking completion.
    """
    state_dir = tmp_path / "state"
    run_id = "run-native-loss"
    doc_id = "doc_native_loss_1"
    pdf_bytes = b"%PDF-1.4 native doc loss"
    ocr_bytes = b"## Page 1\n\nnative ocr loss\n"
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    ocr_sha = hashlib.sha256(ocr_bytes).hexdigest()
    source = OCRInput(pdf_sha256=pdf_sha, pdf_size_bytes=len(pdf_bytes), page_count=1)
    contract = _contract()
    native_key = native_ocr_reuse_key(contract, source)

    run_dir = state_dir / "runs" / run_id
    doc_dir = run_dir / "documents" / doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = doc_dir / f"{doc_id}.pdf"
    pdf_path.write_bytes(pdf_bytes)

    obj_dir = run_dir / "objects"
    obj_dir.mkdir(parents=True, exist_ok=True)
    ocr_path = obj_dir / ocr_sha
    ocr_path.write_bytes(ocr_bytes)

    doc: dict[str, Any] = {
        "document_id": doc_id,
        "issuer": "samsung",
        "page_count": 1,
        "pdf": {
            "sha256": pdf_sha,
            "size_bytes": len(pdf_bytes),
            "media_type": "application/pdf",
            "path": f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
        },
        "ocr": {
            "sha256": ocr_sha,
            "size_bytes": len(ocr_bytes),
            "media_type": "text/markdown; charset=utf-8",
            "path": f"v1/objects/sha256/{ocr_sha[:2]}/{ocr_sha}",
        },
        "ocr_reuse_key": native_key,
        "ocr_cache_kind": "native",
    }

    sealed_dir = run_dir / "sealed"
    sealed_dir.mkdir(parents=True, exist_ok=True)
    publish_doc = {
        "schema_version": "cardrag.publish-seal.v1",
        "run_id": run_id,
        "generation_id": f"gen-{run_id}",
        "manifest": {"corpus_sha256": "c" * 64, "contract_sha256": "k" * 64, "documents": [doc]},
        "objects": [
            {
                "sha256": pdf_sha,
                "size_bytes": len(pdf_bytes),
                "media_type": "application/pdf",
                "path": str(pdf_path),
            },
            {
                "sha256": ocr_sha,
                "size_bytes": len(ocr_bytes),
                "media_type": "text/markdown; charset=utf-8",
                "path": str(ocr_path),
            },
        ],
    }
    (sealed_dir / "publish.json").write_text(json.dumps(publish_doc), encoding="utf-8")
    (run_dir / "issuer-scope.json").write_text(json.dumps({"selected": ["samsung"]}), encoding="utf-8")

    ledger = BackupLedger(state_dir / "backup-ledger.sqlite3")
    native_man_rel = ocr_manifest_path(native_key, kind="native").as_posix()
    native_ready_rel = ocr_ready_path(native_key, kind="native").as_posix()

    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{native_man_rel}", doc_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{native_ready_rel}", doc_id),
        )

    ledger.record_run_success(run_id, state_dir, _settings(state_dir), selected_issuers=("samsung",))
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("samsung",))

    # For native cache_kind with lost local evidence, native paths MUST be in allowed (unbound)
    assert native_man_rel in allowed
    assert native_ready_rel in allowed
    assert native_man_rel not in expected
    assert native_ready_rel not in expected

    diag = ledger.scoped_pending_diagnostics(_settings(state_dir), run_id, ("samsung",))
    assert diag["backup_in_scope_unbound"] == 2
    assert diag["backup_scope_complete"] is None  # read-only

    client = RecordingClient()
    result = asyncio.run(
        ledger.flush(
            _settings(state_dir),  # type: ignore[arg-type]
            client,
            force=True,
            allowed_remote_paths=allowed,
            expected_artifacts=expected,
        )
    )
    # Unbound paths block scope completion
    assert result["in_scope_unbound"] == 2
    assert result["backup_scope_complete"] is False


# -----------------------------------------------------------------------------
# Additional regression and integrity rejection tests
# -----------------------------------------------------------------------------


def test_lookup_prior_sealed_ocr_rejects_hash_mismatch(tmp_path: Path) -> None:
    """When the prior OCR body hash does not match prior.ocr_sha256, it must return None."""
    state_dir = tmp_path / "state"
    pdf_bytes = b"%PDF-1.4 test hash mismatch"
    ocr_bytes = b"## Page 1\n\nOriginal text\n"
    source = OCRInput(
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(), pdf_size_bytes=len(pdf_bytes), page_count=1
    )
    contract = _contract()

    prior = _setup_sealed_prior(
        state_dir,
        "run-prior-tamper",
        document_id="doc_prior_tamper",
        issuer="samsung",
        cache_kind="native",
        reuse_key="n" * 64,
        pdf_bytes=pdf_bytes,
        ocr_bytes=ocr_bytes,
    )

    # Tamper with the prior ocr.md file
    doc_ocr_file = (
        state_dir / "runs" / "run-prior-tamper" / "documents" / "doc_prior_tamper" / "ocr" / "ocr.md"
    )
    doc_ocr_file.write_bytes(b"## Page 1\n\nTampered text\n")

    resolver_stub = SimpleNamespace(cache_epoch=0, contract=contract)
    output_dir = tmp_path / "out" / "doc_new"
    output_dir.mkdir(parents=True, exist_ok=True)

    result = OCRResolver._lookup_prior_local_sealed_ocr(
        resolver_stub,  # type: ignore[arg-type]
        prior=prior,
        source=source,
        document_id="doc_new",
        output_dir=output_dir,
    )
    assert result is None


def test_lookup_prior_sealed_ocr_rejects_symlink(tmp_path: Path) -> None:
    """A symlinked ocr.md or manifest in prior generation must be rejected."""
    state_dir = tmp_path / "state"
    pdf_bytes = b"%PDF-1.4 test symlink"
    ocr_bytes = b"## Page 1\n\nOriginal text\n"
    source = OCRInput(
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(), pdf_size_bytes=len(pdf_bytes), page_count=1
    )
    contract = _contract()

    prior = _setup_sealed_prior(
        state_dir,
        "run-prior-symlink",
        document_id="doc_prior_symlink",
        issuer="samsung",
        cache_kind="native",
        reuse_key="n" * 64,
        pdf_bytes=pdf_bytes,
        ocr_bytes=ocr_bytes,
    )

    # Replace ocr.md with a symlink to outside
    doc_ocr_dir = state_dir / "runs" / "run-prior-symlink" / "documents" / "doc_prior_symlink" / "ocr"
    doc_ocr_file = doc_ocr_dir / "ocr.md"
    outside_file = tmp_path / "outside_ocr.md"
    outside_file.write_bytes(ocr_bytes)
    doc_ocr_file.unlink()
    doc_ocr_file.symlink_to(outside_file)

    resolver_stub = SimpleNamespace(cache_epoch=0, contract=contract)
    output_dir = tmp_path / "out" / "doc_new"
    output_dir.mkdir(parents=True, exist_ok=True)

    result = OCRResolver._lookup_prior_local_sealed_ocr(
        resolver_stub,  # type: ignore[arg-type]
        prior=prior,
        source=source,
        document_id="doc_new",
        output_dir=output_dir,
    )
    assert result is None


def test_stale_past_revision_with_doc_info_mismatch_excluded_when_current_bound(tmp_path: Path) -> None:
    """When a doc_info has mismatched pdf_sha256 or ocr_sha256 (stale past revision)

    and the document's current metadata is bound, the stale path is excluded.
    """
    state_dir = tmp_path / "state"
    run_id = "run-stale-rev"
    ctx = _create_samsung_like_run(state_dir, run_id)
    ledger = ctx["ledger"]
    settings = _settings(state_dir)

    # Modify doc_info to simulate a stale revision (different pdf hash)
    stale_man_rel = "v1/ocr-cache/native/99/9999999999999999999999999999999999999999999999999999999999999999/manifest.json"
    stale_info = json.dumps(
        {
            "document_id": ctx["doc_id"],
            "reuse_key": "9" * 64,
            "pdf_sha256": "different_pdf_hash",
            "ocr_sha256": "different_ocr_hash",
            "kind": "native",
        }
    )
    with ledger._get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{stale_man_rel}", ctx["doc_id"]),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_info_{stale_man_rel}", stale_info),
        )

    ledger.record_run_success(run_id, state_dir, settings, selected_issuers=("samsung",))
    allowed, expected = ledger.scoped_backup_scope(run_id, state_dir, ("samsung",))

    # Stale path must NOT be in allowed scope
    assert stale_man_rel not in allowed
    assert stale_man_rel not in expected


@pytest.mark.parametrize("transport_state", ["lost_source", "absent", "receipt_only", "legacy_lost_source"])
def test_owned_content_metadata_survives_transport_state_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport_state: str
) -> None:
    """Registration ownership outlives queues; the real retry CLI must fail closed."""
    from cardrag_worker import backup as backup_module
    from cardrag_worker import cli as cli_module

    test_samsung_content_run_with_lost_native_bundle_blocks_completion(tmp_path)
    state = tmp_path / "state"
    run = "run-content-lost-native-bundle"
    ledger = BackupLedger(state / "backup-ledger.sqlite3")
    settings = _settings(state)
    settings.webdav_base_url = "https://backup.test/root"
    settings.webdav_username = "test"
    settings.webdav_password = "test"  # noqa: S105 -- dummy factory-only fixture credential
    settings.stable_publication_approved = True
    settings.webdav_upload_chunk_mib = 1
    client = RecordingClient()
    with ledger._get_connection() as conn:
        metadata = conn.execute("SELECT * FROM backup_pending WHERE status='pending'").fetchall()
        assert len(metadata) == 2
        for row in metadata:
            Path(row["local_path"]).unlink(missing_ok=True)
            (state / "backup" / "spool" / row["sha256"]).unlink(missing_ok=True)
        if transport_state in ("lost_source", "legacy_lost_source"):
            conn.execute("UPDATE backup_pending SET status='lost_source'")
        else:
            conn.execute("DELETE FROM backup_pending")
        if transport_state == "legacy_lost_source":
            conn.execute("DELETE FROM backup_meta WHERE key LIKE 'ocr_owned_%'")
        if transport_state == "receipt_only":
            for row in metadata:
                conn.execute(
                    "INSERT INTO backup_receipts (remote_path,sha256,size_bytes,remote_root,verified_at) "
                    "VALUES (?,?,?,?,?)",
                    (row["remote_path"], row["sha256"], row["size_bytes"], "", 1),
                )
        conn.execute("UPDATE backup_receipts SET remote_root=?", (settings.webdav_base_url,))
        receipts = conn.execute("SELECT remote_path,sha256 FROM backup_receipts").fetchall()
        client.objects["v1/backup/index.json"] = json.dumps(
            {"items": [dict(row) for row in receipts]}
        ).encode()
    # Fresh instance: no reliance on in-memory bookkeeping.
    allowed, expected = ledger.scoped_backup_scope(run, state, ("samsung",))
    assert len(allowed - set(expected)) == 2
    assert ledger.scoped_backup_scope(run, state, ("woori",)) == (frozenset(), {})
    foreign, foreign_expected = ledger.scoped_backup_scope(run, state, ("samsung",), remote_root="other")
    assert len(foreign - set(foreign_expected)) == 2
    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", staticmethod(lambda **kw: settings))
    monkeypatch.setattr(backup_module.WebDAVClient, "from_env", staticmethod(lambda **kw: client))
    for _ in range(2):
        result = CliRunner().invoke(app, ["backup", "flush", "--run", run, "--issuer", "samsung"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["backup_scope_complete"] is False
        assert payload["in_scope_unbound"] == 2
        if transport_state in ("lost_source", "legacy_lost_source"):
            assert payload["in_scope_lost_source"] == 2


def test_readonly_scope_subclass_and_missing_run_reset(tmp_path: Path) -> None:
    """Match the deployed monitor's constructor, with real read-only SQLite."""
    import sqlite3

    ctx = _create_samsung_like_run(tmp_path / "state", "run-ro")

    class ReadOnlyLedger(BackupLedger):
        def __init__(self, path: Path) -> None:
            self.db_path = path.resolve()

        def _get_connection(self) -> sqlite3.Connection:
            conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn

    ledger = ReadOnlyLedger(ctx["state_dir"] / "backup-ledger.sqlite3")
    before = ledger.db_path.read_bytes()
    diag = ledger.scoped_pending_diagnostics(_settings(ctx["state_dir"]), "run-ro", ("samsung",))
    assert diag["backup_excluded_shared_cache"] == 2
    diag = ledger.scoped_pending_diagnostics(_settings(ctx["state_dir"]), "missing", ("samsung",))
    assert diag["backup_excluded_shared_cache"] == 0
    assert ledger.db_path.read_bytes() == before
