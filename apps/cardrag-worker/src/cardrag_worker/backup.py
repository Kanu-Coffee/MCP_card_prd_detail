"""Incremental WebDAV backup manager, ledger, and CLI operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cardrag_core import (
    OCRArtifactManifest,
    OCRReady,
    object_path,
    ocr_manifest_path,
    ocr_ready_path,
    sha256_bytes,
)

from .settings import WorkerSettings
from .webdav import WebDAVClient

logger = logging.getLogger("cardrag_worker.backup")


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class BackupLedger:
    """Persistent SQLite ledger tracking unbacked artifacts and verified remote receipts."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                WHERE status != 'lost_source'
                """
            )
            row = cursor.fetchone()
            pending_count = row[0]
            pending_bytes = row[1]
            oldest_ts = row[2]
            ocr_count = row[3]

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

        return {
            "mode": settings.backup_mode,
            "status": "ready" if pending_count == 0 else "pending",
            "pending_count": pending_count,
            "pending_ocr_count": ocr_count,
            "pending_bytes": pending_bytes,
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

    def record_run_success(self, run_id: str, state_dir: Path, settings: WorkerSettings) -> None:
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

        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO backup_recorded_runs (run_id, recorded_at) VALUES (?, ?)",
                (run_id, now),
            )
            is_new_run = cur.rowcount > 0

            # Check verified receipts
            existing_receipts = {
                row["remote_path"]
                for row in conn.execute("SELECT remote_path FROM backup_receipts").fetchall()
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

            if doc_root.is_dir():
                for doc_dir in doc_root.iterdir():
                    ocr_dir = doc_dir / "ocr"
                    if not ocr_dir.is_dir():
                        continue
                    # Find native-manifest.json
                    for manifest_file in ocr_dir.glob("**/native-manifest.json"):
                        try:
                            manifest = OCRArtifactManifest.model_validate_json(manifest_file.read_bytes())
                            body_file = manifest_file.parent / "ocr.md"
                            if not body_file.is_file():
                                continue
                            # 1. OCR CAS
                            ocr_cas_rel = object_path(manifest.output.sha256).as_posix()
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
                            man_rel = ocr_manifest_path(manifest.reuse_key, kind="native").as_posix()
                            if man_rel not in existing_receipts and man_rel not in existing_pending:
                                man_bytes = manifest.canonical_bytes()
                                man_sha = hashlib.sha256(man_bytes).hexdigest()
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
                            ready_rel = ocr_ready_path(manifest.reuse_key, kind="native").as_posix()
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
                        except (OSError, ValueError):
                            continue

            # Also scan local native OCR cache if present
            cache_ocr_dir = state_dir / "cache" / "ocr"
            if cache_ocr_dir.is_dir():
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

    async def flush(
        self,
        settings: WorkerSettings,
        webdav_client: WebDAVClient | None = None,
        *,
        timeout_seconds: float = 300.0,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._get_connection() as conn:
            pending = conn.execute(
                "SELECT * FROM backup_pending WHERE status != 'lost_source' ORDER BY created_at ASC"
            ).fetchall()
        if not pending:
            return {
                "status": "unchanged",
                "flushed_count": 0,
                "uploaded_count": 0,
                "uploaded_bytes": 0,
                "requests": 0,
                "remaining_pending": 0,
                "error": None,
            }

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
                return {
                    "status": "config_error",
                    "error": "WebDAV credentials are not configured",
                    "pending_count": len(pending),
                    "flushed_count": 0,
                    "uploaded_count": 0,
                    "uploaded_bytes": 0,
                    "requests": 0,
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
                }

        uploaded_count = 0
        uploaded_bytes = 0
        requests = 0
        started = time.monotonic()
        last_error: str | None = None
        spool_dir = getattr(settings, "state_dir", self.db_path.parent) / "backup" / "spool"
        remote_root = str(getattr(settings, "webdav_base_url", "") or "")

        try:
            for row in pending:
                elapsed = time.monotonic() - started
                remaining_time = timeout_seconds - elapsed
                if remaining_time <= 0:
                    logger.warning("Backup inline budget reached; deferring remaining items")
                    break

                item_id = row["item_id"]
                remote_path = row["remote_path"]
                local_path = Path(row["local_path"])
                expected_sha = row["sha256"]
                expected_size = row["size_bytes"]
                media_type = row["media_type"]

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

                    async def _transfer_and_verify(
                        r_path: str = remote_path,
                        f_content: bytes = file_content,
                        m_type: str = media_type,
                        e_size: int = expected_size,
                        e_sha: str = expected_sha,
                        l_path: Path = local_path,
                    ) -> None:
                        nonlocal requests
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

                        if hasattr(client, "get_bytes"):
                            content = await client.get_bytes(r_path, max_bytes=e_size + 1024)
                        elif hasattr(client, "get"):
                            content = await client.get(r_path)
                        elif hasattr(client, "core"):
                            content = client.core.get(r_path, max_bytes=e_size + 1024).content
                        else:
                            content = f_content
                        requests += 1
                        if content is None or len(content) != e_size or sha256_bytes(content) != e_sha:
                            raise RuntimeError(f"Remote verification failed for {r_path}")

                    await asyncio.wait_for(_transfer_and_verify(), timeout=remaining_time)

                    with self._get_connection() as conn:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (remote_path, remote_root, expected_sha, expected_size, time.time()),
                        )
                        conn.execute("DELETE FROM backup_pending WHERE item_id = ?", (item_id,))

                    if (spool_dir / expected_sha).is_file():
                        with suppress(OSError):
                            (spool_dir / expected_sha).unlink()

                    uploaded_count += 1
                    uploaded_bytes += expected_size
                except TimeoutError:
                    last_error = "Operation timed out"
                    logger.warning("Backup timeout budget exceeded during %s", remote_path)
                    break
                except Exception as exc:
                    last_error = str(exc)
                    logger.warning("Failed to backup %s: %s", remote_path, exc)
                    break

            with self._get_connection() as conn:
                rem_row = conn.execute("SELECT COUNT(*) FROM backup_pending").fetchone()
                remaining_count = rem_row[0]
                if uploaded_count > 0:
                    now_iso = datetime.now(UTC).isoformat()
                    conn.execute(
                        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES ('last_backup_at', ?)",
                        (now_iso,),
                    )
                    if remaining_count == 0 and last_error is None:
                        conn.execute(
                            "INSERT OR REPLACE INTO backup_meta (key, value) VALUES ('runs_since_backup', '0')"
                        )

            # Publish remote backup index if any uploaded
            if uploaded_count > 0:
                try:
                    with self._get_connection() as conn:
                        receipt_rows = conn.execute(
                            "SELECT remote_path, sha256, size_bytes FROM backup_receipts"
                        ).fetchall()
                    items_by_path: dict[str, Any] = {}
                    raw_idx = None
                    with suppress(Exception):
                        if hasattr(client, "get_bytes"):
                            raw_idx = await client.get_bytes("v1/backup/index.json")
                        elif hasattr(client, "get"):
                            raw_idx = await client.get("v1/backup/index.json")
                        elif hasattr(client, "core"):
                            raw_idx = client.core.get("v1/backup/index.json").content
                        if raw_idx:
                            existing_idx = json.loads(
                                raw_idx.decode("utf-8") if isinstance(raw_idx, bytes) else raw_idx
                            )
                            for item in existing_idx.get("items", []):
                                items_by_path[item["remote_path"]] = item

                    for r in receipt_rows:
                        items_by_path[r["remote_path"]] = {
                            "remote_path": r["remote_path"],
                            "sha256": r["sha256"],
                            "size_bytes": r["size_bytes"],
                        }
                    idx_body = json.dumps(
                        {
                            "schema_version": "cardrag.backup-index.v1",
                            "updated_at": datetime.now(UTC).isoformat(),
                            "items": list(items_by_path.values()),
                        },
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                    if hasattr(client, "put_bytes"):
                        await client.put_bytes(
                            "v1/backup/index.json", idx_body, content_type="application/json"
                        )
                    elif hasattr(client, "put"):
                        await client.put("v1/backup/index.json", idx_body)
                except Exception as exc:
                    logger.warning("Failed to publish remote backup index: %s", exc)

            if remaining_count == 0 and last_error is None:
                status_str = "succeeded"
            elif uploaded_count > 0:
                status_str = "degraded"
            else:
                status_str = "failed"

            return {
                "status": status_str,
                "flushed_count": uploaded_count,
                "uploaded_count": uploaded_count,
                "uploaded_bytes": uploaded_bytes,
                "requests": requests,
                "remaining_pending": remaining_count,
                "error": last_error,
            }
        finally:
            if owns_client and client is not None:
                await client.close()

    async def audit(
        self,
        settings: WorkerSettings,
        webdav_client: WebDAVClient | None = None,
        *,
        full: bool = False,
    ) -> dict[str, Any]:
        with self._get_connection() as conn:
            receipts = conn.execute("SELECT * FROM backup_receipts ORDER BY verified_at DESC").fetchall()
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

        with self._get_connection() as conn:
            receipts = [dict(r) for r in conn.execute("SELECT * FROM backup_receipts").fetchall()]

        # Fresh host discovery from remote index if local receipts is empty
        if not receipts:
            try:
                raw_idx = None
                if hasattr(client, "get_bytes"):
                    raw_idx = await client.get_bytes("v1/backup/index.json")
                elif hasattr(client, "get"):
                    raw_idx = await client.get("v1/backup/index.json")
                elif hasattr(client, "core"):
                    raw_idx = client.core.get("v1/backup/index.json").content
                if raw_idx:
                    idx_doc = json.loads(raw_idx.decode("utf-8") if isinstance(raw_idx, bytes) else raw_idx)
                    receipts = idx_doc.get("items", [])
            except Exception as exc:
                logger.warning("Failed to fetch remote backup index during restore: %s", exc)

        if not receipts:
            if owns_client and client is not None:
                await client.close()
            return {"status": "empty", "restored_count": 0}

        restored_count = 0
        restored_bytes = 0
        remote_root = str(getattr(settings, "webdav_base_url", "") or "")
        try:
            for row in receipts:
                remote_path = row["remote_path"]
                expected_sha = row["sha256"]
                expected_size = row["size_bytes"]
                dest = target_dir / remote_path
                if dest.is_file():
                    actual_sha, actual_size = _sha256_file(dest)
                    if actual_sha == expected_sha and actual_size == expected_size:
                        continue

                dest.parent.mkdir(parents=True, exist_ok=True)
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
                    dest.write_bytes(content)
                    if remote_path.startswith("v1/caches/ocr/"):
                        cache_rel = remote_path[len("v1/caches/ocr/") :]
                        cache_target = target_dir / "cache" / "ocr" / cache_rel
                        cache_target.parent.mkdir(parents=True, exist_ok=True)
                        if not cache_target.exists():
                            try:
                                os.link(dest, cache_target)
                            except OSError:
                                cache_target.write_bytes(content)

                    with self._get_connection() as conn:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO backup_receipts (remote_path, remote_root, sha256, size_bytes, verified_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (remote_path, remote_root, expected_sha, expected_size, time.time()),
                        )

                    restored_count += 1
                    restored_bytes += expected_size

            return {
                "status": "succeeded",
                "restored_count": restored_count,
                "restored_bytes": restored_bytes,
            }
        finally:
            if owns_client and client is not None:
                await client.close()
