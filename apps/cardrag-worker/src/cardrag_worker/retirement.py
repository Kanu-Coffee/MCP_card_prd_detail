"""Justified lineage retirement ledger and policy engine (FIX_03).

Documents that leave the corpus because an issuer permanently delisted a
product must be distinguishable from silent data loss.  A disappearing
document may be classified as *retired* only when every guard is satisfied:

* durable evidence is intact (source row, revision, CAS object verified on
  disk, OCR resolvable through the seed ledger or the prior successful run);
* neither the lineage nor any of its successors is present in the current
  canonical discovery;
* absence persisted for the configured grace (consecutive verified-gate runs
  or calendar days), tracked in a durable ``retirement_candidates`` ledger;
* the run stays inside both caps (absolute per-run count and share of the
  baseline corpus).

Everything else remains ``missing_unjustified`` and fails the run closed.
Retirement is classification, never deletion: CAS objects, OCR artifacts,
lineage rows, and remote generations stay available, and a re-discovered
lineage closes its record as ``reinstated``.  The ledger is sealed with the
same content-addressed, tmp->rename 0600 pattern as the corpus baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .state import WorkerState

from cardrag_core.canonical import canonical_json_bytes

RETIREMENT_LEDGER_SCHEMA_VERSION = "cardrag.retirement-ledger.v1"
RETIREMENT_MAX_RATIO = 0.005
RETIREMENT_STATUSES = frozenset({"candidate", "retired", "reinstated"})

_RETIREMENT_DIRECTORY = Path("audit-reports/retirements")
_POINTER_NAME = "latest"
_MAX_LEDGER_BYTES = 32 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

type LineageKey = tuple[str, str, str]


class RetirementError(RuntimeError):
    """A bounded error code safe to expose in release audit output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RetirementEntry:
    document_id: str
    source_id: str
    issuer: str
    product_code: str
    document_type: str
    pdf_sha256: str
    ocr_sha256: str | None
    status: Literal["candidate", "retired", "reinstated"]
    first_absent_run_id: str
    first_absent_at: str
    last_observed_run_id: str
    last_observed_at: str
    last_checked_run_id: str
    consecutive_absences: int
    retired_run_id: str | None = None
    retired_at: str | None = None
    reinstated_run_id: str | None = None
    reinstated_at: str | None = None
    decision_inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def lineage_key(self) -> LineageKey:
        return (self.issuer, self.product_code, self.document_type)


@dataclass(frozen=True, slots=True)
class RetirementLedger:
    updated_run_id: str
    entries: tuple[RetirementEntry, ...]
    ledger_sha256: str

    def open_by_document(self) -> dict[str, RetirementEntry]:
        return {e.document_id: e for e in self.entries if e.status in {"candidate", "retired"}}


@dataclass(frozen=True, slots=True)
class AbsentDocument:
    document_id: str
    source_id: str
    issuer: str
    product_code: str
    document_type: str
    pdf_sha256: str
    ocr_sha256: str | None
    last_observed_run_id: str
    last_observed_at: str

    @property
    def lineage_key(self) -> LineageKey:
        return (self.issuer, self.product_code, self.document_type)


@dataclass(frozen=True, slots=True)
class RetirementPolicy:
    grace_runs: int
    grace_days: int
    max_per_run: int
    max_ratio: float

    def __post_init__(self) -> None:
        if self.grace_runs < 2:
            raise RetirementError("retirement_grace_runs_minimum")
        if (
            isinstance(self.grace_days, bool)
            or self.grace_days < 1
            or isinstance(self.max_per_run, bool)
            or self.max_per_run < 1
            or not 0 < self.max_ratio <= 0.05
        ):
            raise RetirementError("retirement_policy_invalid")


@dataclass(frozen=True, slots=True)
class RetirementOutcome:
    retired: tuple[dict[str, Any], ...]
    candidates: tuple[dict[str, Any], ...]
    unjustified: dict[str, str]
    ledger: RetirementLedger
    reinstated: tuple[str, ...]


