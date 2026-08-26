"""Durable, resumable finite-run state. This database is never served by MCP."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

RunStatus = Literal["running", "succeeded", "failed", "no_change"]
StageStatus = Literal["pending", "running", "retry", "succeeded", "failed", "skipped"]


class AlreadyRunning(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def worker_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(f"another worker owns {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class StageRow:
    run_id: str
    document_id: str
    stage_name: str
    status: str
    attempt_count: int
    max_attempts: int
    available_at: str
    last_error: str | None


SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','no_change')),
  corpus_sha256 TEXT,
  contract_sha256 TEXT,
  error TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS snapshot (
  snapshot_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES run(run_id),
  issuer TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  record_count INTEGER NOT NULL CHECK(record_count >= 0),
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, snapshot_id)
) STRICT;
CREATE INDEX IF NOT EXISTS snapshot_run_idx ON snapshot(run_id, issuer);

CREATE TABLE IF NOT EXISTS stage (
  run_id TEXT NOT NULL REFERENCES run(run_id),
  document_id TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','running','retry','succeeded','failed','skipped')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
  available_at TEXT NOT NULL,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, document_id, stage_name)
) STRICT;
CREATE INDEX IF NOT EXISTS stage_due_idx ON stage(run_id, status, available_at);

CREATE TABLE IF NOT EXISTS checkpoint (
  run_id TEXT NOT NULL REFERENCES run(run_id),
  document_id TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
  input_sha256 TEXT NOT NULL,
  output_sha256 TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('succeeded','adopted')),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, document_id, stage_name, chunk_index)
) STRICT;

CREATE TABLE IF NOT EXISTS publish (
  generation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES run(run_id),
  corpus_sha256 TEXT NOT NULL,
  contract_sha256 TEXT NOT NULL,
  serving_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready','failed')),
  published_at TEXT NOT NULL,
  details_json TEXT NOT NULL
) STRICT;
DROP INDEX IF EXISTS publish_identity_ready_idx;
CREATE INDEX IF NOT EXISTS publish_identity_idx
  ON publish(corpus_sha256, contract_sha256, published_at) WHERE status = 'ready';

CREATE TABLE IF NOT EXISTS embedding_cache (
  cache_key TEXT PRIMARY KEY,
  contract_sha256 TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  embedding BLOB NOT NULL CHECK(length(embedding)=6144),
  created_at TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS gc_unreferenced (
  remote_path TEXT PRIMARY KEY,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
) STRICT, WITHOUT ROWID;
"""


