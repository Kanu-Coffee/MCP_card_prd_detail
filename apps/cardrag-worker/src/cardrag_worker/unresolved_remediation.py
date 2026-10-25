"""Evidence-bound remediation and exclusion of legacy unresolved revision identities.

In accordance with 019 PLAN_AMENDMENT_01:
- Current product accuracy and batch completion take priority over historical revisions of nonexistent cards.
- Past unresolved identities that cannot be materialized (missing source metadata, missing cache objects,
  or collisions with current documents) are excluded from active publication and revision expansion.
- Dispositions:
  - 'updated_to_latest': active product lineage has updated to the latest verified contract; past error excluded.
  - 'past_excluded': historical irrecoverable identity excluded from active expansion.
  - 'current_error': active current document has a persistent failure; error preserved.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .contracts import SourceRecord, canonical_json_bytes, canonical_sha256
from .pdf_cache import PDFCache, _validate_regular_pdf
from .revision_history_v5 import (
    _SHA256,
    _SOURCE_ID,
    _UNRESOLVED_REASONS,
    UnresolvedRevisionIdentityV5,
    UnresolvedRevisionLedgerEntryV5,
    UnresolvedRevisionReasonV5,
    canonical_unresolved_revision_ledger_v5,
    unresolved_revision_ledger_sha256_v5,
)
from .state import WorkerState

_RUN_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class UnresolvedInventoryItem:
    source_id: str
    pdf_sha256: str
    reason_codes: tuple[str, ...]
    issuer: str | None
    product_code: str | None
    document_type: str | None
    source_exists: bool
    known_source_exists: bool
    revision_id: int | None
    pdf_cache_object_exists: bool
    pdf_cache_object_disk_valid: bool
    is_current_in_cache: bool
    product_in_current_discovery: bool | None
    disposition: str  # 'updated_to_latest' | 'past_excluded' | 'current_error'
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "pdf_sha256": self.pdf_sha256,
            "reason_codes": list(self.reason_codes),
            "issuer": self.issuer,
            "product_code": self.product_code,
            "document_type": self.document_type,
            "source_exists": self.source_exists,
            "known_source_exists": self.known_source_exists,
            "revision_id": self.revision_id,
            "pdf_cache_object_exists": self.pdf_cache_object_exists,
            "pdf_cache_object_disk_valid": self.pdf_cache_object_disk_valid,
            "is_current_in_cache": self.is_current_in_cache,
            "product_in_current_discovery": self.product_in_current_discovery,
            "disposition": self.disposition,
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class UnresolvedRemediationPlan:
    baseline_generation_id: str | None
    baseline_run_id: str | None
    total_identities: int
    items: tuple[UnresolvedInventoryItem, ...]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_generation_id": self.baseline_generation_id,
            "baseline_run_id": self.baseline_run_id,
            "total_identities": self.total_identities,
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
        }


def validate_unresolved_ledger_in_seal(
    seal_data: Mapping[str, Any],
    *,
    state: WorkerState | None = None,
    require_verified_run: bool = False,
    require_verified_serving: bool = False,
    current_serving_generation_id: str | None = None,
) -> tuple[UnresolvedRevisionLedgerEntryV5, ...]:
    """Validate that the unresolved revision ledger in a baseline seal is authentic and consistent."""
    if not isinstance(seal_data, Mapping):
        raise ValueError("Sealed data is not a JSON object")

    generation_id = seal_data.get("generation_id")
    run_id = seal_data.get("run_id")
    if require_verified_run or require_verified_serving:
        if not isinstance(generation_id, str) or not generation_id:
            raise ValueError("Sealed data missing valid generation_id")
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("Sealed data missing valid run_id")
        if state is not None:
            row = state.connection.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone()
            if row is None or str(row[0]) not in ("succeeded", "no_change"):
                raise ValueError(
                    f"Run {run_id} is not a verified completed publication run in state database"
                )
            pub_row = state.connection.execute(
                "SELECT generation_id, run_id, status FROM publish WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if pub_row is None or str(pub_row[2]) != "ready":
                raise ValueError(
                    f"Generation {generation_id} is not a verified ready publication in state database"
                )
            if str(pub_row[1]) != run_id:
                raise ValueError(
                    f"Generation {generation_id} publication run_id {pub_row[1]} does not match seal run_id {run_id}"
                )
            if require_verified_serving:
                if current_serving_generation_id is None:
                    raise ValueError(
                        f"Generation {generation_id} could not be verified against an active serving generation"
                    )
                if generation_id != current_serving_generation_id:
                    raise ValueError(
                        f"Generation {generation_id} is not the current serving publication (current serving is {current_serving_generation_id})"
                    )

                manifest_data = seal_data.get("manifest")
                if manifest_data is None or not isinstance(manifest_data, Mapping):
                    raise ValueError("Sealed data missing valid manifest for verified serving")
                try:
                    from cardrag_core import GenerationManifest

                    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(manifest_data))
                except Exception as exc:
                    raise ValueError(f"Sealed data manifest validation failed: {exc}") from exc
                if manifest.generation_id != generation_id:
                    raise ValueError(
                        f"Manifest generation_id {manifest.generation_id} does not match seal generation_id {generation_id}"
                    )
                pub_verify = state.connection.execute(
                    "SELECT corpus_sha256, contract_sha256, serving_sha256 FROM publish WHERE generation_id=? AND run_id=?",
                    (generation_id, run_id),
                ).fetchone()
                if pub_verify is None:
                    raise ValueError(
                        f"Generation {generation_id} has no matching publication record in state database"
                    )
                if (
                    str(pub_verify[0]) != manifest.corpus_sha256
                    or str(pub_verify[1]) != manifest.contract_sha256
                ):
                    raise ValueError(
                        f"Seal cryptographic hashes do not match publication record in state database for {generation_id}"
                    )
                if "database_sha256" in seal_data and str(pub_verify[2]) != str(seal_data["database_sha256"]):
                    raise ValueError(
                        f"Seal cryptographic hashes do not match publication record in state database for {generation_id}"
                    )
    else:
        if generation_id is not None and not isinstance(generation_id, str):
            raise ValueError("Sealed data has invalid generation_id")
        if run_id is not None and (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)):
            raise ValueError("Sealed data has invalid run_id")

    v5_metrics = seal_data.get("v5_metrics")
    if v5_metrics is None:
        if require_verified_run or require_verified_serving:
            raise ValueError("Sealed data missing valid v5_metrics")
        return ()
    if not isinstance(v5_metrics, Mapping):
        raise ValueError("Sealed data missing valid v5_metrics")

    raw_unresolved = v5_metrics.get("historical_revision_unresolved_identities")
    if raw_unresolved is None:
        if require_verified_run or require_verified_serving:
            raise ValueError("sealed v5 unresolved revision ledger is missing")
        return ()
    if not isinstance(raw_unresolved, list):
        raise ValueError("sealed v5 unresolved revision ledger is invalid")

    parsed: list[UnresolvedRevisionIdentityV5] = []
    for entry in raw_unresolved:
        if not isinstance(entry, Mapping) or set(entry) != {"source_id", "pdf_sha256", "reason_codes"}:
            raise ValueError("sealed v5 unresolved revision entry has invalid schema")
        source_id = str(entry["source_id"])
        pdf_sha256 = str(entry["pdf_sha256"])
        if not _SOURCE_ID.fullmatch(source_id) or not _SHA256.fullmatch(pdf_sha256):
            raise ValueError("sealed v5 unresolved revision identity format is invalid")
        reasons = entry.get("reason_codes")
        if not isinstance(reasons, list) or not reasons:
            raise ValueError("sealed v5 unresolved revision reason_codes is invalid")
        for r in reasons:
            if not isinstance(r, str) or r not in _UNRESOLVED_REASONS:
                raise ValueError(f"unsupported unresolved reason code: {r}")
            parsed.append(
                UnresolvedRevisionIdentityV5(
                    source_id=source_id,
                    pdf_sha256=pdf_sha256,
                    reason_code=cast(UnresolvedRevisionReasonV5, r),
                )
            )

    canonical = canonical_unresolved_revision_ledger_v5(parsed)
    if list(canonical) != list(raw_unresolved):
        raise ValueError("sealed v5 unresolved revision ledger is not canonical or contains duplicates")

    count = v5_metrics.get("historical_revision_unresolved_count")
    if count is not None and (type(count) is not int or count != len(canonical)):
        raise ValueError("sealed v5 unresolved revision count is inconsistent with ledger length")

    expected_sha = unresolved_revision_ledger_sha256_v5(canonical)
    actual_sha = v5_metrics.get("historical_revision_unresolved_sha256")
    if actual_sha is not None and actual_sha != expected_sha:
        raise ValueError(
            f"historical_revision_unresolved_sha256 mismatch: expected {expected_sha}, got {actual_sha}"
        )

    if require_verified_run or require_verified_serving:
        if count is None:
            raise ValueError("sealed v5 unresolved revision count is missing")
        if actual_sha is None:
            raise ValueError("sealed v5 unresolved revision sha256 is missing")

    if state is not None and require_verified_serving:
        if not run_id:
            raise ValueError("Sealed data missing run_id for verified serving")
        reg_seal = state.path.parent / "runs" / run_id / "sealed" / "publish.json"
        if not reg_seal.is_file() or reg_seal.is_symlink():
            raise ValueError(f"Authentic on-disk sealed publication not found at {reg_seal}")
        try:
            disk_data = json.loads(reg_seal.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(
                f"Failed reading authentic on-disk sealed publication {reg_seal}: {exc}"
            ) from exc
        if not isinstance(disk_data, Mapping):
            raise ValueError("Authentic on-disk sealed publication is not a JSON object")
        disk_metrics = disk_data.get("v5_metrics") or {}
        disk_sha = disk_metrics.get("historical_revision_unresolved_sha256")
        if disk_sha != actual_sha:
            raise ValueError(
                "Explicit seal unresolved ledger does not match authentic on-disk sealed publication"
            )
        if disk_data.get("generation_id") != generation_id or disk_data.get("run_id") != run_id:
            raise ValueError("Authentic on-disk sealed publication identity mismatch")
        disk_manifest = disk_data.get("manifest")
        manifest_data = seal_data.get("manifest")
        if (
            disk_manifest is not None
            and manifest_data is not None
            and canonical_sha256(disk_manifest) != canonical_sha256(manifest_data)
        ):
            raise ValueError("Seal manifest does not match authentic on-disk publication manifest")

    return canonical


def verify_pdf_cache_object_disk(
    state: WorkerState,
    pdf_sha256: str,
    *,
    pdf_cache: PDFCache | None = None,
    objects_root: Path | None = None,
) -> bool:
    """Verify that a content-addressed PDF object exists on disk and passes byte validation."""
    if pdf_cache is not None:
        try:
            path = pdf_cache.object_path(pdf_sha256)
            if not path.is_file() or path.is_symlink():
                return False
            _validate_regular_pdf(path, expected_sha256=pdf_sha256, cache_leaf=True)
            return True
        except Exception:
            return False

    root = objects_root
    if root is None and hasattr(state, "path") and state.path is not None:
        candidate_root = state.path.parent / "objects" / "sha256"
        if candidate_root.is_dir():
            root = candidate_root

    if root is not None:
        try:
            path = root / pdf_sha256[:2] / pdf_sha256
            if not path.is_file() or path.is_symlink():
                return False
            _validate_regular_pdf(path, expected_sha256=pdf_sha256, cache_leaf=True)
            return True
        except Exception:
            return False

    return False


def analyze_unresolved_entry(
    state: WorkerState,
    entry: Mapping[str, Any],
    *,
    known_sources: Mapping[str, SourceRecord] | None = None,
    selected_issuers: set[str] | None = None,
    successful_issuers: set[str] | None = None,
    current_discovery: Mapping[str, set[str]] | None = None,
    current_acquired_products: Mapping[str, set[str]] | None = None,
    current_processed_products: Mapping[str, set[str]] | None = None,
    current_failed_products: Mapping[str, set[str]] | None = None,
    current_unsupported_products: Mapping[str, set[str]] | None = None,
    pdf_cache: PDFCache | None = None,
    objects_root: Path | None = None,
    phase: str = "run",
) -> UnresolvedInventoryItem:
    source_id = str(entry["source_id"])
    pdf_sha256 = str(entry["pdf_sha256"])
    raw_reasons = entry.get("reason_codes", ())
    reason_codes = tuple(sorted(str(r) for r in raw_reasons))

    # Inspect SQLite pdf_cache_source
    source_row = state.connection.execute(
        """SELECT issuer, product_code, document_type, superseded_at, superseded_by_source_id
           FROM pdf_cache_source WHERE source_id=?""",
        (source_id,),
    ).fetchone()

    source_exists = source_row is not None
    issuer: str | None = str(source_row["issuer"]) if source_row else None
    product_code: str | None = str(source_row["product_code"]) if source_row else None
    document_type: str | None = str(source_row["document_type"]) if source_row else None
    source_superseded = bool(source_row and source_row["superseded_at"] is not None)

    # Inspect known_sources
    record = known_sources.get(source_id) if known_sources is not None else None
    known_source_exists = record is not None
    if record is not None:
        if issuer is None:
            issuer = record.issuer
        if product_code is None:
            product_code = record.product_code
        if document_type is None:
            document_type = record.document_type

    # Inspect SQLite pdf_cache_source_revision
    rev_row = state.connection.execute(
        """SELECT revision_id, superseded_at, previous_revision_id
           FROM pdf_cache_source_revision WHERE source_id=? AND pdf_sha256=?""",
        (source_id, pdf_sha256),
    ).fetchone()
    revision_id = int(rev_row["revision_id"]) if rev_row else None
    revision_superseded = bool(rev_row and rev_row["superseded_at"] is not None)
    is_current_in_cache = bool(rev_row and not revision_superseded and not source_superseded)

    # Inspect SQLite pdf_cache_object row
    obj_row = state.connection.execute(
        "SELECT 1 FROM pdf_cache_object WHERE pdf_sha256=?",
        (pdf_sha256,),
    ).fetchone()
    pdf_cache_object_exists = obj_row is not None

    # Disk validation for PDF object
    pdf_cache_object_disk_valid = verify_pdf_cache_object_disk(
        state,
        pdf_sha256,
        pdf_cache=pdf_cache,
        objects_root=objects_root,
    )

    # Current discovery status
    product_in_current_discovery: bool | None = None
    if current_discovery is not None and issuer is not None and issuer in current_discovery:
        product_in_current_discovery = bool(product_code and product_code in current_discovery[issuer])

    is_current_failed = bool(
        current_failed_products
        and issuer
        and product_code
        and product_code in current_failed_products.get(issuer, set())
    )
    is_current_unsupported = bool(
        current_unsupported_products
        and issuer
        and product_code
        and product_code in current_unsupported_products.get(issuer, set())
    )

    if current_processed_products is not None:
        is_product_processed = bool(
            current_processed_products
            and issuer
            and product_code
            and product_code in current_processed_products.get(issuer, set())
        )
    else:
        is_product_processed = None

    if current_acquired_products is not None:
        is_product_acquired = bool(
            issuer and product_code and product_code in current_acquired_products.get(issuer, set())
        )
    else:
        is_product_acquired = bool(product_in_current_discovery and not is_current_failed)

    is_selected = selected_issuers is None or (issuer is not None and issuer in selected_issuers)

    # Determine disposition
    if not source_exists and not known_source_exists:
        # Fabricated or untracked source identity with no basis in DB or snapshots
        disposition = "current_error"
        explanation = "Unverified source identity not found in database or known sources; error preserved"
    elif is_current_in_cache and not pdf_cache_object_disk_valid:
        # Active document in cache whose disk object is missing or corrupted is a persistent current error
        disposition = "current_error"
        explanation = "Active current cache revision object is missing or invalid on disk; error preserved"
    elif not is_selected:
        # Non-selected issuer during scoped run: exclude past ledger without claiming product extinction
        disposition = "past_excluded"
        explanation = (
            "Non-selected issuer historical unresolved identity excluded from active publication during "
            "scoped run; preserves serving documents without claiming product extinction"
        )
    elif successful_issuers is not None and issuer is not None and issuer not in successful_issuers:
        # Selected issuer whose collection failed cannot have past identities safely excluded or updated
        disposition = "current_error"
        explanation = "Selected issuer collection failed or incomplete; cannot determine current product status, error preserved"
    elif is_current_failed:
        disposition = "current_error"
        explanation = "Product failed in current collection/processing; error preserved"
    elif is_current_unsupported:
        disposition = "current_error"
        explanation = (
            "Product is unsupported (DRM/ZIP) in current collection; degraded status, error preserved"
        )
    elif is_product_processed is True:
        disposition = "updated_to_latest"
        explanation = "Product active and verified post-OCR in current run; historical revision excluded in favor of verified current contract"
    elif current_discovery is None:
        if rev_row is None:
            # Source exists in DB or known_sources, but no revision in DB links this source_id with this pdf_sha256
            disposition = "current_error"
            explanation = (
                "Unverified revision identity not found in database source revisions; error preserved"
            )
        elif is_current_in_cache:
            # Active current revision without crawler discovery evidence cannot be excluded or updated
            disposition = "current_error"
            explanation = (
                "Active current cache revision cannot be excluded without crawler discovery verification"
            )
        else:
            # Historical superseded revision verified in database cache
            disposition = "past_excluded"
            explanation = "Historical superseded revision excluded from active publication"
    elif product_in_current_discovery is False:
        disposition = "past_excluded"
        explanation = (
            "Historical revision product absent from verified current crawler discovery; "
            "excluded from active publication"
        )
    elif product_in_current_discovery is True:
        if is_current_failed or is_current_unsupported:
            disposition = "current_error"
            explanation = "Product discovered but failed or unsupported in current run; error preserved"
        elif is_product_processed is not None:
            if is_product_processed:
                disposition = "updated_to_latest"
                explanation = "Product active and verified post-OCR in current run; historical revision excluded in favor of verified current contract"
            else:
                disposition = "current_error"
                explanation = (
                    "Product discovered but not successfully processed/verified post-OCR; error preserved"
                )
        elif is_product_acquired:
            if phase in ("expansion", "collect"):
                disposition = "provisional_acquired"
                explanation = "Product discovered and downloaded, pending post-OCR verification"
            else:
                disposition = "updated_to_latest"
                explanation = "Product active and acquired in current run; historical revision excluded in favor of verified current contract"
        else:
            disposition = "current_error"
            explanation = "Product discovered but not successfully acquired; error preserved"
    else:
        disposition = "past_excluded"
        explanation = "Unresolved historical identity excluded from active publication"

    return UnresolvedInventoryItem(
        source_id=source_id,
        pdf_sha256=pdf_sha256,
        reason_codes=reason_codes,
        issuer=issuer,
        product_code=product_code,
        document_type=document_type,
        source_exists=source_exists,
        known_source_exists=known_source_exists,
        revision_id=revision_id,
        pdf_cache_object_exists=pdf_cache_object_exists,
        pdf_cache_object_disk_valid=pdf_cache_object_disk_valid,
        is_current_in_cache=is_current_in_cache,
        product_in_current_discovery=product_in_current_discovery,
        disposition=disposition,
        explanation=explanation,
    )


def plan_unresolved_remediation(
    state: WorkerState,
    seal_data: Mapping[str, Any],
    *,
    known_sources: Mapping[str, SourceRecord] | None = None,
    selected_issuers: set[str] | None = None,
    successful_issuers: set[str] | None = None,
    current_discovery: Mapping[str, set[str]] | None = None,
    current_acquired_products: Mapping[str, set[str]] | None = None,
    current_processed_products: Mapping[str, set[str]] | None = None,
    current_failed_products: Mapping[str, set[str]] | None = None,
    current_unsupported_products: Mapping[str, set[str]] | None = None,
    pdf_cache: PDFCache | None = None,
    objects_root: Path | None = None,
    require_verified_run: bool = False,
    require_verified_serving: bool = False,
    current_serving_generation_id: str | None = None,
    phase: str = "run",
) -> UnresolvedRemediationPlan:
    """Validate baseline seal and plan remediation dispositions for unresolved identities."""
    canonical_entries = validate_unresolved_ledger_in_seal(
        seal_data,
        state=state,
        require_verified_run=require_verified_run,
        require_verified_serving=require_verified_serving,
        current_serving_generation_id=current_serving_generation_id,
    )
    items = tuple(
        analyze_unresolved_entry(
            state,
            entry,
            known_sources=known_sources,
            selected_issuers=selected_issuers,
            successful_issuers=successful_issuers,
            current_discovery=current_discovery,
            current_acquired_products=current_acquired_products,
            current_processed_products=current_processed_products,
            current_failed_products=current_failed_products,
            current_unsupported_products=current_unsupported_products,
            pdf_cache=pdf_cache,
            objects_root=objects_root,
            phase=phase,
        )
        for entry in canonical_entries
    )

    disposition_counts = Counter(item.disposition for item in items)
    issuer_counts = Counter(item.issuer or "unknown" for item in items)
    reason_counts = Counter(r for item in items for r in item.reason_codes)

    summary = {
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "issuer_counts": dict(sorted(issuer_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "total_identities": len(items),
    }

    return UnresolvedRemediationPlan(
        baseline_generation_id=seal_data.get("generation_id"),
        baseline_run_id=seal_data.get("run_id"),
        total_identities=len(items),
        items=items,
        summary=summary,
    )


def apply_unresolved_remediation(
    state: WorkerState,
    plan: UnresolvedRemediationPlan,
    *,
    only_dispositions: tuple[str, ...] | None = None,
) -> int:
    """Persist remediation dispositions to state database."""
    applied = 0
    now = datetime.now(UTC).isoformat()
    with state.transaction():
        for item in plan.items:
            if only_dispositions is not None and item.disposition not in only_dispositions:
                continue
            for reason in item.reason_codes:
                if item.disposition in ("updated_to_latest", "past_excluded"):
                    state.record_remediated_revision(
                        source_id=item.source_id,
                        pdf_sha256=item.pdf_sha256,
                        reason_code=reason,
                        disposition=item.disposition,
                        remediated_at=now,
                    )
                    applied += 1
                elif item.disposition == "current_error":
                    # Update to current_error so is_revision_remediated returns False
                    state.record_remediated_revision(
                        source_id=item.source_id,
                        pdf_sha256=item.pdf_sha256,
                        reason_code=reason,
                        disposition="current_error",
                        remediated_at=now,
                    )
    return applied


def write_remediation_inventory_artifact(
    run_dir: Path,
    plan: UnresolvedRemediationPlan,
    *,
    phase: str,
    run_id: str,
) -> Path:
    """Write remediation inventory audit artifact to run directory."""
    payload = {
        "schema_version": "cardrag.remediation-inventory.v1",
        "run_id": run_id,
        "phase": phase,
        "created_at": datetime.now(UTC).isoformat(),
        "baseline_generation_id": plan.baseline_generation_id,
        "baseline_run_id": plan.baseline_run_id,
        "total_identities": plan.total_identities,
        "summary": plan.summary,
        "items": [item.to_dict() for item in plan.items],
    }
    raw = canonical_json_bytes(payload)

    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / "remediation-inventory.json"
    temp_report = reports_dir / f".tmp.{report_file.name}.{datetime.now(UTC).timestamp()}"
    temp_report.write_bytes(raw)
    temp_report.replace(report_file)

    root_file = run_dir / "remediation-inventory.json"
    temp_root = run_dir / f".tmp.{root_file.name}.{datetime.now(UTC).timestamp()}"
    temp_root.write_bytes(raw)
    temp_root.replace(root_file)

    return report_file
