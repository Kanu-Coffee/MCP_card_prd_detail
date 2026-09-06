"""Durable Worker-only verification policy; SQLite stays on the owning event loop."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any

from cardrag_core import VerifiedArtifact, WebDAVClient, canonical_sha256
from cardrag_core.cas import CASPublisher, GenerationFilePublisher, ImmutablePublisher
from cardrag_core.webdav import WebDAVObjectStat

from .settings import WebDAVVerificationSettings
from .state import WorkerState


class VerificationPolicy:
    def __init__(
        self,
        core: WebDAVClient,
        state: WorkerState,
        settings: WebDAVVerificationSettings,
        *,
        channel: str,
        run_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.core, self.state, self.settings = core, state, settings
        self.channel, self.run_id, self.clock = channel, run_id, clock
        self.loop = asyncio.get_running_loop()
        self.scope = canonical_sha256(
            {
                "policy": "cardrag.webdav-verification.v1",
                "endpoint": core.settings.base_url.rstrip("/"),
                "username": core.settings.username,
                "runs": settings.every_runs,
                "days": settings.max_age_days,
                "gib": settings.new_cas_gib,
            }
        )
        previous_audit = self.get("audit", channel)
        if previous_audit is not None and (
            not isinstance(previous_audit.get("epoch"), str)
            or type(previous_audit.get("runs")) is not int
            or previous_audit["runs"] < 0
            or type(previous_audit.get("new_bytes")) is not int
            or previous_audit["new_bytes"] < 0
            or (
                previous_audit.get("at") is not None
                and (
                    type(previous_audit["at"]) not in {int, float} or not math.isfinite(previous_audit["at"])
                )
            )
        ):
            previous_audit = None
        self.journal_scope = canonical_sha256(
            {
                "journal": "cardrag.generation-upload.v1",
                "endpoint": core.settings.base_url.rstrip("/"),
                "username": core.settings.username,
            }
        )
        self.audit = previous_audit or {
            "epoch": uuid.uuid4().hex,
            "at": None,
            "runs": 0,
            "new_bytes": 0,
            "pending": False,
        }
        self.put("audit", channel, self.audit)
        self._reconcile_finished_runs()
        if self.get("run", f"{channel}:{run_id}") is None:
            self.put("run", f"{channel}:{run_id}", {"epoch": self.audit["epoch"], "counted": False})
        self.memo: dict[str, tuple[str, int, bool]] = {}
        self.locks = tuple(threading.Lock() for _ in range(64))
        self.metrics_lock = threading.Lock()
        self.metrics: dict[str, int | float] = {}
        self.audit_done = False
        self.seal_id: str | None = None
        self.generation_id: str | None = None
        self.last_reasons: list[str] = []
        self.pointer_checked = False
        self.observed_pointer: bytes | None = None

    def get(self, kind: str, identity: str) -> dict[str, Any] | None:
        row = self.state.connection.execute(
            "SELECT payload_json FROM webdav_verification WHERE scope=? AND kind=? AND identity=?",
            (self.journal_scope if kind == "journal" else self.scope, kind, identity),
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def put(self, kind: str, identity: str, payload: dict[str, Any]) -> None:
        self.state.connection.execute(
            "INSERT INTO webdav_verification VALUES(?,?,?,?) "
            "ON CONFLICT(scope,kind,identity) DO UPDATE SET payload_json=excluded.payload_json",
            (
                self.journal_scope if kind == "journal" else self.scope,
                kind,
                identity,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )

    def on_loop[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        async def dispatch() -> T:
            return function(*args, **kwargs)

        # Only small state operations run here. Network I/O remains on the caller's
        # publisher thread, avoiding nested executor starvation with 16 CAS writers.
        return asyncio.run_coroutine_threadsafe(dispatch(), self.loop).result()

    def due(self) -> list[str]:
        reasons: list[str] = []
        at = self.audit.get("at")
        if not isinstance(at, (int, float)) or at > self.clock():
            reasons.append("baseline")
        elif self.clock() - at >= self.settings.max_age_days * 86400:
            reasons.append("age")
        if self.audit.get("pending"):
            reasons.append("incomplete")
        if int(self.audit.get("runs", 0)) + 1 >= self.settings.every_runs and not self.audit_done:
            reasons.append("runs")
        if int(self.audit.get("new_bytes", 0)) >= self.settings.new_cas_gib * 1024**3:
            reasons.append("bytes")
        if not self.audit_done and (self.settings.force_full or self.settings.mode == "strict"):
            reasons.append("forced" if self.settings.force_full else "strict")
        self.last_reasons = reasons
        return reasons

    def start_audit(self) -> None:
        self.audit["pending"] = True
        self.put("audit", self.channel, self.audit)
        self.increment("full_audits_started")
        for reason in self.last_reasons:
            self.increment(f"trigger_{reason}")

    def complete_audit(self, *, generation_id: str, manifest_sha256: str) -> None:
        self.audit.update(
            {
                "at": self.clock(),
                "runs": 0,
                "new_bytes": 0,
                "pending": False,
                "epoch": uuid.uuid4().hex,
                "generation_id": generation_id,
                "manifest_sha256": manifest_sha256,
            }
        )
        with self.state.transaction():
            self.put("audit", self.channel, self.audit)
            self.put("run", f"{self.channel}:{self.run_id}", {"audit_completed": True})
        self.audit_done = True
        self.increment("full_audits_completed")

    def _reconcile_finished_runs(self) -> None:
        rows = self.state.connection.execute(
            "SELECT v.identity, v.payload_json FROM webdav_verification v "
            "JOIN run r ON v.identity=(? || ':' || r.run_id) "
            "WHERE v.scope=? AND v.kind='run' AND r.status IN ('succeeded','no_change')",
            (self.channel, self.scope),
        ).fetchall()
        with self.state.transaction():
            for identity, raw in rows:
                payload = json.loads(raw)
                if (
                    payload.get("epoch") == self.audit["epoch"]
                    and not payload.get("counted")
                    and not payload.get("audit_completed")
                ):
                    self.audit["runs"] = int(self.audit["runs"]) + 1
                    payload["counted"] = True
                    self.put("run", identity, payload)
            self.put("audit", self.channel, self.audit)

    def finish_run(self) -> None:
        key = f"{self.channel}:{self.run_id}"
        previous = self.get("run", key)
        if previous is not None and (previous.get("counted") or previous.get("audit_completed")):
            return
        with self.state.transaction():
            self.audit["runs"] = int(self.audit["runs"]) + 1
            self.put("audit", self.channel, self.audit)
            self.put("run", key, {"counted": True, "epoch": self.audit["epoch"]})

    def invalidate(self, path: PurePosixPath, *, recursive: bool = False) -> None:
        raw = path.as_posix()
        self.state.connection.execute(
            "DELETE FROM webdav_verification WHERE scope=? AND kind='object' AND identity=?",
            (self.scope, raw),
        )
        self.memo.pop(raw, None)
        if recursive:
            self.state.connection.execute(
                "DELETE FROM webdav_verification WHERE scope=? AND kind='object' AND substr(identity,1,?)=?",
                (self.scope, len(raw) + 1, raw + "/"),
            )
            self.memo = {key: value for key, value in self.memo.items() if not key.startswith(raw + "/")}

    def _lookup(self, path: str, digest: str, size: int, force: bool) -> tuple[bool, dict[str, Any] | None]:
        memo = self.memo.get(path)
        if memo is not None and memo[:2] == (digest, size) and (memo[2] or not force):
            return True, None
        intent = self.get("cas", path)
        if force or (intent is not None and intent.get("pending")):
            return False, None
        receipt = self.get("object", path)
        if (
            receipt is None
            or receipt.get("sha256") != digest
            or type(receipt.get("size")) is not int
            or receipt.get("size") != size
            or type(receipt.get("verified_at")) not in {int, float}
            or not 0 <= receipt["verified_at"] <= self.clock()
            or self.clock() - receipt["verified_at"] >= self.settings.max_age_days * 86400
            or receipt.get("method") != "sha256-get"
            or (receipt.get("etag") is not None and not isinstance(receipt["etag"], str))
        ):
            return False, None
        return False, receipt

    def _remember(self, path: str, digest: str, size: int, etag: str | None, *, full: bool) -> None:
        self.memo[path] = (digest, size, full)
        if full:
            self.put(
                "object",
                path,
                {
                    "sha256": digest,
                    "size": size,
                    "etag": etag,
                    "verified_at": self.clock(),
                    "run_id": self.run_id,
                    "method": "sha256-get",
                },
            )
            intent = self.get("cas", path)
            if intent is not None and intent.get("pending"):
                with self.state.transaction():
                    # An intent survives failed runs. Its first verified remote result
                    # is counted once, including recovery after an ambiguous PUT/MOVE.
                    self.audit["new_bytes"] = int(self.audit["new_bytes"]) + size
                    self.put("audit", self.channel, self.audit)
                    self.put("cas", path, {"pending": False, "counted_epoch": self.audit["epoch"]})

    def increment(self, key: str, value: int | float = 1) -> None:
        with self.metrics_lock:
            self.metrics[key] = self.metrics.get(key, 0) + value

    @contextmanager
    def measure(self, reason: str) -> Iterator[None]:
        started = time.monotonic()
        self.increment(f"{reason}_requests")
        try:
            yield
        finally:
            self.increment(f"{reason}_accumulated_seconds", time.monotonic() - started)

    def verify_sync(
        self,
        path: PurePosixPath,
        digest: str,
        size: int,
        *,
        force: bool,
        reason: str,
    ) -> VerifiedArtifact:
        # Deterministic stripes bound lock count without creating one task per reference.
        lock = self.locks[int(canonical_sha256(path.as_posix())[:2], 16) % len(self.locks)]
        with lock:
            skip, receipt = self.on_loop(self._lookup, path.as_posix(), digest, size, force)
            if skip:
                self.increment("same_run_reused")
                return VerifiedArtifact(path, digest, size)
            if receipt is not None:
                try:
                    with self.measure("head"):
                        stat = self.core.head(path)
                except Exception:
                    self.on_loop(self.invalidate, path)
                    raise
                if stat.size_bytes == size and stat.etag == receipt.get("etag"):
                    self.on_loop(self._remember, path.as_posix(), digest, size, stat.etag, full=False)
                    self.increment("receipt_reused")
                    self.increment("avoided_get_bytes", size)
                    return VerifiedArtifact(path, digest, size)
            # Missing/changed validators are never treated as fresh SHA-256 evidence.
            self.on_loop(self.invalidate, path)
            with self.measure(reason):
                observation = self.core.verify_with_metadata(
                    path,
                    expected_sha256=digest,
                    expected_size_bytes=size,
                    payload_observer=lambda count: self.increment(f"{reason}_payload_bytes", count),
                )
            if not path.as_posix().startswith("v1/.incoming/"):
                self.on_loop(self._remember, path.as_posix(), digest, size, observation.etag, full=True)
            return observation.artifact

    def upload_intent(self, path: PurePosixPath, digest: str, size: int) -> None:
        raw = path.as_posix()
        self.invalidate(path)
        if raw.startswith("v1/objects/sha256/"):
            previous = self.get("cas", raw)
            if previous is None or previous.get("counted_epoch") != self.audit["epoch"]:
                self.put("cas", raw, {"pending": True, "sha256": digest, "size": size})
        elif self.seal_id is not None and path.parent.name == self.generation_id:
            journal = self.journal(path)
            if journal.get("exhausted") or int(journal.get("attempts", 0)) >= 2:
                raise RuntimeError("generation upload exhausted its durable retry budget")
            journal.update(
                {
                    "attempts": int(journal.get("attempts", 0)) + 1,
                    "phase": "uploading",
                    "created": False,
                    "sha256": digest,
                    "size": size,
                }
            )
            self.save_journal(path, journal)

    def journal(self, path: PurePosixPath) -> dict[str, Any]:
        return self.get("journal", f"{self.seal_id}:{path}") or {}

    def save_journal(self, path: PurePosixPath, value: dict[str, Any]) -> None:
        self.put("journal", f"{self.seal_id}:{path}", value)

    def moved(self, path: PurePosixPath, result: WebDAVObjectStat) -> None:
        if self.seal_id is not None and path.parent.name == self.generation_id:
            journal = self.journal(path)
            journal.update({"phase": "moved", "created": result.status_code == 201})
            self.save_journal(path, journal)

    def snapshot(self) -> dict[str, int | float]:
        with self.metrics_lock:
            return {
                **self.metrics,
                "runs_since_full": int(self.audit["runs"]),
                "new_cas_bytes": int(self.audit["new_bytes"]),
                "full_verified_at": float(self.audit.get("at") or 0),
            }


class ObservedPublisher(ImmutablePublisher):
    def __init__(self, policy: VerificationPolicy, *, chunk_size: int) -> None:
        super().__init__(policy.core, upload_chunk_size_bytes=chunk_size)
        self.policy = policy

    def _verify_existing(self, path: PurePosixPath, *, digest: str, size_bytes: int) -> VerifiedArtifact:
        force = bool(self.policy.on_loop(self.policy.due))
        return self.policy.verify_sync(path, digest, size_bytes, force=force, reason="existing")

    def _verify_collision(self, path: PurePosixPath, *, digest: str, size_bytes: int) -> None:
        self.policy.on_loop(self.policy.invalidate, path)
        self.policy.verify_sync(path, digest, size_bytes, force=True, reason="collision")

    def _verify_remote(self, path: PurePosixPath, *, digest: str, size_bytes: int) -> VerifiedArtifact:
        reason = "temporary" if path.as_posix().startswith("v1/.incoming/") else "final"
        return self.policy.verify_sync(path, digest, size_bytes, force=True, reason=reason)

    def _before_upload(self, destination: PurePosixPath, *, digest: str, size_bytes: int) -> None:
        self.policy.on_loop(self.policy.upload_intent, destination, digest, size_bytes)

    def _move_completed(self, destination: PurePosixPath, result: WebDAVObjectStat) -> None:
        self.policy.on_loop(self.policy.moved, destination, result)


class ObservedGenerationPublisher(ObservedPublisher, GenerationFilePublisher):
    def _verify_existing(self, path: PurePosixPath, *, digest: str, size_bytes: int) -> VerifiedArtifact:
        # Resume/collision verification is positive byte proof, not a periodic receipt.
        return self.policy.verify_sync(path, digest, size_bytes, force=True, reason="existing_generation")


class ObservedCASPublisher(CASPublisher):
    def __init__(self, policy: VerificationPolicy, *, chunk_size: int) -> None:
        self._publisher = ObservedPublisher(policy, chunk_size=chunk_size)
