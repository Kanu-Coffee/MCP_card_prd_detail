"""One durable KST day assignment with three runs, under the Worker writer lock."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .contracts import canonical_json_bytes
from .issuer_scope import IssuerScope
from .partial_execution import PartialExecutionError

if TYPE_CHECKING:
    from .state import WorkerState

_DAY_SELECT = """SELECT d.*, COALESCE((SELECT MAX(a.attempt) FROM issuer_rotation_attempt a
WHERE a.schedule=d.schedule AND a.channel=d.channel AND a.day=d.day),1) AS attempt_count
FROM issuer_rotation_day d """


@dataclass
class Assignment:
    scope: IssuerScope
    day: str | None = None
    run_id: str | None = None
    result: dict[str, Any] | None = None
    attempt: int = 1


class Rotation:
    def __init__(self, state: WorkerState, *, schedule: str = "daily", channel: str):
        self.state, self.schedule, self.channel = state, schedule, channel
        state.connection.executescript("""
            CREATE TABLE IF NOT EXISTS issuer_rotation (
                schedule TEXT NOT NULL, channel TEXT NOT NULL, remaining_json TEXT NOT NULL,
                PRIMARY KEY(schedule,channel)
            );
            CREATE TABLE IF NOT EXISTS issuer_rotation_day (
                schedule TEXT NOT NULL, channel TEXT NOT NULL, day TEXT NOT NULL,
                issuer TEXT NOT NULL, run_id TEXT, result_json TEXT,
                PRIMARY KEY(schedule,channel,day)
            );
            CREATE TABLE IF NOT EXISTS issuer_rotation_migration (
                schedule TEXT, channel TEXT, day TEXT, before_json TEXT, after_json TEXT,
                PRIMARY KEY(schedule,channel,day)
            );
            CREATE TABLE IF NOT EXISTS issuer_rotation_attempt (
                schedule TEXT NOT NULL, channel TEXT NOT NULL, day TEXT NOT NULL,
                attempt INTEGER NOT NULL, issuer TEXT NOT NULL, run_id TEXT, result_json TEXT,
                PRIMARY KEY(schedule,channel,day,attempt)
            );
        """)

        if "roster_json" not in {
            row[1] for row in state.connection.execute("PRAGMA table_info(issuer_rotation)")
        }:
            state.connection.execute(
                "ALTER TABLE issuer_rotation ADD COLUMN roster_json TEXT NOT NULL DEFAULT '[]'"
            )
        with state.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO issuer_rotation_attempt "
                "SELECT d.schedule,d.channel,d.day,1,d.issuer,d.run_id,d.result_json "
                "FROM issuer_rotation_day d WHERE NOT EXISTS (SELECT 1 FROM issuer_rotation_attempt a "
                "WHERE a.schedule=d.schedule AND a.channel=d.channel AND a.day=d.day)"
            )

    def check_limit(self, *, now: datetime | None = None) -> None:
        """Reject a fourth completed attempt before provider startup; do not claim."""
        day = (
            (now or datetime.now(ZoneInfo("Asia/Seoul")))
            .astimezone(ZoneInfo("Asia/Seoul"))
            .date()
            .isoformat()
        )
        row = self.state.connection.execute(
            _DAY_SELECT + "WHERE d.schedule=? AND d.channel=? AND d.day=?",
            (self.schedule, self.channel, day),
        ).fetchone()
        if row is None or row["attempt_count"] < 3:
            return
        status = self.state.run_status(row["run_id"]) if row["run_id"] else None
        if row["result_json"] is not None or status in {"succeeded", "no_change", "failed"}:
            raise PartialExecutionError("rotation_daily_attempt_limit", "scope")

    def claim(self, pool: tuple[str, ...], *, now: datetime | None = None) -> Assignment:
        day = (
            (now or datetime.now(ZoneInfo("Asia/Seoul")))
            .astimezone(ZoneInfo("Asia/Seoul"))
            .date()
            .isoformat()
        )
        with self.state.transaction() as db:
            row = db.execute(
                _DAY_SELECT + "WHERE d.schedule=? AND d.channel=? AND d.day=?",
                (self.schedule, self.channel, day),
            ).fetchone()
            if row is not None:
                if row["issuer"] not in pool:
                    raise PartialExecutionError("rotation_assigned_issuer_disabled", "scope")
                attempt = row["attempt_count"]
                result_json = row["result_json"]
                if result_json is None and row["run_id"] is not None:
                    status = db.execute("SELECT status FROM run WHERE run_id=?", (row["run_id"],)).fetchone()
                    if status is not None and status[0] in {"succeeded", "no_change", "failed"}:
                        result_json = canonical_json_bytes(
                            {"status": status[0], "run_id": row["run_id"], "rotation_reconciled": True}
                        ).decode()
                        db.execute(
                            "UPDATE issuer_rotation_day SET result_json=? "
                            "WHERE schedule=? AND channel=? AND day=?",
                            (result_json, self.schedule, self.channel, day),
                        )
                        db.execute(
                            "UPDATE issuer_rotation_attempt SET result_json=? "
                            "WHERE schedule=? AND channel=? AND day=? AND attempt=?",
                            (result_json, self.schedule, self.channel, day, attempt),
                        )
                if result_json is not None:
                    if attempt >= 3:
                        raise PartialExecutionError("rotation_daily_attempt_limit", "scope")
                    attempt += 1
                    db.execute(
                        "UPDATE issuer_rotation_day SET run_id=NULL,result_json=NULL "
                        "WHERE schedule=? AND channel=? AND day=?",
                        (self.schedule, self.channel, day),
                    )
                    db.execute(
                        "INSERT INTO issuer_rotation_attempt VALUES (?,?,?,?,?,NULL,NULL)",
                        (self.schedule, self.channel, day, attempt, row["issuer"]),
                    )
                    return Assignment(
                        IssuerScope(pool, (row["issuer"],), "round_robin"), day, attempt=attempt
                    )
                return Assignment(
                    IssuerScope(pool, (row["issuer"],), "round_robin"),
                    day,
                    row["run_id"],
                    None,
                    attempt,
                )
            # An unresolved sealed run may already have committed. Never repeat
            # network/OCR or overwrite a newer head to resolve ambiguity.
            old = db.execute(
                "SELECT run_id FROM issuer_rotation_day WHERE schedule=? AND channel=? "
                "AND result_json IS NULL AND run_id IS NOT NULL",
                (self.schedule, self.channel),
            )
            for old_row in old:
                if (self.state.path.parent / "runs" / old_row[0] / "sealed/publish.json").exists():
                    status = db.execute("SELECT status FROM run WHERE run_id=?", (old_row[0],)).fetchone()
                    if status is None or status[0] not in {"succeeded", "no_change", "failed"}:
                        raise PartialExecutionError("rotation_publication_reconciliation_required", "scope")
            cycle = db.execute(
                "SELECT remaining_json,roster_json FROM issuer_rotation WHERE schedule=? AND channel=?",
                (self.schedule, self.channel),
            ).fetchone()
            roster = json.loads(cycle[1]) if cycle else list(pool)
            remaining = [code for code in json.loads(cycle[0]) if code in pool] if cycle else []
            if cycle and roster != list(pool):
                db.execute(
                    "INSERT OR REPLACE INTO issuer_rotation_migration VALUES (?,?,?,?,?)",
                    (self.schedule, self.channel, day, json.dumps(roster), json.dumps(pool)),
                )
            if not remaining:
                remaining = list(pool)
                roster = list(pool)
            issuer = remaining.pop(0)
            db.execute(
                "INSERT OR REPLACE INTO issuer_rotation VALUES (?,?,?,?)",
                (self.schedule, self.channel, json.dumps(remaining), json.dumps(roster)),
            )
            db.execute(
                "INSERT INTO issuer_rotation_day "
                "(schedule,channel,day,issuer,run_id,result_json) VALUES (?,?,?,?,NULL,NULL)",
                (self.schedule, self.channel, day, issuer),
            )
            db.execute(
                "INSERT INTO issuer_rotation_attempt VALUES (?,?,?,?,?,NULL,NULL)",
                (self.schedule, self.channel, day, 1, issuer),
            )
            return Assignment(IssuerScope(pool, (issuer,), "round_robin"), day)

    def bind(self, assignment: Assignment, run_id: str) -> None:
        if assignment.run_id is not None and assignment.run_id != run_id:
            raise ValueError("rotation run binding changed")
        with self.state.transaction() as db:
            updated = db.execute(
                "UPDATE issuer_rotation_day SET run_id=? "
                "WHERE schedule=? AND channel=? AND day=? AND ?=(SELECT MAX(attempt) "
                "FROM issuer_rotation_attempt WHERE schedule=? AND channel=? AND day=?)",
                (
                    run_id,
                    self.schedule,
                    self.channel,
                    assignment.day,
                    assignment.attempt,
                    self.schedule,
                    self.channel,
                    assignment.day,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("rotation attempt binding changed")
            db.execute(
                "UPDATE issuer_rotation_attempt SET run_id=? "
                "WHERE schedule=? AND channel=? AND day=? AND attempt=?",
                (run_id, self.schedule, self.channel, assignment.day, assignment.attempt),
            )
        assignment.run_id = run_id

    def complete(self, assignment: Assignment, result: dict[str, Any]) -> None:
        with self.state.transaction() as db:
            payload = canonical_json_bytes(result).decode()
            updated = db.execute(
                "UPDATE issuer_rotation_day SET result_json=? "
                "WHERE schedule=? AND channel=? AND day=? AND ?=(SELECT MAX(attempt) "
                "FROM issuer_rotation_attempt WHERE schedule=? AND channel=? AND day=?)",
                (
                    payload,
                    self.schedule,
                    self.channel,
                    assignment.day,
                    assignment.attempt,
                    self.schedule,
                    self.channel,
                    assignment.day,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("rotation attempt completion changed")
            db.execute(
                "UPDATE issuer_rotation_attempt SET result_json=? "
                "WHERE schedule=? AND channel=? AND day=? AND attempt=?",
                (payload, self.schedule, self.channel, assignment.day, assignment.attempt),
            )
        assignment.result = result


@asynccontextmanager
async def scope_session(
    state: WorkerState,
    root: Path,
    pool: tuple[str, ...],
    *,
    selected: tuple[str, ...] | None,
    mode: str,
    channel: str,
    resume: str | None,
    transport: Any = None,
    stable_publication_approved: bool = False,
) -> AsyncIterator[tuple[Assignment, Callable[[str], None] | None]]:
    rotation = Rotation(state, channel=channel) if mode == "round_robin" and resume is None else None
    reconciled_today: Assignment | None = None
    if rotation and transport is not None:
        from .pipeline import SealedPublicationResumer

        today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        for row in state.connection.execute(
            _DAY_SELECT + "WHERE d.schedule=? AND d.channel=? AND d.result_json IS NULL "
            "AND d.run_id IS NOT NULL",
            (rotation.schedule, channel),
        ).fetchall():
            old_pool = pool if row["issuer"] in pool else (*pool, row["issuer"])
            old_run_id: str = row["run_id"]
            old = Assignment(
                IssuerScope(old_pool, (row["issuer"],), "round_robin"),
                row["day"],
                row["run_id"],
                attempt=row["attempt_count"],
            )
            status = state.connection.execute(
                "SELECT status FROM run WHERE run_id=?", (old_run_id,)
            ).fetchone()
            if status and status[0] in {"succeeded", "no_change", "failed"}:
                rotation.complete(
                    old, {"status": status[0], "run_id": old_run_id, "rotation_reconciled": True}
                )
                if old.day == today:
                    reconciled_today = old
                continue
            seal_path = root / "runs" / old_run_id / "sealed/publish.json"
            if seal_path.exists():
                resumer = SealedPublicationResumer(
                    state=state,
                    state_dir=root,
                    webdav=transport,
                    stable_publication_approved=stable_publication_approved,
                )
                _, sealed = resumer._load_seal(old_run_id)
                validated = await resumer._validate_local_seal(sealed)
                if await resumer._reconcile_validated_publication(old_run_id, validated):
                    state.finish_run(
                        old_run_id,
                        "succeeded",
                        corpus_sha256=validated.manifest.corpus_sha256,
                        contract_sha256=validated.manifest.contract_sha256,
                    )
                    rotation.complete(
                        old,
                        {
                            "status": "succeeded",
                            "run_id": old_run_id,
                            "generation_id": validated.manifest.generation_id,
                            "rotation_reconciled": True,
                        },
                    )
                elif old.day == today:
                    # Only publication may resume here; the predecessor fence
                    # refuses to roll back a newer manual publication.
                    result = await resumer._resume_publication_locked(old_run_id)
                    rotation.complete(
                        old,
                        {
                            "status": result.status,
                            "run_id": old_run_id,
                            "generation_id": result.generation_id,
                            "rotation_reconciled": True,
                        },
                    )
                else:
                    state.finish_run_if_running(old_run_id, "interrupted", error="rotation_day_elapsed")
                    rotation.complete(
                        old,
                        {
                            "status": "interrupted",
                            "run_id": old_run_id,
                            "reason_code": "rotation_day_elapsed_unpublished",
                        },
                    )
            elif old.day != today:
                state.finish_run_if_running(old_run_id, "interrupted", error="rotation_day_elapsed")
                rotation.complete(
                    old,
                    {"status": "interrupted", "run_id": old_run_id, "reason_code": "rotation_day_elapsed"},
                )
            if old.day == today and old.result is not None:
                reconciled_today = old
    assignment = reconciled_today or (
        rotation.claim(pool) if rotation else Assignment(IssuerScope(pool, selected or pool, mode))
    )
    target_run_id = resume or (assignment.run_id if rotation and assignment.result is None else None)
    if target_run_id is not None:
        from .partial_execution import read_json

        path = root / "runs" / target_run_id / "issuer-scope.json"
        if path.exists():
            saved = read_json(root, path, "scope")
            if selected is not None and list(selected) != saved["selected"]:
                raise PartialExecutionError("issuer_scope_changed", "scope")
            assignment.scope = IssuerScope(tuple(saved["pool"]), tuple(saved["selected"]), saved["mode"])
    if rotation and assignment.run_id and assignment.result is None:
        row = state.connection.execute(
            "SELECT status FROM run WHERE run_id=?", (assignment.run_id,)
        ).fetchone()
        if row is not None and row[0] in {"succeeded", "no_change", "failed"}:
            rotation.complete(
                assignment, {"status": row[0], "run_id": assignment.run_id, "rotation_reconciled": True}
            )
        elif (root / "runs" / assignment.run_id / "sealed/publish.json").exists():
            raise PartialExecutionError("rotation_publication_reconciliation_required", "scope")
    try:
        yield assignment, (lambda run_id: rotation.bind(assignment, run_id)) if rotation else None
    except BaseException as exc:
        import asyncio

        if rotation and not isinstance(exc, asyncio.CancelledError):
            rotation.complete(
                assignment,
                {"status": "failed", "run_id": assignment.run_id, "reason_code": "rotation_execution_failed"},
            )
        raise
    else:
        if rotation and assignment.result is not None:
            rotation.complete(assignment, assignment.result)
        elif rotation and assignment.run_id:
            row = state.connection.execute(
                "SELECT status FROM run WHERE run_id=?", (assignment.run_id,)
            ).fetchone()
            if row is not None and row[0] in {"succeeded", "no_change", "failed"}:
                rotation.complete(assignment, {"status": row[0], "run_id": assignment.run_id})
