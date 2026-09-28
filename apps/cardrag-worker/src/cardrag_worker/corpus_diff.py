"""Post-acquisition corpus difference analysis and fail-closed disappearance gate."""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cardrag_core.canonical import canonical_json_bytes

from .state_seed_v122 import StateSeedLedger

CORPUS_DIFF_SCHEMA_VERSION = "cardrag.corpus-diff.v1"


class CorpusDiffError(RuntimeError):
    """Raised when prior documents disappear without justification."""

    def __init__(
        self,
        message: str | None = None,
        *,
        run_id: str = "unknown",
        missing_count: int = 0,
        report_path: Path | None = None,
        report: str | None = None,
        reason_code: str = "corpus_diff_missing",
        sample: Sequence[str] = (),
    ) -> None:
        self.run_id = run_id
        self.missing_count = missing_count
        self.reason_code = reason_code
        self.sample = tuple(sample[:5])
        self.report = report if report is not None else f"runs/{run_id}/reports/corpus-diff.json"
        self.report_path = report_path
        if message is not None:
            self.stored_error = message
        else:
            sample_str = f" (sample: {list(self.sample)})" if self.sample else ""
            self.stored_error = (
                f"Corpus diff check failed: {missing_count} seed documents disappeared "
                f"without justification{sample_str}; report={self.report}"
            )
        super().__init__(self.stored_error)


