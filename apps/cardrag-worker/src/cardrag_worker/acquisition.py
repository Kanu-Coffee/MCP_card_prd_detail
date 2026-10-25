"""Validated unsealed collection receipts; never interpreted as published data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from .contracts import canonical_json_bytes, canonical_sha256
from .downloader import DownloadedPDF, validate_pdf
from .partial_execution import ExecutionPlan, PartialExecutionError, read_json, regular_path

if TYPE_CHECKING:
    from .pipeline import PipelineResult
    from .state import WorkerState


class AcquisitionSource:
    def __init__(self, root: Path, run_id: str, state_database: Path) -> None:
        from cardrag_core.paths import validate_identifier

        validate_identifier(run_id, label="run_id")
        self.root, self.run_id, self.state_database = root, run_id, state_database
        self.directory = root / "runs" / run_id
        receipt = read_json(root, self.directory / "acquisition.json", "pdf")
        digest = receipt.pop("payload_sha256", None)
        if (
            digest != canonical_sha256(receipt)
            or receipt["schema_version"] != "cardrag.acquisition-receipt.v1"
            or receipt["run_id"] != run_id
        ):
            raise PartialExecutionError("acquisition_receipt_invalid", "pdf")
        self.receipt = receipt
        self.selected = tuple(receipt["selected_issuers"])
        self.documents: dict[str, SimpleNamespace] = {}
        self.revisions: dict[str, Any] = {}
        self._pdfs: dict[str, DownloadedPDF] = {}
        self.failed: list[Any] = []
        self.unsupported = receipt["unsupported"]
        self.unsupported_attachments = receipt.get("unsupported_attachments", [])
        self.manifest = SimpleNamespace(
            created_at=datetime.fromisoformat(receipt["observed_at"]),
            corpus_sha256=receipt["corpus_sha256"],
            generation_id=receipt["baseline_generation_id"],
        )
        self.seal = {"v5_metrics": receipt["revision_metrics"]}
        for row in receipt["documents"]:
            if row["issuer"] not in self.selected or row["document_id"] in self.documents:
                raise PartialExecutionError("acquisition_scope_invalid", "pdf")
            path = regular_path(root, root / row["pdf_relative_path"], stage="pdf")
            if validate_pdf(path, expected_sha256=row["pdf_sha256"]) != (
                row["pdf_sha256"],
                row["pdf_size_bytes"],
                row["page_count"],
            ):
                raise PartialExecutionError("acquisition_pdf_invalid", "pdf")
            pdf_ref = SimpleNamespace(sha256=row["pdf_sha256"], size_bytes=row["pdf_size_bytes"])
            self.documents[row["document_id"]] = SimpleNamespace(
                document_id=row["document_id"],
                issuer=row["issuer"],
                pdf=pdf_ref,
                page_count=row["page_count"],
                ocr=None,
                ocr_cache_kind=None,
                ocr_reuse_key=None,
                ocr_variant_id=None,
            )
            self.revisions[row["document_id"]] = row
            self._pdfs[row["document_id"]] = DownloadedPDF(
                path, row["pdf_sha256"], row["pdf_size_bytes"], row["page_count"], row["source_url"]
            )

    def pdf(self, document_id: str, source_url: str) -> DownloadedPDF:
        return self._pdfs[document_id]

    def preflight(self, plan: ExecutionPlan) -> dict[str, Any]:
        if plan.skipped != frozenset({"pdf"}) or plan.phase != "process":
            raise PartialExecutionError("acquisition_phase_conflict", "plan")
        return {**plan.payload(), "acquired_documents": len(self.documents), "origin_requests": 0}


def write_acquisition(
    pipeline: Any,
    run_id: str,
    acquired: Sequence[Any],
    unsupported: Sequence[Any],
    *,
    corpus_sha256: str,
    unresolved_revision_ledger: Sequence[Any],
    unresolved_revision_sha256: str,
    baseline_generation_id: str | None,
    unsupported_attachments: Sequence[Any] = (),
) -> PipelineResult:
    from datetime import UTC, datetime

    from .pipeline import PipelineResult, _atomic_write
    from .retirement import ledger_payload

    selected = set(pipeline.selected_issuers)
    payload: dict[str, Any] = {
        "schema_version": "cardrag.acquisition-receipt.v1",
        "run_id": run_id,
        "selected_issuers": list(pipeline.selected_issuers),
        "observed_at": datetime.now(UTC).isoformat(),
        "baseline_generation_id": baseline_generation_id,
        "corpus_sha256": corpus_sha256,
        "retirement_ledger": ledger_payload(pipeline._pending_retirement_ledger)
        if pipeline._pending_retirement_ledger is not None
        else None,
        "revision_metrics": {
            "historical_revision_unresolved_identities": list(unresolved_revision_ledger),
            "historical_revision_unresolved_sha256": unresolved_revision_sha256,
        },
        "documents": [
            {
                "document_id": a.source.document_id(a.pdf.sha256),
                "source_id": a.source.source_id,
                "issuer": a.source.issuer,
                "source_url": a.source.source_url,
                "pdf_sha256": a.pdf.sha256,
                "pdf_size_bytes": a.pdf.size_bytes,
                "page_count": a.pdf.page_count,
                "pdf_relative_path": str(a.pdf.path.relative_to(pipeline.state_dir)),
                "temporal_status": a.temporal_status,
                "supersedes_document_id": a.supersedes_document_id,
            }
            for a in acquired
            if a.source.issuer in selected
        ],
        "unsupported": [
            {
                "issuer": u.source.issuer,
                "source_payload_json": canonical_json_bytes(u.source.discovery_payload).decode(),
                "protected_sha256": u.protected_sha256,
                "protected_size_bytes": u.protected_size_bytes,
                "protected_magic": u.protected_magic,
            }
            for u in unsupported
            if u.source.issuer in selected
        ],
        "unsupported_attachments": [
            {
                "issuer": u.source.issuer,
                "product_code": u.source.product_code,
                "source_id": u.source.source_id,
                "source_payload_json": canonical_json_bytes(u.source.discovery_payload).decode(),
                "reason": u.reason,
                "format": u.format,
                "sha256": u.sha256,
                "size_bytes": u.size_bytes,
            }
            for u in (unsupported_attachments or ())
            if u.source.issuer in selected
        ],
    }
    _atomic_write(
        pipeline.state_dir / "runs" / run_id / "acquisition.json",
        canonical_json_bytes({**payload, "payload_sha256": canonical_sha256(payload)}),
    )
    pipeline.state.finish_run(
        run_id, "succeeded", corpus_sha256=corpus_sha256, contract_sha256=pipeline.contract_sha256
    )
    return PipelineResult(
        run_id,
        "collected",
        corpus_sha256,
        pipeline.contract_sha256,
        None,
        len(payload["documents"]),
        0,
        unsupported_document_count=len(unsupported),
        unsupported_attachment_count=len(payload["unsupported_attachments"]),
        collection_status="degraded" if payload["unsupported_attachments"] else "complete",
        published=False,
        execution={"phase": "collect", "receipt": f"runs/{run_id}/acquisition.json"},
    )


def pending_acquisitions(root: Path, state: WorkerState | None = None) -> list[dict[str, Any]]:
    runs = root / "runs"
    if not runs.exists():
        return []
    if runs.is_symlink():
        raise PartialExecutionError("acquisition_root_unsafe", "pdf")
    pending = []
    for path in runs.glob("*/acquisition.json"):
        if (path.parent / "acquisition-consumed.json").exists():
            # Only a canonical verified process run may release a pending pin.
            marker = read_json(root, path.parent / "acquisition-consumed.json", "pdf")
            if state is not None:
                proof = state.connection.execute(
                    "SELECT 1 FROM publish WHERE run_id=? AND generation_id=? AND status='ready'",
                    (marker["process_run_id"], marker["generation_id"]),
                ).fetchone()
                if proof is None:
                    raise PartialExecutionError("acquisition_release_invalid", "pdf")
            else:
                target = read_json(root, runs / marker["process_run_id"] / "sealed/publish.json", "pdf")
                if target["generation_id"] != marker["generation_id"]:
                    raise PartialExecutionError("acquisition_release_invalid", "pdf")
            continue
        payload = read_json(root, path, "pdf")
        digest = payload.pop("payload_sha256", None)
        if digest != canonical_sha256(payload) or path.parent.name != payload["run_id"]:
            raise PartialExecutionError("acquisition_receipt_invalid", "pdf")
        pending.append(payload)
    return pending
