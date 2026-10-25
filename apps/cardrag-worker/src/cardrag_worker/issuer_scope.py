"""Frozen work scopes and immutable, non-scheduled publication inputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .contracts import canonical_json_bytes
from .partial_execution import PartialExecutionError, ReuseSource, read_json
from .revision_history_v5 import UnresolvedRevisionReasonV5


@dataclass(frozen=True)
class IssuerScope:
    pool: tuple[str, ...]
    selected: tuple[str, ...]
    mode: str = "all"

    def __post_init__(self) -> None:
        if (
            not self.selected
            or len(set(self.selected)) != len(self.selected)
            or not set(self.selected) <= set(self.pool)
        ):
            raise ValueError("issuer selection must be a nonempty subset of the configured pool")
        if self.mode not in {"all", "selected", "round_robin"}:
            raise ValueError("invalid issuer execution mode")

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "cardrag.issuer-scope.v1",
            "pool": list(self.pool),
            "selected": list(self.selected),
            "mode": self.mode,
        }


def freeze_scope(root: Path, run_id: str, scope: IssuerScope) -> None:
    from .pipeline import _atomic_write

    path = root / "runs" / run_id / "issuer-scope.json"
    if path.exists():
        if read_json(root, path, "scope") != scope.payload:
            raise PartialExecutionError("issuer_scope_changed", "scope")
    else:
        _atomic_write(path, canonical_json_bytes(scope.payload))


async def current_serving_baseline_source(pipeline: Any) -> ReuseSource | None:
    """Find and cryptographically validate the ReuseSource matching the current serving generation."""
    if pipeline.v5_profile is None:
        return None
    current = await pipeline.webdav.validated_current_generation()
    if current is None:
        return None
    from cardrag_core import GenerationManifest, generation_manifest_path

    body = await pipeline.webdav.get_bytes(
        generation_manifest_path(current.generation_id), max_bytes=64 * 1024 * 1024
    )
    if body is None:
        return None
    manifest = GenerationManifest.model_validate_json(body)
    if (
        manifest.canonical_bytes() != body
        or manifest.generation_id != current.generation_id
        or manifest.corpus_sha256 != current.corpus_sha256
        or manifest.contract_sha256 != current.contract_sha256
    ):
        return None

    for run_id in pipeline.state.retained_publication_run_ids(limit=max(2, pipeline.retained_generations)):
        path = pipeline.state_dir / "runs" / run_id / "sealed/publish.json"
        if not path.exists():
            continue
        sealed = read_json(pipeline.state_dir, path, "scope")
        if sealed.get("generation_id") != current.generation_id:
            continue
        source = ReuseSource(pipeline.state_dir, run_id, pipeline.state.path)
        if (
            source.manifest.corpus_sha256 != current.corpus_sha256
            or source.manifest.contract_sha256 != current.contract_sha256
        ):
            continue
        if pipeline.v5_profile is None or source.profile["profile_id"] != pipeline.v5_profile.profile_id:
            continue
        if "v5_metrics" in source.seal:
            from .unresolved_remediation import validate_unresolved_ledger_in_seal

            try:
                validate_unresolved_ledger_in_seal(source.seal, state=pipeline.state)
            except Exception as exc:
                raise PartialExecutionError("issuer_scope_baseline_incompatible", "scope") from exc
        return source
    return None


async def preservation_source(pipeline: Any) -> ReuseSource | None:
    """Bind preservation to the actual served generation, never the last run."""
    if pipeline.v5_profile is None:
        if set(pipeline.selected_issuers) != {a.spec.code for a in pipeline.adapters}:
            raise PartialExecutionError("issuer_scope_requires_v5", "scope")
        return None
    current = await pipeline.webdav.validated_current_generation()
    if current is None:
        if set(pipeline.selected_issuers) != {a.spec.code for a in pipeline.adapters}:
            raise PartialExecutionError("issuer_scope_baseline_required", "scope")
        return None
    from cardrag_core import GenerationManifest, generation_manifest_path

    body = await pipeline.webdav.get_bytes(
        generation_manifest_path(current.generation_id), max_bytes=64 * 1024 * 1024
    )
    if body is None:
        raise PartialExecutionError("issuer_scope_baseline_unavailable", "scope")
    manifest = GenerationManifest.model_validate_json(body)
    if (
        manifest.canonical_bytes() != body
        or manifest.generation_id != current.generation_id
        or manifest.corpus_sha256 != current.corpus_sha256
        or manifest.contract_sha256 != current.contract_sha256
    ):
        raise PartialExecutionError("issuer_scope_baseline_incompatible", "scope")
    if not set(manifest.issuer_codes) - set(pipeline.selected_issuers):
        return None
    for run_id in pipeline.state.retained_publication_run_ids(limit=max(2, pipeline.retained_generations)):
        path = pipeline.state_dir / "runs" / run_id / "sealed/publish.json"
        if not path.exists():
            continue
        sealed = read_json(pipeline.state_dir, path, "scope")
        if sealed.get("generation_id") != current.generation_id:
            continue
        source = ReuseSource(pipeline.state_dir, run_id, pipeline.state.path)
        if (
            source.manifest.corpus_sha256 != current.corpus_sha256
            or source.manifest.contract_sha256 != current.contract_sha256
        ):
            raise PartialExecutionError("issuer_scope_baseline_incompatible", "scope")
        if set(source.manifest.issuer_codes) - set(pipeline.selected_issuers):
            if pipeline.v5_profile is None or source.profile["profile_id"] != pipeline.v5_profile.profile_id:
                raise PartialExecutionError("issuer_scope_baseline_incompatible", "embedding")
            if "v5_metrics" in source.seal:
                from .unresolved_remediation import validate_unresolved_ledger_in_seal

                try:
                    validate_unresolved_ledger_in_seal(source.seal, state=pipeline.state)
                except Exception as exc:
                    raise PartialExecutionError("issuer_scope_baseline_incompatible", "scope") from exc
            return source
        return None
    raise PartialExecutionError("issuer_scope_baseline_unavailable", "scope")


def restore_preserved_adapters(pipeline: Any, issuer_codes: Any) -> None:
    if not issuer_codes:
        return
    missing = set(issuer_codes) - {a.spec.code for a in pipeline.adapters}
    if not missing:
        return
    from .contracts import IssuerSpec
    from .issuers.registry import REGISTERED_ISSUERS

    class _PreservedDummyAdapter:
        def __init__(self, code: str):
            self.parser_version = "preserved.v1"
            self.spec = IssuerSpec(
                code=code,
                display_name=code,
                sort_order=999,
                allowed_hosts=frozenset({f"{code}.example"}),
                categories=("credit",),
            )

    extra = [
        REGISTERED_ISSUERS[code].factory() if code in REGISTERED_ISSUERS else _PreservedDummyAdapter(code)
        for code in sorted(missing)
    ]
    pipeline.adapters = tuple(pipeline.adapters) + tuple(extra)


def preserve_inputs(pipeline: Any, source: ReuseSource | None, run_id: str) -> tuple[Any, Any, Any, Any]:
    """Copy sealed rows/artifacts; invoke no OCR, parser, view builder or provider."""
    from .contracts import DocumentRecord, OCRFailedProductRecord, PageRecord, UnsupportedProductRecord
    from .pipeline import (
        _AcquiredDocument,
        _atomic_write,
        _known_snapshot_sources,
        _OCRFailedDocument,
        _ProcessedDocument,
        _restore_snapshot,
    )
    from .state_seed_v122 import load_state_seed_ledger

    pipeline._preserved_unresolved = ()
    if source is None:
        return (), (), (), ()
    restore_preserved_adapters(pipeline, source.manifest.issuer_codes)
    known = _known_snapshot_sources(
        pipeline.state, pipeline.adapters, (), seed_ledger=load_state_seed_ledger(pipeline.state_dir)
    )
    selected = set(pipeline.selected_issuers)
    from .revision_history_v5 import UnresolvedRevisionIdentityV5

    if "v5_metrics" in source.seal:
        from .unresolved_remediation import validate_unresolved_ledger_in_seal

        try:
            validated_entries = validate_unresolved_ledger_in_seal(source.seal, state=pipeline.state)
        except Exception as exc:
            raise PartialExecutionError("issuer_scope_baseline_incompatible", "scope") from exc
        raw_entries = list(validated_entries)
    else:
        raw_entries = source.seal.get("v5_metrics", {}).get("historical_revision_unresolved_identities", [])

    unresolved: list[UnresolvedRevisionIdentityV5] = []
    for entry in raw_entries:
        record = known.get(entry["source_id"])
        issuer: str | None = None
        if record is not None:
            issuer = record.issuer
        elif hasattr(pipeline.state, "pdf_cache_source_issuer"):
            issuer = pipeline.state.pdf_cache_source_issuer(entry["source_id"])
        else:
            row = pipeline.state.connection.execute(
                "SELECT issuer FROM pdf_cache_source WHERE source_id=?",
                (entry["source_id"],),
            ).fetchone()
            issuer = row[0] if row is not None else None

        if issuer in selected:
            continue

        is_remediated = False
        if hasattr(pipeline.state, "is_revision_remediated"):
            is_remediated = pipeline.state.is_revision_remediated(entry["source_id"], entry["pdf_sha256"])
        if is_remediated or record is None:
            continue

        unresolved.extend(
            UnresolvedRevisionIdentityV5(
                entry["source_id"], entry["pdf_sha256"], cast(UnresolvedRevisionReasonV5, reason)
            )
            for reason in entry["reason_codes"]
        )
    pipeline._preserved_unresolved = tuple(unresolved)
    fallback_reasons = {
        entry["document_id"]: entry["reason_code"]
        for entry in source.seal.get("v5_metrics", {}).get("structure_fallback_documents", [])
    }
    acquired, processed, failed, unsupported = [], [], [], []
    with source.connect() as db:
        for doc_id, doc in source.documents.items():
            if doc.issuer in selected:
                continue
            row = source.revisions.get(doc_id)
            if row is None:
                failure = next(r for r in source.failed if r["document_id"] == doc_id)
                matches = [
                    r
                    for r in known.values()
                    if r.issuer == doc.issuer and r.document_id(doc.pdf.sha256) == doc_id
                ]
                if len(matches) != 1:
                    raise PartialExecutionError("issuer_scope_source_missing", "scope", doc_id)
                record = matches[0]
                pdf = source.pdf(doc_id, record.source_url)
                acquired.append(_AcquiredDocument(record, pdf))
                failed.append(
                    _OCRFailedDocument(
                        OCRFailedProductRecord(
                            doc_id,
                            failure["issuer"],
                            failure["product_code"],
                            failure["name"],
                            failure["title"],
                            doc.pdf.sha256,
                            doc.pdf.size_bytes,
                            doc.page_count,
                            failure["reason_code"],
                            failure["reason"],
                            failure["attempts"],
                        ),
                        pdf.path,
                        is_historical=False,
                        source=record,
                        temporal_status="current",
                        supersedes_document_id=None,
                    )
                )
                continue
            record = known.get(row["source_id"])
            if record is None or record.document_id(doc.pdf.sha256) != doc_id or doc.ocr is None:
                raise PartialExecutionError("issuer_scope_source_missing", "scope", doc_id)
            pdf = source.pdf(doc_id, record.source_url)
            artifact, views = source.structure(doc_id, profile_id=pipeline.v5_profile.profile_id)
            pages = tuple(
                PageRecord(doc_id, r["page"], r["text"])
                for r in db.execute(
                    "SELECT page,text FROM document_pages WHERE contract_revision_id=? ORDER BY page",
                    (row["contract_revision_id"],),
                )
            )
            if len(pages) != doc.page_count:
                raise PartialExecutionError("issuer_scope_pages_missing", "scope", doc_id)
            directory = pipeline.state_dir / "runs" / run_id / "documents" / doc_id
            ocr_path = directory / "ocr/ocr.md"
            _atomic_write(ocr_path, source.object_path(doc.ocr.sha256, "ocr").read_bytes())
            # Copy exact validated source bytes, retaining provenance/hash contracts.
            for name in ("structure.v2.json", "views.v1.json"):
                original = source.directory / "documents" / doc_id / "structure" / name
                _atomic_write(
                    directory / "structure" / name,
                    canonical_json_bytes(read_json(source.root, original, "structure")),
                )
            acquired.append(
                _AcquiredDocument(
                    record,
                    pdf,
                    row["temporal_status"],
                    row["supersedes_document_id"],
                    row["temporal_status"] != "current",
                )
            )
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
                    doc.ocr.sha256,
                    doc.ocr.size_bytes,
                    doc.ocr_cache_kind,
                    doc.ocr_reuse_key,
                    doc.ocr_variant_id,
                    (),
                    row["temporal_status"],
                    row["supersedes_document_id"],
                    row["temporal_status"] != "current",
                    artifact,
                    views,
                    fallback_reasons.get(doc_id),
                )
            )
    for row in source.unsupported:
        if row["issuer"] in selected:
            continue
        import json

        snapshot = _restore_snapshot(
            {
                "contract_version": "cardrag.source-snapshot.v1",
                "issuer": row["issuer"],
                "parser_version": "preserved",
                "records": [json.loads(row["source_payload_json"])],
            },
            observed_at=source.manifest.created_at,
            expected_issuer=row["issuer"],
            expected_parser_version="preserved",
        )
        unsupported.append(
            UnsupportedProductRecord(
                snapshot.records[0],
                row["protected_sha256"],
                row["protected_size_bytes"],
                row["protected_magic"],
            )
        )
    pipeline.performance.set("not_scheduled_documents", len(acquired))
    return tuple(acquired), tuple(processed), tuple(failed), tuple(unsupported)
