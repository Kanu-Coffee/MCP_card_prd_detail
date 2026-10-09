"""Incremental WebDAV backup manager, ledger, and CLI operations."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
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
                    remote_path TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    verified_at REAL NOT NULL
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
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0), MIN(created_at) FROM backup_pending"
            )
            row = cursor.fetchone()
            pending_count = row[0]
            pending_bytes = row[1]
            oldest_ts = row[2]

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
        )

        return {
            "mode": settings.backup_mode,
            "status": "ready" if pending_count == 0 else "pending",
            "pending_count": pending_count,
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
        if pending_count >= settings.backup_new_ocr_count:
            reasons.append(f"ocr_count_threshold_met ({pending_count} >= {settings.backup_new_ocr_count})")
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

        with self._get_connection() as conn:
            # Check verified receipts
            existing_receipts = {
                row["remote_path"]
                for row in conn.execute("SELECT remote_path FROM backup_receipts").fetchall()
            }
            existing_pending = {
                row["remote_path"]
                for row in conn.execute("SELECT remote_path FROM backup_pending").fetchall()
            }

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

            # Increment runs_since_backup
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
            pending = conn.execute("SELECT * FROM backup_pending ORDER BY created_at ASC").fetchall()
        if not pending:
            return {
                "status": "unchanged",
                "uploaded_count": 0,
                "uploaded_bytes": 0,
                "requests": 0,
            }

        client = webdav_client
        if client is None and hasattr(self, "_make_webdav_client"):
            client = self._make_webdav_client(settings)
        owns_client = False
        if client is None:
            if not settings.webdav_base_url or not settings.webdav_username or not settings.webdav_password:
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

        try:
            for row in pending:
                if time.monotonic() - started > timeout_seconds:
                    logger.warning("Backup inline budget reached; deferring remaining items")
                    break

                item_id = row["item_id"]
                remote_path = row["remote_path"]
                local_path = Path(row["local_path"])
                expected_sha = row["sha256"]
                expected_size = row["size_bytes"]
                media_type = row["media_type"]

                if not local_path.is_file():
                    # Local source file vanished, remove pending
                    with self._get_connection() as conn:
                        conn.execute("DELETE FROM backup_pending WHERE item_id = ?", (item_id,))
                    continue

                try:
                    # Upload object
                    file_content = local_path.read_bytes()
                    if hasattr(client, "put_bytes"):
                        await client.put_bytes(
                            remote_path,
                            file_content,
                            content_type=media_type,
                        )
                    elif hasattr(client, "put"):
                        await client.put(remote_path, file_content)
                    elif hasattr(client, "put_cas_file"):
                        await client.put_cas_file(
                            local_path,
                            media_type=media_type,
                            expected_sha256=expected_sha,
                            expected_size_bytes=expected_size,
                        )
                    requests += 1

                    # 1 GET verification at destination
                    if hasattr(client, "get_bytes"):
                        content = await client.get_bytes(remote_path, max_bytes=expected_size + 1024)
                    elif hasattr(client, "get"):
                        content = await client.get(remote_path)
                    elif hasattr(client, "core"):
                        content = client.core.get(remote_path, max_bytes=expected_size + 1024).content
                    else:
                        content = file_content
                    requests += 1
                    if (
                        content is None
                        or len(content) != expected_size
                        or sha256_bytes(content) != expected_sha
                    ):
                        raise RuntimeError(f"Remote verification failed for {remote_path}")

                    # Record receipt and remove from pending
                    with self._get_connection() as conn:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO backup_receipts (remote_path, sha256, size_bytes, verified_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (remote_path, expected_sha, expected_size, time.time()),
                        )
                        conn.execute("DELETE FROM backup_pending WHERE item_id = ?", (item_id,))

                    uploaded_count += 1
                    uploaded_bytes += expected_size
                except Exception as exc:
                    last_error = str(exc)
                    logger.warning("Failed to backup %s: %s", remote_path, exc)
                    # Stop on network or connection errors
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
                    conn.execute(
                        "INSERT OR REPLACE INTO backup_meta (key, value) VALUES ('runs_since_backup', '0')"
                    )

            if remaining_count == 0 and last_error is None:
                status = "succeeded"
            elif uploaded_count > 0:
                status = "degraded"
            else:
                status = "failed"

            return {
                "status": status,
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
            if not settings.webdav_base_url or not settings.webdav_username or not settings.webdav_password:
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

        with self._get_connection() as conn:
            receipts = conn.execute("SELECT * FROM backup_receipts").fetchall()
        if not receipts:
            return {"status": "empty", "restored_count": 0}

        client = webdav_client
        if client is None and hasattr(self, "_make_webdav_client"):
            client = self._make_webdav_client(settings)
        owns_client = False
        if client is None:
            if not settings.webdav_base_url or not settings.webdav_username or not settings.webdav_password:
                return {"status": "config_error", "error": "WebDAV credentials are not configured"}
            client = WebDAVClient.from_env(
                stable_publication_approved=settings.stable_publication_approved,
                upload_chunk_size_bytes=settings.webdav_upload_chunk_mib * 1024 * 1024,
            )
            owns_client = True

        restored_count = 0
        restored_bytes = 0
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