@dataclass(frozen=True, slots=True)
class CorpusDiffReport:
    schema_version: str
    run_id: str
    counts: dict[str, int]
    unchanged: tuple[str, ...]
    same_source_byte_revisions: tuple[dict[str, Any], ...]
    successor_sources: tuple[dict[str, Any], ...]
    new_products: tuple[dict[str, Any], ...]
    historical_maintained: tuple[str, ...]
    retired_lineages: tuple[str, ...]
    missing_unjustified: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_corpus_diff_report(
    *,
    run_id: str,
    acquired_documents: Sequence[Any],
    seed_ledger: StateSeedLedger | None = None,
    output_path: Path | None = None,
    fail_on_missing: bool = True,
) -> CorpusDiffReport:
    """Compare acquired documents against seed ledger or prior seal.

    Categorizes documents into:
    - unchanged: current docs that retain their prior content exactly.
    - same_source_byte_revisions: current docs from an existing source with a new PDF SHA.
    - successor_sources: current docs superseding an older source.
    - new_products: completely new products not observed in prior corpus.
    - historical_maintained: historical docs preserved in the corpus (both prior historical
      and predecessors that were replaced by new byte revisions).
    - retired_lineages: sources explicitly retired.
    - missing_unjustified: prior documents that vanished without valid lineage record.

    Fails closed if any prior seed document disappears without justification.
    """
    current_docs = [d for d in acquired_documents if not getattr(d, "is_historical", False)]
    historical_docs = [d for d in acquired_documents if getattr(d, "is_historical", False)]

    current_map = {
        d.source.document_id(d.pdf.sha256): d for d in current_docs
    }
    historical_map = {
        d.source.document_id(d.pdf.sha256): d for d in historical_docs
    }
    all_acquired_ids = set(current_map.keys()) | set(historical_map.keys())

    if seed_ledger is None:
        # First run or unseeded execution: all acquired are treated as new
        new_products = tuple(
            {
                "document_id": doc_id,
                "source_id": d.source.source_id,
                "issuer": d.source.issuer,
                "product_code": d.source.product_code,
                "pdf_sha256": d.pdf.sha256,
            }
            for doc_id, d in sorted(current_map.items())
        )
        counts = {
            "prior_current": 0,
            "prior_historical": 0,
            "unchanged": 0,
            "same_source_byte_revision": 0,
            "new_products": len(new_products),
            "successor_sources": 0,
            "replaced_predecessors": 0,
            "final_current": len(current_map),
            "final_historical": len(historical_map),
            "final_corpus": len(all_acquired_ids),
            "missing_unjustified": 0,
        }
        report = CorpusDiffReport(
            schema_version=CORPUS_DIFF_SCHEMA_VERSION,
            run_id=run_id,
            counts=counts,
            unchanged=(),
            same_source_byte_revisions=(),
            successor_sources=(),
            new_products=new_products,
            historical_maintained=tuple(sorted(historical_map.keys())),
            retired_lineages=(),
            missing_unjustified=(),
        )
    else:
        prior_current = set(seed_ledger.prior_current_doc_ids)
        prior_historical = set(seed_ledger.prior_historical_doc_ids)
        prior_all = prior_current | prior_historical
        prior_entries = seed_ledger.entries_by_doc_id

        # Prior source IDs and prior product lineages
        if getattr(seed_ledger, "source_records", None):
            prior_source_ids = set(seed_ledger.source_records.keys())
            prior_products = {
                (s.issuer, s.product_code) for s in seed_ledger.source_records.values()
            }
        else:
            prior_source_ids = {pe.source_id for pe in prior_entries.values()}
            prior_products = {
                (pe.issuer, getattr(pe, "product_code", None) or pe.source_id)
                for pe in prior_entries.values()
            }

        # Unchanged current docs: doc_id matches and was in prior_current
        unchanged = tuple(sorted(set(current_map.keys()) & prior_current))

        # Replaced predecessors: were in prior_current, now in historical_map
        replaced_predecessors = set(historical_map.keys()) & prior_current

        # New current docs: were not in prior_current
        new_current_ids = sorted(set(current_map.keys()) - prior_current)

        same_source_byte_revisions: list[dict[str, Any]] = []
        successor_sources: list[dict[str, Any]] = []
        new_products_list: list[dict[str, Any]] = []

        for doc_id in new_current_ids:
            doc = current_map[doc_id]
            source = doc.source
            superseded_id = getattr(doc, "supersedes_document_id", None)

            if source.source_id in prior_source_ids:
                # Same source ID existed previously with different PDF SHA
                if superseded_id is None:
                    for pe in prior_entries.values():
                        if pe.source_id == source.source_id:
                            superseded_id = pe.document_id
                            break
                same_source_byte_revisions.append(
                    {
                        "document_id": doc_id,
                        "source_id": source.source_id,
                        "issuer": source.issuer,
                        "product_code": source.product_code,
                        "new_pdf_sha256": doc.pdf.sha256,
                        "previous_document_id": superseded_id,
                    }
                )
            elif (
                superseded_id is not None
                and (superseded_id in replaced_predecessors or superseded_id in prior_entries or superseded_id in prior_all)
            ) or (source.issuer, source.product_code) in prior_products:
                # New source ID replaces a prior source/product lineage
                if superseded_id is None:
                    for pe in prior_entries.values():
                        if pe.issuer == source.issuer and getattr(pe, "product_code", None) == source.product_code:
                            superseded_id = pe.document_id
                            break
                successor_sources.append(
                    {
                        "document_id": doc_id,
                        "source_id": source.source_id,
                        "issuer": source.issuer,
                        "product_code": source.product_code,
                        "new_pdf_sha256": doc.pdf.sha256,
                        "previous_document_id": superseded_id,
                    }
                )
            else:
                # Completely new product
                new_products_list.append(
                    {
                        "document_id": doc_id,
                        "source_id": source.source_id,
                        "issuer": source.issuer,
                        "product_code": source.product_code,
                        "pdf_sha256": doc.pdf.sha256,
                    }
                )

        historical_maintained = tuple(sorted(historical_map.keys()))
        missing = sorted(prior_all - all_acquired_ids)

        counts = {
            "prior_current": len(prior_current),
            "prior_historical": len(prior_historical),
            "unchanged": len(unchanged),
            "same_source_byte_revision": len(same_source_byte_revisions),
            "successor_sources": len(successor_sources),
            "new_products": len(new_products_list),
            "replaced_predecessors": len(replaced_predecessors),
            "final_current": len(current_map),
            "final_historical": len(historical_map),
            "final_corpus": len(all_acquired_ids),
            "missing_unjustified": len(missing),
        }

        report = CorpusDiffReport(
            schema_version=CORPUS_DIFF_SCHEMA_VERSION,
            run_id=run_id,
            counts=counts,
            unchanged=unchanged,
            same_source_byte_revisions=tuple(same_source_byte_revisions),
            successor_sources=tuple(successor_sources),
            new_products=tuple(new_products_list),
            historical_maintained=historical_maintained,
            retired_lineages=(),
            missing_unjustified=tuple(missing),
        )

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload_bytes = canonical_json_bytes(report.as_dict())
        tmp = output_path.parent / f".corpus_diff.{uuid.uuid4().hex}.tmp"
        tmp.write_bytes(payload_bytes)
        os.chmod(tmp, 0o600)
        tmp.replace(output_path)

    if seed_ledger is not None and not seed_ledger.is_ocr_recovery_only and missing and fail_on_missing:
        raise CorpusDiffError(
            run_id=run_id,
            missing_count=len(missing),
            report_path=output_path,
            sample=missing[:5],
        )

    return report
