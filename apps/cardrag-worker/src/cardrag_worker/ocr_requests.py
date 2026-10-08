"""Immutable, bounded requests for deliberate OCR reprocessing."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from cardrag_core import GenerationDocument, GenerationManifest, canonical_json_bytes
from cardrag_core.domain import PositiveInt, Sha256Hex, StrictFrozenModel
from cardrag_core.paths import validate_identifier
from pydantic import Field, model_validator


class OCRRequestTarget(StrictFrozenModel):
    document_id: str
    pdf_sha256: Sha256Hex
    pdf_size_bytes: PositiveInt
    page_count: PositiveInt

    @model_validator(mode="after")
    def valid_document_id(self) -> OCRRequestTarget:
        validate_identifier(self.document_id, label="document_id")
        return self


class OCRReprocessRequest(StrictFrozenModel):
    schema_version: str = "cardrag.ocr-reprocess-request.v1"
    request_id: str
    source_generation_id: str
    created_at: datetime
    targets: tuple[OCRRequestTarget, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_request(self) -> OCRReprocessRequest:
        validate_identifier(self.request_id, label="request_id")
        validate_identifier(self.source_generation_id, label="generation_id")
        if self.schema_version != "cardrag.ocr-reprocess-request.v1":
            raise ValueError("unsupported OCR reprocess request schema")
        if self.created_at.tzinfo is None:
            raise ValueError("OCR reprocess request timestamp needs a timezone")
        ids = [target.document_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate OCR reprocess target")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


def request_targets(documents: tuple[GenerationDocument, ...]) -> tuple[OCRRequestTarget, ...]:
    return tuple(
        OCRRequestTarget(
            document_id=doc.document_id,
            pdf_sha256=doc.pdf.sha256,
            pdf_size_bytes=doc.pdf.size_bytes,
            page_count=doc.page_count,
        )
        for doc in sorted(documents, key=lambda item: item.document_id)
        if doc.ocr is not None
    )


def plan_reprocess_requests(
    manifest: GenerationManifest,
    *,
    document_ids: tuple[str, ...] = (),
    pdf_sha256: str | None = None,
    issuer: str | None = None,
    all_documents: bool = False,
    confirm_all: bool = False,
    max_documents: int = 10,
) -> tuple[OCRReprocessRequest, ...]:
    if max_documents < 1 or max_documents > 100:
        raise ValueError("max_documents must be between 1 and 100")
    if not (document_ids or pdf_sha256 or issuer or all_documents):
        raise ValueError("select document IDs, PDF SHA, issuer, or all")
    if all_documents and not confirm_all:
        raise ValueError("all-document OCR reprocess requires confirm_all")
    if pdf_sha256 is not None and (
        len(pdf_sha256) != 64 or any(c not in "0123456789abcdef" for c in pdf_sha256)
    ):
        raise ValueError("PDF SHA-256 is invalid")
    requested_ids = set(document_ids)
    for document_id in requested_ids:
        validate_identifier(document_id, label="document_id")
    available_ids = {doc.document_id for doc in manifest.documents if doc.ocr is not None}
    if requested_ids.difference(available_ids):
        raise ValueError("requested document is not served in the current generation")
    selected = tuple(
        doc
        for doc in manifest.documents
        if doc.ocr is not None
        and (
            all_documents
            or doc.document_id in requested_ids
            or (pdf_sha256 is not None and doc.pdf.sha256 == pdf_sha256)
            or (issuer is not None and doc.issuer == issuer)
        )
    )
    if not selected:
        raise ValueError("OCR reprocess selector matched no served document")
    targets = request_targets(selected)
    created_at = datetime.now(UTC)
    return tuple(
        OCRReprocessRequest(
            request_id=f"ocr-{secrets.token_hex(16)}",
            source_generation_id=manifest.generation_id,
            created_at=created_at,
            targets=targets[offset : offset + max_documents],
        )
        for offset in range(0, len(targets), max_documents)
    )


def _request_root(state_dir: Path) -> Path:
    return state_dir / "ocr-requests"


def queue_reprocess_requests(state_dir: Path, requests: tuple[OCRReprocessRequest, ...]) -> None:
    root = _request_root(state_dir)
    if root.is_symlink():
        raise ValueError("OCR request directory is unsafe")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for request in requests:
        path = root / f"{request.request_id}.json"
        temporary = root / f".{request.request_id}-{secrets.token_hex(8)}.tmp"
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(request.canonical_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)


def load_next_reprocess_request(state_dir: Path) -> OCRReprocessRequest | None:
    root = _request_root(state_dir)
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise ValueError("OCR request directory is unsafe")
    completed = root / "completed"
    for path in sorted(root.glob("ocr-*.json")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("OCR request file is unsafe")
        try:
            request = OCRReprocessRequest.model_validate_json(path.read_bytes())
        except ValueError as exc:
            raise ValueError("OCR request file is invalid") from exc
        if request.canonical_bytes() != path.read_bytes() or path.name != f"{request.request_id}.json":
            raise ValueError("OCR request file identity is invalid")
        receipt = completed / f"{request.request_id}.json"
        if receipt.exists():
            if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 4096:
                raise ValueError("OCR request completion receipt is unsafe")
            receipt_body = receipt.read_bytes()
            try:
                receipt_payload = json.loads(receipt_body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("OCR request completion receipt is invalid") from exc
            if (
                not isinstance(receipt_payload, dict)
                or set(receipt_payload) != {"schema_version", "request_id", "run_id", "generation_id"}
                or receipt_payload["schema_version"] != "cardrag.ocr-reprocess-completion.v1"
                or receipt_payload["request_id"] != request.request_id
                or canonical_json_bytes(receipt_payload) != receipt_body
            ):
                raise ValueError("OCR request completion receipt contract is invalid")
            validate_identifier(receipt_payload["run_id"], label="run_id")
            validate_identifier(receipt_payload["generation_id"], label="generation_id")
            continue
        return request
    return None


def select_run_reprocess_request(state_dir: Path, run_id: str) -> OCRReprocessRequest | None:
    """Keep a resumed run on its original request, including an empty selection."""

    safe_run_id = validate_identifier(run_id, label="run_id")
    path = state_dir / "runs" / safe_run_id / "ocr-reprocess-selection.json"
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("OCR run request selection is unsafe")
        body = path.read_bytes()
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("OCR run request selection is invalid") from exc
        if (
            canonical_json_bytes(payload) != body
            or not isinstance(payload, dict)
            or set(payload) != {"request"}
        ):
            raise ValueError("OCR run request selection contract is invalid")
        request = payload["request"]
        return (
            None
            if request is None
            else OCRReprocessRequest.model_validate_json(canonical_json_bytes(request))
        )

    request = load_next_reprocess_request(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = canonical_json_bytes({"request": request})
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return request


def complete_reprocess_request(
    state_dir: Path, request: OCRReprocessRequest, *, run_id: str, generation_id: str
) -> None:
    validate_identifier(run_id, label="run_id")
    validate_identifier(generation_id, label="generation_id")
    root = _request_root(state_dir) / "completed"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{request.request_id}.json"
    body = canonical_json_bytes(
        {
            "schema_version": "cardrag.ocr-reprocess-completion.v1",
            "request_id": request.request_id,
            "run_id": run_id,
            "generation_id": generation_id,
        }
    )
    try:
        with path.open("xb") as handle:
            os.chmod(path, 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != body:
            raise ValueError("OCR request completion receipt conflicts") from None


def reprocess_success_proof(
    request: OCRReprocessRequest,
    target: OCRRequestTarget,
    *,
    ocr_sha256: str,
    ocr_size_bytes: int,
    reuse_key: str | None,
    variant_id: str | None,
) -> dict[str, object]:
    """Bind the resolver's durable content result to this immutable request."""
    import hashlib

    return {
        "schema_version": "cardrag.ocr-reprocess-proof.v1",
        "request_sha256": hashlib.sha256(request.canonical_bytes()).hexdigest(),
        "target": target.model_dump(mode="json"),
        "ocr_sha256": ocr_sha256,
        "ocr_size_bytes": ocr_size_bytes,
        "reuse_key": reuse_key,
        "variant_id": variant_id,
    }


