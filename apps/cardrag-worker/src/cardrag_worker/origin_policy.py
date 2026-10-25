"""Durable origin audit schedule and negative observations, independent of run TTLs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .contracts import canonical_sha256

if TYPE_CHECKING:
    from .pdf_cache import PDFCacheHit, PDFSourceIdentity
    from .state import WorkerState


class OriginPolicy:
    def __init__(self, state: WorkerState, *, interval: timedelta, visit_days: int = 0) -> None:
        self.state = state
        self.interval = interval
        self.visit_days = visit_days
        state.connection.executescript("""
            CREATE TABLE IF NOT EXISTS origin_audit (
                source_id TEXT PRIMARY KEY, origin_checked_at TEXT NOT NULL,
                next_audit_at TEXT NOT NULL, result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS origin_negative (
                source_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, expires_at TEXT NOT NULL
            );
        """)

    def due(self, identity: PDFSourceIdentity, cached: PDFCacheHit, *, now: datetime) -> bool:
        if cached.origin_checked_at > now:
            return True
        row = self.state.connection.execute(
            "SELECT origin_checked_at,next_audit_at FROM origin_audit WHERE source_id=?",
            (identity.source_id,),
        ).fetchone()
        checked = cached.origin_checked_at.isoformat()
        if row is None or row[0] != checked:
            # One-time migration: distribute existing bindings, without touching
            # their actual origin timestamp. Once scheduled, resumes keep the due.
            fraction = int(canonical_sha256(identity.source_id)[:8], 16) / 0xFFFFFFFF
            deadline = cached.origin_checked_at + self.interval
            if row is None:
                remaining = (deadline - now).total_seconds()
                # Existing, not-yet-overdue bindings stay within their original
                # 90-day ceiling. Already-overdue installations get one visible
                # migration window instead of a cold origin burst.
                span = remaining if remaining > 0 else self.interval.total_seconds()
                deadline = now + timedelta(seconds=span * fraction)
            self.state.connection.execute(
                "INSERT OR REPLACE INTO origin_audit VALUES (?,?,?,?)",
                (
                    identity.source_id,
                    checked,
                    deadline.isoformat(),
                    "migration" if row is None else "verified",
                ),
            )
            self.state.connection.commit()
        else:
            deadline = datetime.fromisoformat(row[1])
        return now + timedelta(days=self.visit_days) >= deadline

    def inherit(self, source_id: str, prior_source_id: str) -> None:
        self.state.connection.execute(
            "INSERT OR IGNORE INTO origin_audit SELECT ?,origin_checked_at,next_audit_at,result "
            "FROM origin_audit WHERE source_id=?",
            (source_id, prior_source_id),
        )
        self.state.connection.commit()

    def verified(self, source_id: str, checked_at: datetime, *, result: str) -> None:
        # Early jitter ensures neither fresh downloads nor a migration batch
        # produce another synchronized expiry. Never extend beyond 90 days.
        fraction = int(canonical_sha256(source_id)[:8], 16) / 0xFFFFFFFF
        due = checked_at + self.interval * (0.9 + 0.1 * fraction)
        self.state.connection.execute(
            "INSERT OR REPLACE INTO origin_audit VALUES (?,?,?,?)",
            (source_id, checked_at.isoformat(), due.isoformat(), result),
        )
        self.state.connection.execute("DELETE FROM origin_negative WHERE source_id=?", (source_id,))
        self.state.connection.commit()

    def negative(self, source_id: str, *, now: datetime) -> dict[str, Any] | None:
        import json

        row = self.state.connection.execute(
            "SELECT payload_json,expires_at FROM origin_negative WHERE source_id=?", (source_id,)
        ).fetchone()
        return json.loads(row[0]) if row is not None and now < datetime.fromisoformat(row[1]) else None

    def remember_negative(
        self, source_id: str, payload: dict[str, Any], *, now: datetime, known: bool
    ) -> None:
        from .contracts import canonical_json_bytes

        # Unknown protection receives a short observation window, never an
        # indefinite skip. Identity changes naturally invalidate the binding.
        self.state.connection.execute(
            "INSERT OR REPLACE INTO origin_negative VALUES (?,?,?)",
            (
                source_id,
                canonical_json_bytes(payload).decode(),
                (now + (self.interval if known else timedelta(days=1))).isoformat(),
            ),
        )
        self.state.connection.commit()

    def report(self, *, now: datetime) -> dict[str, Any]:
        rows = self.state.connection.execute("SELECT * FROM origin_audit").fetchall()
        return {
            "scheduled": len(rows),
            "overdue": sum(now > datetime.fromisoformat(row[1]) + self.interval for row in rows),
            "scheduled_due": sum(now > datetime.fromisoformat(row[2]) for row in rows),
            "migration_pending": sum(row[3] == "migration" for row in rows),
        }