class WorkerState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> WorkerState:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def start_run(self, *, run_id: str | None = None) -> str:
        identifier = run_id or uuid.uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO run(run_id,started_at,status) VALUES(?,?, 'running')",
                (identifier, _now().isoformat()),
            )
        return identifier

    def assert_resumable(self, run_id: str) -> None:
        row = self.connection.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        if row["status"] in {"succeeded", "no_change"}:
            raise ValueError(f"run {run_id} is already complete")
        now = _now().isoformat()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE run SET status='running', finished_at=NULL, error=NULL WHERE run_id=?", (run_id,)
            )
            # Explicit resume starts one new finite attempt epoch but preserves
            # downloaded files and chunk checkpoints.
            connection.execute(
                """UPDATE stage SET status='retry',attempt_count=0,available_at=?,updated_at=?
                   WHERE run_id=? AND status IN ('failed','retry','running','skipped')""",
                (now, now, run_id),
            )
            # Latest-only serving requires every explicit resume to refresh
            # issuer discovery. Content-addressed download/OCR/embedding
            # checkpoints remain reusable after the fresh snapshot is proven.
            connection.execute(
                """UPDATE stage SET status='retry',attempt_count=0,available_at=?,
                          last_error=NULL,updated_at=?
                   WHERE run_id=? AND stage_name='discovery'""",
                (now, now, run_id),
            )

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        corpus_sha256: str | None = None,
        contract_sha256: str | None = None,
        error: str | None = None,
    ) -> None:
        if status == "running":
            raise ValueError("finish_run cannot leave a run running")
        self.connection.execute(
            "UPDATE run SET status=?,finished_at=?,corpus_sha256=?,contract_sha256=?,error=? WHERE run_id=?",
            (status, _now().isoformat(), corpus_sha256, contract_sha256, error, run_id),
        )

    def record_snapshot(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        issuer: str,
        source_sha256: str,
        record_count: int,
        payload: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO snapshot
               (snapshot_id,run_id,issuer,observed_at,source_sha256,record_count,payload_json)
               VALUES(?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                run_id,
                issuer,
                _now().isoformat(),
                source_sha256,
                record_count,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )

    def run_snapshot(self, run_id: str, issuer: str) -> tuple[dict[str, Any], datetime] | None:
        row = self.connection.execute(
            """SELECT payload_json,observed_at FROM snapshot
               WHERE run_id=? AND issuer=? ORDER BY observed_at DESC LIMIT 1""",
            (run_id, issuer),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise RuntimeError("stored snapshot payload is not a JSON object")
        return payload, datetime.fromisoformat(str(row["observed_at"]))

    def ensure_stage(self, run_id: str, document_id: str, stage_name: str, *, max_attempts: int = 4) -> None:
        now = _now().isoformat()
        self.connection.execute(
            """INSERT OR IGNORE INTO stage
               (run_id,document_id,stage_name,status,attempt_count,max_attempts,available_at,updated_at)
               VALUES(?,?,?,'pending',0,?,?,?)""",
            (run_id, document_id, stage_name, max_attempts, now, now),
        )

    def stage_started(self, run_id: str, document_id: str, stage_name: str) -> int:
        now = _now().isoformat()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT attempt_count,max_attempts FROM stage WHERE run_id=? AND document_id=? AND stage_name=?",
                (run_id, document_id, stage_name),
            ).fetchone()
            if row is None:
                raise KeyError("stage was not initialized")
            attempt = int(row["attempt_count"]) + 1
            if attempt > int(row["max_attempts"]):
                raise RuntimeError("stage exhausted its finite attempt budget")
            connection.execute(
                """UPDATE stage SET status='running',attempt_count=?,last_error=NULL,updated_at=?
                   WHERE run_id=? AND document_id=? AND stage_name=?""",
                (attempt, now, run_id, document_id, stage_name),
            )
        return attempt

    def stage_succeeded(self, run_id: str, document_id: str, stage_name: str) -> None:
        self.connection.execute(
            """UPDATE stage SET status='succeeded',last_error=NULL,updated_at=?
               WHERE run_id=? AND document_id=? AND stage_name=?""",
            (_now().isoformat(), run_id, document_id, stage_name),
        )

    def stage_skipped(
        self,
        run_id: str,
        document_id: str,
        stage_name: str,
        reason: str,
    ) -> None:
        self.connection.execute(
            """UPDATE stage SET status='skipped',last_error=?,updated_at=?
               WHERE run_id=? AND document_id=? AND stage_name=?""",
            (reason[:4000], _now().isoformat(), run_id, document_id, stage_name),
        )

    def stage_status_count(self, run_id: str, stage_name: str, status: StageStatus) -> int:
        row = self.connection.execute(
            """SELECT count(*) FROM stage
               WHERE run_id=? AND stage_name=? AND status=?""",
            (run_id, stage_name, status),
        ).fetchone()
        return int(row[0])

    def stage_failed(
        self,
        run_id: str,
        document_id: str,
        stage_name: str,
        error: BaseException,
        *,
        delay_seconds: float,
    ) -> str:
        row = self.connection.execute(
            "SELECT attempt_count,max_attempts FROM stage WHERE run_id=? AND document_id=? AND stage_name=?",
            (run_id, document_id, stage_name),
        ).fetchone()
        if row is None:
            raise KeyError("stage was not initialized")
        exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
        status = "failed" if exhausted else "retry"
        available = (_now() + timedelta(seconds=max(0.0, delay_seconds))).isoformat()
        self.connection.execute(
            """UPDATE stage SET status=?,available_at=?,last_error=?,updated_at=?
               WHERE run_id=? AND document_id=? AND stage_name=?""",
            (status, available, str(error)[:4000], _now().isoformat(), run_id, document_id, stage_name),
        )
        return status

    def due_stages(self, run_id: str) -> tuple[StageRow, ...]:
        rows = self.connection.execute(
            """SELECT run_id,document_id,stage_name,status,attempt_count,max_attempts,available_at,last_error
               FROM stage WHERE run_id=? AND status IN ('pending','retry') AND available_at<=?
               ORDER BY document_id,stage_name""",
            (run_id, _now().isoformat()),
        ).fetchall()
        return tuple(StageRow(**dict(row)) for row in rows)

    def get_stage(self, run_id: str, document_id: str, stage_name: str) -> StageRow | None:
        row = self.connection.execute(
            """SELECT run_id,document_id,stage_name,status,attempt_count,max_attempts,available_at,last_error
               FROM stage WHERE run_id=? AND document_id=? AND stage_name=?""",
            (run_id, document_id, stage_name),
        ).fetchone()
        return None if row is None else StageRow(**dict(row))

    def save_checkpoint(
        self,
        *,
        run_id: str,
        document_id: str,
        stage_name: str,
        chunk_index: int,
        input_sha256: str,
        output_sha256: str,
        artifact_path: Path,
        adopted: bool = False,
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO checkpoint
               (run_id,document_id,stage_name,chunk_index,input_sha256,output_sha256,artifact_path,status,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                document_id,
                stage_name,
                chunk_index,
                input_sha256,
                output_sha256,
                str(artifact_path),
                "adopted" if adopted else "succeeded",
                _now().isoformat(),
            ),
        )

    def checkpoint(
        self, run_id: str, document_id: str, stage_name: str, chunk_index: int
    ) -> sqlite3.Row | None:
        row = self.connection.execute(
            """SELECT * FROM checkpoint
               WHERE run_id=? AND document_id=? AND stage_name=? AND chunk_index=?""",
            (run_id, document_id, stage_name, chunk_index),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def ready_publish(self, corpus_sha256: str, contract_sha256: str) -> sqlite3.Row | None:
        row = self.connection.execute(
            """SELECT * FROM publish
               WHERE corpus_sha256=? AND contract_sha256=? AND status='ready'
               ORDER BY published_at DESC LIMIT 1""",
            (corpus_sha256, contract_sha256),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def record_publish(
        self,
        *,
        generation_id: str,
        run_id: str,
        corpus_sha256: str,
        contract_sha256: str,
        serving_sha256: str,
        status: Literal["ready", "failed"],
        details: Mapping[str, Any],
    ) -> None:
        details_json = json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        identity = (
            run_id,
            corpus_sha256,
            contract_sha256,
            serving_sha256,
            status,
            details_json,
        )
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT run_id,corpus_sha256,contract_sha256,serving_sha256,status,details_json
                   FROM publish WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                persisted = tuple(str(existing[index]) for index in range(6))
                if persisted != identity:
                    raise RuntimeError("generation publication identity conflicts with durable state")
                return
            connection.execute(
                """INSERT INTO publish
                   (generation_id,run_id,corpus_sha256,contract_sha256,serving_sha256,status,published_at,details_json)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    run_id,
                    corpus_sha256,
                    contract_sha256,
                    serving_sha256,
                    status,
                    _now().isoformat(),
                    details_json,
                ),
            )

    def last_successful_snapshot_count(self, issuer: str) -> int | None:
        row = self.connection.execute(
            """SELECT s.record_count FROM snapshot s
               JOIN run r ON r.run_id=s.run_id
               WHERE s.issuer=? AND r.status IN ('succeeded','no_change')
               ORDER BY COALESCE(r.finished_at,r.started_at) DESC LIMIT 1""",
            (issuer,),
        ).fetchone()
        return None if row is None else int(row["record_count"])

    def retained_publication_run_ids(self, *, limit: int = 3) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("publication retention limit must be positive")
        rows = self.connection.execute(
            """SELECT run_id FROM publish WHERE status='ready'
               ORDER BY published_at DESC, generation_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    def completed_run_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT run_id FROM run WHERE status IN ('succeeded','no_change') ORDER BY started_at"
        ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    def prunable_incomplete_run_ids(self, *, keep: int = 10) -> tuple[str, ...]:
        if keep < 1:
            raise ValueError("incomplete run retention must be positive")
        rows = self.connection.execute(
            """SELECT run_id FROM run WHERE status IN ('failed','running')
               ORDER BY started_at DESC,run_id DESC LIMIT -1 OFFSET ?""",
            (keep,),
        ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    def get_embedding(self, cache_key: str) -> bytes | None:
        row = self.connection.execute(
            "SELECT embedding FROM embedding_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
        return None if row is None else bytes(row["embedding"])

    def put_embedding(
        self,
        *,
        cache_key: str,
        contract_sha256: str,
        text_sha256: str,
        embedding: bytes,
    ) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO embedding_cache
               (cache_key,contract_sha256,text_sha256,embedding,created_at) VALUES(?,?,?,?,?)""",
            (cache_key, contract_sha256, text_sha256, embedding, _now().isoformat()),
        )

    def note_unreferenced(self, remote_path: str, *, observed_at: datetime | None = None) -> datetime:
        now = (observed_at or _now()).astimezone(UTC)
        self.connection.execute(
            """INSERT INTO gc_unreferenced(remote_path,first_seen_at,last_seen_at) VALUES(?,?,?)
               ON CONFLICT(remote_path) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (remote_path, now.isoformat(), now.isoformat()),
        )
        value = self.connection.execute(
            "SELECT first_seen_at FROM gc_unreferenced WHERE remote_path=?", (remote_path,)
        ).fetchone()[0]
        return datetime.fromisoformat(str(value))

    def clear_unreferenced_except(self, paths: set[str]) -> None:
        existing = {str(row[0]) for row in self.connection.execute("SELECT remote_path FROM gc_unreferenced")}
        self.connection.executemany(
            "DELETE FROM gc_unreferenced WHERE remote_path=?",
            ((path,) for path in sorted(existing.difference(paths))),
        )

    def clear_unreferenced(self, remote_path: str) -> None:
        self.connection.execute("DELETE FROM gc_unreferenced WHERE remote_path=?", (remote_path,))


def retry_delay(attempt: int, *, base_seconds: float = 1.0, cap_seconds: float = 30.0) -> float:
    if attempt < 1:
        raise ValueError("attempt is one-based")
    return float(min(cap_seconds, base_seconds * (2 ** (attempt - 1))))
