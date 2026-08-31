#!/usr/bin/env python3
"""Fail-closed, read-only verification for CardRAG offline volume promotion.

Both roots must be offline snapshots with no running mount or other writer for
the complete scan. The path walk detects file mutation but is not a live-snapshot
mechanism.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

SQLITE_HEADER = b"SQLite format 3\x00"
CHUNK_BYTES = 1024 * 1024


class VerificationError(RuntimeError):
    """The offline promotion contract was not satisfied."""


@dataclass(frozen=True)
class _Entry:
    canonical: dict[str, int | str | None]
    identity: tuple[int, int]
    sqlite_database: bool = False


def _fail(reason: str) -> NoReturn:
    raise VerificationError(reason)


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _absolute_directory(raw: str, *, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        _fail(f"{label}_path_not_canonical_absolute")

    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            value = current.lstat()
        except OSError as exc:
            raise VerificationError(f"{label}_path_unavailable") from exc
        if stat.S_ISLNK(value.st_mode):
            _fail(f"{label}_path_has_symlink_component")

    try:
        root = path.lstat()
    except OSError as exc:
        raise VerificationError(f"{label}_path_unavailable") from exc
    if not stat.S_ISDIR(root.st_mode):
        _fail(f"{label}_not_directory")
    return path


def _require_distinct_roots(source: Path, destination: Path) -> None:
    try:
        if os.path.samefile(source, destination):
            _fail("source_destination_same_directory")
    except OSError as exc:
        raise VerificationError("source_destination_identity_unavailable") from exc


def _hash_regular_file(path: Path, expected: os.stat_result) -> tuple[str, bytes]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VerificationError("regular_file_open_failed") from exc
    digest = hashlib.sha256()
    prefix = bytearray()
    try:
        before = os.fstat(descriptor)
        if _identity(before) != _identity(expected) or not stat.S_ISREG(before.st_mode):
            _fail("regular_file_identity_changed")
        while True:
            block = os.read(descriptor, CHUNK_BYTES)
            if not block:
                break
            if len(prefix) < len(SQLITE_HEADER):
                prefix.extend(block[: len(SQLITE_HEADER) - len(prefix)])
            digest.update(block)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(before):
            _fail("regular_file_changed_during_read")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), bytes(prefix)


def _scan_state(root: Path, *, maximum_entries: int) -> tuple[list[_Entry], list[Path]]:
    root_stat = root.lstat()
    root_device = root_stat.st_dev
    entries: list[_Entry] = [
        _Entry(
            canonical={
                "gid": root_stat.st_gid,
                "kind": "directory",
                "mode": stat.S_IMODE(root_stat.st_mode),
                "path": ".",
                "sha256": None,
                "size": None,
                "uid": root_stat.st_uid,
            },
            identity=(root_stat.st_dev, root_stat.st_ino),
        )
    ]
    sqlite_paths: list[Path] = []
    pending: list[tuple[Path, str]] = [(root, "")]

    while pending:
        directory, relative_parent = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise VerificationError("state_directory_scan_failed") from exc
        for child in children:
            if len(entries) >= maximum_entries:
                _fail("state_entry_limit_exceeded")
            relative = f"{relative_parent}/{child.name}" if relative_parent else child.name
            try:
                value = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise VerificationError("state_entry_stat_failed") from exc
            if value.st_dev != root_device:
                _fail("state_cross_filesystem_entry")
            if stat.S_ISLNK(value.st_mode):
                _fail("state_symlink_forbidden")
            if stat.S_ISDIR(value.st_mode):
                entries.append(
                    _Entry(
                        canonical={
                            "gid": value.st_gid,
                            "kind": "directory",
                            "mode": stat.S_IMODE(value.st_mode),
                            "path": relative,
                            "sha256": None,
                            "size": None,
                            "uid": value.st_uid,
                        },
                        identity=(value.st_dev, value.st_ino),
                    )
                )
                pending.append((Path(child.path), relative))
                continue
            if not stat.S_ISREG(value.st_mode):
                _fail("state_special_file_forbidden")
            if value.st_nlink != 1:
                _fail("state_regular_file_hardlink_forbidden")
            if child.name == "auth.json":
                _fail("credential_file_in_state_forbidden")
            if child.name.endswith(("-wal", "-shm", "-journal")):
                _fail("sqlite_transient_file_present")
            content_sha256, prefix = _hash_regular_file(Path(child.path), value)
            is_sqlite = prefix == SQLITE_HEADER
            entries.append(
                _Entry(
                    canonical={
                        "gid": value.st_gid,
                        "kind": "file",
                        "mode": stat.S_IMODE(value.st_mode),
                        "path": relative,
                        "sha256": content_sha256,
                        "size": value.st_size,
                        "uid": value.st_uid,
                    },
                    identity=(value.st_dev, value.st_ino),
                    sqlite_database=is_sqlite,
                )
            )
            if is_sqlite:
                sqlite_paths.append(Path(child.path))

    entries.sort(key=lambda entry: str(entry.canonical["path"]))
    return entries, sqlite_paths


def _tree_digest(entries: list[_Entry]) -> str:
    canonical = [entry.canonical for entry in entries]
    payload = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _verify_sqlite(path: Path) -> None:
    uri = f"{path.as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            quick_rows = connection.execute("PRAGMA quick_check").fetchall()
            rows = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise VerificationError("sqlite_integrity_check_failed") from exc
    if quick_rows != [("ok",)] or rows != [("ok",)] or foreign_key_rows:
        _fail("sqlite_integrity_check_failed")


def verify_state(source_raw: str, destination_raw: str, *, maximum_entries: int) -> dict[str, object]:
    source = _absolute_directory(source_raw, label="source")
    destination = _absolute_directory(destination_raw, label="destination")
    _require_distinct_roots(source, destination)
    source_entries, source_sqlite = _scan_state(source, maximum_entries=maximum_entries)
    destination_entries, destination_sqlite = _scan_state(
        destination,
        maximum_entries=maximum_entries,
    )

    source_canonical = [entry.canonical for entry in source_entries]
    destination_canonical = [entry.canonical for entry in destination_entries]
    if source_canonical != destination_canonical:
        _fail("state_tree_mismatch")
    source_inodes = {entry.identity for entry in source_entries}
    destination_inodes = {entry.identity for entry in destination_entries}
    if source_inodes & destination_inodes:
        _fail("source_destination_inode_overlap")
    if not source_sqlite or len(source_sqlite) != len(destination_sqlite):
        _fail("sqlite_inventory_invalid")
    for database in (*source_sqlite, *destination_sqlite):
        _verify_sqlite(database)

    digest = _tree_digest(source_entries)
    file_count = sum(entry.canonical["kind"] == "file" for entry in source_entries)
    directory_count = len(source_entries) - file_count
    return {
        "content_tree_sha256": digest,
        "directory_count": directory_count,
        "file_count": file_count,
        "mode": "state",
        "schema_version": "cardrag.offline-volume-verification.v1",
        "sqlite_database_count": len(source_sqlite),
        "status": "passed",
    }


def _require_private_directory(path: Path, *, uid: int, gid: int, reason: str) -> None:
    try:
        value = path.lstat()
    except OSError as exc:
        raise VerificationError(reason) from exc
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or stat.S_IMODE(value.st_mode) != 0o700
        or (value.st_uid, value.st_gid) != (uid, gid)
    ):
        _fail(reason)


def _auth_stat(path: Path, *, uid: int, gid: int, maximum_bytes: int, reason: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise VerificationError(reason) from exc
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or stat.S_IMODE(value.st_mode) != 0o600
        or (value.st_uid, value.st_gid) != (uid, gid)
        or value.st_nlink != 1
        or not 1 <= value.st_size <= maximum_bytes
    ):
        _fail(reason)
    return value


def _files_equal(
    source: Path,
    source_stat: os.stat_result,
    destination: Path,
    destination_stat: os.stat_result,
) -> bool:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        for path, expected in ((source, source_stat), (destination, destination_stat)):
            descriptor = os.open(path, flags)
            descriptors.append(descriptor)
            if _identity(os.fstat(descriptor)) != _identity(expected):
                _fail("codex_auth_identity_changed")
        while True:
            source_block = os.read(descriptors[0], CHUNK_BYTES)
            destination_block = os.read(descriptors[1], CHUNK_BYTES)
            if source_block != destination_block:
                return False
            if not source_block:
                break
        for descriptor, expected in zip(
            descriptors,
            (source_stat, destination_stat),
            strict=True,
        ):
            if _identity(os.fstat(descriptor)) != _identity(expected):
                _fail("codex_auth_changed_during_read")
    except OSError as exc:
        raise VerificationError("codex_auth_read_failed") from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    return True


def verify_codex_home(
    source_raw: str,
    destination_raw: str,
    *,
    expected_uid: int,
    expected_gid: int,
    maximum_auth_bytes: int,
) -> dict[str, object]:
    source = _absolute_directory(source_raw, label="source")
    destination = _absolute_directory(destination_raw, label="destination")
    _require_distinct_roots(source, destination)
    _require_private_directory(source, uid=expected_uid, gid=expected_gid, reason="source_root_invalid")
    _require_private_directory(
        destination,
        uid=expected_uid,
        gid=expected_gid,
        reason="destination_root_invalid",
    )
    source_auth = source / "auth.json"
    destination_auth = destination / "auth.json"
    destination_home = destination / "home"
    source_auth_stat = _auth_stat(
        source_auth,
        uid=expected_uid,
        gid=expected_gid,
        maximum_bytes=maximum_auth_bytes,
        reason="source_auth_invalid",
    )
    try:
        destination_names = {entry.name for entry in os.scandir(destination)}
    except OSError as exc:
        raise VerificationError("destination_inventory_unavailable") from exc
    if destination_names != {"auth.json", "home"}:
        _fail("destination_inventory_invalid")
    _require_private_directory(
        destination_home,
        uid=expected_uid,
        gid=expected_gid,
        reason="destination_home_invalid",
    )
    try:
        if next(os.scandir(destination_home), None) is not None:
            _fail("destination_home_not_empty")
    except OSError as exc:
        raise VerificationError("destination_home_inventory_unavailable") from exc
    destination_auth_stat = _auth_stat(
        destination_auth,
        uid=expected_uid,
        gid=expected_gid,
        maximum_bytes=maximum_auth_bytes,
        reason="destination_auth_invalid",
    )
    if (source_auth_stat.st_dev, source_auth_stat.st_ino) == (
        destination_auth_stat.st_dev,
        destination_auth_stat.st_ino,
    ):
        _fail("source_destination_auth_inode_overlap")
    if not _files_equal(source_auth, source_auth_stat, destination_auth, destination_auth_stat):
        _fail("codex_auth_content_mismatch")

    return {
        "auth_content_equal": True,
        "destination_entry_count": 2,
        "destination_home_empty": True,
        "mode": "codex-home",
        "schema_version": "cardrag.offline-volume-verification.v1",
        "status": "passed",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    state_parser = subparsers.add_parser("state")
    state_parser.add_argument("--source", required=True)
    state_parser.add_argument("--destination", required=True)
    state_parser.add_argument("--maximum-entries", type=int, default=1_000_000)
    codex_parser = subparsers.add_parser("codex-home")
    codex_parser.add_argument("--source", required=True)
    codex_parser.add_argument("--destination", required=True)
    codex_parser.add_argument("--expected-uid", type=int, default=10001)
    codex_parser.add_argument("--expected-gid", type=int, default=10001)
    codex_parser.add_argument("--maximum-auth-bytes", type=int, default=2 * 1024 * 1024)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "state":
            if args.maximum_entries <= 0:
                _fail("maximum_entries_invalid")
            result = verify_state(
                args.source,
                args.destination,
                maximum_entries=args.maximum_entries,
            )
        else:
            if args.expected_uid < 0 or args.expected_gid < 0 or args.maximum_auth_bytes <= 0:
                _fail("codex_limit_invalid")
            result = verify_codex_home(
                args.source,
                args.destination,
                expected_uid=args.expected_uid,
                expected_gid=args.expected_gid,
                maximum_auth_bytes=args.maximum_auth_bytes,
            )
    except VerificationError as exc:
        print(f"offline_volume_verification_failed:{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
