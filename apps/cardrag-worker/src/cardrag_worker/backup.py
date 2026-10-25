"""Incremental WebDAV backup manager, ledger, and CLI operations."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import inspect
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cardrag_core import (
    OCRArtifactManifest,
    OCRReady,
    WebDAVHTTPError,
    canonical_json_bytes,
    object_path,
    ocr_manifest_path,
    ocr_ready_path,
    ocr_variant_manifest_path,
    ocr_variant_ready_path,
    sha256_bytes,
)
from cardrag_core.webdav import WebDAVIntegrityError

from .settings import WorkerSettings
from .webdav import WebDAVClient

logger = logging.getLogger("cardrag_worker.backup")

BACKUP_INDEX_PATH = "v1/backup/index.json"
# The committed backup index grows with the corpus (16,888 items / ~8.6 MiB in
# production on 2026-10-21).  It is a dedicated backup control object, read
# with its own explicit finite bound instead of raising the shared 8 MiB
# control-object default or allowing an unbounded read.
BACKUP_INDEX_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BackupIndexRead:
    """Outcome of one bounded backup-index read with a typed failure cause."""

    state: str  # "present" | "absent" | "unreadable"
    payload: dict[str, Any] | None = None
    error_kind: str | None = None


def canonical_remote_root(settings: Any) -> str:
    """Public accessor for the configured canonical remote-root binding."""
    return _canonical_remote_root(settings)


def _canonical_remote_root(settings: Any) -> str:
    url = str(getattr(settings, "webdav_base_url", "") or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        url = parts._replace(netloc=netloc).geturl()
    return url.rstrip("/")


async def _atomic_replace_backup_index(
    client: Any,
    path: str,
    body: bytes,
) -> None:
    if hasattr(client, "atomic_replace_bytes"):
        await client.atomic_replace_bytes(path, body, content_type="application/json")
        return
    has_move = (hasattr(client, "core") and hasattr(client.core, "move")) or hasattr(client, "move")
    if not has_move:
        if hasattr(client, "put"):
            await client.put(path, body)
        elif hasattr(client, "core") and hasattr(client.core, "put"):
            client.core.put(path, body)
        return

    token = uuid.uuid4().hex
    temp_path = f"v1/.incoming/backup/{token}.tmp"
    if hasattr(client, "put_bytes"):
        await client.put_bytes(temp_path, body, content_type="application/json")
    elif hasattr(client, "put"):
        await client.put(temp_path, body)
    elif hasattr(client, "core") and hasattr(client.core, "put"):
        client.core.put(temp_path, body)

    if hasattr(client, "core") and hasattr(client.core, "move"):
        client.core.move(temp_path, path, overwrite=True)
    elif hasattr(client, "move"):
        await client.move(temp_path, path, overwrite=True)

    with suppress(Exception):
        if hasattr(client, "delete"):
            await client.delete(temp_path, missing_ok=True)
        elif hasattr(client, "core") and hasattr(client.core, "delete"):
            client.core.delete(temp_path, missing_ok=True)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def scoped_backup_result_fields(flush_res: dict[str, Any]) -> dict[str, Any]:
    """Map flush scope diagnostics into run/status payload fields.

    None means "not a scoped flush" and must be preserved: the absence of a
    scope statement is not a completion statement.
    """
    res = {
        "backup_in_scope_remaining": flush_res.get("in_scope_remaining"),
        "backup_in_scope_lost_source": flush_res.get("in_scope_lost_source"),
        "backup_in_scope_conflicts": flush_res.get("in_scope_conflicts"),
        "backup_in_scope_missing": flush_res.get("in_scope_missing"),
        "backup_in_scope_unbound": flush_res.get("in_scope_unbound"),
        "backup_pending_out_of_scope": flush_res.get("pending_out_of_scope"),
        "backup_pending_out_of_scope_bytes": flush_res.get("pending_out_of_scope_bytes"),
        "backup_scope_complete": flush_res.get("backup_scope_complete"),
    }
    if "excluded_shared_cache" in flush_res and flush_res["excluded_shared_cache"] is not None:
        res["backup_excluded_shared_cache"] = flush_res["excluded_shared_cache"]
    if flush_res.get("index_read_error") is not None:
        res["backup_index_read_error"] = flush_res["index_read_error"]
    return res


_LOCAL_CACHE_MANIFEST_MAX_BYTES = 1024 * 1024
_LOCAL_CACHE_READY_MAX_BYTES = 64 * 1024


def acquire_cache_recovery_lock(state_dir: Path) -> int | None:
    """Non-blocking exclusive lock for local OCR cache evidence writes.

    The normal Worker batch writes run-local artifacts, not ``cache/ocr``;
    operator-driven cache materialization (metadata recovery and restore) is
    serialized by this lock.  ``None`` means another writer holds it.
    """
    lock_path = state_dir / "ocr-cache-recovery.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(descriptor)
        return None
    return descriptor


def release_cache_recovery_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    with suppress(OSError):
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    with suppress(OSError):
        os.close(descriptor)


def _read_cache_leaf(path: Path, state_dir: Path, maximum_bytes: int) -> tuple[bytes | None, str | None]:
    """Read a local cache leaf through the pinned no-follow state chain.

    Returns (bytes, None) for a verified regular leaf, (None, None) when absent,
    and (None, reason) for symlinks, foreign-owned/hardlinked/non-regular
    leaves, size violations, or read failures.  An unsafe leaf is never
    silently treated as missing or as matching bytes.
    """
    from .ocr import _read_nofollow_regular

    try:
        return _read_nofollow_regular(path, maximum_bytes=maximum_bytes, state_root=state_dir), None
    except FileNotFoundError:
        return None, None
    except Exception as exc:
        return None, f"{path.name}: {exc}"


def _create_cache_leaf_no_replace(parent_fd: int, name: str, body: bytes) -> str:
    """Create one leaf beneath a pinned directory, never replacing a file.

    Returns ``created``, ``present`` (concurrent writer produced identical
    bytes), ``conflict`` (concurrent writer produced different bytes), or
    ``error:<reason>``.  The temporary file is created with O_NOFOLLOW|O_EXCL
    and published with an atomic no-replace link; the final bytes are re-read
    through the no-follow chain before success is reported.
    """
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                return "error:local cache write made no progress"
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except FileExistsError:
            return "present"
        except OSError as exc:
            return f"error:local cache leaf publish failed: {exc}"
        return "created"
    except OSError as exc:
        return f"error:local cache leaf write failed: {exc}"
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)


def publish_cache_pair(
    state_dir: Path,
    target_dir: Path,
    manifest_body: bytes,
    ready_bytes: bytes,
) -> tuple[str, str | None]:
    """Materialize a verified manifest/READY pair inside the cache boundary.

    Every path component and leaf is opened beneath the worker state root with
    O_NOFOLLOW (via the existing OCR cache helpers), so directory or file
    symlinks are rejected instead of followed.  Existing leaves are compared
    byte-for-byte and never overwritten; a leaf created by a concurrent writer
    between the check and publish is accepted only when its bytes are
    identical.  Newly created leaves are rolled back when the pair cannot be
    completed, and success is reported only after re-reading the final bytes.
    Returns (action, error) with action in ``restored``/``already_present``.
    """
    from .ocr import _open_beneath_directory

    try:
        parent_fd = _open_beneath_directory(target_dir, state_root=state_dir, create=True)
    except Exception as exc:
        return "error", f"local cache directory boundary is unsafe: {exc}"
    entries = (("manifest.json", manifest_body), ("READY.json", ready_bytes))
    created: list[str] = []
    try:
        for name, body in entries:
            existing, read_error = _read_cache_leaf(target_dir / name, state_dir, max(len(body), 1) + 1)
            if read_error is not None:
                for created_name in created:
                    with suppress(FileNotFoundError):
                        os.unlink(created_name, dir_fd=parent_fd)
                with suppress(OSError):
                    os.fsync(parent_fd)
                return "error", f"local cache evidence is unsafe: {read_error}"
            if existing is None:
                outcome = _create_cache_leaf_no_replace(parent_fd, name, body)
                if outcome == "created":
                    created.append(name)
                elif outcome == "present":
                    concurrent, concurrent_error = _read_cache_leaf(
                        target_dir / name, state_dir, max(len(body), 1) + 1
                    )
                    if concurrent_error is not None or concurrent != body:
                        for created_name in created:
                            with suppress(FileNotFoundError):
                                os.unlink(created_name, dir_fd=parent_fd)
                        with suppress(OSError):
                            os.fsync(parent_fd)
                        return "error", "local cache evidence changed during recovery"
                elif outcome == "conflict":
                    for created_name in created:
                        with suppress(FileNotFoundError):
                            os.unlink(created_name, dir_fd=parent_fd)
                    with suppress(OSError):
                        os.fsync(parent_fd)
                    return "error", "local cache evidence conflicts with the verified remote pair"
                else:
                    for created_name in created:
                        with suppress(FileNotFoundError):
                            os.unlink(created_name, dir_fd=parent_fd)
                    with suppress(OSError):
                        os.fsync(parent_fd)
                    return "error", outcome.partition(":")[2]
            elif existing != body:
                for created_name in created:
                    with suppress(FileNotFoundError):
                        os.unlink(created_name, dir_fd=parent_fd)
                with suppress(OSError):
                    os.fsync(parent_fd)
                return "error", "local cache evidence conflicts with the verified remote pair"
            with suppress(OSError):
                os.fsync(parent_fd)
        for name, body in entries:
            final, final_error = _read_cache_leaf(target_dir / name, state_dir, max(len(body), 1) + 1)
            if final_error is not None or final != body:
                # The leaf may have been replaced after our publish; never
                # report success without verified final bytes.  Do not delete
                # a leaf another writer may now own.
                return "error", "final local cache bytes could not be verified"
        return ("restored" if created else "already_present"), None
    finally:
        with suppress(OSError):
            os.close(parent_fd)


def _is_valid_remote_ocr_variant(
    state_dir: Path,
    reuse_key: str,
    candidate_rows: list[tuple[str, int]],
    sealed_pdf_sha: str,
    local_ocr_sha: str,
    *,
    conn: sqlite3.Connection,
    remote_root: str,
) -> bool:
    """Check whether a canonical receipt claim is corroborated by verified bytes.

    Receipt rows are claims, never authority.  A canonical-path row at the
    current remote root counts as a verified pre-existing variant of this
    reuse key only when ALL of the following hold:

    * the exact manifest bytes it records (SHA-256 AND size) are locally
      present as backup spool or in the worker OCR cache (canonical or a
      variants/ directory), parse as a canonical ``OCRArtifactManifest`` bound
      to this reuse key and, when given, to the sealed PDF identity;
    * its output differs from the incoming bundle's OCR body (an identical
      output under a different canonical manifest is not a coexisting variant
      and is held as an unverified collision instead);
    * the READY receipt at the same root equals the digest and size of the
      canonical READY bytes derived from that exact manifest, and any locally
      present READY file for that identity matches those bytes.

    Ledger metadata (``doc_info``/``doc_map``) is navigation and index-entry
    visibility only and never authenticates content.  Receipts bound to a
    different remote root authenticate nothing here; they surface as scope
    conflicts at transport time instead.  When local evidence was pruned, the
    formal read paths (``backup restore``/``audit``) re-materialize and verify
    the remote pair first; this resolver deliberately holds unverifiable
    collisions as unverified rather than trusting metadata or inventing a GET
    inside the synchronous ledger scope calculation.
    """
    can_ready = ocr_ready_path(reuse_key, kind="native").as_posix()
    ready_rows = conn.execute(
        "SELECT sha256, size_bytes FROM backup_receipts WHERE remote_path = ? AND remote_root = ?",
        (can_ready, remote_root),
    ).fetchall()
    if not ready_rows:
        return False
    ready_pairs = {(row["sha256"], row["size_bytes"]) for row in ready_rows}

    spool_dir = state_dir / "backup" / "spool"
    cache_dir = state_dir / "cache" / "ocr" / reuse_key

    for man_sha, man_size in candidate_rows:
        if not man_sha or man_sha == local_ocr_sha:
            continue
        candidate_bodies: list[bytes] = []
        spool_file = spool_dir / man_sha
        if spool_file.is_file():
            candidate_bodies.append(spool_file.read_bytes())
        can_file = cache_dir / "manifest.json"
        if can_file.is_file():
            candidate_bodies.append(can_file.read_bytes())
        if cache_dir.is_dir():
            for f in sorted(cache_dir.glob("variants/*/manifest.json")):
                if f.is_file():
                    candidate_bodies.append(f.read_bytes())
        for body in candidate_bodies:
            if sha256_bytes(body) != man_sha or len(body) != man_size:
                continue
            with suppress(Exception):
                m = OCRArtifactManifest.model_validate_json(body)
                if not (
                    m.canonical_bytes() == body
                    and m.reuse_key == reuse_key
                    and (not sealed_pdf_sha or m.source.pdf_sha256 == sealed_pdf_sha)
                    and (not local_ocr_sha or m.output.sha256 != local_ocr_sha)
                ):
                    continue
                ready = OCRReady(reuse_key=reuse_key, manifest_sha256=man_sha, ocr_sha256=m.output.sha256)
                ready_bytes = ready.canonical_bytes()
                if (sha256_bytes(ready_bytes), len(ready_bytes)) not in ready_pairs:
                    continue
                can_ready_file = cache_dir / "READY.json"
                if can_ready_file.is_file() and can_ready_file.read_bytes() != ready_bytes:
                    continue
                var_ready_file = cache_dir / "variants" / m.output.sha256 / "READY.json"
                if var_ready_file.is_file() and var_ready_file.read_bytes() != ready_bytes:
                    continue
                return True
    return False


def resolve_ocr_bundle_paths(
    state_dir: Path,
    reuse_key: str,
    ocr_sha256: str,
    manifest_sha256: str,
    *,
    is_generation_only: bool,
    pdf_sha256: str = "",
    kind: str = "native",
    conn: sqlite3.Connection | None = None,
    remote_root: str = "",
) -> tuple[str, str, str]:
    """Return (cas_path, manifest_path, ready_path) for an OCR bundle.

    If the document is generation-only (null seal reuse key) and the canonical
    cache path is already occupied by a different verified variant in receipts
    bound to the current canonical remote root, routes to an immutable variant
    metadata path under
    `v1/ocr-cache/native/{prefix}/{reuse_key}/variants/{ocr_sha256}/...`.
    Otherwise, returns the canonical cache path.  Receipt claims authenticate a
    variant only through byte-corroborated local evidence
    (`_is_valid_remote_ocr_variant`); anything else is held on the canonical
    path so scoped transport reports it as a conflict instead of overwriting
    or re-routing past it.
    """
    cas_path = object_path(ocr_sha256).as_posix()
    if kind != "native":
        can_man = ocr_manifest_path(reuse_key, kind=kind).as_posix()
        can_ready = ocr_ready_path(reuse_key, kind=kind).as_posix()
        return cas_path, can_man, can_ready

    can_man = ocr_manifest_path(reuse_key, kind="native").as_posix()
    can_ready = ocr_ready_path(reuse_key, kind="native").as_posix()
    var_man = ocr_variant_manifest_path(reuse_key, ocr_sha256, kind="native").as_posix()
    var_ready = ocr_variant_ready_path(reuse_key, ocr_sha256, kind="native").as_posix()

    def _check(c: sqlite3.Connection) -> tuple[str, str, str]:
        # Receipts for the canonical path at the current remote root only.
        # Multiple digest rows under the same root are ambiguous claims: each
        # must be individually corroborated, never arbitrated by fetch order.
        man_rows = c.execute(
            "SELECT sha256, size_bytes FROM backup_receipts WHERE remote_path = ? AND remote_root = ?",
            (can_man, remote_root),
        ).fetchall()
        if man_rows:
            if any(row["sha256"] == manifest_sha256 for row in man_rows):
                return cas_path, can_man, can_ready
            if is_generation_only and _is_valid_remote_ocr_variant(
                state_dir,
                reuse_key,
                [(row["sha256"], row["size_bytes"]) for row in man_rows if row["sha256"] != manifest_sha256],
                pdf_sha256,
                ocr_sha256,
                conn=c,
                remote_root=remote_root,
            ):
                return cas_path, var_man, var_ready
            return cas_path, can_man, can_ready

        # Check if variant path is already in receipts at this root
        var_row = c.execute(
            "SELECT sha256 FROM backup_receipts WHERE remote_path = ? AND remote_root = ? LIMIT 1",
            (var_man, remote_root),
        ).fetchone()
        if var_row is not None:
            return cas_path, var_man, var_ready

        return cas_path, can_man, can_ready

    if conn is not None:
        try:
            return _check(conn)
        except Exception:
            return cas_path, can_man, can_ready

    db_path = state_dir / "backup-ledger.sqlite3"
    if not db_path.is_file():
        return cas_path, can_man, can_ready

    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        with c:
            return _check(c)
    except Exception:
        return cas_path, can_man, can_ready


def verified_document_ocr_bundles(
    state_dir: Path,
    run_id: str,
    doc: dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
    remote_root: str = "",
) -> list[dict[str, Any]]:
    """Return native OCR artifact bundles cryptographically linked to one sealed document.

    A bundle is the OCR body CAS object plus its manifest and READY control
    files as one backup unit.  The run-local ``native-manifest.json`` must be
    canonical, bind the sealed PDF identity (SHA-256, size, page count) and
    match the sealed OCR artifact bytes (SHA-256, size, stored body hash).
    Documents without an OCR artifact, missing or corrupt manifests, and any
    identity mismatch yield no bundle so callers fail closed.  Seal documents
    that already carry an optional ``ocr_reuse_key`` keep their own cache
    identity path; this linkage only derives evidence the document itself owns.
    """
    document_id = doc.get("document_id")
    ocr_ref = doc.get("ocr")
    pdf_ref = doc.get("pdf")
    if (
        not isinstance(document_id, str)
        or not document_id
        or not isinstance(ocr_ref, dict)
        or not isinstance(pdf_ref, dict)
    ):
        return []
    pdf_sha = pdf_ref.get("sha256")
    ocr_sha = ocr_ref.get("sha256")
    if not isinstance(pdf_sha, str) or not isinstance(ocr_sha, str):
        return []
    doc_ocr_dir = state_dir / "runs" / run_id / "documents" / document_id / "ocr"
    if not doc_ocr_dir.is_dir():
        return []
    pdf_size = pdf_ref.get("size_bytes")
    ocr_size = ocr_ref.get("size_bytes")
    page_count = doc.get("page_count")
    bundles: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for manifest_file in sorted(doc_ocr_dir.glob("**/native-manifest.json")):
        try:
            manifest_body = manifest_file.read_bytes()
            manifest = OCRArtifactManifest.model_validate_json(manifest_body)
            if manifest.canonical_bytes() != manifest_body:
                continue
            if manifest.source.pdf_sha256 != pdf_sha:
                continue
            if isinstance(pdf_size, int) and manifest.source.pdf_size_bytes != pdf_size:
                continue
            if isinstance(page_count, int) and manifest.source.page_count != page_count:
                continue
            if manifest.output.sha256 != ocr_sha:
                continue
            if isinstance(ocr_size, int) and manifest.output.size_bytes != ocr_size:
                continue
            body_file = manifest_file.parent / "ocr.md"
            if not body_file.is_file():
                continue
            body_sha, _body_size = _sha256_file(body_file)
            if body_sha != ocr_sha:
                continue
            if manifest.reuse_key in seen_keys:
                continue
            manifest_sha = hashlib.sha256(manifest_body).hexdigest()
            ready = OCRReady(
                reuse_key=manifest.reuse_key,
                manifest_sha256=manifest_sha,
                ocr_sha256=manifest.output.sha256,
            )
            seen_keys.add(manifest.reuse_key)
            bundle_paths = resolve_ocr_bundle_paths(
                state_dir,
                manifest.reuse_key,
                manifest.output.sha256,
                manifest_sha,
                is_generation_only=doc.get("ocr_reuse_key") is None,
                pdf_sha256=pdf_sha,
                kind="native",
                conn=conn,
                remote_root=remote_root,
            )
            bundles.append(
                {
                    "run_id": run_id,
                    "document_id": document_id,
                    "reuse_key": manifest.reuse_key,
                    "kind": "native",
                    "manifest": manifest,
                    "manifest_file": manifest_file,
                    "manifest_body": manifest_body,
                    "manifest_sha256": manifest_sha,
                    "ready": ready,
                    "body_file": body_file,
                    "paths": bundle_paths,
                }
            )
        except (OSError, ValueError):
            continue
    return bundles


def _record_owned_ocr_paths(
    conn: sqlite3.Connection,
    run_id: str,
    document_id: str,
    manifest: OCRArtifactManifest,
    paths: tuple[str, ...],
) -> None:
    """Retain required-path visibility, never authority to upload bytes.

    Written only after bundle verification. Unlike transport rows, this
    run-bound obligation survives successful upload and loss of local sources.
    Scope still requires independent byte evidence to bind expected artifacts.
    """
    prefix = "ocr_owned_" + hashlib.sha256(run_id.encode()).hexdigest() + "_"
    identity = hashlib.sha256((document_id + "\0" + manifest.reuse_key).encode()).hexdigest()
    value = {
        "document_id": document_id,
        "pdf_sha256": manifest.source.pdf_sha256,
        "ocr_sha256": manifest.output.sha256,
        "paths": list(paths[1:]),
    }
    conn.execute(
        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
        (prefix + identity, json.dumps(value)),
    )


def _register_verified_ocr_bundle(
    conn: sqlite3.Connection,
    bundle: dict[str, Any],
    *,
    spool: Any,
    existing_receipts: set[str],
    existing_pending: set[str],
    now: float,
) -> None:
    """Insert one verified document-linked OCR bundle as a single backup unit.

    Same-path pending rows whose SHA/size disagree with the verified bundle are
    evidence-backed ledger repairs: the conflicting row is replaced by the
    verified one (the verified bytes come from this run's sealed document).
    Verified receipts are never rewritten here; a receipt/content disagreement
    is blocked at flush time for operator review instead of a blind overwrite.
    """
    manifest: OCRArtifactManifest = bundle["manifest"]
    document_id = bundle["document_id"]

    def _upsert(
        *,
        item_id: str,
        item_type: str,
        sha: str,
        size: int,
        local_path: Path,
        rel_path: str,
        media_type: str,
        spool_source: Path,
    ) -> None:
        if rel_path in existing_receipts:
            return
        conflicting = conn.execute(
            """
            SELECT item_id, sha256, size_bytes FROM backup_pending
            WHERE remote_path = ? AND (sha256 <> ? OR size_bytes <> ?)
            """,
            (rel_path, sha, size),
        ).fetchall()
        for stale in conflicting:
            logger.warning(
                "Repairing scoped backup pending row %s for %s: recorded %s/%s differs from the "
                "verified artifact %s/%s",
                stale["item_id"],
                rel_path,
                str(stale["sha256"])[:12],
                stale["size_bytes"],
                sha[:12],
                size,
            )
            conn.execute("DELETE FROM backup_pending WHERE item_id = ?", (stale["item_id"],))
            existing_pending.discard(rel_path)
        if rel_path in existing_pending and item_id not in {
            row[0]
            for row in conn.execute("SELECT item_id FROM backup_pending WHERE remote_path = ?", (rel_path,))
        }:
            # A same-content row registered under a different item id may already exist.
            return
        spool(spool_source, sha)
        conn.execute(
            """
            INSERT OR REPLACE INTO backup_pending
            (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, item_type, sha, size, str(local_path), rel_path, media_type, now, "pending"),
        )
        existing_pending.add(rel_path)

    ocr_cas_rel = bundle["paths"][0]
    man_rel = bundle["paths"][1]
    ready_rel = bundle["paths"][2]
    _record_owned_ocr_paths(conn, bundle["run_id"], document_id, manifest, bundle["paths"])
    _upsert(
        item_id=f"cas-{manifest.output.sha256}",
        item_type="ocr_cas",
        sha=manifest.output.sha256,
        size=manifest.output.size_bytes,
        local_path=bundle["body_file"],
        rel_path=ocr_cas_rel,
        media_type="text/markdown; charset=utf-8",
        spool_source=bundle["body_file"],
    )
    _upsert(
        item_id=f"man-{manifest.reuse_key}",
        item_type="ocr_manifest",
        sha=bundle["manifest_sha256"],
        size=len(bundle["manifest_body"]),
        local_path=bundle["manifest_file"],
        rel_path=man_rel,
        media_type="application/json",
        spool_source=bundle["manifest_file"],
    )
    ready_bytes = bundle["ready"].canonical_bytes()
    ready_sha = hashlib.sha256(ready_bytes).hexdigest()
    ready_path_local = bundle["manifest_file"].parent / "READY.json"
    ready_path_local.write_bytes(ready_bytes)
    _upsert(
        item_id=f"ready-{manifest.reuse_key}",
        item_type="ocr_ready",
        sha=ready_sha,
        size=len(ready_bytes),
        local_path=ready_path_local,
        rel_path=ready_rel,
        media_type="application/json",
        spool_source=ready_path_local,
    )
    doc_info = {
        "document_id": document_id,
        "reuse_key": manifest.reuse_key,
        "pdf_sha256": manifest.source.pdf_sha256,
        "pdf_size_bytes": manifest.source.pdf_size_bytes,
        "page_count": manifest.source.page_count,
        "ocr_sha256": manifest.output.sha256,
        "ocr_size_bytes": manifest.output.size_bytes,
        "model": manifest.contract.model,
        "provider": manifest.contract.provider,
        "kind": "native",
    }
    for rel_path in (ocr_cas_rel, man_rel, ready_rel):
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_map_{rel_path}", document_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
            (f"doc_info_{rel_path}", json.dumps(doc_info)),
        )