def pending_reprocess_targets(
    state_dir: Path, request: OCRReprocessRequest, *, run_id: str, manifest: GenerationManifest
) -> list[dict[str, str]]:
    """Require both request-bound success and its presence in the validated seal."""
    documents = {doc.document_id: doc for doc in manifest.documents}
    pending = []
    for target in request.targets:
        doc = documents.get(target.document_id)
        reason = "reprocess_target_unavailable"
        if doc is not None and doc.ocr is not None and doc.availability != "ocr_failed":
            reason = "reprocess_target_identity_mismatch"
            if (doc.pdf.sha256, doc.pdf.size_bytes, doc.page_count) == (
                target.pdf_sha256,
                target.pdf_size_bytes,
                target.page_count,
            ):
                reason = "reprocess_variant_unverified"
                if doc.ocr_cache_kind == "content" and doc.ocr_variant_id is not None:
                    path = state_dir / "runs" / run_id / "ocr-reprocess-proofs" / f"{target.document_id}.json"
                    try:
                        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
                            raise ValueError("unverified proof")
                        expected = canonical_json_bytes(
                            reprocess_success_proof(
                                request,
                                target,
                                ocr_sha256=doc.ocr.sha256,
                                ocr_size_bytes=doc.ocr.size_bytes,
                                reuse_key=doc.ocr_reuse_key,
                                variant_id=doc.ocr_variant_id,
                            )
                        )
                        if path.read_bytes() == expected:
                            continue
                    except (OSError, ValueError):
                        pass
        pending.append({"document_id": target.document_id, "reason_code": reason})
    return pending
