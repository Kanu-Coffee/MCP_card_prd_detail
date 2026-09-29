"""Rolling corpus baseline records for the daily-batch disappearance gate.

FIX_03: the corpus-diff gate must compare each run against the *previous
successful generation*, not a permanently pinned v1.0.28 seed ledger.  A
baseline is a canonical, content-addressed record written immediately after a
successful sealed publication under ``audit-reports/corpus-baseline/`` using
the same tmp->rename 0600 discipline as the state-seed and embedding-seed
ledgers.  Loading verifies (a) the file name equals its content SHA-256,
(b) the schema and internal counts, and (c) that the bound run is ``succeeded``
and the bound generation is ``ready`` in the state database; any mismatch
fails closed.  Pruning happens only in the existing cleanup stage and never
removes the newest record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cardrag_core.canonical import canonical_json_bytes

from .state import WorkerState

CORPUS_BASELINE_SCHEMA_VERSION = "cardrag.corpus-baseline.v1"

_BASELINE_DIRECTORY = Path("audit-reports/corpus-baseline")
_MAX_BASELINE_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class CorpusBaselineError(RuntimeError):
    """A bounded error code safe to expose in release audit output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CorpusBaselineDocument:
    document_id: str
    source_id: str
    issuer: str
    product_code: str
    document_type: str
    temporal_status: str
    pdf_sha256: str


@dataclass(frozen=True, slots=True)
class CorpusBaseline:
    generation_id: str
    run_id: str
    corpus_sha256: str
    contract_sha256: str
    documents: tuple[CorpusBaselineDocument, ...]
    issuer_counts: dict[str, int]
    baseline_sha256: str

    @property
    def current_doc_ids(self) -> frozenset[str]:
        return frozenset(d.document_id for d in self.documents if d.temporal_status == "current")

    @property
    def historical_doc_ids(self) -> frozenset[str]:
        return frozenset(d.document_id for d in self.documents if d.temporal_status == "historical")