def _cache_bound_ocr_pair(
    state_dir: Path,
    key: str,
    kind: str,
    doc: dict[str, Any],
) -> tuple[tuple[str, int], tuple[str, int]] | None:
    """Bind a seal reuse-key cache pair from the worker's local OCR cache directory.

    Second formal evidence source when the current run document no longer holds
    the native manifest (retention pruned or the run was a cache reuse).  The
    local cache manifest must parse, be canonical, and match the sealed PDF and
    OCR identity exactly.  A READY file that disagrees with the derived pair
    voids the evidence instead of trusting either side.  Returns
    ((manifest_sha, manifest_size), (ready_sha, ready_size)) or None.
    """
    if kind != "native":
        return None
    cache_dir = state_dir / "cache" / "ocr" / key
    ocr_ref = doc.get("ocr") or {}
    ocr_sha = ocr_ref.get("sha256")

    candidate_files = []
    if ocr_sha:
        var_file = cache_dir / "variants" / ocr_sha / "manifest.json"
        if var_file.is_file():
            candidate_files.append(var_file)
    can_file = cache_dir / "manifest.json"
    if can_file.is_file():
        candidate_files.append(can_file)

    if not candidate_files:
        return None

    pdf_ref = doc.get("pdf") or {}
    pdf_sha = pdf_ref.get("sha256")
    pdf_size = pdf_ref.get("size_bytes")
    page_count = doc.get("page_count")

    for manifest_file in candidate_files:
        try:
            body = manifest_file.read_bytes()
            manifest = OCRArtifactManifest.model_validate_json(body)
            if manifest.canonical_bytes() != body:
                continue
            if manifest.reuse_key != key:
                continue
            if manifest.source.pdf_sha256 != pdf_sha:
                continue
            if isinstance(pdf_size, int) and manifest.source.pdf_size_bytes != pdf_size:
                continue
            if isinstance(page_count, int) and manifest.source.page_count != page_count:
                continue
            if manifest.output.sha256 != ocr_sha:
                continue
            manifest_sha = hashlib.sha256(body).hexdigest()
            ready = OCRReady(reuse_key=key, manifest_sha256=manifest_sha, ocr_sha256=manifest.output.sha256)
            ready_bytes = ready.canonical_bytes()
            ready_file = manifest_file.parent / "READY.json"
            if ready_file.is_file() and ready_file.read_bytes() != ready_bytes:
                continue
            return (manifest_sha, len(body)), (hashlib.sha256(ready_bytes).hexdigest(), len(ready_bytes))
        except (OSError, ValueError):
            continue
    return None


