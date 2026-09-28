"""Fail-closed OCR restoration/seed from WebDAV generation manifest into an empty worker state.

Enables full recovery of existing OCR results without re-executing OCR or calling
external OCR providers, even in a total host loss scenario.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from cardrag_core import (
    STABLE_POINTER_PATH,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    canonical_json_bytes,
    generation_manifest_path,
    generation_ready_path,
    validate_identifier,
    verify_ocr_bytes,
)

from .providers import reject_credential_bearing_ocr
from .webdav import WebDAVClient

LOGGER = logging.getLogger(__name__)

OCR_RECOVERY_LEDGER_SCHEMA = "cardrag.ocr-recovery-ledger.v1"
OCR_RECOVERY_REPORT_SCHEMA = "cardrag.ocr-recovery-report.v1"
_LEDGER_DIRECTORY = Path("audit-reports/state-seed")
_OCR_SEED_DIRECTORY = Path("ocr-seed")


class OCRRecoveryError(RuntimeError):
    """Fail-closed OCR recovery error."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class OCRRecoveryResult:
    generation_id: str
    channel: str
    total_documents: int
    total_ocr_documents: int
    unique_ocr_cas_objects: int
    unbound_cache_documents: int
    imported_ocr_files: int
    reused_ocr_files: int
    total_bytes_transferred: int
    ledger_path: str | None
    ledger_sha256: str | None
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OCR_RECOVERY_REPORT_SCHEMA,
            "generation_id": self.generation_id,
            "channel": self.channel,
            "total_documents": self.total_documents,
            "total_ocr_documents": self.total_ocr_documents,
            "unique_ocr_cas_objects": self.unique_ocr_cas_objects,
            "unbound_cache_documents": self.unbound_cache_documents,
            "imported_ocr_files": self.imported_ocr_files,
            "reused_ocr_files": self.reused_ocr_files,
            "total_bytes_transferred": self.total_bytes_transferred,
            "ledger_path": self.ledger_path,
            "ledger_sha256": self.ledger_sha256,
            "status": "verified" if self.dry_run else "applied",
            "dry_run": self.dry_run,
        }


def _atomic_write_file(target: Path, data: bytes, *, mode: int = 0o600) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".tmp_{os.getpid()}_{target.name}"
    try:
        tmp_path.write_bytes(data)
        os.chmod(tmp_path, mode)
        tmp_path.replace(target)
    finally:
        if tmp_path.exists():
            with suppress_error():
                tmp_path.unlink()


class suppress_error:
    def __enter__(self) -> None:
        pass

    def __exit__(self, *args: Any) -> bool:
        return True