def _secure_ledger_directory(state_dir: Path) -> Path:
    root = Path(os.path.abspath(state_dir))
    current = root
    for part in _BASELINE_DIRECTORY.parts:
        current = current / part
        try:
            os.mkdir(current, mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise CorpusBaselineError("baseline_directory_creation_failed") from exc
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise CorpusBaselineError("unsafe_baseline_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise CorpusBaselineError("unsafe_baseline_path")
    return current


def _validate_inputs(
    *,
    generation_id: str,
    run_id: str,
    corpus_sha256: str,
    contract_sha256: str,
    documents: Sequence[CorpusBaselineDocument],
    issuer_counts: Mapping[str, int],
) -> None:
    for value, field in (
        (generation_id, "generation_id"),
        (run_id, "run_id"),
        (corpus_sha256, "corpus_sha256"),
        (contract_sha256, "contract_sha256"),
    ):
        if not isinstance(value, str) or not value or len(value) > 256:
            raise CorpusBaselineError(f"baseline_{field}_invalid")
    if not _SHA256.fullmatch(corpus_sha256) or not _SHA256.fullmatch(contract_sha256):
        raise CorpusBaselineError("baseline_sha256_invalid")
    seen: set[str] = set()
    for document in documents:
        if document.document_id in seen:
            raise CorpusBaselineError("baseline_duplicate_document")
        seen.add(document.document_id)
        if not _ID.fullmatch(document.document_id) or not _ID.fullmatch(document.source_id):
            raise CorpusBaselineError("baseline_identity_invalid")
        if document.temporal_status not in {"current", "historical"}:
            raise CorpusBaselineError("baseline_temporal_status_invalid")
        if not _SHA256.fullmatch(document.pdf_sha256):
            raise CorpusBaselineError("baseline_pdf_sha_invalid")
    for issuer, count in issuer_counts.items():
        if not _ID.fullmatch(issuer) or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise CorpusBaselineError("baseline_issuer_counts_invalid")


def baseline_payload(
    *,
    generation_id: str,
    run_id: str,
    corpus_sha256: str,
    contract_sha256: str,
    documents: Sequence[CorpusBaselineDocument],
    issuer_counts: Mapping[str, int],
) -> dict[str, Any]:
    ordered = sorted(documents, key=lambda d: d.document_id)
    return {
    "contract_sha256": contract_sha256,
    "corpus_sha256": corpus_sha256,
    "counts": {
        "current": sum(1 for d in ordered if d.temporal_status == "current"),
        "historical": sum(1 for d in ordered if d.temporal_status == "historical"),
        "total": len(ordered),
    },
    "documents": [
        {
            "document_id": d.document_id,
            "document_type": d.document_type,
            "issuer": d.issuer,
            "pdf_sha256": d.pdf_sha256,
            "product_code": d.product_code,
            "source_id": d.source_id,
            "temporal_status": d.temporal_status,
        }
        for d in ordered
    ],
    "generation_id": generation_id,
    "issuer_counts": dict(sorted(issuer_counts.items())),
    "run_id": run_id,
    "schema_version": CORPUS_BASELINE_SCHEMA_VERSION,
    }


def record_corpus_baseline(
    state_dir: Path,
    *,
    generation_id: str,
    run_id: str,
    corpus_sha256: str,
    contract_sha256: str,
    documents: Sequence[CorpusBaselineDocument],
    issuer_counts: Mapping[str, int],
) -> Path:
    """Atomically seal one content-addressed baseline; re-record is idempotent."""

    _validate_inputs(
        generation_id=generation_id,
        run_id=run_id,
        corpus_sha256=corpus_sha256,
        contract_sha256=contract_sha256,
        documents=documents,
        issuer_counts=issuer_counts,
    )
    payload_bytes = canonical_json_bytes(
        baseline_payload(
            generation_id=generation_id,
            run_id=run_id,
            corpus_sha256=corpus_sha256,
            contract_sha256=contract_sha256,
            documents=documents,
            issuer_counts=issuer_counts,
        )
    )
    if len(payload_bytes) > _MAX_BASELINE_BYTES:
        raise CorpusBaselineError("baseline_limit_exceeded")
    baseline_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    directory = _secure_ledger_directory(state_dir)
    final_name = f"{baseline_sha256}.json"
    temp_name = f".{baseline_sha256}.{uuid.uuid4().hex}.tmp"
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    temp_fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        view = memoryview(payload_bytes)
        while view:
            written = os.write(temp_fd, view)
            if written < 1:
                raise OSError("corpus baseline write stalled")
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        try:
            os.link(temp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            existing = os.stat(final_name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                stat.S_ISLNK(existing.st_mode)
                or not stat.S_ISREG(existing.st_mode)
                or existing.st_size != len(payload_bytes)
            ):
                raise CorpusBaselineError("baseline_conflict") from None
            with os.fdopen(
                os.open(final_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd), "rb"
            ) as handle:
                if hashlib.sha256(handle.read()).hexdigest() != baseline_sha256:
                    raise CorpusBaselineError("baseline_conflict") from None
        os.unlink(temp_name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as exc:
        raise CorpusBaselineError("baseline_write_failed") from exc
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=dir_fd)
        os.close(dir_fd)
    return directory / final_name


def _parse_baseline(content: bytes) -> CorpusBaseline:
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CorpusBaselineError("corpus_baseline_invalid") from None
    if not isinstance(raw, dict) or raw.get("schema_version") != CORPUS_BASELINE_SCHEMA_VERSION:
        raise CorpusBaselineError("corpus_baseline_invalid")
    try:
        documents = tuple(
            CorpusBaselineDocument(
                document_id=str(d["document_id"]),
                source_id=str(d["source_id"]),
                issuer=str(d["issuer"]),
                product_code=str(d["product_code"]),
                document_type=str(d["document_type"]),
                temporal_status=str(d["temporal_status"]),
                pdf_sha256=str(d["pdf_sha256"]),
            )
            for d in raw["documents"]
        )
        issuer_counts = {str(k): int(v) for k, v in raw["issuer_counts"].items()}
        baseline = CorpusBaseline(
            generation_id=str(raw["generation_id"]),
            run_id=str(raw["run_id"]),
            corpus_sha256=str(raw["corpus_sha256"]),
            contract_sha256=str(raw["contract_sha256"]),
            documents=documents,
            issuer_counts=issuer_counts,
            baseline_sha256="",
        )
        counts = raw["counts"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise CorpusBaselineError("corpus_baseline_invalid") from None
    if (
        len({d.document_id for d in documents}) != len(documents)
        or any(
            not _SHA256.fullmatch(d.pdf_sha256) or d.temporal_status not in {"current", "historical"}
            for d in documents
        )
        or baseline.current_doc_ids & baseline.historical_doc_ids
        or counts.get("total") != len(documents)
        or counts.get("current") != sum(1 for d in documents if d.temporal_status == "current")
        or counts.get("historical") != sum(1 for d in documents if d.temporal_status == "historical")
    ):
        raise CorpusBaselineError("corpus_baseline_invalid")
    return baseline


def load_corpus_baseline(state_dir: Path, state: WorkerState) -> CorpusBaseline | None:
    """Load the newest baseline and verify its durable binding; malformed latest fails closed."""

    root = Path(os.path.abspath(state_dir))
    directory = root / _BASELINE_DIRECTORY
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".json") and _SHA256.fullmatch(name[:-5])]
    except FileNotFoundError:
        return None
    if not names:
        return None
    entries = []
    for name in names:
        path = directory / name
        try:
            listed = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(listed.st_mode):
            raise CorpusBaselineError("corpus_baseline_invalid")
        entries.append((listed.st_mtime_ns, name, path))
    if not entries:
        return None
    entries.sort(key=lambda item: (-item[0], item[1]))
    _, name, path = entries[0]
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != name[:-5]:
        raise CorpusBaselineError("corpus_baseline_invalid")
    baseline = _parse_baseline(content)
    if state.run_status(baseline.run_id) != "succeeded":
        raise CorpusBaselineError("corpus_baseline_unbound")
    publish = state.publish_by_generation(baseline.generation_id)
    if (
        publish is None
        or str(publish["status"]) != "ready"
        or str(publish["run_id"]) != baseline.run_id
        or str(publish["corpus_sha256"]) != baseline.corpus_sha256
        or str(publish["contract_sha256"]) != baseline.contract_sha256
    ):
        raise CorpusBaselineError("corpus_baseline_unbound")
    return CorpusBaseline(
        generation_id=baseline.generation_id,
        run_id=baseline.run_id,
        corpus_sha256=baseline.corpus_sha256,
        contract_sha256=baseline.contract_sha256,
        documents=baseline.documents,
        issuer_counts=baseline.issuer_counts,
        baseline_sha256=name[:-5],
    )


def prune_corpus_baselines(state_dir: Path, *, keep: int) -> int:
    """Delete older baselines beyond *keep*, newest first; never the newest."""

    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise CorpusBaselineError("baseline_keep_invalid")
    directory = Path(os.path.abspath(state_dir)) / _BASELINE_DIRECTORY
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".json") and _SHA256.fullmatch(name[:-5])]
    except FileNotFoundError:
        return 0
    entries = []
    for name in names:
        path = directory / name
        try:
            listed = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(listed.st_mode):
            entries.append((listed.st_mtime_ns, name))
    entries.sort(key=lambda item: (-item[0], item[1]))
    removed = 0
    for _, name in entries[keep:]:
        try:
            os.unlink(directory / name)
            removed += 1
        except FileNotFoundError:
            continue
    return removed