def entry_payload(entry: RetirementEntry) -> dict[str, Any]:
    return {
        "consecutive_absences": entry.consecutive_absences,
        "decision_inputs": entry.decision_inputs,
        "document_id": entry.document_id,
        "document_type": entry.document_type,
        "first_absent_at": entry.first_absent_at,
        "first_absent_run_id": entry.first_absent_run_id,
        "issuer": entry.issuer,
        "last_checked_run_id": entry.last_checked_run_id,
        "last_observed_at": entry.last_observed_at,
        "last_observed_run_id": entry.last_observed_run_id,
        "ocr_sha256": entry.ocr_sha256,
        "pdf_sha256": entry.pdf_sha256,
        "product_code": entry.product_code,
        "reinstated_at": entry.reinstated_at,
        "reinstated_run_id": entry.reinstated_run_id,
        "retired_at": entry.retired_at,
        "retired_run_id": entry.retired_run_id,
        "source_id": entry.source_id,
        "status": entry.status,
    }


def _entry_from_payload(raw: Any) -> RetirementEntry:
    if not isinstance(raw, dict):
        raise RetirementError("retirement_ledger_invalid")
    try:
        status = str(raw["status"])
        if status not in RETIREMENT_STATUSES:
            raise RetirementError("retirement_ledger_invalid")
        return RetirementEntry(
            document_id=str(raw["document_id"]),
            source_id=str(raw["source_id"]),
            issuer=str(raw["issuer"]),
            product_code=str(raw["product_code"]),
            document_type=str(raw["document_type"]),
            pdf_sha256=str(raw["pdf_sha256"]),
            ocr_sha256=None if raw.get("ocr_sha256") is None else str(raw["ocr_sha256"]),
            status=status,  # type: ignore[arg-type]
            first_absent_run_id=str(raw["first_absent_run_id"]),
            first_absent_at=str(raw["first_absent_at"]),
            last_observed_run_id=str(raw["last_observed_run_id"]),
            last_observed_at=str(raw["last_observed_at"]),
            last_checked_run_id=str(raw["last_checked_run_id"]),
            consecutive_absences=int(raw["consecutive_absences"]),
            retired_run_id=None if raw.get("retired_run_id") is None else str(raw["retired_run_id"]),
            retired_at=None if raw.get("retired_at") is None else str(raw["retired_at"]),
            reinstated_run_id=(
                None if raw.get("reinstated_run_id") is None else str(raw["reinstated_run_id"])
            ),
            reinstated_at=None if raw.get("reinstated_at") is None else str(raw["reinstated_at"]),
            decision_inputs=dict(raw.get("decision_inputs") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RetirementError("retirement_ledger_invalid") from exc


def ledger_payload(ledger: RetirementLedger) -> dict[str, Any]:
    return {
        "entries": [entry_payload(entry) for entry in sorted(ledger.entries, key=lambda e: (e.document_id,))],
        "schema_version": RETIREMENT_LEDGER_SCHEMA_VERSION,
        "updated_run_id": ledger.updated_run_id,
    }


def ledger_bytes(ledger: RetirementLedger) -> bytes:
    return canonical_json_bytes(ledger_payload(ledger))


def _secure_directory(state_dir: Path) -> Path:
    current = Path(os.path.abspath(state_dir))
    for part in _RETIREMENT_DIRECTORY.parts:
        current = current / part
        try:
            os.mkdir(current, mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RetirementError("retirement_directory_creation_failed") from exc
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise RetirementError("unsafe_retirement_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RetirementError("unsafe_retirement_path")
    return current


def _atomic_write(
    directory: Path, final_name: str, payload_bytes: bytes, *, mode: int = 0o600, overwrite: bool = False
) -> None:
    temp_name = f".{final_name}.{uuid.uuid4().hex}.tmp"
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    temp_fd = -1
    try:
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        temp_fd = os.open(temp_name, flags, mode, dir_fd=dir_fd)
        view = memoryview(payload_bytes)
        while view:
            written = os.write(temp_fd, view)
            if written < 1:
                raise OSError("retirement ledger write stalled")
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        if overwrite:
            with suppress(FileNotFoundError):
                os.unlink(final_name, dir_fd=dir_fd)
            os.link(temp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            os.unlink(temp_name, dir_fd=dir_fd)
            os.fsync(dir_fd)
            return
        try:
            os.link(temp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            existing = os.stat(final_name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                stat.S_ISLNK(existing.st_mode)
                or not stat.S_ISREG(existing.st_mode)
                or existing.st_size != len(payload_bytes)
            ):
                raise RetirementError("retirement_ledger_conflict") from None
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as exc:
        raise RetirementError("retirement_ledger_write_failed") from exc
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=dir_fd)
        os.close(dir_fd)


def write_retirement_ledger(state_dir: Path, ledger: RetirementLedger) -> Path:
    """Seal one content-addressed ledger version and move the pointer last."""

    payload_bytes = ledger_bytes(ledger)
    if len(payload_bytes) > _MAX_LEDGER_BYTES:
        raise RetirementError("retirement_ledger_limit_exceeded")
    version_sha = hashlib.sha256(payload_bytes).hexdigest()
    directory = _secure_directory(state_dir)
    _atomic_write(directory, f"{version_sha}.json", payload_bytes)
    _atomic_write(directory, _POINTER_NAME, (version_sha + "\n").encode("ascii"), overwrite=True)
    return directory / f"{version_sha}.json"


def _parse_ledger(content: bytes) -> RetirementLedger:
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RetirementError("retirement_ledger_invalid") from None
    if not isinstance(raw, dict) or raw.get("schema_version") != RETIREMENT_LEDGER_SCHEMA_VERSION:
        raise RetirementError("retirement_ledger_invalid")
    entries = tuple(_entry_from_payload(item) for item in raw.get("entries") or [])
    if len({entry.document_id for entry in entries}) != len(entries):
        raise RetirementError("retirement_ledger_invalid")
    for entry in entries:
        if entry.status == "retired" and not entry.decision_inputs:
            raise RetirementError("retirement_ledger_invalid")
    return RetirementLedger(
        updated_run_id=str(raw.get("updated_run_id") or ""),
        entries=entries,
        ledger_sha256="",
    )


def load_retirement_ledger(
    state_dir: Path,
    state: WorkerState | None = None,
) -> RetirementLedger | None:
    """Load the pointed ledger version; a missing ledger family yields None."""

    directory = Path(os.path.abspath(state_dir)) / _RETIREMENT_DIRECTORY
    if not directory.exists():
        return None
    if directory.is_symlink() or not directory.is_dir():
        raise RetirementError("unsafe_retirement_path")
    pointer_path = directory / _POINTER_NAME
    names = [
        name for name in os.listdir(directory) if name.endswith(".json") and _SHA256.fullmatch(name[:-5])
    ]
    if not names:
        return None

    def _try_load_version(v_path: Path) -> RetirementLedger | None:
        content = v_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != v_path.name[:-5]:
            raise RetirementError("retirement_ledger_invalid")
        ledger = _parse_ledger(content)
        if state is not None and ledger.updated_run_id:
            status = state.run_status(ledger.updated_run_id)
            if status not in ("succeeded", "no_change"):
                return None
        return RetirementLedger(
            updated_run_id=ledger.updated_run_id,
            entries=ledger.entries,
            ledger_sha256=v_path.name[:-5],
        )

    if pointer_path.exists():
        if pointer_path.is_symlink() or not pointer_path.is_file():
            raise RetirementError("retirement_ledger_invalid")
        pointer = pointer_path.read_text(encoding="ascii", errors="strict").strip()
        if not _SHA256.fullmatch(pointer):
            raise RetirementError("retirement_ledger_invalid")
        if pointer not in [name[:-5] for name in names]:
            raise RetirementError("retirement_ledger_invalid")
        pointed_version = directory / f"{pointer}.json"
        loaded = _try_load_version(pointed_version)
        if loaded is not None:
            return loaded

    # Crash window or unconfirmed pointer: fallback to newest version verified by state.
    sorted_names = sorted(
        names,
        key=lambda name: (directory / name).lstat().st_mtime_ns,
        reverse=True,
    )
    for name in sorted_names:
        loaded = _try_load_version(directory / name)
        if loaded is not None:
            return loaded

    return None


def prune_retirement_ledgers(state_dir: Path, *, keep: int) -> int:
    """Keep the newest *keep* sealed versions; the pointer file is never pruned."""

    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise RetirementError("retirement_keep_invalid")
    directory = Path(os.path.abspath(state_dir)) / _RETIREMENT_DIRECTORY
    try:
        names = [
            name for name in os.listdir(directory) if name.endswith(".json") and _SHA256.fullmatch(name[:-5])
        ]
    except FileNotFoundError:
        return 0
    pointer_target: str | None = None
    pointer_path = directory / _POINTER_NAME
    if pointer_path.is_file():
        with suppress(ValueError, UnicodeDecodeError):
            pointer_target = pointer_path.read_text(encoding="ascii").strip()
    entries = sorted(
        ((directory / name).lstat().st_mtime_ns, name) for name in names if (directory / name).lstat().st_mode
    )
    entries.sort(key=lambda item: (-item[0], item[1]))
    kept: list[str] = []
    if pointer_target is not None:
        kept.append(f"{pointer_target}.json")
    for _, name in entries:
        if len(kept) >= keep:
            break
        if name not in kept:
            kept.append(name)
    removed = 0
    for _, name in entries:
        if name in kept:
            continue
        try:
            os.unlink(directory / name)
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def evaluate_retirements(
    *,
    run_id: str,
    run_started_at: datetime,
    policy: RetirementPolicy,
    baseline_total: int,
    ledger: RetirementLedger | None,
    absent: tuple[AbsentDocument, ...],
    durable_ok: dict[str, bool],
    lineage_absent: dict[str, bool],
    discovered_lineages: set[LineageKey],
) -> RetirementOutcome:
    """Pure policy pass: classify each absent document and produce the new ledger.

    ``durable_ok`` and ``lineage_absent`` are per-document evidence verdicts
    supplied by the pipeline (CAS hash re-verified, source/revision rows,
    OCR resolvable; lineage and successors absent from canonical discovery).
    """

    run_stamp = run_started_at.astimezone(UTC).isoformat()
    previous = {entry.document_id: entry for entry in (ledger.entries if ledger else ())}
    updated: dict[str, RetirementEntry] = dict(previous)
    retired: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    unjustified: dict[str, str] = {}

    ordered = sorted(absent, key=lambda item: (item.issuer, item.product_code, item.document_id))
    new_judgements = 0
    grace_runs_cap = policy.max_per_run
    ratio_cap = int(baseline_total * policy.max_ratio)
    effective_cap = max(1, min(grace_runs_cap, ratio_cap if baseline_total > 0 else grace_runs_cap))

    for item in ordered:
        prior = previous.get(item.document_id)
        if prior is not None and prior.status == "reinstated":
            # Re-discovery then disappearance restarts grace continuity.
            prior = None
        if not durable_ok.get(item.document_id, False) or not lineage_absent.get(item.document_id, False):
            unjustified[item.document_id] = "retirement_evidence_incomplete"
            continue
        if prior is not None and prior.status == "retired":
            retired.append(_retired_record(prior, already=True))
            continue
        if prior is not None and prior.last_checked_run_id == run_id:
            # Resuming the same run must be idempotent; do not increment absences.
            absences = prior.consecutive_absences
        else:
            absences = (prior.consecutive_absences + 1) if prior is not None else 1
        first_run = prior.first_absent_run_id if prior is not None else run_id
        first_at = prior.first_absent_at if prior is not None else run_stamp
        days = _elapsed_days(first_at, run_stamp)
        # Retirement requires at least 2 distinct completed qualifying runs (consecutive_absences >= 2).
        # Elapsed days alone cannot substitute for the minimum 2 runs requirement.
        meets_grace = absences >= policy.grace_runs and absences >= 2
        if new_judgements >= effective_cap:
            unjustified[item.document_id] = "retirement_cap_exceeded"
            continue
        new_judgements += 1
        entry = RetirementEntry(
            document_id=item.document_id,
            source_id=item.source_id,
            issuer=item.issuer,
            product_code=item.product_code,
            document_type=item.document_type,
            pdf_sha256=item.pdf_sha256,
            ocr_sha256=item.ocr_sha256,
            status="retired" if meets_grace else "candidate",
            first_absent_run_id=first_run,
            first_absent_at=first_at,
            last_observed_run_id=item.last_observed_run_id,
            last_observed_at=item.last_observed_at,
            last_checked_run_id=run_id,
            consecutive_absences=absences,
            retired_run_id=run_id if meets_grace else None,
            retired_at=run_stamp if meets_grace else None,
            decision_inputs=(
                {
                    "baseline_total": baseline_total,
                    "consecutive_absences": absences,
                    "elapsed_days": days,
                    "grace_days": policy.grace_days,
                    "grace_runs": policy.grace_runs,
                    "max_per_run": policy.max_per_run,
                    "max_ratio": policy.max_ratio,
                }
                if meets_grace
                else {}
            ),
        )
        updated[entry.document_id] = entry
        if meets_grace:
            retired.append(_retired_record(entry, already=False))
        else:
            candidates.append(_candidate_record(entry))

    reinstated: list[str] = []
    for document_id, entry in previous.items():
        if entry.status in {"candidate", "retired"} and entry.lineage_key in discovered_lineages:
            replacement = replace(
                entry,
                status="reinstated",
                last_checked_run_id=run_id,
                reinstated_run_id=run_id,
                reinstated_at=run_stamp,
            )
            updated[document_id] = replacement
            reinstated.append(document_id)

    new_ledger = RetirementLedger(
        updated_run_id=run_id,
        entries=tuple(updated.values()),
        ledger_sha256="",
    )
    return RetirementOutcome(
        retired=tuple(retired),
        candidates=tuple(candidates),
        unjustified=unjustified,
        ledger=new_ledger,
        reinstated=tuple(sorted(reinstated)),
    )


def _retired_record(entry: RetirementEntry, *, already: bool) -> dict[str, Any]:
    return {
        "consecutive_absences": entry.consecutive_absences,
        "document_id": entry.document_id,
        "document_type": entry.document_type,
        "first_absent_run_id": entry.first_absent_run_id,
        "issuer": entry.issuer,
        "last_observed_run_id": entry.last_observed_run_id,
        "ocr_sha256": entry.ocr_sha256,
        "pdf_sha256": entry.pdf_sha256,
        "product_code": entry.product_code,
        "retired_run_id": entry.retired_run_id,
        "revalidated": already,
        "source_id": entry.source_id,
    }


def _candidate_record(entry: RetirementEntry) -> dict[str, Any]:
    return {
        "consecutive_absences": entry.consecutive_absences,
        "document_id": entry.document_id,
        "document_type": entry.document_type,
        "first_absent_run_id": entry.first_absent_run_id,
        "issuer": entry.issuer,
        "last_observed_run_id": entry.last_observed_run_id,
        "pdf_sha256": entry.pdf_sha256,
        "product_code": entry.product_code,
        "source_id": entry.source_id,
    }


def _elapsed_days(first_at: str, now_at: str) -> int:
    try:
        first = datetime.fromisoformat(first_at)
        now = datetime.fromisoformat(now_at)
    except ValueError:
        return 0
    return max(0, (now - first).days)
