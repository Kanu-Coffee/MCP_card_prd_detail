"""Hold Worker/backup fences through snapshot and release-pointer transition.

Run explicitly in a maintenance container, never as the Worker entrypoint.
The caller must first disable scheduling and reject active Worker containers.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import signal
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path


def snapshot(state: Path, serving: Path, tag: str) -> dict:
    """Caller must hold both fences and stop all writers before entering."""
    if not tag or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in tag
    ):
        raise ValueError("invalid snapshot tag")
    out = state / "deployment-backups" / tag
    out.mkdir(mode=0o750, parents=True, exist_ok=False)
    evidence = {"created_at": datetime.now(UTC).isoformat(), "databases": {}}
    for name in ("worker-state.sqlite3", "backup-ledger.sqlite3"):
        source = sqlite3.connect((state / name).as_uri() + "?mode=ro", uri=True)
        dest = sqlite3.connect(out / name)
        try:
            source.backup(dest)
            if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("snapshot quick_check failed")
        finally:
            dest.close()
            source.close()
        h = hashlib.sha256()
        with (out / name).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        evidence["databases"][name] = {"size": (out / name).stat().st_size, "sha256": h.hexdigest()}
    conn = sqlite3.connect((out / "worker-state.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        evidence["latest_run"] = conn.execute(
            "SELECT run_id,status FROM run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    evidence["stable"] = json.loads((serving / "v1/channels/stable.json").read_text())
    evidence["locks"] = ["worker.lock", "backup.lock"]
    (out / "snapshot.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("serving", type=Path)
    parser.add_argument("tag")
    parser.add_argument("--hold-only", action="store_true")
    parser.add_argument("--ready-dir", type=Path, default=Path("/tmp"))  # noqa: S108 -- private container tmpfs
    args = parser.parse_args()
    # A fresh ready directory (tmpfs in production) prevents stale success.
    ready = args.ready_dir / "ready"
    ready.unlink(missing_ok=True)
    with ExitStack() as stack:
        for name in ("worker.lock", "backup.lock"):
            stream = stack.enter_context((args.state / name).open("a+b"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        evidence = {} if args.hold_only else snapshot(args.state.resolve(), args.serving.resolve(), args.tag)
        (args.ready_dir / "evidence.json").write_text(json.dumps(evidence) + "\n")
        ready.touch()
        print("READY", flush=True)
        while True:
            signal.pause()


if __name__ == "__main__":
    main()
