"""Post-acquisition corpus difference analysis and fail-closed disappearance gate.

FIX_03 extensions:

* The comparison baseline is a rolling ``CorpusBaseline`` (the previous
  successful sealed generation) when one exists, falling back to the v1.0.28
  state-seed ledger for the first post-seed run.
* Documents absent from the acquisition may be classified as *retired* (grace
  satisfied, durable evidence complete) or as *retirement candidates* (evidence
  complete, grace pending) via an injected resolver; only the residual
  ``missing_unjustified`` set fails the run closed.
* Classification never deletes durable artifacts; the report names them
  "history-restorable" and carries per-lineage evidence records.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cardrag_core.canonical import canonical_json_bytes

from .retirement import LineageKey, RetirementOutcome
from .state_seed_v122 import StateSeedLedger

CORPUS_DIFF_SCHEMA_VERSION = "cardrag.corpus-diff.v2"


class CorpusDiffError(RuntimeError):
    """Raised when prior documents disappear without justification."""

    def __init__(
        self,
        message: str | None = None,
        *,
        run_id: str = "unknown",
        missing_count: int = 0,
        retired_count: int = 0,
        candidate_count: int = 0,
        report_path: Path | None = None,
        report: str | None = None,
        reason_code: str = "corpus_diff_missing",
        sample: Sequence[str] = (),
    ) -> None:
        self.run_id = run_id
        self.missing_count = missing_count
        self.retired_count = retired_count
        self.candidate_count = candidate_count
        self.reason_code = reason_code
        self.sample = tuple(sample[:5])
        self.report = report if report is not None else f"runs/{run_id}/reports/corpus-diff.json"
        self.report_path = report_path
        if message is not None:
            self.stored_error = message
        else:
            sample_str = f" (sample: {list(self.sample)})" if self.sample else ""
            self.stored_error = (
                f"Corpus diff check failed: {missing_count} prior documents disappeared "
                f"without justification (retired={retired_count}, candidates={candidate_count})"
                f"{sample_str}; report={self.report}"
            )
        super().__init__(self.stored_error)


@dataclass(frozen=True, slots=True)
class PriorEntry:
    document_id: str
    source_id: str
    issuer: str
    product_code: str
    document_type: str
    pdf_sha256: str
    ocr_sha256: str | None


@dataclass(frozen=True, slots=True)
class CorpusPriorView:
    """The verified prior corpus: rolling baseline or seed-ledger fallback."""

    kind: str  # "rolling-baseline" | "seed-ledger"
    generation_id: str
    run_id: str
    observed_at: str  # bound run's finished_at (grace-day anchor)
    current_doc_ids: frozenset[str]
    historical_doc_ids: frozenset[str]
    entries: Mapping[str, PriorEntry]


@dataclass(frozen=True, slots=True)
class RetirementRequest:
    document_id: str
    source_id: str
    issuer: str
    product_code: str
    document_type: str
    pdf_sha256: str
    ocr_sha256: str | None


RetirementResolver = Callable[
    [tuple[RetirementRequest, ...], frozenset[LineageKey]],
    "RetirementOutcome | None",
]


@dataclass(frozen=True, slots=True)
class CorpusDiffReport:
    schema_version: str
    run_id: str
    baseline_kind: str
    counts: dict[str, int]
    unchanged: tuple[str, ...]
    same_source_byte_revisions: tuple[dict[str, Any], ...]
    successor_sources: tuple[dict[str, Any], ...]
    new_products: tuple[dict[str, Any], ...]
    historical_maintained: tuple[str, ...]
    retired_lineages: tuple[dict[str, Any], ...] = ()
    retirement_candidates: tuple[dict[str, Any], ...] = ()
    missing_unjustified: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def prior_view_from_seed_ledger(seed_ledger: StateSeedLedger, observed_at: str) -> CorpusPriorView:
    entries: dict[str, PriorEntry] = {}
    for doc_id, entry in seed_ledger.entries_by_doc_id.items():
        record = seed_ledger.source_records.get(entry.source_id)
        entries[doc_id] = PriorEntry(
            document_id=doc_id,
            source_id=entry.source_id,
            issuer=entry.issuer,
            product_code=record.product_code if record is not None else "",
            document_type=record.document_type if record is not None else "",
            pdf_sha256=entry.pdf_sha256,
            ocr_sha256=entry.ocr_sha256,
        )
    return CorpusPriorView(
        kind="seed-ledger",
        generation_id=seed_ledger.generation_id,
        run_id=seed_ledger.run_id,
        observed_at=observed_at,
        current_doc_ids=frozenset(seed_ledger.prior_current_doc_ids),
        historical_doc_ids=frozenset(seed_ledger.prior_historical_doc_ids),
        entries=entries,
    )


def generate_corpus_diff_report(
    *,
    run_id: str,
    acquired_documents: Sequence[Any],
    seed_ledger: StateSeedLedger | None = None,
    prior: CorpusPriorView | None = None,
    retirement_resolver: RetirementResolver | None = None,
    output_path: Path | None = None,
    fail_on_missing: bool = True,
    prior_observed_at: str = "",
) -> CorpusDiffReport:
    """Compare acquired documents against the rolling baseline or seed ledger.

    Categorizes documents into:
    - unchanged: current docs that retain their prior content exactly.
    - same_source_byte_revisions: current docs from an existing source with a new PDF SHA.
    - successor_sources: current docs superseding an older source.
    - new_products: completely new products not observed in prior corpus.
    - historical_maintained: historical docs preserved in the corpus.
    - retired_lineages: lineages retired with durable evidence, grace, caps,
      and sealed ledger records (history restorable; nothing is deleted).
    - retirement_candidates: evidence-complete absences still inside grace.
    - missing_unjustified: disappearances the zero-loss gate must fail closed on.
    """
    if prior is None and seed_ledger is not None:
        prior = prior_view_from_seed_ledger(seed_ledger, prior_observed_at)
    current_docs = [d for d in acquired_documents if not getattr(d, "is_historical", False)]
    historical_docs = [d for d in acquired_documents if getattr(d, "is_historical", False)]

    current_map = {d.source.document_id(d.pdf.sha256): d for d in current_docs}
    historical_map = {d.source.document_id(d.pdf.sha256): d for d in historical_docs}
    all_acquired_ids = set(current_map.keys()) | set(historical_map.keys())

    if prior is None:
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
            "retired": 0,
            "retirement_candidates": 0,
            "final_current": len(current_map),
            "final_historical": len(historical_map),
            "final_corpus": len(all_acquired_ids),
            "missing_unjustified": 0,
        }
        report = CorpusDiffReport(
            schema_version=CORPUS_DIFF_SCHEMA_VERSION,
            run_id=run_id,
            baseline_kind="none",
            counts=counts,
            unchanged=(),
            same_source_byte_revisions=(),
            successor_sources=(),
            new_products=new_products,
            historical_maintained=tuple(sorted(historical_map.keys())),
        )
        if output_path is not None:
            _write_report(output_path, report)
        return report

    prior_current = set(prior.current_doc_ids)
    prior_historical = set(prior.historical_doc_ids)
    prior_all = prior_current | prior_historical
    prior_entries = prior.entries
    prior_source_ids = {pe.source_id for pe in prior_entries.values()}
    prior_products = {
        (pe.issuer, pe.product_code) for pe in prior_entries.values() if pe.product_code
    }

    unchanged = prior_current & set(current_map.keys())
    replaced_predecessors: set[str] = set(historical_map.keys()) & prior_current
    new_current_ids = sorted(set(current_map.keys()) - prior_current)

    same_source_byte_revisions: list[dict[str, Any]] = []
    successor_sources: list[dict[str, Any]] = []
    new_products_list: list[dict[str, Any]] = []

    for doc_id in new_current_ids:
        doc = current_map[doc_id]
        source = doc.source
        superseded_id = getattr(doc, "supersedes_document_id", None)

        if source.source_id in prior_source_ids:
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
            if superseded_id is None:
                for pe in prior_entries.values():
                    if pe.issuer == source.issuer and pe.product_code == source.product_code:
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

    retired_records: tuple[dict[str, Any], ...] = ()
    candidate_records: tuple[dict[str, Any], ...] = ()
    unjustified: tuple[str, ...] = tuple(missing)
    if missing and retirement_resolver is not None:
        discovered_lineages = frozenset(
            (d.source.issuer, d.source.product_code, d.source.document_type) for d in current_docs
        )
        requests = tuple(
            RetirementRequest(
                document_id=doc_id,
                source_id=prior_entries[doc_id].source_id if doc_id in prior_entries else "",
                issuer=prior_entries[doc_id].issuer if doc_id in prior_entries else "",
                product_code=prior_entries[doc_id].product_code if doc_id in prior_entries else "",
                document_type=prior_entries[doc_id].document_type if doc_id in prior_entries else "",
                pdf_sha256=prior_entries[doc_id].pdf_sha256 if doc_id in prior_entries else "",
                ocr_sha256=prior_entries[doc_id].ocr_sha256 if doc_id in prior_entries else None,
            )
            for doc_id in missing
        )
        outcome = retirement_resolver(requests, discovered_lineages)
        if outcome is not None:
            unjustified = tuple(sorted(outcome.unjustified))
            retired_records = outcome.retired
            candidate_records = outcome.candidates

    counts = {
        "prior_current": len(prior_current),
        "prior_historical": len(prior_historical),
        "unchanged": len(unchanged),
        "same_source_byte_revision": len(same_source_byte_revisions),
        "successor_sources": len(successor_sources),
        "new_products": len(new_products_list),
        "replaced_predecessors": len(replaced_predecessors),
        "retired": len(retired_records),
        "retirement_candidates": len(candidate_records),
        "final_current": len(current_map),
        "final_historical": len(historical_map),
        "final_corpus": len(all_acquired_ids),
        "missing_unjustified": len(unjustified),
    }

    report = CorpusDiffReport(
        schema_version=CORPUS_DIFF_SCHEMA_VERSION,
        run_id=run_id,
        baseline_kind=prior.kind,
        counts=counts,
        unchanged=tuple(sorted(unchanged)),
        same_source_byte_revisions=tuple(same_source_byte_revisions),
        successor_sources=tuple(successor_sources),
        new_products=tuple(new_products_list),
        historical_maintained=historical_maintained,
        retired_lineages=retired_records,
        retirement_candidates=candidate_records,
        missing_unjustified=unjustified,
    )

    if output_path is not None:
        _write_report(output_path, report)

    if (
        unjustified
        and fail_on_missing
        and not (seed_ledger is not None and seed_ledger.is_ocr_recovery_only and retirement_resolver is None)
    ):
        raise CorpusDiffError(
            run_id=run_id,
            missing_count=len(unjustified),
            retired_count=len(retired_records),
            candidate_count=len(candidate_records),
            report_path=output_path,
            sample=unjustified[:5],
        )

    return report


def _write_report(output_path: Path, report: CorpusDiffReport) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload_bytes = canonical_json_bytes(report.as_dict())
    tmp = output_path.parent / f".corpus_diff.{uuid.uuid4().hex}.tmp"
    tmp.write_bytes(payload_bytes)
    os.chmod(tmp, 0o600)
    tmp.replace(output_path)