async def restore_ocr_seed_from_generation(
    *,
    webdav: WebDAVClient,
    destination: Path,
    generation_id: str | None = None,
    pointer_path: PurePosixPath = STABLE_POINTER_PATH,
    dry_run: bool = False,
    concurrency: int = 16,
) -> OCRRecoveryResult:
    """Download and verify OCR CAS objects from WebDAV into destination ocr-seed/ and commit ledger."""
    destination = Path(os.path.abspath(os.fspath(destination)))
    if concurrency < 1:
        raise ValueError("concurrency must be positive")

    # 1. Resolve and strictly bind generation control files (pointer -> READY -> manifest)
    pointer_body = await webdav.get_bytes(pointer_path)
    if pointer_body is None:
        raise OCRRecoveryError("pointer_missing", f"Pointer not found at {pointer_path}")
    try:
        pointer = GenerationPointer.model_validate_json(pointer_body)
    except Exception as exc:
        raise OCRRecoveryError("pointer_invalid", str(exc)) from exc
    if pointer.canonical_bytes() != pointer_body:
        raise OCRRecoveryError("pointer_not_canonical", f"Pointer JSON is not canonical at {pointer_path}")

    target_gen_id = generation_id or pointer.generation_id
    try:
        validate_identifier(target_gen_id, label="generation_id")
    except ValueError as exc:
        raise OCRRecoveryError("generation_id_invalid", f"Unsafe generation ID {target_gen_id}") from exc

    manifest_rel_path = generation_manifest_path(target_gen_id)
    manifest_bytes = await webdav.get_bytes(manifest_rel_path)
    if manifest_bytes is None:
        raise OCRRecoveryError("manifest_missing", f"Manifest not found for generation {target_gen_id}")
    try:
        manifest = GenerationManifest.model_validate_json(manifest_bytes)
    except Exception as exc:
        raise OCRRecoveryError("manifest_invalid", str(exc)) from exc
    if manifest.canonical_bytes() != manifest_bytes:
        raise OCRRecoveryError("manifest_not_canonical", f"Manifest JSON is not canonical for generation {target_gen_id}")
    if manifest.generation_id != target_gen_id:
        raise OCRRecoveryError(
            "generation_id_mismatch",
            f"Manifest generation_id {manifest.generation_id} != requested {target_gen_id}",
        )

    ready_rel_path = generation_ready_path(target_gen_id)
    ready_bytes = await webdav.get_bytes(ready_rel_path)
    if ready_bytes is None:
        raise OCRRecoveryError("ready_missing", f"READY not found for generation {target_gen_id}")
    try:
        ready = GenerationReady.model_validate_json(ready_bytes)
    except Exception as exc:
        raise OCRRecoveryError("ready_invalid", str(exc)) from exc
    if ready.canonical_bytes() != ready_bytes:
        raise OCRRecoveryError("ready_not_canonical", f"READY JSON is not canonical for generation {target_gen_id}")
    if ready.generation_id != target_gen_id:
        raise OCRRecoveryError(
            "generation_id_mismatch",
            f"READY generation_id {ready.generation_id} != requested {target_gen_id}",
        )
    if hashlib.sha256(manifest_bytes).hexdigest() != ready.manifest_sha256:
        raise OCRRecoveryError("manifest_ready_sha_mismatch", "Manifest SHA-256 does not match READY")

    if target_gen_id == pointer.generation_id:
        if pointer.manifest_sha256 != ready.manifest_sha256:
            raise OCRRecoveryError("pointer_manifest_sha_mismatch", "Pointer manifest SHA-256 does not match READY")
        if pointer.ready_sha256 != hashlib.sha256(ready_bytes).hexdigest():
            raise OCRRecoveryError("pointer_ready_sha_mismatch", "Pointer ready SHA-256 does not match READY bytes")

    # 2. Collect OCR documents
    ocr_docs = []
    # cas_sha -> set of documents referencing it
    cas_to_docs: dict[str, list[Any]] = defaultdict(list)
    unbound_cache_count = 0

    for doc in manifest.documents:
        if doc.ocr is None:
            continue
        ocr_docs.append(doc)
        cas_to_docs[doc.ocr.sha256].append(doc)
        if doc.ocr_cache_kind is None:
            unbound_cache_count += 1

    total_ocr_docs = len(ocr_docs)
    unique_cas = len(cas_to_docs)
    LOGGER.info(
        "Recovering OCR seed for generation %s: %d total docs, %d OCR docs, %d unique OCR CAS, %d unbound cache docs (unverified provenance)",
        target_gen_id,
        len(manifest.documents),
        total_ocr_docs,
        unique_cas,
        unbound_cache_count,
    )

    ocr_seed_root = destination / _OCR_SEED_DIRECTORY
    download_queue: list[tuple[str, str, int]] = []  # (cas_sha, cas_path, size_bytes)
    reused_count = 0
    cas_payloads: dict[str, bytes] = {}

    # Check already present local files to avoid redundant downloads
    for cas_sha, docs in cas_to_docs.items():
        sample_doc = docs[0]
        cas_path = sample_doc.ocr.path
        size_bytes = sample_doc.ocr.size_bytes
        # If all referencing documents already have a valid local ocr.md, we can reuse
        all_reused = True
        sample_bytes: bytes | None = None
        for d in docs:
            local_file = ocr_seed_root / d.document_id / "ocr.md"
            if local_file.is_file() and not local_file.is_symlink() and local_file.stat().st_size == size_bytes:
                content = local_file.read_bytes()
                if hashlib.sha256(content).hexdigest() == cas_sha:
                    sample_bytes = content
                    continue
            all_reused = False
            break

        if all_reused and sample_bytes is not None:
            reused_count += len(docs)
            cas_payloads[cas_sha] = sample_bytes
        else:
            download_queue.append((cas_sha, cas_path, size_bytes))

    # 3. Stream download missing CAS objects with concurrency limit
    total_bytes_transferred = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def _fetch_cas(cas_sha: str, path: str, expected_size: int) -> tuple[str, bytes]:
        nonlocal total_bytes_transferred
        async with semaphore:
            body = await webdav.get_bytes(PurePosixPath(path))
            if body is None:
                raise OCRRecoveryError(
                    "ocr_cas_missing",
                    f"OCR CAS object missing at {path} (sha256={cas_sha})",
                )
            if len(body) != expected_size:
                raise OCRRecoveryError(
                    "ocr_cas_size_mismatch",
                    f"OCR CAS size mismatch for {path}: expected {expected_size}, got {len(body)}",
                )
            actual_sha = hashlib.sha256(body).hexdigest()
            if actual_sha != cas_sha:
                raise OCRRecoveryError(
                    "ocr_cas_hash_mismatch",
                    f"OCR CAS hash mismatch for {path}: expected {cas_sha}, got {actual_sha}",
                )
            reject_credential_bearing_ocr(body)
            total_bytes_transferred += len(body)
            return cas_sha, body

    if download_queue:
        LOGGER.info("Streaming %d unique OCR CAS objects from WebDAV...", len(download_queue))
        tasks = [_fetch_cas(sha, p, sz) for sha, p, sz in download_queue]
        results = await asyncio.gather(*tasks)
        for sha, body in results:
            cas_payloads[sha] = body

    imported_count = 0
    # 4. Materialize ocr.md for each document and verify format
    ledger_entries = []
    for doc in ocr_docs:
        assert doc.ocr is not None
        body = cas_payloads[doc.ocr.sha256]
        # Verify markdown page format
        try:
            verify_ocr_bytes(
                body,
                expected_page_count=doc.page_count,
                expected_sha256=doc.ocr.sha256,
                expected_size_bytes=doc.ocr.size_bytes,
            )
        except Exception as exc:
            raise OCRRecoveryError(
                "ocr_verification_failed",
                f"Document {doc.document_id} OCR verification failed: {exc}",
            ) from exc

        if not dry_run:
            doc_dir = ocr_seed_root / doc.document_id
            target_ocr = doc_dir / "ocr.md"
            if not target_ocr.is_file() or target_ocr.read_bytes() != body:
                _atomic_write_file(target_ocr, body, mode=0o600)
                imported_count += 1

        has_remote_cache = doc.ocr_cache_kind is not None
        ledger_entries.append(
            {
                "document_id": doc.document_id,
                "issuer": doc.issuer,
                "source_id": f"source_{doc.pdf.sha256}",
                "pdf_sha256": doc.pdf.sha256,
                "pdf_size_bytes": doc.pdf.size_bytes,
                "page_count": doc.page_count,
                "ocr_sha256": doc.ocr.sha256,
                "ocr_size_bytes": doc.ocr.size_bytes,
                "kind": doc.ocr_cache_kind or "native",
                "reuse_key": doc.ocr_reuse_key or doc.pdf.sha256,
                "model": "restored-native" if has_remote_cache else "unverified",
            }
        )

    # 5. Build and commit recovery ledger
    ledger_path_rel: str | None = None
    ledger_sha256: str | None = None

    if not dry_run:
        ledger_data = {
            "schema_version": OCR_RECOVERY_LEDGER_SCHEMA,
            "status": "applied",
            "generation_id": target_gen_id,
            "run_id": f"recovery_{target_gen_id}",
            "corpus_sha256": manifest.corpus_sha256,
            "contract_sha256": manifest.contract_sha256,
            "prior_current_doc_ids": [],
            "prior_historical_doc_ids": [],
            "ocr_documents": ledger_entries,
            "applied_at": datetime.now(UTC).isoformat(),
        }
        ledger_bytes = canonical_json_bytes(ledger_data)
        ledger_sha256 = hashlib.sha256(ledger_bytes).hexdigest()
        ledger_file = destination / _LEDGER_DIRECTORY / f"{ledger_sha256}.json"
        _atomic_write_file(ledger_file, ledger_bytes, mode=0o600)
        ledger_path_rel = str(ledger_file.relative_to(destination))
        LOGGER.info("Committed OCR recovery ledger %s", ledger_path_rel)

    return OCRRecoveryResult(
        generation_id=target_gen_id,
        channel=webdav.channel,
        total_documents=len(manifest.documents),
        total_ocr_documents=total_ocr_docs,
        unique_ocr_cas_objects=unique_cas,
        unbound_cache_documents=unbound_cache_count,
        imported_ocr_files=imported_count,
        reused_ocr_files=reused_count,
        total_bytes_transferred=total_bytes_transferred,
        ledger_path=ledger_path_rel,
        ledger_sha256=ledger_sha256,
        dry_run=dry_run,
    )