class BackupLedger:
    """Persistent SQLite ledger tracking unbacked artifacts and verified remote receipts."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_excluded_shared_cache: int = 0
        self._last_index_read_error: str | None = None
        self._last_excluded_shared_cache_by_run: dict[str, int] = {}
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(os.fspath(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_pending (
                    item_id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    local_path TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_receipts (
                    remote_path TEXT NOT NULL,
                    remote_root TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    verified_at REAL NOT NULL,
                    PRIMARY KEY (remote_path, remote_root, sha256)
                )
                """
            )
            with suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE backup_receipts ADD COLUMN remote_root TEXT NOT NULL DEFAULT ''")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_recorded_runs (
                    run_id TEXT PRIMARY KEY,
                    recorded_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def get_status(self, settings: WorkerSettings) -> dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT 
                    COUNT(*), 
                    COALESCE(SUM(size_bytes), 0), 
                    MIN(created_at),
                    COUNT(CASE WHEN item_type IN ('ocr_cas', 'ocr_cache') THEN 1 END)
                FROM backup_pending
                WHERE status = 'pending'
                """
            )
            row = cursor.fetchone()
            pending_count = row[0]
            pending_bytes = row[1]
            oldest_ts = row[2]
            ocr_count = row[3]

            lost_row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM backup_pending
                WHERE status = 'lost_source'
                """
            ).fetchone()
            lost_count = lost_row[0]
            lost_bytes = lost_row[1]

            meta = dict(conn.execute("SELECT key, value FROM backup_meta").fetchall())
            runs_since = int(meta.get("runs_since_backup", "0"))
            last_backup_at = meta.get("last_backup_at")

        oldest_pending_iso = (
            datetime.fromtimestamp(oldest_ts, tz=UTC).isoformat() if oldest_ts is not None else None
        )

        should_trigger, reasons = self.evaluate_triggers(
            settings=settings,
            pending_count=pending_count,
            pending_bytes=pending_bytes,
            oldest_ts=oldest_ts,
            runs_since=runs_since,
            ocr_count=ocr_count,
        )

        if lost_count > 0:
            overall_status = "failed"
        elif pending_count > 0:
            overall_status = "pending"
        else:
            overall_status = "ready"

        return {
            "mode": settings.backup_mode,
            "status": overall_status,
            "pending_count": pending_count,
            "pending_ocr_count": ocr_count,
            "pending_bytes": pending_bytes,
            "lost_count": lost_count,
            "lost_bytes": lost_bytes,
            "oldest_pending_at": oldest_pending_iso,
            "last_backup_at": last_backup_at,
            "runs_since_last_backup": runs_since,
            "should_trigger": should_trigger,
            "trigger_reasons": reasons,
        }

    def evaluate_triggers(
        self,
        settings: WorkerSettings,
        pending_count: int,
        pending_bytes: int,
        oldest_ts: float | None,
        runs_since: int,
        ocr_count: int = 0,
    ) -> tuple[bool, list[str]]:
        mode = settings.backup_mode
        if mode == "disabled":
            return False, []
        if mode == "immediate":
            if pending_count > 0:
                return True, ["immediate_pending_items"]
            return False, []
        if mode == "manual":
            return False, []

        # hybrid mode
        reasons = []
        if runs_since >= settings.backup_every_runs:
            reasons.append(f"runs_threshold_met ({runs_since} >= {settings.backup_every_runs})")
        effective_count = ocr_count if ocr_count > 0 else pending_count
        if effective_count >= settings.backup_new_ocr_count:
            reasons.append(f"ocr_count_threshold_met ({effective_count} >= {settings.backup_new_ocr_count})")
        if pending_bytes >= settings.backup_new_bytes:
            reasons.append(f"bytes_threshold_met ({pending_bytes} >= {settings.backup_new_bytes})")
        if oldest_ts is not None:
            age_hours = (time.time() - oldest_ts) / 3600.0
            if age_hours >= settings.backup_max_pending_age_hours:
                reasons.append(
                    f"age_threshold_met ({age_hours:.1f}h >= {settings.backup_max_pending_age_hours}h)"
                )

        return (len(reasons) > 0 and pending_count > 0), reasons

    def record_run_success(
        self,
        run_id: str,
        state_dir: Path,
        settings: WorkerSettings,
        *,
        selected_issuers: tuple[str, ...] | None = None,
    ) -> None:
        if settings.backup_mode == "disabled":
            # Disabled: do not track pending items to avoid disk accumulation
            return

        now = time.time()
        run_root = state_dir / "runs" / run_id
        doc_root = run_root / "documents"
        spool_dir = state_dir / "backup" / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)

        def _spool(src: Path, sha: str) -> None:
            if not src.is_file():
                return
            target = spool_dir / sha
            if not target.exists():
                try:
                    os.link(src, target)
                except OSError:
                    with suppress(OSError):
                        shutil.copyfile(src, target)

        scope_ids = None
        scope_shas = None
        scope_docs: list[dict[str, Any]] | None = None
        if selected_issuers is not None:
            from .partial_execution import read_json

            seal_path = run_root / "sealed/publish.json"
            sealed = read_json(state_dir, seal_path, "backup") if seal_path.exists() else {}
            documents = sealed.get("manifest", {}).get("documents", [])
            chosen = [doc for doc in documents if doc["issuer"] in selected_issuers]
            scope_ids = {doc["document_id"] for doc in chosen}
            scope_shas = {ref["sha256"] for doc in chosen for ref in (doc["pdf"], doc.get("ocr")) if ref}
            scope_docs = chosen
        canonical_remote_root = _canonical_remote_root(settings)
        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO backup_recorded_runs (run_id, recorded_at) VALUES (?, ?)",
                (run_id, now),
            )
            is_new_run = cur.rowcount > 0

            # Check verified receipts scoped to canonical_remote_root
            existing_receipts = {
                row["remote_path"]
                for row in conn.execute(
                    "SELECT remote_path FROM backup_receipts WHERE remote_root = ?",
                    (canonical_remote_root,),
                ).fetchall()
            }
            existing_pending = {
                row["remote_path"]
                for row in conn.execute("SELECT remote_path FROM backup_pending").fetchall()
            }

            # 1. Check seal if available for referenced CAS PDFs and OCR objects
            seal_file = run_root / "sealed" / "publish.json"
            if seal_file.is_file():
                with suppress(Exception):
                    seal_data = json.loads(seal_file.read_bytes())
                    for obj in seal_data.get("objects", []):
                        mtype = obj.get("media_type") or ""
                        sha = obj.get("sha256")
                        size = obj.get("size_bytes")
                        raw_path = obj.get("path")
                        if not sha or not size:
                            continue
                        if scope_shas is not None and sha not in scope_shas:
                            continue
                        rel_obj = object_path(sha).as_posix()
                        if rel_obj in existing_receipts or rel_obj in existing_pending:
                            continue
                        local_p = Path(raw_path) if raw_path else None
                        if (local_p is None or not local_p.is_file()) and mtype == "application/pdf":
                            cand = state_dir / "cache" / "pdf" / "objects" / sha[:2] / sha
                            if cand.is_file():
                                local_p = cand
                        if local_p is not None and local_p.is_file():
                            _spool(local_p, sha)
                            item_t = "cas_pdf" if mtype == "application/pdf" else "ocr_cas"
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO backup_pending
                                (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    f"{item_t}-{sha}",
                                    item_t,
                                    sha,
                                    size,
                                    str(local_p),
                                    rel_obj,
                                    mtype,
                                    now,
                                    "pending",
                                ),
                            )
                            existing_pending.add(rel_obj)

            # Scoped runs register only the bundles cryptographically linked
            # to their sealed documents; the recorder and the scope planner
            # share verified_document_ocr_bundles as one evidence source.
            if scope_docs is not None:
                for doc in scope_docs:
                    for bundle in verified_document_ocr_bundles(
                        state_dir, run_id, doc, conn=conn, remote_root=canonical_remote_root
                    ):
                        _register_verified_ocr_bundle(
                            conn,
                            bundle,
                            spool=_spool,
                            existing_receipts=existing_receipts,
                            existing_pending=existing_pending,
                            now=now,
                        )

            if doc_root.is_dir() and scope_docs is None:
                for doc_dir in doc_root.iterdir():
                    if scope_ids is not None and doc_dir.name not in scope_ids:
                        continue
                    ocr_dir = doc_dir / "ocr"
                    if not ocr_dir.is_dir():
                        continue
                    doc_id = doc_dir.name
                    # Find native-manifest.json
                    for manifest_file in ocr_dir.glob("**/native-manifest.json"):
                        try:
                            manifest = OCRArtifactManifest.model_validate_json(manifest_file.read_bytes())
                            body_file = manifest_file.parent / "ocr.md"
                            if not body_file.is_file():
                                continue
                            man_bytes = manifest.canonical_bytes()
                            man_sha = hashlib.sha256(man_bytes).hexdigest()
                            bundle_paths = resolve_ocr_bundle_paths(
                                state_dir,
                                manifest.reuse_key,
                                manifest.output.sha256,
                                man_sha,
                                is_generation_only=True,
                                kind="native",
                                conn=conn,
                                remote_root=canonical_remote_root,
                            )
                            ocr_cas_rel = bundle_paths[0]
                            man_rel = bundle_paths[1]
                            ready_rel = bundle_paths[2]

                            # 1. OCR CAS
                            if ocr_cas_rel not in existing_receipts and ocr_cas_rel not in existing_pending:
                                _spool(body_file, manifest.output.sha256)
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO backup_pending
                                    (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        f"cas-{manifest.output.sha256}",
                                        "ocr_cas",
                                        manifest.output.sha256,
                                        manifest.output.size_bytes,
                                        str(body_file),
                                        ocr_cas_rel,
                                        "text/markdown; charset=utf-8",
                                        now,
                                        "pending",
                                    ),
                                )
                                existing_pending.add(ocr_cas_rel)

                            # 2. OCR manifest
                            if man_rel not in existing_receipts and man_rel not in existing_pending:
                                _spool(manifest_file, man_sha)
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO backup_pending
                                    (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        f"man-{manifest.reuse_key}",
                                        "ocr_manifest",
                                        man_sha,
                                        len(man_bytes),
                                        str(manifest_file),
                                        man_rel,
                                        "application/json",
                                        now,
                                        "pending",
                                    ),
                                )
                                existing_pending.add(man_rel)

                            # 3. OCR READY
                            if ready_rel not in existing_receipts and ready_rel not in existing_pending:
                                ready = OCRReady(
                                    reuse_key=manifest.reuse_key,
                                    manifest_sha256=hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
                                    ocr_sha256=manifest.output.sha256,
                                )
                                ready_bytes = ready.canonical_bytes()
                                ready_sha = hashlib.sha256(ready_bytes).hexdigest()
                                ready_path_local = manifest_file.parent / "READY.json"
                                ready_path_local.write_bytes(ready_bytes)
                                _spool(ready_path_local, ready_sha)
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO backup_pending
                                    (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        f"ready-{manifest.reuse_key}",
                                        "ocr_ready",
                                        ready_sha,
                                        len(ready_bytes),
                                        str(ready_path_local),
                                        ready_rel,
                                        "application/json",
                                        now,
                                        "pending",
                                    ),
                                )
                                existing_pending.add(ready_rel)

                            conn.execute(
                                "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                                (f"doc_map_{ocr_cas_rel}", doc_id),
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                                (f"doc_map_{man_rel}", doc_id),
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                                (f"doc_map_{ready_rel}", doc_id),
                            )
                            doc_info = {
                                "document_id": doc_id,
                                "reuse_key": manifest.reuse_key,
                                "pdf_sha256": manifest.source.pdf_sha256,
                                "pdf_size_bytes": manifest.source.pdf_size_bytes,
                                "page_count": manifest.source.page_count,
                                "ocr_sha256": manifest.output.sha256,
                                "ocr_size_bytes": manifest.output.size_bytes,
                                "model": manifest.contract.model,
                                "provider": manifest.contract.provider,
                                "kind": "native",
                            }
                            conn.execute(
                                "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                                (f"doc_info_{man_rel}", json.dumps(doc_info)),
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                                (f"doc_info_{ocr_cas_rel}", json.dumps(doc_info)),
                            )
                            _record_owned_ocr_paths(
                                conn, run_id, doc_id, manifest, (ocr_cas_rel, man_rel, ready_rel)
                            )
                        except (OSError, ValueError):
                            continue

            # Also scan local native OCR cache if present
            cache_ocr_dir = state_dir / "cache" / "ocr"
            if selected_issuers is None and cache_ocr_dir.is_dir():
                for key_dir in cache_ocr_dir.iterdir():
                    if not key_dir.is_dir():
                        continue
                    key = key_dir.name
                    for item_file in key_dir.iterdir():
                        if not item_file.is_file() or item_file.name.startswith("."):
                            continue
                        rel_path = f"v1/caches/ocr/{key}/{item_file.name}"
                        if rel_path in existing_receipts or rel_path in existing_pending:
                            continue
                        try:
                            file_bytes = item_file.read_bytes()
                            file_sha = hashlib.sha256(file_bytes).hexdigest()
                            _spool(item_file, file_sha)
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO backup_pending
                                (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    f"cache-{key}-{item_file.name}",
                                    "ocr_cache",
                                    file_sha,
                                    len(file_bytes),
                                    str(item_file),
                                    rel_path,
                                    "application/json" if item_file.suffix == ".json" else "text/markdown",
                                    now,
                                    "pending",
                                ),
                            )
                            existing_pending.add(rel_path)
                        except OSError:
                            continue

            # Increment runs_since_backup ONLY if is_new_run
            if is_new_run:
                meta = dict(conn.execute("SELECT key, value FROM backup_meta").fetchall())
                runs = int(meta.get("runs_since_backup", "0")) + 1
                conn.execute(
                    "INSERT OR REPLACE INTO backup_meta (key, value) VALUES ('runs_since_backup', ?)",
                    (str(runs),),
                )

    def scoped_backup_scope(
        self,
        run_id: str,
        state_dir: Path,
        selected_issuers: tuple[str, ...],
        *,
        remote_root: str = "",
    ) -> tuple[frozenset[str], dict[str, tuple[str, int | None]]]:
        """Return (allowed remote paths, path -> verified (sha256, size|None)).

        ``remote_root`` binds receipt-based variant routing to the current
        canonical remote root, exactly as the recorder does, so registration
        and scope planning share one routing authority.

        The expected map binds each transportable path to exact artifact bytes
        verified from local evidence: the run document's native manifest bundle
        or, for a seal reuse key whose run-local manifest is gone, the worker's
        local OCR cache pair cross-checked against the sealed PDF/OCR identity.
        Paths allowed by the seal without any verifiable local evidence stay in
        the allow list for visibility but unbound: a scoped flush holds them as
        explicitly unverified instead of trusting pending/receipt/path strings,
        and they block selected completion.  In-scope pending rows whose run
        evidence was lost entirely (doc_map still names the selected document)
        are likewise kept visible and unbound.  Non-selected backlog is never
        included.
        """
        from .partial_execution import read_json

        path = state_dir / "runs" / run_id / "sealed/publish.json"
        # RO subclasses intentionally skip __init__ to avoid schema writes.
        self._last_excluded_shared_cache = 0
        self._last_excluded_stale_object = 0
        if not hasattr(self, "_last_excluded_shared_cache_by_run"):
            self._last_excluded_shared_cache_by_run = {}
        self._last_excluded_shared_cache_by_run[run_id] = 0
        if not path.exists():
            return frozenset(), {}
        sealed = read_json(state_dir, path, "backup")
        paths = set()
        expected: dict[str, tuple[str, int | None]] = {}
        selected_doc_ids: set[str] = set()
        selected_docs: dict[str, dict[str, Any]] = {}
        doc_has_bound_metadata: dict[str, bool] = {}
        doc_bound_keys: dict[str, set[str]] = {}
        doc_has_owned_run_bundle: dict[str, bool] = {}

        def _bind(rel: str, sha: str, size: int | None, *, verified: bool) -> None:
            paths.add(rel)
            if not verified:
                return
            previous = expected.get(rel)
            if previous is None or (previous[1] is None and size is not None):
                expected[rel] = (sha, size)

        with self._get_connection() as conn:
            for doc in sealed["manifest"]["documents"]:
                if doc["issuer"] not in selected_issuers:
                    continue
                doc_id = doc.get("document_id")
                if isinstance(doc_id, str):
                    selected_doc_ids.add(doc_id)
                    selected_docs[doc_id] = doc
                    doc_has_bound_metadata[doc_id] = False
                    doc_bound_keys[doc_id] = set()
                    doc_has_owned_run_bundle[doc_id] = False

                for ref in (doc["pdf"], doc.get("ocr")):
                    if ref:
                        size = ref.get("size_bytes")
                        _bind(
                            object_path(ref["sha256"]).as_posix(),
                            ref["sha256"],
                            size if isinstance(size, int) else None,
                            verified=True,
                        )

                # Track if the run directory contained owned OCR files for this document
                if doc_id:
                    doc_ocr_dir = state_dir / "runs" / run_id / "documents" / doc_id / "ocr"
                    if doc_ocr_dir.is_dir() and any(doc_ocr_dir.iterdir()):
                        doc_has_owned_run_bundle[doc_id] = True

                bundle_paths: set[str] = set()
                # Generation-only OCR keeps the seal reuse key null while the run
                # document still owns a verified native manifest.  Include exactly
                # the body/manifest/READY bundle cryptographically linked to this
                # document, never a prefix or filename guess.
                for bundle in verified_document_ocr_bundles(
                    state_dir, run_id, doc, conn=conn, remote_root=remote_root
                ):
                    manifest = bundle["manifest"]
                    ready_bytes = bundle["ready"].canonical_bytes()
                    bundle_paths.update(bundle["paths"])
                    _bind(
                        bundle["paths"][0],
                        manifest.output.sha256,
                        manifest.output.size_bytes,
                        verified=True,
                    )
                    _bind(
                        bundle["paths"][1],
                        bundle["manifest_sha256"],
                        len(bundle["manifest_body"]),
                        verified=True,
                    )
                    _bind(
                        bundle["paths"][2],
                        hashlib.sha256(ready_bytes).hexdigest(),
                        len(ready_bytes),
                        verified=True,
                    )
                    if doc_id:
                        doc_has_bound_metadata[doc_id] = True
                        doc_bound_keys[doc_id].add(manifest.reuse_key)
                        doc_has_owned_run_bundle[doc_id] = True

                key = doc.get("ocr_reuse_key")
                if key and not doc_has_bound_metadata.get(doc_id):
                    kind = doc.get("ocr_cache_kind") or "native"
                    if kind == "native":
                        pair = (
                            None
                            if any(
                                p.startswith(f"v1/ocr-cache/native/{key[:2]}/{key}/") for p in bundle_paths
                            )
                            else _cache_bound_ocr_pair(state_dir, key, kind, doc)
                        )
                        if pair is not None:
                            bundle_paths_rebound = resolve_ocr_bundle_paths(
                                state_dir,
                                key,
                                (doc.get("ocr") or {}).get("sha256") or "",
                                pair[0][0],
                                is_generation_only=False,
                                kind=kind,
                                conn=conn,
                                remote_root=remote_root,
                            )
                            man_rel = bundle_paths_rebound[1]
                            ready_rel = bundle_paths_rebound[2]
                            _bind(man_rel, pair[0][0], pair[0][1], verified=True)
                            _bind(ready_rel, pair[1][0], pair[1][1], verified=True)
                            if doc_id:
                                doc_has_bound_metadata[doc_id] = True
                                doc_bound_keys[doc_id].add(key)
                        else:
                            # Required native cache identity with no locally
                            # verifiable evidence: visible in scope, unbound, held.
                            # Its absence (even with no rows at all) blocks
                            # completion; a rogue row here is never transported.
                            man_rel = ocr_manifest_path(key, kind=kind).as_posix()
                            ready_rel = ocr_ready_path(key, kind=kind).as_posix()
                            paths.add(man_rel)
                            paths.add(ready_rel)
                    # adopted/content cache identities are shared cache artifacts,
                    # not the document-owned backup unit; scoped backup neither
                    # claims nor transports them (no policy backlog inflation).

            # Legacy transport records are conservative visibility hints, not
            # evidence authorizing uploads. Include lost_source as well.
            pending_all_remotes: set[str] = {
                row["remote_path"]
                for row in conn.execute(
                    "SELECT remote_path FROM backup_pending WHERE status IN ('pending', 'lost_source')"
                ).fetchall()
            }

            # Obligations recorded from verified current-run bundles survive
            # transport-row deletion/receipts and do not become shared cache
            # merely because the current document uses content/adopted OCR.
            prefix = "ocr_owned_" + hashlib.sha256(run_id.encode()).hexdigest() + "_"
            for row in conn.execute("SELECT key,value FROM backup_meta WHERE key LIKE ?", (prefix + "%",)):
                try:
                    owned = json.loads(row["value"])
                    doc = selected_docs.get(owned["document_id"])
                    if (
                        doc is not None
                        and owned["pdf_sha256"] == doc["pdf"]["sha256"]
                        and owned["ocr_sha256"] == (doc.get("ocr") or {}).get("sha256")
                    ):
                        # Only required-path visibility: no SHA/size binding
                        # is taken from this metadata record.
                        owned_paths = owned["paths"]
                        if (
                            not isinstance(owned_paths, list)
                            or len(owned_paths) != 2
                            or not all(
                                isinstance(rel, str)
                                and rel.startswith("v1/ocr-cache/native/")
                                and rel.endswith(("/manifest.json", "/READY.json"))
                                for rel in owned_paths
                            )
                        ):
                            raise ValueError("invalid required-path record")
                        paths.update(owned_paths)
                except (KeyError, TypeError, ValueError):
                    # A damaged obligation cannot authorize transport, nor
                    # can it silently convert a missing bundle into success.
                    paths.add(
                        "v1/backup/unverified-ownership/" + hashlib.sha256(row["key"].encode()).hexdigest()
                    )

            # Artifacts recorded for selected documents whose run evidence has since
            # been lost must not be silently omitted from the scope allowance just
            # because pending rows were absent or already cleared.  The doc_map ledger
            # tag preserves required-artifact visibility independently of pending
            # rows; it never binds content or authorizes transport on its own, but
            # verified cache evidence matching the sealed document identity may
            # formally rebind them.
            tagged = conn.execute("SELECT key, value FROM backup_meta WHERE key LIKE 'doc_map_%'").fetchall()
            doc_infos = {
                row["key"][len("doc_info_") :]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM backup_meta WHERE key LIKE 'doc_info_%'"
                ).fetchall()
            }
            # Two-phase ledger resolution ensures order-independent classification:
            # Phase 1: Collect and formally rebind all valid current-revision evidence
            # from local cache pairs across all selected documents.
            # Phase 2: Preserve visibility of unbound/evidence-lost artifacts while
            # excluding demonstrably stale past revisions for documents whose current
            # metadata is already verified and bound.
            tagged_items: list[tuple[str, dict[str, Any], dict[str, Any] | None, bool]] = []
            for meta_key, doc_id in ((row["key"], row["value"]) for row in tagged):
                if doc_id not in selected_docs:
                    continue
                rel = meta_key[len("doc_map_") :]
                doc = selected_docs[doc_id]
                doc_pdf_sha = (doc.get("pdf") or {}).get("sha256")
                doc_ocr_sha = (doc.get("ocr") or {}).get("sha256")

                info_raw = doc_infos.get(rel)
                parsed_info: dict[str, Any] | None = None
                if info_raw:
                    with suppress(Exception):
                        parsed_info = json.loads(info_raw)

                is_stale_revision = False
                if parsed_info is not None:
                    info_pdf = parsed_info.get("pdf_sha256")
                    info_ocr = parsed_info.get("ocr_sha256")
                    is_stale_revision = bool(
                        (doc_pdf_sha and info_pdf and info_pdf != doc_pdf_sha)
                        or (doc_ocr_sha and info_ocr and info_ocr != doc_ocr_sha)
                    )

                # Phase 1: Rebind current revision candidates from local cache
                # When cryptographic evidence in local cache matches the current sealed document identity,
                # formally rebind it regardless of cache_kind, satisfying requirement (c).
                if not is_stale_revision and rel not in expected and rel.startswith("v1/ocr-cache/native/"):
                    rebind_key: str | None = None
                    rebind_kind: str = "native"
                    if parsed_info is not None:
                        rebind_key = parsed_info.get("reuse_key")
                        rebind_kind = parsed_info.get("kind") or "native"
                    if rebind_key is None:
                        parts = rel.split("/")
                        if (
                            len(parts) == 6
                            and parts[5] in ("manifest.json", "READY.json")
                            and len(parts[4]) == 64
                        ) or (
                            len(parts) == 8
                            and parts[5] == "variants"
                            and parts[7] in ("manifest.json", "READY.json")
                            and len(parts[4]) == 64
                        ):
                            rebind_key = parts[4]
                    if rebind_key and rebind_kind == "native":
                        pair = _cache_bound_ocr_pair(state_dir, rebind_key, rebind_kind, doc)
                        if pair is not None:
                            bundle_paths_rebound = resolve_ocr_bundle_paths(
                                state_dir,
                                rebind_key,
                                doc_ocr_sha or "",
                                pair[0][0],
                                is_generation_only=doc.get("ocr_reuse_key") is None,
                                pdf_sha256=doc_pdf_sha or "",
                                kind=rebind_kind,
                                conn=conn,
                                remote_root=remote_root,
                            )
                            man_rel = bundle_paths_rebound[1]
                            ready_rel = bundle_paths_rebound[2]
                            _bind(man_rel, pair[0][0], pair[0][1], verified=True)
                            _bind(ready_rel, pair[1][0], pair[1][1], verified=True)
                            doc_has_bound_metadata[doc_id] = True
                            doc_bound_keys[doc_id].add(rebind_key)

                tagged_items.append((rel, doc, parsed_info, is_stale_revision))

            # Phase 2: Scope visibility resolution (order-independent)
            excluded_shared_cache_paths: set[str] = set()
            excluded_stale_object_paths: set[str] = set()
            for rel, doc, _parsed_info, is_stale_revision in tagged_items:
                doc_id = doc.get("document_id")
                # If rel is already in paths (e.g. CAS object v1/objects/... or verified bound bundle),
                # it is in-scope and NEVER counted as excluded shared cache.  This is also the
                # protection contract for CAS objects required by the current seal, another
                # selected current document, a verified bundle, or an ownership record.
                if rel in paths:
                    continue

                # A past-revision tag is never a current required artifact, regardless
                # of whether the current document owns native metadata (content/adopted
                # documents legitimately own none).  Retained receipts/objects and other
                # generations are left untouched; only this selected scope stops claiming
                # the path.  Current references reaching this point would already be in
                # `paths` and were skipped above.
                if is_stale_revision:
                    if rel.startswith("v1/ocr-cache/"):
                        excluded_shared_cache_paths.add(rel)
                    else:
                        excluded_stale_object_paths.add(rel)
                    continue

                # For OCR cache paths (v1/ocr-cache/):
                if rel.startswith("v1/ocr-cache/"):
                    # Case 1: Active pending rows in backup_pending for this selected document
                    # must always remain visible in scope (never silently dropped to out-of-scope).
                    # If unverified/forged/lost, they stay unbound, blocking completion.
                    if rel in pending_all_remotes:
                        paths.add(rel)
                        continue

                    # Case 2: Document ALREADY has bound metadata in expected:
                    if doc_id and doc_has_bound_metadata.get(doc_id):
                        # Any other OCR cache path for a different key or un-bound canonical superseded by variant
                        # is excluded from document-owned backup scope.
                        excluded_shared_cache_paths.add(rel)
                        continue

                    # Case 3: Document does NOT have bound metadata:
                    # Distinguish between native document requiring cache vs past shared cache:
                    doc_kind = doc.get("ocr_cache_kind") or "native"
                    if doc_kind == "native":
                        # Native document whose evidence is missing: must stay unbound in scope.
                        paths.add(rel)
                    else:
                        # Past shared cache path for a content/adopted document with no owned bundle:
                        # Correctly excluded under the shared cache policy.
                        excluded_shared_cache_paths.add(rel)
                    continue

                # Non-cache object paths (CAS): a doc_map tag alone is not current
                # byte evidence.  content/adopted documents have no document-owned
                # native metadata by design, so a leftover tag for a CAS object that
                # the current seal does not reference is legacy/shared, not required.
                # Native or unknown provenance stays visible and unbound (fail closed);
                # current items are never blanket-excluded.
                doc_kind = doc.get("ocr_cache_kind") or "native"
                if doc_kind in ("adopted", "content"):
                    excluded_shared_cache_paths.add(rel)
                    continue
                paths.add(rel)

            excluded_shared_cache_paths -= paths
            excluded_stale_object_paths -= paths
            self._last_excluded_shared_cache = len(excluded_shared_cache_paths)
            self._last_excluded_stale_object = len(excluded_stale_object_paths)
            self._last_excluded_shared_cache_by_run[run_id] = len(excluded_shared_cache_paths)
        return frozenset(paths), expected

    def scope_remote_paths(
        self, run_id: str, state_dir: Path, selected_issuers: tuple[str, ...], *, remote_root: str = ""
    ) -> frozenset[str]:
        paths, _expected = self.scoped_backup_scope(
            run_id, state_dir, selected_issuers, remote_root=remote_root
        )
        return paths

    def scoped_pending_diagnostics(
        self,
        settings: WorkerSettings,
        run_id: str,
        selected_issuers: tuple[str, ...],
    ) -> dict[str, Any]:
        """Read-only selected-scope view for policy-deferred flushes (no transport).

        Used when hybrid/manual thresholds defer a flush: counts computable
        from the local ledger and sealed evidence are still reported, while
        completion deliberately stays None because no commit/remote-index
        evidence was produced by this path.  No provider, network, or write.
        """
        root = _canonical_remote_root(settings)
        allowed, expected = self.scoped_backup_scope(
            run_id, settings.state_dir, selected_issuers, remote_root=root
        )
        diagnostics = self._scope_diagnostics(
            allowed,
            expected,
            root,
            excluded_shared_cache=self._last_excluded_shared_cache_by_run.get(
                run_id, self._last_excluded_shared_cache
            ),
        )
        fields = scoped_backup_result_fields({**diagnostics, "backup_scope_complete": None})
        fields["backup_diagnostics_mode"] = "read_only"
        fields["backup_scope_run_id"] = run_id
        return fields

    def record_document_ocr(
        self,
        run_id: str,
        document_id: str,
        state_dir: Path,
        settings: WorkerSettings,
    ) -> None:
        """Record completed OCR artifacts immediately after OCR resolution."""
        if getattr(settings, "backup_mode", "disabled") == "disabled":
            return
        doc_ocr_dir = state_dir / "runs" / run_id / "documents" / document_id / "ocr"
        if not doc_ocr_dir.is_dir():
            return
        spool_dir = state_dir / "backup" / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        canonical_remote_root = _canonical_remote_root(settings)
        now = time.time()
        with self._get_connection() as conn:
            existing_receipts = {
                row["remote_path"]
                for row in conn.execute(
                    "SELECT remote_path FROM backup_receipts WHERE remote_root = ?",
                    (canonical_remote_root,),
                ).fetchall()
            }
            existing_pending = {
                row["remote_path"]
                for row in conn.execute("SELECT remote_path FROM backup_pending").fetchall()
            }
            for manifest_file in doc_ocr_dir.glob("**/native-manifest.json"):
                try:
                    manifest = OCRArtifactManifest.model_validate_json(manifest_file.read_bytes())
                    body_file = manifest_file.parent / "ocr.md"
                    if not body_file.is_file():
                        continue
                    man_bytes = manifest.canonical_bytes()
                    man_sha = hashlib.sha256(man_bytes).hexdigest()
                    bundle_paths = resolve_ocr_bundle_paths(
                        state_dir,
                        manifest.reuse_key,
                        manifest.output.sha256,
                        man_sha,
                        is_generation_only=True,
                        kind="native",
                        conn=conn,
                        remote_root=canonical_remote_root,
                    )
                    ocr_cas_rel = bundle_paths[0]
                    man_rel = bundle_paths[1]
                    ready_rel = bundle_paths[2]

                    # 1. OCR CAS
                    if ocr_cas_rel not in existing_receipts and ocr_cas_rel not in existing_pending:
                        target = spool_dir / manifest.output.sha256
                        if not target.exists():
                            with suppress(OSError):
                                shutil.copyfile(body_file, target)
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO backup_pending
                            (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"cas-{manifest.output.sha256}",
                                "ocr_cas",
                                manifest.output.sha256,
                                manifest.output.size_bytes,
                                str(body_file),
                                ocr_cas_rel,
                                "text/markdown; charset=utf-8",
                                now,
                                "pending",
                            ),
                        )
                        existing_pending.add(ocr_cas_rel)

                    # 2. OCR manifest
                    if man_rel not in existing_receipts and man_rel not in existing_pending:
                        target = spool_dir / man_sha
                        if not target.exists():
                            with suppress(OSError):
                                shutil.copyfile(manifest_file, target)
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO backup_pending
                            (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"man-{manifest.reuse_key}",
                                "ocr_manifest",
                                man_sha,
                                len(man_bytes),
                                str(manifest_file),
                                man_rel,
                                "application/json",
                                now,
                                "pending",
                            ),
                        )
                        existing_pending.add(man_rel)

                    # 3. OCR READY
                    if ready_rel not in existing_receipts and ready_rel not in existing_pending:
                        ready = OCRReady(
                            reuse_key=manifest.reuse_key,
                            manifest_sha256=hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
                            ocr_sha256=manifest.output.sha256,
                        )
                        ready_bytes = ready.canonical_bytes()
                        ready_sha = hashlib.sha256(ready_bytes).hexdigest()
                        ready_path_local = manifest_file.parent / "READY.json"
                        ready_path_local.write_bytes(ready_bytes)
                        target = spool_dir / ready_sha
                        if not target.exists():
                            with suppress(OSError):
                                shutil.copyfile(ready_path_local, target)
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO backup_pending
                            (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"ready-{manifest.reuse_key}",
                                "ocr_ready",
                                ready_sha,
                                len(ready_bytes),
                                str(ready_path_local),
                                ready_rel,
                                "application/json",
                                now,
                                "pending",
                            ),
                        )
                        existing_pending.add(ready_rel)

                    conn.execute(
                        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                        (f"doc_map_{ocr_cas_rel}", document_id),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                        (f"doc_map_{man_rel}", document_id),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                        (f"doc_map_{ready_rel}", document_id),
                    )
                    doc_info = {
                        "document_id": document_id,
                        "reuse_key": manifest.reuse_key,
                        "pdf_sha256": manifest.source.pdf_sha256,
                        "pdf_size_bytes": manifest.source.pdf_size_bytes,
                        "page_count": manifest.source.page_count,
                        "ocr_sha256": manifest.output.sha256,
                        "ocr_size_bytes": manifest.output.size_bytes,
                        "model": manifest.contract.model,
                        "provider": manifest.contract.provider,
                        "kind": "native",
                    }
                    conn.execute(
                        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                        (f"doc_info_{man_rel}", json.dumps(doc_info)),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                        (f"doc_info_{ocr_cas_rel}", json.dumps(doc_info)),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?, ?)",
                        (f"doc_info_{ready_rel}", json.dumps(doc_info)),
                    )
                    _record_owned_ocr_paths(
                        conn, run_id, document_id, manifest, (ocr_cas_rel, man_rel, ready_rel)
                    )
                except Exception as exc:
                    logger.debug("Failed to record document ocr metadata: %s", exc)
                    continue

    def _scoped_integrity(
        self,
        expected_artifacts: dict[str, tuple[str, int | None]],
        canonical_root: str,
    ) -> tuple[set[str], list[tuple[str, str]], list[str]]:
        """Compare bound artifacts with root receipts and rows; no writes.

        Returns (receipt_conflict_paths, [(path, sha) with a matching receipt],
        missing_paths).  A matching receipt at the canonical root proves a
        previously verified upload; a receipt with different bytes is a
        conflict to report, never something to blind-overwrite; an expected
        artifact with neither a matching receipt nor a live row is missing.
        A receipt claiming the same path under a *different* remote root with
        different bytes is also a conflict: the physical bucket path is shared,
        so transporting the bound bytes would overwrite previously verified
        content that this root's receipts can no longer see.
        """
        with self._get_connection() as conn:
            receipt_by_path = {
                row["remote_path"]: (row["sha256"], row["size_bytes"])
                for row in conn.execute(
                    "SELECT remote_path, sha256, size_bytes FROM backup_receipts WHERE remote_root = ?",
                    (canonical_root,),
                ).fetchall()
            }
            foreign_by_path: dict[str, set[str]] = {}
            for row in conn.execute(
                "SELECT remote_path, sha256 FROM backup_receipts WHERE remote_root <> ?",
                (canonical_root,),
            ).fetchall():
                foreign_by_path.setdefault(row["remote_path"], set()).add(row["sha256"])
            row_paths = {
                row["remote_path"]
                for row in conn.execute(
                    "SELECT DISTINCT remote_path FROM backup_pending WHERE status IN ('pending', 'lost_source')"
                ).fetchall()
            }
        conflicts: set[str] = set()
        done: list[tuple[str, str]] = []
        missing: list[str] = []
        for rel, (sha, size) in expected_artifacts.items():
            receipt = receipt_by_path.get(rel)
            if receipt == (sha, size):
                done.append((rel, sha))
                continue
            if receipt is not None or any(f_sha != sha for f_sha in foreign_by_path.get(rel, ())):
                conflicts.add(rel)
            if rel not in row_paths:
                missing.append(rel)
        return conflicts, done, sorted(missing)

    def _scope_diagnostics(
        self,
        allowed_remote_paths: frozenset[str] | None,
        expected_artifacts: dict[str, tuple[str, int | None]] | None = None,
        canonical_root: str = "",
        *,
        transport_conflicts: int = 0,
        excluded_shared_cache: int | None = None,
    ) -> dict[str, Any]:
        """One diagnostic schema for every flush return branch.

        An unscoped flush has no scope semantics at all, so the keys are None
        rather than zero-filled; a zero alone must never read as completion.
        ``in_scope_remaining`` includes ``lost_source`` and unbound/held rows:
        an incomplete selected artifact is never silently drained.  When
        ``expected_artifacts`` is provided, the pending-independent integrity
        view (missing required artifacts, receipt conflicts, unbound rows)
        joins the per-row counters; legacy callers that pass only an allow set
        keep the pre-FIX_08 schema.
        """
        if allowed_remote_paths is None:
            return {
                "in_scope_remaining": None,
                "in_scope_lost_source": None,
                "in_scope_conflicts": None,
                "in_scope_missing": None,
                "in_scope_unbound": None,
                "pending_out_of_scope": None,
                "pending_out_of_scope_bytes": None,
            }
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT remote_path, sha256, size_bytes, status FROM backup_pending"
            ).fetchall()
        pending_in = 0
        pending_out = 0
        lost_in = 0
        out_bytes = 0
        row_conflicts: set[str] = set()
        for row in rows:
            inside = row["remote_path"] in allowed_remote_paths
            if row["status"] == "pending":
                if not inside:
                    pending_out += 1
                    out_bytes += row["size_bytes"]
                    continue
                pending_in += 1
            elif row["status"] == "lost_source":
                if not inside:
                    continue
                lost_in += 1
            else:
                continue
            if expected_artifacts is not None:
                exp = expected_artifacts.get(row["remote_path"])
                if exp is not None and (
                    exp[0] != row["sha256"] or (exp[1] is not None and exp[1] != row["size_bytes"])
                ):
                    row_conflicts.add(row["remote_path"])
        # Required-artifact accounting is path-based and pending-independent:
        # a scoped path that no verified evidence binds is uncomplete even when
        # its rows (or receipts) were absent entirely.  Duplicate rows never
        # change the conclusion.
        unbound_required = 0
        if expected_artifacts is not None:
            unbound_required = len([rel for rel in allowed_remote_paths if rel not in expected_artifacts])
        missing_count: int | None = None
        receipt_conflicts: set[str] = set()
        if expected_artifacts is not None:
            receipt_conflicts, _done, missing_list = self._scoped_integrity(
                expected_artifacts, canonical_root
            )
            missing_count = len(missing_list)
        conflict_paths = row_conflicts | receipt_conflicts
        if excluded_shared_cache is None and expected_artifacts is not None:
            excluded_shared_cache = getattr(self, "_last_excluded_shared_cache", None)
        diag = {
            "in_scope_remaining": pending_in + lost_in,
            "in_scope_lost_source": lost_in,
            "in_scope_conflicts": max(transport_conflicts, len(conflict_paths)),
            "in_scope_missing": missing_count,
            "in_scope_unbound": None if expected_artifacts is None else unbound_required,
            "pending_out_of_scope": pending_out,
            "pending_out_of_scope_bytes": out_bytes,
        }
        if excluded_shared_cache is not None:
            diag["excluded_shared_cache"] = excluded_shared_cache
        return diag

    def _index_read_failure(self, kind: str) -> BackupIndexRead:
        self._last_index_read_error = kind
        return BackupIndexRead("unreadable", None, kind)

    async def _read_backup_index(self, client: Any) -> BackupIndexRead:
        """Read the committed backup index with the dedicated bounded policy.

        Distinguishes a genuinely absent index (404/None) from an unreadable
        one: size limit, HTTP/auth, transport, malformed JSON, or a client
        without a supported read method.  Callers must never treat an
        unreadable index as an empty index when writing.
        """
        self._last_index_read_error = None

        def _accepts_max_bytes(method: Any) -> bool:
            try:
                parameters = inspect.signature(method).parameters.values()
            except (TypeError, ValueError):
                return False
            return any(
                parameter.name == "max_bytes" or parameter.kind is parameter.VAR_KEYWORD
                for parameter in parameters
            )

        try:
            if hasattr(client, "get_bytes"):
                if _accepts_max_bytes(client.get_bytes):
                    raw = await client.get_bytes(BACKUP_INDEX_PATH, max_bytes=BACKUP_INDEX_MAX_BYTES)
                else:
                    # get-only fakes keep their contract; the reader's own
                    # length check still enforces the dedicated bound.
                    raw = await client.get_bytes(BACKUP_INDEX_PATH)
            elif hasattr(client, "get"):
                raw = await client.get(BACKUP_INDEX_PATH)
            elif hasattr(client, "core"):
                raw = client.core.get(BACKUP_INDEX_PATH, max_bytes=BACKUP_INDEX_MAX_BYTES).content
            else:
                return self._index_read_failure("client_contract")
        except WebDAVHTTPError as exc:
            if exc.status_code == 404:
                return BackupIndexRead("absent")
            return self._index_read_failure(f"http_{exc.status_code}")
        except WebDAVIntegrityError:
            return self._index_read_failure("size_limit")
        except Exception:
            return self._index_read_failure("transport")
        if raw is None:
            # Only the explicit 404/None contract means "no committed index".
            return BackupIndexRead("absent")
        if not isinstance(raw, (bytes, bytearray)):
            return self._index_read_failure("client_contract")
        if not raw:
            # HTTP 200 with a zero-byte body is a malformed index, not absence:
            # never treat an existing-but-unreadable object as a new remote.
            return self._index_read_failure("invalid_json")
        if len(raw) > BACKUP_INDEX_MAX_BYTES:
            return self._index_read_failure("size_limit")
        try:
            payload = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError):
            return self._index_read_failure("invalid_json")
        if not isinstance(payload, dict):
            return self._index_read_failure("invalid_json")
        items = payload.get("items")
        if not isinstance(items, list):
            return self._index_read_failure("invalid_json")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("remote_path"), str):
                return self._index_read_failure("invalid_json")
        return BackupIndexRead("present", payload)

    async def _remote_index_entries(self, client: Any) -> dict[str, str] | None:
        """Read the committed backup index once; None when absent/unreadable."""
        read = await self._read_backup_index(client)
        if read.state != "present" or read.payload is None:
            return None
        return {str(item["remote_path"]): str(item.get("sha256", "")) for item in read.payload["items"]}

    async def _remote_index_repair_needed(self, client: Any, canonical_root: str) -> bool:
        """True when the readable committed index omits or mismatches local receipts.

        Only a *readable* index document can be judged for repair: a missing
        or unreadable/unparseable index is never blindly replaced by a
        transport-less probe (a brand-new remote gets its first index from the
        batch that actually transports artifacts, and transient failures must
        not destroy remote state).  A committed index that exists but has
        drifted from this root's verified receipts (partial loss) is repaired
        from receipt evidence without transporting artifact bodies.
        """
        read = await self._read_backup_index(client)
        if read.state != "present" or read.payload is None:
            return False
        entries = {str(item["remote_path"]): str(item.get("sha256", "")) for item in read.payload["items"]}
        with self._get_connection() as conn:
            receipt_rows = conn.execute(
                "SELECT remote_path, sha256 FROM backup_receipts WHERE remote_root = ?",
                (canonical_root,),
            ).fetchall()
        return any(entries.get(row["remote_path"]) != row["sha256"] for row in receipt_rows)

    async def _scoped_completion(
        self,
        client: Any,
        expected_artifacts: dict[str, tuple[str, int | None]],
        diagnostics: dict[str, Any],
        canonical_root: str,
    ) -> bool | None:
        """Completion needs receipt+index evidence for every bound artifact.

        A batch commit of the other items is not proof: each expected path must
        hold a matching receipt at the canonical root and appear in the
        committed remote index with the same digest.  None means "not
        verifiable" (unreadable index), which is deliberately not completion.
        """
        if diagnostics.get("in_scope_remaining") != 0:
            return False
        if diagnostics.get("in_scope_conflicts"):
            return False
        if diagnostics.get("in_scope_unbound"):
            return False
        if diagnostics.get("in_scope_missing"):
            return False
        _conflicts, done, _missing = self._scoped_integrity(expected_artifacts, canonical_root)
        if _conflicts:
            return False
        entries = await self._remote_index_entries(client)
        if entries is None:
            return None
        return all(entries.get(rel) == sha for rel, sha in done)

    async def flush(
        self,
        settings: WorkerSettings,
        webdav_client: WebDAVClient | None = None,
        *,
        timeout_seconds: float = 300.0,
        force: bool = False,
        allowed_remote_paths: frozenset[str] | None = None,
        expected_artifacts: dict[str, tuple[str, int | None]] | None = None,
    ) -> dict[str, Any]:
        spool_dir = getattr(settings, "state_dir", self.db_path.parent) / "backup" / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        canonical_remote_root = _canonical_remote_root(settings)

        lock_file = getattr(settings, "state_dir", self.db_path.parent) / "backup.lock"
        lock_fd = None
        try:
            lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            if lock_fd is not None:
                with suppress(OSError):
                    os.close(lock_fd)
            with self._get_connection() as conn:
                p_cnt = conn.execute(
                    "SELECT COUNT(*) FROM backup_pending WHERE status = 'pending'"
                ).fetchone()[0]
            return {
                "status": "locked",
                "error": "Another backup flush is currently in progress",
                "flushed_count": 0,
                "uploaded_count": 0,
                "uploaded_bytes": 0,
                "requests": 0,
                "remaining_pending": p_cnt,
                "backup_scope_complete": None,
                **self._scope_diagnostics(allowed_remote_paths, expected_artifacts, canonical_remote_root),
            }

        try:
            # Unmark lost_source if source file or spool has appeared
            with self._get_connection() as conn:
                lost_rows = conn.execute(
                    "SELECT item_id, local_path, sha256 FROM backup_pending WHERE status = 'lost_source'"
                ).fetchall()
                for l_row in lost_rows:
                    if Path(l_row["local_path"]).is_file() or (spool_dir / l_row["sha256"]).is_file():
                        conn.execute(
                            "UPDATE backup_pending SET status = 'pending' WHERE item_id = ?",
                            (l_row["item_id"],),
                        )

                pending = conn.execute(
                    "SELECT * FROM backup_pending WHERE status = 'pending' ORDER BY created_at ASC"
                ).fetchall()

            if allowed_remote_paths is not None:
                pending = [row for row in pending if row["remote_path"] in allowed_remote_paths]
            client: Any = None
            owns_client = False

            def _build_probe_client() -> tuple[Any, bool]:
                """Standard client chain: injected -> hook -> env default (owned)."""
                if webdav_client is not None:
                    return webdav_client, False
                if hasattr(self, "_make_webdav_client"):
                    hooked = self._make_webdav_client(settings)
                    if hooked is not None:
                        return hooked, False
                if (
                    getattr(settings, "webdav_base_url", None)
                    and getattr(settings, "webdav_username", None)
                    and getattr(settings, "webdav_password", None)
                ):
                    return (
                        WebDAVClient.from_env(
                            stable_publication_approved=settings.stable_publication_approved,
                            upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
                        ),
                        True,
                    )
                return None, False

            if not pending:
                needs_index_repair = False
                probe_client: Any = None
                owns_probe = False
                if force or (allowed_remote_paths is not None and expected_artifacts is not None):
                    with suppress(Exception):
                        probe_client, owns_probe = _build_probe_client()
                try:
                    if force and probe_client is not None:
                        with suppress(Exception):
                            needs_index_repair = await self._remote_index_repair_needed(
                                probe_client, canonical_remote_root
                            )
                    if not needs_index_repair:
                        with self._get_connection() as conn:
                            total_remaining = conn.execute(
                                "SELECT COUNT(*) FROM backup_pending WHERE status IN ('pending', 'lost_source')"
                            ).fetchone()[0]
                        diagnostics = self._scope_diagnostics(
                            allowed_remote_paths, expected_artifacts, canonical_remote_root
                        )
                        scope_complete: bool | None = None
                        if allowed_remote_paths is not None and expected_artifacts is not None:
                            if probe_client is None:
                                with suppress(Exception):
                                    probe_client, owns_probe = _build_probe_client()
                            if probe_client is not None:
                                try:
                                    scope_complete = await self._scoped_completion(
                                        probe_client,
                                        expected_artifacts,
                                        diagnostics,
                                        canonical_remote_root,
                                    )
                                except Exception:
                                    scope_complete = None
                        return {
                            "status": "unchanged",
                            "flushed_count": 0,
                            "uploaded_count": 0,
                            "uploaded_bytes": 0,
                            "requests": 0,
                            "remaining_pending": total_remaining,
                            "error": None,
                            "backup_scope_complete": scope_complete,
                            "index_read_error": getattr(self, "_last_index_read_error", None),
                            **diagnostics,
                        }
                    # Pending-less index repair: continue into the commit path
                    # with the probe client handed over (standard lifecycle,
                    # lock and budget contracts below; no artifact bodies are
                    # transported here, only the index is republished).
                    client = probe_client
                    owns_client = owns_probe
                    probe_client = None
                finally:
                    if owns_probe and probe_client is not None:
                        with suppress(Exception):
                            await probe_client.close()

            # Check triggers when not explicitly forced
            if not force:
                status = self.get_status(settings)
                if not status["should_trigger"]:
                    return {
                        "status": "deferred",
                        "flushed_count": 0,
                        "uploaded_count": 0,
                        "uploaded_bytes": 0,
                        "requests": 0,
                        "remaining_pending": status["pending_count"],
                        "error": None,
                        "backup_scope_complete": None,
                        **self._scope_diagnostics(
                            allowed_remote_paths, expected_artifacts, canonical_remote_root
                        ),
                    }

            if client is None:
                client = webdav_client
            if client is None and hasattr(self, "_make_webdav_client"):
                client = self._make_webdav_client(settings)
            if client is None:
                if (
                    not getattr(settings, "webdav_base_url", None)
                    or not getattr(settings, "webdav_username", None)
                    or not getattr(settings, "webdav_password", None)
                ):
                    return {
                        "status": "config_error",
                        "error": "WebDAV credentials are not configured",
                        "pending_count": len(pending),
                        "flushed_count": 0,
                        "uploaded_count": 0,
                        "uploaded_bytes": 0,
                        "requests": 0,
                        "backup_scope_complete": None,
                        **self._scope_diagnostics(
                            allowed_remote_paths, expected_artifacts, canonical_remote_root
                        ),
                    }
                try:
                    client = WebDAVClient.from_env(
                        stable_publication_approved=settings.stable_publication_approved,
                        upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
                    )
                    owns_client = True
                except Exception as exc:
                    return {
                        "status": "config_error",
                        "error": f"Failed to initialize WebDAV client: {exc}",
                        "pending_count": len(pending),
                        "flushed_count": 0,
                        "uploaded_count": 0,
                        "uploaded_bytes": 0,
                        "requests": 0,
                        "backup_scope_complete": None,
                        **self._scope_diagnostics(
                            allowed_remote_paths, expected_artifacts, canonical_remote_root
                        ),
                    }

            COMMIT_RESERVE_SECONDS = 45.0
            data_budget_seconds = (
                max(10.0, timeout_seconds - COMMIT_RESERVE_SECONDS)
                if timeout_seconds > (COMMIT_RESERVE_SECONDS + 10.0)
                else max(0.01, timeout_seconds * 0.5)
            )

            actual_uploaded_count = 0
            actual_uploaded_bytes = 0
            receipts_reused_count = 0
            receipts_reused_bytes = 0
            requests = 0
            body_requests = 0
            control_requests = 0
            started = time.monotonic()
            last_error: str | None = None
            in_scope_conflicts = 0
            held_unbound_rows = 0
            verified_in_this_batch: list[sqlite3.Row] = []

            # Check already verified receipts on this canonical_remote_root
            with self._get_connection() as conn:
                existing_verified = {
                    row["remote_path"]: (row["sha256"], row["size_bytes"])
                    for row in conn.execute(
                        "SELECT remote_path, sha256, size_bytes FROM backup_receipts WHERE remote_root = ?",
                        (canonical_remote_root,),
                    ).fetchall()
                }
                foreign_verified: dict[str, set[str]] = {}
                for f_row in conn.execute(
                    "SELECT remote_path, sha256 FROM backup_receipts WHERE remote_root <> ?",
                    (canonical_remote_root,),
                ).fetchall():
                    foreign_verified.setdefault(f_row["remote_path"], set()).add(f_row["sha256"])

            try:
                for row in pending:
                    elapsed = time.monotonic() - started
                    remaining_data_time = data_budget_seconds - elapsed
                    if remaining_data_time <= 0:
                        logger.info(
                            "Backup data processing budget reached (%.1fs); reserving budget for index commit",
                            elapsed,
                        )
                        break

                    item_id = row["item_id"]
                    remote_path = row["remote_path"]
                    local_path = Path(row["local_path"])
                    expected_sha = row["sha256"]
                    expected_size = row["size_bytes"]
                    media_type = row["media_type"]

                    # Content binding: a row is only transportable because it
                    # equals the verified artifact at that path, never merely
                    # because it shares the path.
                    binding = expected_artifacts.get(remote_path) if expected_artifacts is not None else None
                    if expected_artifacts is not None and binding is None:
                        # Scoped rows without verifiable local evidence are
                        # held explicitly: blind transport of a reuse-key or
                        # lost-evidence path would defeat the binding entirely.
                        held_unbound_rows += 1
                        note = f"Held unverified scoped row {item_id} at {remote_path}"
                        if last_error is None:
                            last_error = note
                        logger.warning("%s", note)
                        continue
                    if binding is not None and (
                        binding[0] != expected_sha or (binding[1] is not None and binding[1] != expected_size)
                    ):
                        in_scope_conflicts += 1
                        last_error = (
                            f"Scoped pending {item_id} at {remote_path} disagrees with the "
                            f"verified artifact {binding[0][:12]}/{binding[1]}"
                        )
                        logger.warning("%s; transport blocked", last_error)
                        continue
                    if (
                        binding is not None
                        and remote_path in existing_verified
                        and existing_verified[remote_path][0] != binding[0]
                    ):
                        in_scope_conflicts += 1
                        last_error = (
                            f"Verified receipt for {remote_path} disagrees with the sealed artifact; "
                            "remote overwrite blocked for operator review"
                        )
                        logger.warning("%s", last_error)
                        continue
                    if binding is not None and any(
                        f_sha != binding[0] for f_sha in foreign_verified.get(remote_path, ())
                    ):
                        in_scope_conflicts += 1
                        last_error = (
                            f"A receipt at a different remote root binds {remote_path} to different "
                            "bytes; remote overwrite blocked for operator review"
                        )
                        logger.warning("%s", last_error)
                        continue

                    # If already uploaded and verified to this remote_root in a previous attempt, skip upload
                    if remote_path in existing_verified and existing_verified[remote_path] == (
                        expected_sha,
                        expected_size,
                    ):
                        verified_in_this_batch.append(row)
                        receipts_reused_count += 1
                        receipts_reused_bytes += expected_size
                        continue

                    if not local_path.is_file():
                        spool_candidate = spool_dir / expected_sha
                        if spool_candidate.is_file():
                            local_path = spool_candidate
                        else:
                            with self._get_connection() as conn:
                                conn.execute(
                                    "UPDATE backup_pending SET status = 'lost_source' WHERE item_id = ?",
                                    (item_id,),
                                )
                            last_error = f"Source file missing for {item_id}: {local_path}"
                            logger.warning(last_error)
                            continue

                    try:
                        file_content = local_path.read_bytes()
                        if binding is not None and sha256_bytes(file_content) != binding[0]:
                            in_scope_conflicts += 1
                            last_error = (
                                f"Local bytes for {remote_path} differ from the verified artifact "
                                "(spool/source corruption?); transport blocked"
                            )
                            logger.warning("%s", last_error)
                            continue

                        async def _transfer_and_verify(
                            r_path: str = remote_path,
                            f_content: bytes = file_content,
                            m_type: str = media_type,
                            e_size: int = expected_size,
                            e_sha: str = expected_sha,
                            l_path: Path = local_path,
                        ) -> None:
                            nonlocal requests, body_requests
                            if hasattr(client, "put_bytes"):
                                await client.put_bytes(
                                    r_path,
                                    f_content,
                                    content_type=m_type,
                                )
                            elif hasattr(client, "put"):
                                await client.put(r_path, f_content)
                            elif hasattr(client, "put_cas_file"):
                                await client.put_cas_file(
                                    l_path,
                                    media_type=m_type,
                                    expected_sha256=e_sha,
                                    expected_size_bytes=e_size,
                                )
                            requests += 1
                            body_requests += 1

                            # If client did not self-verify in put_bytes, verify via get
                            if not hasattr(client, "put_bytes") and not hasattr(client, "put_cas_file"):
                                if hasattr(client, "get_bytes"):
                                    content = await client.get_bytes(r_path, max_bytes=e_size + 1024)
                                elif hasattr(client, "get"):
                                    content = await client.get(r_path)
                                elif hasattr(client, "core"):
                                    content = client.core.get(r_path, max_bytes=e_size + 1024).content
                                else:
                                    content = f_content
                                requests += 1
                                body_requests += 1
                                if (
                                    content is None
                                    or len(content) != e_size
                                    or sha256_bytes(content) != e_sha
                                ):
                                    raise RuntimeError(f"Remote verification failed for {r_path}")

                        item_timeout = min(30.0, max(0.01, remaining_data_time))
                        await asyncio.wait_for(_transfer_and_verify(), timeout=item_timeout)

                        with self._get_connection() as conn:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (
                                    remote_path,
                                    canonical_remote_root,
                                    expected_sha,
                                    expected_size,
                                    time.time(),
                                ),
                            )

                        verified_in_this_batch.append(row)
                        actual_uploaded_count += 1
                        actual_uploaded_bytes += expected_size
                    except TimeoutError:
                        last_error = "Operation timed out"
                        logger.warning("Backup timeout budget exceeded during %s", remote_path)
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        logger.warning("Failed to backup %s: %s", remote_path, exc)
                        break

                index_committed = False
                needs_index_repair = False
                if not verified_in_this_batch and client is not None:
                    with suppress(Exception):
                        remote_entries = await self._remote_index_entries(client)
                        if remote_entries is not None:
                            with self._get_connection() as conn:
                                receipt_rows_check = conn.execute(
                                    "SELECT remote_path, sha256 FROM backup_receipts WHERE remote_root = ?",
                                    (canonical_remote_root,),
                                ).fetchall()
                            for r in receipt_rows_check:
                                if remote_entries.get(r["remote_path"]) != r["sha256"]:
                                    needs_index_repair = True
                                    break

                if verified_in_this_batch or needs_index_repair:
                    try:
                        elapsed = time.monotonic() - started
                        rem_time = max(10.0, timeout_seconds - elapsed)

                        async def _publish_index_and_pointer() -> None:
                            nonlocal requests, control_requests
                            with self._get_connection() as conn:
                                receipt_rows = conn.execute(
                                    "SELECT remote_path, sha256, size_bytes FROM backup_receipts WHERE remote_root = ?",
                                    (canonical_remote_root,),
                                ).fetchall()

                            items_by_path: dict[str, Any] = {}
                            index_read = await self._read_backup_index(client)
                            requests += 1
                            control_requests += 1
                            if index_read.state == "unreadable":
                                # Never merge "unknown" into an empty document: a
                                # transient/auth/size/parse failure would drop
                                # remote-only items from the republished index.
                                raise RuntimeError(
                                    "existing backup index is unreadable "
                                    f"({index_read.error_kind}); refusing to overwrite it"
                                )
                            if index_read.state == "present" and index_read.payload is not None:
                                for item in index_read.payload["items"]:
                                    items_by_path[item["remote_path"]] = dict(item)

                            with self._get_connection() as conn:
                                for r in receipt_rows:
                                    r_path = r["remote_path"]
                                    entry: dict[str, Any] = {
                                        "remote_path": r_path,
                                        "sha256": r["sha256"],
                                        "size_bytes": r["size_bytes"],
                                    }
                                    doc_map = conn.execute(
                                        "SELECT value FROM backup_meta WHERE key = ?",
                                        (f"doc_map_{r_path}",),
                                    ).fetchone()
                                    if doc_map:
                                        entry["document_id"] = doc_map[0]
                                    info_row = conn.execute(
                                        "SELECT value FROM backup_meta WHERE key = ?",
                                        (f"doc_info_{r_path}",),
                                    ).fetchone()
                                    if info_row:
                                        with suppress(Exception):
                                            entry.update(json.loads(info_row[0]))
                                    items_by_path[r_path] = entry

                            if expected_artifacts:
                                # Never publish index entries that contradict
                                # the verified binding (bad receipts or stale
                                # remote rows); report them via diagnostics.
                                for rel_p, (exp_sha, _exp_size) in expected_artifacts.items():
                                    item = items_by_path.get(rel_p)
                                    if item is not None and item.get("sha256") != exp_sha:
                                        items_by_path.pop(rel_p)

                            idx_doc = {
                                "schema_version": "cardrag.backup-index.v1",
                                "updated_at": datetime.now(UTC).isoformat(),
                                "items": list(items_by_path.values()),
                            }
                            idx_body = json.dumps(
                                idx_doc,
                                indent=2,
                                sort_keys=True,
                            ).encode("utf-8")

                            # 1. Publish immutable batch manifest (if full WebDAV client with put_bytes)
                            batch_sha = hashlib.sha256(idx_body).hexdigest()
                            batch_path = f"v1/backup/batches/{batch_sha}.json"
                            if hasattr(client, "put_bytes"):
                                await client.put_bytes(batch_path, idx_body, content_type="application/json")
                                requests += 1
                                control_requests += 1

                            # 2. Atomically update current index pointer (v1/backup/index.json)
                            await _atomic_replace_backup_index(client, "v1/backup/index.json", idx_body)
                            requests += 1
                            control_requests += 1

                        await asyncio.wait_for(_publish_index_and_pointer(), timeout=rem_time)
                        index_committed = True
                    except Exception as exc:
                        last_error = f"Failed to commit remote backup index: {exc}"
                        logger.warning("%s", last_error)

                # Post-commit processing: delete committed pending and unlink pinned spools
                if index_committed:
                    with self._get_connection() as conn:
                        for v_item in verified_in_this_batch:
                            conn.execute("DELETE FROM backup_pending WHERE item_id = ?", (v_item["item_id"],))

                        now_iso = datetime.now(UTC).isoformat()
                        conn.execute(
                            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES ('last_backup_at', ?)",
                            (now_iso,),
                        )
                        rem_pending = conn.execute(
                            "SELECT COUNT(*) FROM backup_pending WHERE status = 'pending'"
                        ).fetchone()[0]
                        if rem_pending == 0 and last_error is None:
                            conn.execute(
                                "INSERT OR REPLACE INTO backup_meta (key, value) VALUES ('runs_since_backup', '0')"
                            )

                    for v_item in verified_in_this_batch:
                        s_pin = spool_dir / v_item["sha256"]
                        if s_pin.is_file():
                            with suppress(OSError):
                                s_pin.unlink()

                with self._get_connection() as conn:
                    rem_pending = conn.execute(
                        "SELECT COUNT(*) FROM backup_pending WHERE status = 'pending'"
                    ).fetchone()[0]
                    rem_lost = conn.execute(
                        "SELECT COUNT(*) FROM backup_pending WHERE status = 'lost_source'"
                    ).fetchone()[0]
                total_remaining = rem_pending + rem_lost
                diagnostics = self._scope_diagnostics(
                    allowed_remote_paths,
                    expected_artifacts,
                    canonical_remote_root,
                    transport_conflicts=in_scope_conflicts,
                )
                # Selected-run completion is an explicit conclusion, never a
                # bare zero or a commit of the other items: every bound
                # artifact needs a matching receipt at this root AND an entry
                # with the same digest in the committed remote index, with no
                # remaining, held-unbound, missing, or conflicting row.
                backup_scope_complete: bool | None = None
                if allowed_remote_paths is not None and expected_artifacts is not None and client is not None:
                    # Uncommitted uploads keep their rows pending, so the
                    # remaining check below already refuses completion.
                    backup_scope_complete = await self._scoped_completion(
                        client, expected_artifacts, diagnostics, canonical_remote_root
                    )

                processed_count = len(verified_in_this_batch)
                processed_bytes = actual_uploaded_bytes + receipts_reused_bytes

                if rem_pending == 0 and rem_lost == 0 and last_error is None:
                    status_str = "succeeded"
                elif index_committed:
                    status_str = "degraded"
                elif processed_count > 0:
                    status_str = "pending_commit"
                else:
                    status_str = "failed"
                if status_str == "succeeded" and backup_scope_complete is False:
                    # The global ledger drained, but selected evidence proves
                    # this scope is not complete; do not report success.
                    status_str = "degraded"

                return {
                    "status": status_str,
                    "flushed_count": processed_count if index_committed else 0,
                    "processed_count": processed_count,
                    "processed_bytes": processed_bytes,
                    "uploaded_count": processed_count if index_committed else 0,
                    "uploaded_bytes": processed_bytes if index_committed else 0,
                    "actual_uploaded_count": getattr(client, "actual_uploaded_count", None),
                    "actual_uploaded_bytes": getattr(client, "actual_uploaded_bytes", None),
                    "verified_existing_count": receipts_reused_count,
                    "verified_existing_bytes": receipts_reused_bytes,
                    "receipts_reused_count": receipts_reused_count,
                    "requests": requests,
                    "body_requests": body_requests,
                    "control_requests": control_requests,
                    "remaining_pending": total_remaining,
                    **diagnostics,
                    "backup_scope_complete": backup_scope_complete,
                    "index_read_error": getattr(self, "_last_index_read_error", None),
                    "error": last_error,
                }

            finally:
                if owns_client and client is not None:
                    await client.close()
        finally:
            if lock_fd is not None:
                with suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)

    async def recover_verified_ocr_metadata(
        self,
        settings: WorkerSettings,
        run_id: str,
        selected_issuers: tuple[str, ...],
        webdav_client: WebDAVClient | None = None,
        keys: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Verify current native OCR metadata pairs from WebDAV and restore local evidence.

        FIX_18: a retained canonical manifest/READY pair whose remote bytes are
        provably the current sealed document's OCR identity is legitimate backup
        evidence even when the local cache copy is gone.  This path fetches only
        the control metadata (manifest/READY), verifies it against the current
        seal identity AND this root's receipts byte-for-byte, then materializes
        the standard local cache evidence so the ordinary scope/flush contract
        rebinds it.  It never writes remotely, never calls providers/OCR, never
        edits receipts, seals, or pending rows, and never overwrites a differing
        local artifact (a local canonical manifest of another variant is kept;
        the verified pair is placed under its variant cache directory instead).
        Missing, corrupt, receipt-less, or identity-mismatched remote pairs stay
        unverified and keep blocking selected completion.
        """
        from .partial_execution import read_json

        state_dir = settings.state_dir
        canonical_root = _canonical_remote_root(settings)
        seal_path = state_dir / "runs" / run_id / "sealed/publish.json"
        if not seal_path.is_file():
            return {
                "status": "config_error",
                "error": "run seal is unavailable; retention may have pruned it",
                "run_id": run_id,
            }
        sealed = read_json(state_dir, seal_path, "backup")

        _, expected_before = self.scoped_backup_scope(
            run_id, state_dir, selected_issuers, remote_root=canonical_root
        )
        allowed_before = set(
            self.scope_remote_paths(run_id, state_dir, selected_issuers, remote_root=canonical_root)
        )
        unbound_before = allowed_before - set(expected_before)

        candidates: dict[str, dict[str, Any]] = {}
        for doc in sealed["manifest"]["documents"]:
            if doc.get("issuer") not in selected_issuers:
                continue
            key = doc.get("ocr_reuse_key")
            kind = doc.get("ocr_cache_kind") or "native"
            if not key or kind != "native":
                continue
            entry = candidates.setdefault(key, {"documents": [], "kind": kind, "identities": set()})
            entry["documents"].append(doc)

        if keys is not None:
            wanted = set(keys)
            candidates = {key: entry for key, entry in candidates.items() if key in wanted}

        if not candidates:
            return {
                "status": "empty",
                "run_id": run_id,
                "verified_count": 0,
                "pairs": [],
                "message": "no selected native OCR reuse keys",
            }

        client = webdav_client
        if client is None and hasattr(self, "_make_webdav_client"):
            client = self._make_webdav_client(settings)
        owns_client = False
        if client is None:
            if (
                not getattr(settings, "webdav_base_url", None)
                or not getattr(settings, "webdav_username", None)
                or not getattr(settings, "webdav_password", None)
            ):
                return {"status": "config_error", "error": "WebDAV credentials are not configured"}
            try:
                client = WebDAVClient.from_env(
                    stable_publication_approved=settings.stable_publication_approved,
                    upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
                )
                owns_client = True
            except Exception as exc:
                return {"status": "config_error", "error": f"Failed to initialize WebDAV client: {exc}"}

        async def _read_remote(path: str, max_bytes: int) -> bytes | None:
            try:
                if hasattr(client, "get_bytes"):
                    body = await client.get_bytes(path, max_bytes=max_bytes)
                elif hasattr(client, "get"):
                    body = await client.get(path)
                elif hasattr(client, "core"):
                    body = client.core.get(path, max_bytes=max_bytes).content
                else:
                    return None
            except Exception:
                return None
            if not isinstance(body, (bytes, bytearray)) or len(body) > max_bytes:
                return None
            return bytes(body)

        def _root_receipt(path: str, sha: str, size: int) -> bool:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT size_bytes FROM backup_receipts"
                    " WHERE remote_path = ? AND remote_root = ? AND sha256 = ?",
                    (path, canonical_root, sha),
                ).fetchone()
            return row is not None and row["size_bytes"] == size

        lock_descriptor = acquire_cache_recovery_lock(state_dir)
        if lock_descriptor is None:
            if owns_client and client is not None:
                with suppress(Exception):
                    await client.close()
            return {
                "status": "locked",
                "run_id": run_id,
                "error": "another local OCR cache writer is in progress",
                "verified_count": 0,
                "pairs": [],
            }

        pairs: list[dict[str, Any]] = []
        verified = 0
        try:
            for key, entry in candidates.items():
                docs: list[dict[str, Any]] = entry["documents"]
                identities = {
                    (
                        doc["pdf"]["sha256"],
                        doc["pdf"].get("size_bytes"),
                        doc.get("page_count"),
                        doc["ocr"]["sha256"],
                        doc["ocr"].get("size_bytes"),
                    )
                    for doc in docs
                }
                doc = docs[0]
                ocr_sha = doc["ocr"]["sha256"]
                can_man = ocr_manifest_path(key, kind="native").as_posix()
                can_ready = ocr_ready_path(key, kind="native").as_posix()
                var_man = ocr_variant_manifest_path(key, ocr_sha, kind="native").as_posix()
                var_ready = ocr_variant_ready_path(key, ocr_sha, kind="native").as_posix()
                result: dict[str, Any] = {
                    "reuse_key": key,
                    "document_ids": [d.get("document_id") for d in docs],
                    "action": "error",
                    "error": None,
                }
                pairs.append(result)

                if len(identities) != 1:
                    result["error"] = "sealed documents disagree on the native OCR identity for this key"
                    continue
                pdf_sha, pdf_size, page_count, _, ocr_size = next(iter(identities))
                if (can_man in expected_before and can_ready in expected_before) or (
                    var_man in expected_before and var_ready in expected_before
                ):
                    result["action"] = "already_bound"
                    result["error"] = None
                    verified += 1
                    continue

                # A candidate is the complete unit: manifest bytes + identity +
                # root receipt + canonical READY pairing + READY receipt + the
                # routing result the current scope actually requires.  A partial
                # or invalid variant must not stop a healthy canonical pair from
                # being recovered (and vice versa).
                selected_pair: tuple[str, str, bool, bytes, OCRArtifactManifest, bytes] | None = None
                candidate_errors: list[str] = []
                for man_path, ready_path, is_variant in (
                    (var_man, var_ready, True),
                    (can_man, can_ready, False),
                ):
                    label = "variant" if is_variant else "canonical"
                    body = await _read_remote(man_path, _LOCAL_CACHE_MANIFEST_MAX_BYTES)
                    if body is None:
                        candidate_errors.append(f"{label}: manifest unavailable")
                        continue
                    manifest: OCRArtifactManifest | None = None
                    with suppress(Exception):
                        manifest = OCRArtifactManifest.model_validate_json(body)
                    if manifest is None:
                        candidate_errors.append(f"{label}: manifest is not canonical JSON")
                        continue
                    if not (
                        manifest.canonical_bytes() == body
                        and manifest.reuse_key == key
                        and manifest.source.pdf_sha256 == pdf_sha
                        and (not isinstance(pdf_size, int) or manifest.source.pdf_size_bytes == pdf_size)
                        and (not isinstance(page_count, int) or manifest.source.page_count == page_count)
                        and manifest.output.sha256 == ocr_sha
                        and (not isinstance(ocr_size, int) or manifest.output.size_bytes == ocr_size)
                    ):
                        candidate_errors.append(f"{label}: manifest identity mismatch")
                        continue
                    candidate_man_sha = sha256_bytes(body)
                    if not _root_receipt(man_path, candidate_man_sha, len(body)):
                        candidate_errors.append(f"{label}: manifest lacks a matching root receipt")
                        continue
                    ready = OCRReady(
                        reuse_key=key,
                        manifest_sha256=candidate_man_sha,
                        ocr_sha256=manifest.output.sha256,
                    )
                    candidate_ready = ready.canonical_bytes()
                    remote_ready = await _read_remote(ready_path, _LOCAL_CACHE_READY_MAX_BYTES)
                    if remote_ready != candidate_ready:
                        candidate_errors.append(f"{label}: READY is absent, corrupt, or unpaired")
                        continue
                    candidate_ready_sha = sha256_bytes(candidate_ready)
                    if not _root_receipt(ready_path, candidate_ready_sha, len(candidate_ready)):
                        candidate_errors.append(f"{label}: READY lacks a matching root receipt")
                        continue
                    with self._get_connection() as conn:
                        routed = resolve_ocr_bundle_paths(
                            state_dir,
                            key,
                            ocr_sha,
                            candidate_man_sha,
                            is_generation_only=False,
                            pdf_sha256=pdf_sha,
                            conn=conn,
                            remote_root=canonical_root,
                        )
                    if (routed[1], routed[2]) != (man_path, ready_path):
                        candidate_errors.append(f"{label}: pair does not match current scope routing")
                        continue
                    selected_pair = (man_path, ready_path, is_variant, body, manifest, candidate_ready)
                    break
                if selected_pair is None:
                    result["error"] = "; ".join(candidate_errors) or "no verifiable current native pair"
                    continue

                man_path, ready_path, is_variant, manifest_body, manifest_obj, ready_bytes = selected_pair
                man_sha = sha256_bytes(manifest_body)
                ready_sha = sha256_bytes(ready_bytes)

                # Choose the write target without clobbering another local
                # variant or an unreadable/partial canonical leaf.  Unsafe
                # boundaries are rejected with an error, never followed.
                cache_dir = state_dir / "cache" / "ocr" / key
                canonical_manifest, canonical_manifest_error = _read_cache_leaf(
                    cache_dir / "manifest.json", state_dir, _LOCAL_CACHE_MANIFEST_MAX_BYTES
                )
                if canonical_manifest_error is not None:
                    result["error"] = f"existing canonical manifest is unsafe: {canonical_manifest_error}"
                    continue
                canonical_ready, canonical_ready_error = _read_cache_leaf(
                    cache_dir / "READY.json", state_dir, _LOCAL_CACHE_READY_MAX_BYTES
                )
                if canonical_ready_error is not None:
                    result["error"] = f"existing canonical READY is unsafe: {canonical_ready_error}"
                    continue
                canonical_matches = canonical_manifest == manifest_body and canonical_ready == ready_bytes
                canonical_empty = canonical_manifest is None and canonical_ready is None
                target_dir = cache_dir
                if is_variant or not (canonical_matches or canonical_empty):
                    target_dir = cache_dir / "variants" / ocr_sha

                action, write_error = publish_cache_pair(state_dir, target_dir, manifest_body, ready_bytes)
                if write_error is not None:
                    result["error"] = write_error
                    continue
                result.update(
                    {
                        "action": action,
                        "manifest_path": man_path,
                        "ready_path": ready_path,
                        "manifest_sha256": man_sha,
                        "ready_sha256": ready_sha,
                        "local_paths": [
                            (target_dir / "manifest.json").as_posix(),
                            (target_dir / "READY.json").as_posix(),
                        ],
                        "verified": True,
                        "error": None,
                    }
                )
                verified += 1
        finally:
            release_cache_recovery_lock(lock_descriptor)
            if owns_client and client is not None:
                with suppress(Exception):
                    await client.close()

        _, expected_after = self.scoped_backup_scope(
            run_id, state_dir, selected_issuers, remote_root=canonical_root
        )
        allowed_after = set(
            self.scope_remote_paths(run_id, state_dir, selected_issuers, remote_root=canonical_root)
        )
        unbound_after = allowed_after - set(expected_after)
        failed = [p for p in pairs if p.get("action") == "error"]
        return {
            "status": "succeeded" if not failed else "partial",
            "run_id": run_id,
            "pairs": pairs,
            "verified_count": verified,
            "failed_count": len(failed),
            "in_scope_unbound_before": len(unbound_before),
            "in_scope_unbound_after": len(unbound_after),
        }

    async def audit(
        self,
        settings: WorkerSettings,
        webdav_client: WebDAVClient | None = None,
        *,
        full: bool = False,
    ) -> dict[str, Any]:
        canonical_remote_root = _canonical_remote_root(settings)
        with self._get_connection() as conn:
            receipts = conn.execute(
                "SELECT * FROM backup_receipts WHERE remote_root = ? ORDER BY verified_at DESC",
                (canonical_remote_root,),
            ).fetchall()
        if not receipts:
            return {"status": "empty", "verified_count": 0, "receipts_total": 0}

        sample = receipts if full else receipts[:20]

        client = webdav_client
        if client is None and hasattr(self, "_make_webdav_client"):
            client = self._make_webdav_client(settings)
        owns_client = False
        if client is None:
            if (
                not getattr(settings, "webdav_base_url", None)
                or not getattr(settings, "webdav_username", None)
                or not getattr(settings, "webdav_password", None)
            ):
                return {"status": "config_error", "error": "WebDAV credentials are not configured"}
            try:
                client = WebDAVClient.from_env(
                    stable_publication_approved=settings.stable_publication_approved,
                    upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
                )
                owns_client = True
            except Exception as exc:
                return {"status": "config_error", "error": str(exc)}

        verified = 0
        failed = 0
        errors: list[str] = []
        try:
            for row in sample:
                remote_path = row["remote_path"]
                expected_sha = row["sha256"]
                expected_size = row["size_bytes"]
                try:
                    if hasattr(client, "get_bytes"):
                        content = await client.get_bytes(remote_path, max_bytes=expected_size + 1024)
                    elif hasattr(client, "get"):
                        content = await client.get(remote_path)
                    elif hasattr(client, "core"):
                        content = client.core.get(remote_path, max_bytes=expected_size + 1024).content
                    else:
                        content = b""
                    if (
                        content is not None
                        and len(content) == expected_size
                        and sha256_bytes(content) == expected_sha
                    ):
                        verified += 1
                    else:
                        failed += 1
                        errors.append(f"Mismatched content for {remote_path}")
                except Exception as exc:
                    failed += 1
                    errors.append(f"{remote_path}: {exc}")

            return {
                "status": "succeeded" if failed == 0 else "failed",
                "sample_size": len(sample),
                "verified_count": verified,
                "verified_receipts": verified,
                "missing_receipts": failed,
                "failed_count": failed,
                "total_receipts": len(receipts),
                "errors": errors[:5],
            }
        finally:
            if owns_client and client is not None:
                await client.close()

    async def restore(
        self,
        settings: WorkerSettings,
        target_dir: Path,
        webdav_client: WebDAVClient | None = None,
    ) -> dict[str, Any]:
        """Restore backed up OCR cache objects from WebDAV into local target_dir."""
        target_dir = target_dir.resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        canonical_remote_root = _canonical_remote_root(settings)

        client = webdav_client
        if client is None and hasattr(self, "_make_webdav_client"):
            client = self._make_webdav_client(settings)
        owns_client = False
        if client is None:
            if (
                not getattr(settings, "webdav_base_url", None)
                or not getattr(settings, "webdav_username", None)
                or not getattr(settings, "webdav_password", None)
            ):
                return {"status": "config_error", "error": "WebDAV credentials are not configured"}
            client = WebDAVClient.from_env(
                stable_publication_approved=settings.stable_publication_approved,
                upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
            )
            owns_client = True

        lock_descriptor = acquire_cache_recovery_lock(settings.state_dir)
        if lock_descriptor is None:
            if owns_client and client is not None:
                with suppress(Exception):
                    await client.close()
            return {
                "status": "locked",
                "error": "another local OCR cache writer is in progress",
                "restored_count": 0,
                "restored_bytes": 0,
            }

        with self._get_connection() as conn:
            receipts = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM backup_receipts WHERE remote_root = ?",
                    (canonical_remote_root,),
                ).fetchall()
            ]

        # Fresh host discovery from remote index if local receipts is empty.
        # The same bounded reader distinguishes absent from unreadable; an
        # unreadable index yields no discovery rather than a false empty.
        if not receipts:
            index_read = await self._read_backup_index(client)
            if index_read.state == "present" and index_read.payload is not None:
                receipts = list(index_read.payload["items"])
            elif index_read.state == "unreadable":
                logger.warning(
                    "Failed to read remote backup index during restore (%s); no receipt discovery performed",
                    index_read.error_kind,
                )
                release_cache_recovery_lock(lock_descriptor)
                if owns_client and client is not None:
                    await client.close()
                return {
                    "status": "failed",
                    "error": f"remote backup index is unreadable ({index_read.error_kind})",
                    "index_read_error": index_read.error_kind,
                    "restored_count": 0,
                    "restored_bytes": 0,
                }

        if not receipts:
            release_cache_recovery_lock(lock_descriptor)
            if owns_client and client is not None:
                await client.close()
            return {"status": "empty", "restored_count": 0, "restored_bytes": 0}

        restored_count = 0
        restored_bytes = 0
        failed_items: list[str] = []
        try:
            for row in receipts:
                remote_path = row["remote_path"]
                expected_sha = row["sha256"]
                expected_size = row["size_bytes"]
                dest = target_dir / remote_path
                if dest.is_file():
                    actual_sha, actual_size = _sha256_file(dest)
                    if actual_sha == expected_sha and actual_size == expected_size:
                        restored_count += 1
                        restored_bytes += expected_size
                        continue

                dest.parent.mkdir(parents=True, exist_ok=True)
                content = None
                try:
                    if hasattr(client, "get_bytes"):
                        content = await client.get_bytes(remote_path, max_bytes=expected_size + 1024)
                    elif hasattr(client, "get"):
                        content = await client.get(remote_path)
                    elif hasattr(client, "core"):
                        content = client.core.get(remote_path, max_bytes=expected_size + 1024).content
                except Exception as exc:
                    failed_items.append(f"Failed to fetch {remote_path}: {exc}")
                    continue

                if content is None or len(content) != expected_size or sha256_bytes(content) != expected_sha:
                    failed_items.append(f"Verification mismatch for {remote_path}")
                    continue

                dest.write_bytes(content)
                if remote_path.startswith("v1/caches/ocr/"):
                    cache_rel = remote_path[len("v1/caches/ocr/") :]
                    cache_target = target_dir / "cache" / "ocr" / cache_rel
                    cache_target.parent.mkdir(parents=True, exist_ok=True)
                    if not cache_target.exists():
                        with suppress(OSError):
                            os.link(dest, cache_target)
                        if not cache_target.exists():
                            cache_target.write_bytes(content)

                with self._get_connection() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (remote_path, canonical_remote_root, expected_sha, expected_size, time.time()),
                    )

                restored_count += 1
                restored_bytes += expected_size

            # Materialize native OCR artifacts into state seed and cache structure for local OCRResolver
            manifest_files = list(target_dir.glob("v1/ocr-cache/native/**/manifest.json"))
            ocr_documents_for_ledger = []
            for man_file in manifest_files:
                try:
                    man_bytes = man_file.read_bytes()
                    manifest = OCRArtifactManifest.model_validate_json(man_bytes)
                    cas_sha = manifest.output.sha256
                    cas_dest = target_dir / object_path(cas_sha)
                    if not cas_dest.is_file():
                        continue
                    body_bytes = cas_dest.read_bytes()
                    if hashlib.sha256(body_bytes).hexdigest() != cas_sha:
                        continue

                    matched = next(
                        (
                            r
                            for r in receipts
                            if r.get("sha256") == hashlib.sha256(man_bytes).hexdigest()
                            or r.get("remote_path") == man_file.relative_to(target_dir).as_posix()
                            or r.get("reuse_key") == manifest.reuse_key
                            or r.get("remote_path")
                            == ocr_manifest_path(manifest.reuse_key, kind="native").as_posix()
                        ),
                        {},
                    )
                    doc_id = matched.get("document_id") or "doc1"

                    # 1. Seed into ocr-seed/{doc_id}
                    seed_dir = target_dir / "ocr-seed" / doc_id
                    seed_dir.mkdir(parents=True, exist_ok=True)
                    (seed_dir / "ocr.md").write_bytes(body_bytes)
                    (seed_dir / "manifest.json").write_bytes(man_bytes)
                    ready = OCRReady(
                        reuse_key=manifest.reuse_key,
                        manifest_sha256=hashlib.sha256(man_bytes).hexdigest(),
                        ocr_sha256=cas_sha,
                    )
                    ready_bytes = ready.canonical_bytes()
                    (seed_dir / "READY.json").write_bytes(ready_bytes)

                    # 2. Seed into cache/ocr/{reuse_key}
                    cache_dir = target_dir / "cache" / "ocr" / manifest.reuse_key
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    is_variant = "variants" in man_file.parts
                    if is_variant:
                        var_cache_dir = cache_dir / "variants" / cas_sha
                        var_cache_dir.mkdir(parents=True, exist_ok=True)
                        (var_cache_dir / "ocr.md").write_bytes(body_bytes)
                        (var_cache_dir / "manifest.json").write_bytes(man_bytes)
                        (var_cache_dir / "READY.json").write_bytes(ready_bytes)
                    if not (cache_dir / "manifest.json").is_file() or not is_variant:
                        (cache_dir / "ocr.md").write_bytes(body_bytes)
                        (cache_dir / "manifest.json").write_bytes(man_bytes)
                        (cache_dir / "READY.json").write_bytes(ready_bytes)

                    ocr_documents_for_ledger.append(
                        {
                            "document_id": doc_id,
                            "issuer": "unknown",
                            "source_id": None,
                            "pdf_sha256": manifest.source.pdf_sha256,
                            "pdf_size_bytes": manifest.source.pdf_size_bytes,
                            "page_count": manifest.source.page_count,
                            "ocr_sha256": cas_sha,
                            "ocr_size_bytes": manifest.output.size_bytes,
                            "kind": "native",
                            "reuse_key": manifest.reuse_key,
                            "manifest_sha256": hashlib.sha256(man_bytes).hexdigest(),
                            "ready_sha256": hashlib.sha256(ready_bytes).hexdigest(),
                            "model": manifest.contract.model,
                        }
                    )
                except Exception as exc:
                    logger.warning("Failed to materialize restored OCR artifact %s: %s", man_file, exc)

            if ocr_documents_for_ledger:
                ledger_data = {
                    "schema_version": "cardrag.ocr-recovery-ledger.v1",
                    "status": "applied",
                    "generation_id": f"gen-restored-{uuid.uuid4().hex[:8]}",
                    "run_id": f"run-restored-{uuid.uuid4().hex[:8]}",
                    "corpus_sha256": "0" * 64,
                    "contract_sha256": "0" * 64,
                    "source_records": [],
                    "ocr_documents": ocr_documents_for_ledger,
                }
                l_bytes = canonical_json_bytes(ledger_data)
                l_sha = hashlib.sha256(l_bytes).hexdigest()
                l_dir = target_dir / "audit-reports" / "state-seed"
                l_dir.mkdir(parents=True, exist_ok=True)
                (l_dir / f"{l_sha}.json").write_bytes(l_bytes)

            status_str = "failed" if failed_items else "succeeded"
            return {
                "status": status_str,
                "restored_count": restored_count,
                "restored_bytes": restored_bytes,
                "failed_count": len(failed_items),
                "errors": failed_items[:5] if failed_items else None,
            }
        finally:
            release_cache_recovery_lock(lock_descriptor)
            if owns_client and client is not None:
                await client.close()
