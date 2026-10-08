"""Explicit stage selection and source-bound reuse for finite Worker runs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cardrag_core import GenerationManifest

from .contracts import canonical_json_bytes, canonical_sha256
from .downloader import DownloadedPDF
from .ocr import OCRResult, split_ocr_pages
from .structure import (
    DerivedView,
    NodeLink,
    NodeSpan,
    StructureArtifact,
    StructureNode,
    StructurePage,
    issuer_parser_profile,
    validate_structure_artifact,
)

if TYPE_CHECKING:
    from .pipeline import PipelineResult

STAGES = ("pdf", "ocr", "structure", "embedding", "export", "webdav")
_RUN_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")


class PartialExecutionError(RuntimeError):
    """A bounded, secret-free refusal rather than an implicit paid fallback."""

    def __init__(self, reason: str, stage: str, document_id: str = "corpus") -> None:
        self.reason_code = reason
        self.stage = stage
        self.document_id = document_id
        super().__init__(f"{reason}: stage={stage} document_id={document_id}")


@dataclass
class ExecutionPlan:
    skipped: frozenset[str] = frozenset()
    source_run_id: str | None = None
    dry_run: bool = False
    channel: str = "candidate-009"
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.skipped.issubset(STAGES):
            raise ValueError("unknown skip stage; choose " + ", ".join(STAGES))
        if self.source_run_id is not None and not _RUN_ID.fullmatch(self.source_run_id):
            raise ValueError("invalid reuse source run ID")
        if self.skipped - {"webdav"} and self.source_run_id is None:
            raise ValueError("skipping upstream stages requires --reuse-from-run")

    def skips(self, stage: str) -> bool:
        return stage in self.skipped

    def record(self, stage: str, action: str, count: int = 1, seconds: float = 0) -> None:
        row = self.actions.setdefault(stage, {"action": action, "document_count": 0, "elapsed_seconds": 0.0})
        row["action"] = action
        row["document_count"] += count
        row["elapsed_seconds"] = round(row["elapsed_seconds"] + seconds, 6)

    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": "cardrag.execution-plan.v1",
            "source_run_id": self.source_run_id,
            "channel": self.channel,
            "skipped_stages": [s for s in STAGES if self.skips(s)],
        }

    def payload(self) -> dict[str, Any]:
        return {
            **self.identity(),
            "execution_mode": "reprocess",
            "dry_run": self.dry_run,
            "no_op": set(self.skipped) == set(STAGES),
            "stages": {
                s: self.actions.get(s, {"action": "reused" if self.skips(s) else "pending"}) for s in STAGES
            },
            "elapsed_seconds": round(time.monotonic() - self.started, 6),
        }


def regular_path(root: Path, path: Path, *, stage: str) -> Path:
    absolute = path.absolute()
    base = root.absolute()
    try:
        relative = absolute.relative_to(base)
        current = base
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink")
        if not absolute.is_file() or not absolute.resolve().is_relative_to(base.resolve()):
            raise ValueError("not a regular source artifact")
    except (ValueError, OSError):
        raise PartialExecutionError("skip_artifact_missing", stage) from None
    return absolute


def read_json(root: Path, path: Path, stage: str) -> dict[str, Any]:
    target = regular_path(root, path, stage=stage)
    if target.stat().st_size > 64 * 1024 * 1024:
        raise PartialExecutionError("skip_artifact_incompatible", stage)
    try:
        payload = json.loads(target.read_bytes())
        if not isinstance(payload, dict):
            raise ValueError("not an object")
        return payload
    except (ValueError, OSError):
        raise PartialExecutionError("skip_artifact_incompatible", stage) from None


class ReuseSource:
    """Read immutable source files; never reopen the completed run as running."""

    def __init__(self, root: Path, run_id: str, state_database: Path) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid reuse source run ID")
        self.root = root.absolute()
        self.run_id = run_id
        self.state_database = state_database
        self.directory = self.root / "runs" / run_id
        self.seal = read_json(self.root, self.directory / "sealed/publish.json", "export")
        if self.seal.get("run_id") != run_id:
            raise PartialExecutionError("skip_source_unavailable", "pdf")
        with sqlite3.connect(f"{state_database.absolute().as_uri()}?mode=ro", uri=True) as connection:
            status = connection.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone()
        if status is None or status[0] not in {"succeeded", "no_change"}:
            receipt_path = self.directory / "execution-result.json"
            receipt = read_json(self.root, receipt_path, "export") if receipt_path.exists() else {}
            if status is None or receipt.get("status") != "local_only":
                raise PartialExecutionError("skip_source_unavailable", "pdf")
        self.manifest = GenerationManifest.model_validate_json(canonical_json_bytes(self.seal["manifest"]))
        self.database = self.verify_object(
            {
                "path": self.seal["database_path"],
                "sha256": self.seal["database_sha256"],
                "size_bytes": self.seal["database_size_bytes"],
            },
            "export",
        )
        self.objects = {str(item["sha256"]): item for item in self.seal["objects"]}
        self.documents = {item.document_id: item for item in self.manifest.documents}
        self.verified: set[str] = set()
        with self.connect() as c:
            self.metadata = dict(c.execute("SELECT key,value FROM metadata"))
            self.profile = dict(c.execute("SELECT * FROM embedding_profiles LIMIT 1").fetchone())
            self.revisions = {
                str(r["document_id"]): dict(r)
                for r in c.execute(
                    "SELECT r.*,p.issuer,p.product_code,p.name AS product_name,p.document_type,"
                    "s.document_id AS supersedes_document_id FROM contract_revisions r "
                    "JOIN product_lineages p USING(product_lineage_id) LEFT JOIN contract_revisions s "
                    "ON s.contract_revision_id=r.supersedes_revision_id"
                )
            }
            self.unsupported = [dict(r) for r in c.execute("SELECT * FROM unsupported_products")]
            self.failed = [dict(r) for r in c.execute("SELECT * FROM ocr_failed_products")]
        if set(self.revisions) | {str(r["document_id"]) for r in self.failed} != set(self.documents):
            raise PartialExecutionError("skip_artifact_incompatible", "export")

    def connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"{self.database.as_uri()}?mode=ro&immutable=1", uri=True)
        c.row_factory = sqlite3.Row
        return c

    def verify_object(self, ref: Mapping[str, Any], stage: str) -> Path:
        path = regular_path(self.root, Path(str(ref["path"])), stage=stage)
        if path.stat().st_size != ref["size_bytes"]:
            raise PartialExecutionError("skip_artifact_incompatible", stage)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != ref["sha256"]:
            raise PartialExecutionError("skip_artifact_incompatible", stage)
        return path

    def object_path(self, sha: str, stage: str) -> Path:
        ref = self.objects.get(sha)
        if ref is None:
            raise PartialExecutionError("skip_artifact_missing", stage)
        if sha not in self.verified:
            self.verify_object(ref, stage)
            self.verified.add(sha)
        return regular_path(self.root, Path(ref["path"]), stage=stage)

    def pdf(self, document_id: str, source_url: str) -> DownloadedPDF:
        doc = self.documents.get(document_id)
        if doc is None:
            raise PartialExecutionError("skip_artifact_missing", "pdf", document_id)
        return DownloadedPDF(
            self.object_path(doc.pdf.sha256, "pdf"),
            doc.pdf.sha256,
            doc.pdf.size_bytes,
            doc.page_count,
            source_url,
        )

    def ocr(self, document_id: str, pdf_sha: str) -> OCRResult:
        doc = self.documents.get(document_id)
        if doc is None or doc.ocr is None:
            raise PartialExecutionError("skip_artifact_missing", "ocr", document_id)
        if doc.pdf.sha256 != pdf_sha:
            raise PartialExecutionError("skip_artifact_incompatible", "ocr", document_id)
        body = self.object_path(doc.ocr.sha256, "ocr").read_bytes()
        text = body.decode("utf-8")
        pages = split_ocr_pages(text, expected_count=doc.page_count)
        return OCRResult(
            pages,
            body,
            text,
            doc.ocr.sha256,
            doc.ocr.size_bytes,
            "verified-source-run",
            "retained",
            "retained",
            doc.ocr_reuse_key or doc.ocr.sha256,
            cache_kind=doc.ocr_cache_kind,
            cache_reuse_key=doc.ocr_reuse_key,
            cache_variant_id=doc.ocr_variant_id,
            cache_reused=True,
        )

    def structure(
        self, document_id: str, *, profile_id: str
    ) -> tuple[StructureArtifact, tuple[DerivedView, ...]]:
        artifact_path = self.directory / "documents" / document_id / "structure/structure.v2.json"
        raw = read_json(self.root, artifact_path, "structure")
        profile = issuer_parser_profile(str(raw["issuer"]))
        if raw["issuer_profile_sha256"] != profile.sha256:
            raise PartialExecutionError("skip_artifact_incompatible", "structure", document_id)
        values = {
            key: value
            for key, value in raw.items()
            if key
            not in {"issuer_profile", "issuer_profile_id", "issuer_profile_sha256", "pages", "nodes", "links"}
        }
        values["issuer_profile"] = profile
        values["pages"] = tuple(StructurePage(**p) for p in raw["pages"])
        values["nodes"] = tuple(
            StructureNode(
                **{
                    **n,
                    "spans": tuple(NodeSpan(**v) for v in n["spans"]),
                    "table_headers": tuple(n["table_headers"]),
                    "table_cells": tuple(n["table_cells"]),
                }
            )
            for n in raw["nodes"]
        )
        values["links"] = tuple(NodeLink(**v) for v in raw["links"])
        artifact = StructureArtifact(**values)
        validate_structure_artifact(artifact)
        payload = read_json(self.root, artifact_path.with_name("views.v1.json"), "structure")
        if (
            payload["input_structure_sha256"] != artifact.artifact_sha256
            or payload["embedding_profile_id"] != profile_id
        ):
            raise PartialExecutionError("skip_artifact_incompatible", "structure", document_id)
        views = tuple(
            DerivedView(
                **{
                    **v,
                    "spans": tuple(NodeSpan(**span) for span in v["spans"]),
                    "context": tuple(v["context"]),
                }
            )
            for v in payload["views"]
        )
        return artifact, views

    def preflight(self, plan: ExecutionPlan) -> dict[str, Any]:
        for doc in self.documents.values():
            if plan.skips("pdf"):
                self.object_path(doc.pdf.sha256, "pdf")
            if plan.skips("ocr") and doc.ocr is not None:
                self.ocr(doc.document_id, doc.pdf.sha256)
            if plan.skips("structure") and doc.ocr is not None:
                self.structure(doc.document_id, profile_id=str(self.profile["profile_id"]))
        if plan.skips("embedding"):
            from .embedding_v5 import QwenEmbeddingProfileV5

            profile = QwenEmbeddingProfileV5(
                **self.profile,
                endpoint_name=self.metadata["embedding_endpoint_name"],
                endpoint_metadata_sha256=self.metadata["embedding_endpoint_metadata_sha256"],
            )
            with (
                self.connect() as serving,
                sqlite3.connect(f"{self.state_database.absolute().as_uri()}?mode=ro", uri=True) as cache,
            ):
                for row in serving.execute("SELECT DISTINCT input_sha256 FROM embedding_views"):
                    digest = row["input_sha256"]
                    key = canonical_sha256(
                        {
                            "cache_namespace": profile.cache_namespace,
                            "input_kind": "document",
                            "input_sha256": digest,
                            "schema_version": "cardrag.embedding-cache-key.v5",
                        }
                    )
                    bound = cache.execute(
                        "SELECT dimension,dtype,normalization,length(embedding) FROM embedding_cache_v5 "
                        "WHERE cache_key=? AND profile_id=? AND input_sha256=?",
                        (key, profile.profile_id, digest),
                    ).fetchone()
                    if bound is None:
                        raise PartialExecutionError("skip_artifact_missing", "embedding")
                    if bound != (
                        profile.dimension,
                        profile.dtype,
                        profile.normalization,
                        profile.dimension * 4,
                    ):
                        raise PartialExecutionError("skip_artifact_incompatible", "embedding")
        return {
            **plan.payload(),
            "expected_external_calls": {
                "issuer_http": "zero" if plan.skips("pdf") else "possible",
                "ocr_provider": "zero" if plan.skips("ocr") else "cache misses only",
                "embedding_provider": "zero" if plan.skips("embedding") else "cache misses only",
                "remote_writes": "zero" if plan.skips("webdav") else "candidate publication possible",
            },
            "source_generation_id": self.manifest.generation_id,
            "source_created_at": self.manifest.created_at.isoformat(),
            "document_count": len(self.documents),
            "published": False,
            "compatibility": "source_checked; changed downstream inputs checked at stage boundary",
        }


class LocalWebDAV:
    """Local-only runs expose no remote I/O or write-capable publisher."""

    channel = "candidate-009-local"
    pointer_path = Path("channels/candidate-009-local/current.json")

    async def validated_current_generation(self) -> None:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get_bytes(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def observed_pointer_bytes(self) -> None:
        return None

    async def close(self) -> None:
        return None


class SkippedOCRProvider:
    provider = "retained"
    model = "retained"

    async def recognize(self, *args: Any, **kwargs: Any) -> str:
        raise PartialExecutionError("skip_artifact_missing", "ocr")


async def replay_pdf_source(pipeline: Any, run_id: str) -> PipelineResult:
    """Process the frozen source corpus without discovery or origin HTTP."""
    from .contracts import DocumentRecord, OCRFailedProductRecord, UnsupportedProductRecord
    from .ocr import page_records
    from .pipeline import (
        V5_VIEW_MAXIMUM_CHARACTERS,
        _atomic_write,
        _known_snapshot_sources,
        _OCRFailedDocument,
        _ProcessedDocument,
        _restore_snapshot,
    )
    from .structure import build_derived_views, parse_structure_artifact

    source: ReuseSource = pipeline.reuse_source
    plan: ExecutionPlan = pipeline.execution_plan
    profile = pipeline.v5_profile
    if profile is None:
        raise PartialExecutionError("skip_artifact_incompatible", "embedding")
    # A complete frozen generation includes current, superseded, unsupported,
    # and OCR-failed documents. None may silently disappear on replay.
    from .state_seed_v122 import load_state_seed_ledger

    known = _known_snapshot_sources(
        pipeline.state, pipeline.adapters, (), seed_ledger=load_state_seed_ledger(pipeline.state_dir)
    )
    processed = []
    failed = []
    unsupported = []
    provider_called = 0
    request = pipeline._active_ocr_request
    requested_ids = set()
    if request is not None:
        for target in request.targets:
            retained = source.documents.get(target.document_id)
            if (
                retained is None
                or target.document_id not in source.revisions
                or (retained.pdf.sha256, retained.pdf.size_bytes, retained.page_count)
                != (target.pdf_sha256, target.pdf_size_bytes, target.page_count)
            ):
                raise PartialExecutionError("skip_artifact_incompatible", "ocr", target.document_id)
            requested_ids.add(target.document_id)
    report_path = source.directory / "reports/issuer-collection.json"
    if report_path.is_file():
        from .issuer_collection import IssuerCollectionOutcome

        outcomes = read_json(source.root, report_path, "pdf").get("issuers", [])
        if isinstance(outcomes, dict):
            outcomes = list(outcomes.values())
        pipeline._issuer_outcomes = {row["issuer"]: IssuerCollectionOutcome(**row) for row in outcomes}
    for doc_id, doc in source.documents.items():
        started = time.monotonic()
        row = source.revisions.get(doc_id)
        if row is None:
            failure = next(r for r in source.failed if r["document_id"] == doc_id)
            pdf = source.pdf(doc_id, "")
            failed.append(
                _OCRFailedDocument(
                    OCRFailedProductRecord(
                        document_id=doc_id,
                        issuer=failure["issuer"],
                        product_code=failure["product_code"],
                        product_name=failure["name"],
                        title=failure["title"],
                        pdf_sha256=doc.pdf.sha256,
                        pdf_size_bytes=doc.pdf.size_bytes,
                        page_count=doc.page_count,
                        reason_code=failure["reason_code"],
                        reason=failure["reason"],
                        attempts=failure["attempts"],
                    ),
                    pdf.path,
                )
            )
            continue
        record = known.get(row["source_id"])
        if record is None or record.document_id(doc.pdf.sha256) != doc_id:
            raise PartialExecutionError("skip_source_unavailable", "pdf", doc_id)
        pdf = source.pdf(doc_id, record.source_url)
        plan.record("pdf", "reused", seconds=time.monotonic() - started)
        ocr_dir = pipeline.state_dir / "runs" / run_id / "documents" / doc_id / "ocr"
        started = time.monotonic()
        if plan.skips("ocr"):
            result = source.ocr(doc_id, pdf.sha256)
        else:

            async def recognize(
                document_id: str = doc_id,
                acquired: DownloadedPDF = pdf,
                output: Path = ocr_dir,
                retained_identity: tuple[str, int] | None = (
                    (doc.ocr.sha256, doc.ocr.size_bytes)
                    if doc.ocr is not None and doc_id not in requested_ids
                    else None
                ),
                request_id: str | None = (
                    request.request_id if request is not None and doc_id in requested_ids else None
                ),
            ) -> OCRResult:
                return cast(
                    OCRResult,
                    await pipeline.ocr.resolve(
                        run_id=run_id,
                        document_id=document_id,
                        pdf_path=acquired.path,
                        pdf_sha256=acquired.sha256,
                        pdf_size_bytes=acquired.size_bytes,
                        page_count=acquired.page_count,
                        output_dir=output,
                        retained_ocr_identity=retained_identity,
                        reprocess_request_id=request_id,
                    ),
                )

            result = await pipeline._finite_stage(
                run_id=run_id, document_id=doc_id, name="ocr", operation=recognize
            )
            if doc_id in requested_ids and (
                result.cache_kind != "content" or result.cache_variant_id is None
            ):
                raise PartialExecutionError("skip_artifact_incompatible", "ocr", doc_id)
            provider_called += int(result.provider_called)
        plan.record("ocr", "reused" if plan.skips("ocr") else "executed", seconds=time.monotonic() - started)
        started = time.monotonic()
        pages = page_records(doc_id, result)
        if plan.skips("structure") and pipeline.contract_sha256 != source.manifest.contract_sha256:
            raise PartialExecutionError("skip_artifact_incompatible", "structure", doc_id)
        if plan.skips("structure"):
            artifact, views = source.structure(doc_id, profile_id=profile.profile_id)
            if tuple(p.text_sha256 for p in artifact.pages) != tuple(p.text_sha256 for p in pages):
                raise PartialExecutionError("skip_artifact_incompatible", "structure", doc_id)
        else:
            artifact = parse_structure_artifact(
                pages,
                issuer=record.issuer,
                product_code=record.product_code,
                product_name=record.product_name,
                source_version=record.source_version,
                effective_date=record.effective_date.isoformat(),
                document_type=record.document_type,
                source_id=record.source_id,
                pdf_sha256=pdf.sha256,
            )
            validate_structure_artifact(artifact)
            views = build_derived_views(
                artifact,
                maximum_chars=V5_VIEW_MAXIMUM_CHARACTERS,
                maximum_tokens=profile.maximum_tokens,
                token_counter=pipeline.embeddings.token_counter,
            )
        plan.record(
            "structure",
            "reused" if plan.skips("structure") else "executed",
            seconds=time.monotonic() - started,
        )
        structure_dir = ocr_dir.parent / "structure"
        _atomic_write(structure_dir / "structure.v2.json", artifact.canonical_bytes)
        _atomic_write(
            structure_dir / "views.v1.json",
            canonical_json_bytes(
                {
                    "schema_version": "cardrag.embedding-views.v1",
                    "embedding_profile_id": profile.profile_id,
                    "input_structure_sha256": artifact.artifact_sha256,
                    "views": [v.payload for v in views],
                }
            ),
        )
        ocr_path = ocr_dir / "ocr.md"
        # Preserve original immutable OCR bytes and provenance in a small checkpoint.
        _atomic_write(ocr_path, result.ocr_bytes)
        processed.append(
            _ProcessedDocument(
                record,
                DocumentRecord(
                    doc_id,
                    record.issuer,
                    record.product_code,
                    record.product_name,
                    record.product_name,
                    pdf.sha256,
                    pdf.size_bytes,
                    pdf.page_count,
                    pages,
                ),
                pdf.path,
                ocr_path,
                result.ocr_sha256,
                result.size_bytes,
                result.cache_kind,
                result.cache_reuse_key,
                result.cache_variant_id,
                (),
                temporal_status=row["temporal_status"],
                supersedes_document_id=row["supersedes_document_id"],
                is_historical=row["temporal_status"] != "current",
                structure_artifact=artifact,
                embedding_views=views,
            )
        )
    for row in source.unsupported:
        payload = json.loads(row["source_payload_json"])
        snapshot = _restore_snapshot(
            {
                "contract_version": "cardrag.source-snapshot.v1",
                "issuer": row["issuer"],
                "parser_version": "replay",
                "records": [payload],
            },
            observed_at=source.manifest.created_at,
            expected_issuer=row["issuer"],
            expected_parser_version="replay",
        )
        unsupported.append(
            UnsupportedProductRecord(
                snapshot.records[0],
                row["protected_sha256"],
                row["protected_size_bytes"],
                row["protected_magic"],
            )
        )
    pipeline._unsupported_count = len(unsupported)
    # No new collection snapshot or retirement grace is recorded by frozen replay.
    built: PipelineResult = await pipeline._build_v5_generation(
        run_id=run_id,
        run_dir=pipeline.state_dir / "runs" / run_id,
        seal_path=pipeline.state_dir / "runs" / run_id / "sealed/publish.json",
        corpus_sha256=source.manifest.corpus_sha256,
        contract_sha256=pipeline.contract_sha256,
        processed=processed,
        unsupported=unsupported,
        failed_documents=failed,
        issuer_ocr_counts=source.manifest.issuer_ocr_counts,
        ocr_cache_publication_deferred=0,
        unresolved_revision_ledger=source.seal["v5_metrics"]["historical_revision_unresolved_identities"],
        unresolved_revision_sha256=source.seal["v5_metrics"]["historical_revision_unresolved_sha256"],
        historical_pdf_cache_hits=0,
        ocr_cache_reused_count=len(processed) if plan.skips("ocr") else 0,
        ocr_provider_called_count=provider_called,
    )
    return built
