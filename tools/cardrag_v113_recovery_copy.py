#!/usr/bin/env python3
"""Fail-closed offline copy helper for the CardRAG v1.0.13 SIGBUS recovery.

This tool is intentionally narrow.  ``state`` copies one stopped Worker tree
while omitting only its root SQLite SHM wal-index.  ``codex`` copies only the
bounded Codex ``auth.json`` into an otherwise pristine destination home.

The source is never opened for writing.  A failed destination is deliberately
left as-is and must never be reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

SCHEMA_VERSION: Final = "cardrag.v113-recovery-copy.v1"
PRODUCTION_UID: Final = 10001
PRODUCTION_GID: Final = 10001
MAXIMUM_AUTH_BYTES: Final = 2 * 1024 * 1024
MAXIMUM_STATE_ENTRIES: Final = 1_000_000
MAXIMUM_DEPTH: Final = 128
COPY_CHUNK_BYTES: Final = 1024 * 1024
SQLITE_HEADER: Final = b"SQLite format 3\x00"
MAIN_DATABASE: Final = ("worker-state.sqlite3",)
INCIDENT_WAL: Final = ("worker-state.sqlite3-wal",)
INCIDENT_SHM: Final = ("worker-state.sqlite3-shm",)
STATE_DIRECTORY_MODES: Final = frozenset({0o700, 0o755})
STATE_FILE_MODES: Final = frozenset({0o600, 0o644})
SQLITE_TRANSIENT_SUFFIXES: Final = ("-wal", "-shm", "-journal")
CODEX_TEMPORARY_NAME: Final = ".auth.json.v113-recovery-copy"
INCIDENT_MAIN_DATABASE_BYTES: Final = 3_713_409_024
INCIDENT_WAL_BYTES: Final = 19_071_512
INCIDENT_SHM_BYTES: Final = 32_768
INCIDENT_MAIN_DATABASE_SHA256: Final = "0c963e6317979c610697c07603b9896c3dd00d566fea572780a78e8e4ad916ae"
INCIDENT_WAL_SHA256: Final = "aad1a45f0c3be2fee507a571c810a5217056710f4eeeaadfeb80130c24755a06"
INCIDENT_SHM_SHA256: Final = "2ae18281d101cd39dc09b438047be8620b2456e947f4ffb4fa8f64f7e20cc473"
INCIDENT_SOURCE_FILE_COUNT: Final = 15_872
INCIDENT_SOURCE_DIRECTORY_ENTRY_COUNT: Final = 10_422
INCIDENT_SOURCE_TOTAL_FILE_BYTES: Final = 8_643_016_164


class RecoveryCopyError(RuntimeError):
    """The incident recovery copy contract was not satisfied."""


@dataclass(frozen=True, slots=True)
class _Identity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    link_count: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _Identity:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            uid=value.st_uid,
            gid=value.st_gid,
            link_count=value.st_nlink,
            size_bytes=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _Entry:
    relative: tuple[str, ...]
    kind: str
    identity: _Identity


def _fail(reason: str) -> NoReturn:
    raise RecoveryCopyError(reason)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _write_flags() -> int:
    return os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _normalize_absolute(raw: str | Path, *, field: str) -> Path:
    path = Path(raw)
    rendered = str(path)
    if (
        not path.is_absolute()
        or path == Path("/")
        or "\x00" in rendered
        or ".." in path.parts
        or os.path.normpath(rendered) != rendered
    ):
        _fail(f"{field}_path_invalid")
    return path


def _safe_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        _fail("unsafe_entry_name")
    try:
        if os.fsencode(name).decode("utf-8", errors="strict") != name:
            _fail("non_utf8_entry_name")
    except UnicodeError as exc:
        raise RecoveryCopyError("non_utf8_entry_name") from exc


def _open_child_directory(parent: int, name: str, *, field: str) -> tuple[int, _Identity]:
    _safe_name(name)
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    except OSError as exc:
        raise RecoveryCopyError(f"{field}_directory_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        named_identity = _Identity.from_stat(named)
        opened_identity = _Identity.from_stat(opened)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or named_identity != opened_identity
        ):
            _fail(f"{field}_directory_identity_changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened_identity


def _open_absolute_directory(path: Path, *, field: str) -> tuple[int, _Identity]:
    try:
        descriptor = os.open(os.sep, _directory_flags())
    except OSError as exc:
        raise RecoveryCopyError(f"{field}_root_open_failed") from exc
    identity = _Identity.from_stat(os.fstat(descriptor))
    try:
        for component in path.parts[1:]:
            child, identity = _open_child_directory(descriptor, component, field=field)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def _verify_absolute_directory(path: Path, descriptor: int, expected: _Identity, *, field: str) -> None:
    reopened, identity = _open_absolute_directory(path, field=field)
    try:
        if identity != expected or _Identity.from_stat(os.fstat(descriptor)) != expected:
            _fail(f"{field}_root_identity_changed")
    finally:
        os.close(reopened)


def _require_distinct_roots(
    source: Path,
    source_identity: _Identity,
    destination: Path,
    destination_identity: _Identity,
) -> None:
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        _fail("source_destination_roots_overlap")
    if (source_identity.device, source_identity.inode) == (
        destination_identity.device,
        destination_identity.inode,
    ):
        _fail("source_destination_roots_same_inode")


def _require_owner(identity: _Identity, *, expected_uid: int, expected_gid: int, field: str) -> None:
    if (identity.uid, identity.gid) != (expected_uid, expected_gid):
        _fail(f"{field}_owner_invalid")


def _require_state_root(
    identity: _Identity,
    *,
    expected_uid: int,
    expected_gid: int,
    destination: bool,
) -> None:
    field = "destination_root" if destination else "source_root"
    if not stat.S_ISDIR(identity.mode):
        _fail(f"{field}_not_directory")
    _require_owner(identity, expected_uid=expected_uid, expected_gid=expected_gid, field=field)
    mode = stat.S_IMODE(identity.mode)
    allowed = STATE_DIRECTORY_MODES if destination else frozenset({0o700})
    if mode not in allowed:
        _fail(f"{field}_mode_invalid")


def _list_names(descriptor: int, *, field: str) -> list[str]:
    try:
        with os.scandir(descriptor) as iterator:
            names = [entry.name for entry in iterator]
    except OSError as exc:
        raise RecoveryCopyError(f"{field}_inventory_failed") from exc
    for name in names:
        _safe_name(name)
    return sorted(names, key=os.fsencode)


def _entry_path(relative: tuple[str, ...]) -> str:
    return "." if not relative else "/".join(relative)


def _require_state_entry(
    value: os.stat_result,
    relative: tuple[str, ...],
    *,
    root_device: int,
    expected_uid: int,
    expected_gid: int,
    allow_incident_shm: bool,
) -> str:
    rendered = _entry_path(relative)
    identity = _Identity.from_stat(value)
    if identity.device != root_device:
        _fail("state_cross_filesystem_entry")
    _require_owner(
        identity,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        field="state_entry",
    )
    name = relative[-1]
    is_allowed_wal = relative == INCIDENT_WAL
    is_allowed_shm = allow_incident_shm and relative == INCIDENT_SHM
    if name.endswith(SQLITE_TRANSIENT_SUFFIXES) and not (is_allowed_wal or is_allowed_shm):
        _fail("unexpected_sqlite_transient")
    if stat.S_ISLNK(value.st_mode):
        _fail("state_symlink_forbidden")
    if stat.S_ISDIR(value.st_mode):
        if stat.S_IMODE(value.st_mode) not in STATE_DIRECTORY_MODES:
            _fail("state_directory_mode_invalid")
        return "directory"
    if not stat.S_ISREG(value.st_mode):
        _fail("state_special_file_forbidden")
    if identity.link_count != 1:
        _fail("state_hardlink_forbidden")
    if stat.S_IMODE(value.st_mode) not in STATE_FILE_MODES:
        _fail("state_file_mode_invalid")
    if rendered == ".":
        _fail("state_entry_invalid")
    return "file"


def _scan_state_tree(
    root_descriptor: int,
    root_identity: _Identity,
    *,
    expected_uid: int,
    expected_gid: int,
    allow_incident_shm: bool,
) -> list[_Entry]:
    entries = [_Entry(relative=(), kind="directory", identity=root_identity)]

    def walk(descriptor: int, relative: tuple[str, ...], depth: int) -> None:
        if depth > MAXIMUM_DEPTH:
            _fail("state_maximum_depth_exceeded")
        for name in _list_names(descriptor, field="state"):
            if len(entries) >= MAXIMUM_STATE_ENTRIES:
                _fail("state_entry_limit_exceeded")
            child_relative = (*relative, name)
            try:
                value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise RecoveryCopyError("state_entry_stat_failed") from exc
            kind = _require_state_entry(
                value,
                child_relative,
                root_device=root_identity.device,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allow_incident_shm=allow_incident_shm,
            )
            identity = _Identity.from_stat(value)
            entries.append(_Entry(relative=child_relative, kind=kind, identity=identity))
            if kind == "directory":
                child, opened_identity = _open_child_directory(descriptor, name, field="state")
                try:
                    if opened_identity != identity:
                        _fail("state_directory_identity_changed")
                    walk(child, child_relative, depth + 1)
                    if _Identity.from_stat(os.fstat(child)) != identity:
                        _fail("state_directory_changed_during_scan")
                finally:
                    os.close(child)

    walk(root_descriptor, (), 0)
    if _Identity.from_stat(os.fstat(root_descriptor)) != root_identity:
        _fail("state_root_changed_during_scan")
    return entries


def _entry_map(entries: list[_Entry]) -> dict[tuple[str, ...], _Entry]:
    return {entry.relative: entry for entry in entries}


def _validate_incident_inventory(
    entries: list[_Entry],
    *,
    source: bool,
    enforce_exact_incident: bool,
) -> None:
    by_path = _entry_map(entries)
    required = (MAIN_DATABASE, INCIDENT_WAL, INCIDENT_SHM) if source else (MAIN_DATABASE, INCIDENT_WAL)
    for relative in required:
        entry = by_path.get(relative)
        if entry is None or entry.kind != "file":
            _fail(f"required_{relative[0].replace('.', '_').replace('-', '_')}_missing")
    if not source and INCIDENT_SHM in by_path:
        _fail("destination_shm_present")
    if by_path[MAIN_DATABASE].identity.size_bytes <= 0:
        _fail("main_database_empty")
    if by_path[INCIDENT_WAL].identity.size_bytes <= 0:
        _fail("incident_wal_empty")
    if source and enforce_exact_incident:
        file_entries = [entry for entry in entries if entry.kind == "file"]
        directory_entries = [entry for entry in entries if entry.kind == "directory" and entry.relative]
        if by_path[MAIN_DATABASE].identity.size_bytes != INCIDENT_MAIN_DATABASE_BYTES:
            _fail("incident_main_database_size_mismatch")
        if by_path[INCIDENT_WAL].identity.size_bytes != INCIDENT_WAL_BYTES:
            _fail("incident_wal_size_mismatch")
        if by_path[INCIDENT_SHM].identity.size_bytes != INCIDENT_SHM_BYTES:
            _fail("incident_shm_size_mismatch")
        if len(file_entries) != INCIDENT_SOURCE_FILE_COUNT:
            _fail("incident_source_file_count_mismatch")
        if len(directory_entries) != INCIDENT_SOURCE_DIRECTORY_ENTRY_COUNT:
            _fail("incident_source_directory_count_mismatch")
        if sum(entry.identity.size_bytes for entry in file_entries) != INCIDENT_SOURCE_TOTAL_FILE_BYTES:
            _fail("incident_source_total_bytes_mismatch")


def _open_source_regular(parent: int, name: str, expected: _Identity, *, field: str) -> int:
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(name, _read_flags(), dir_fd=parent)
    except OSError as exc:
        raise RecoveryCopyError(f"{field}_open_failed") from exc
    if _Identity.from_stat(named) != expected or _Identity.from_stat(os.fstat(descriptor)) != expected:
        os.close(descriptor)
        _fail(f"{field}_identity_changed")
    return descriptor


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
        except OSError as exc:
            raise RecoveryCopyError("destination_write_failed") from exc
        if written <= 0:
            _fail("destination_short_write")
        remaining = remaining[written:]


def _copy_stream(source_descriptor: int, destination_descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    remaining = expected_size
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while remaining:
            block = os.read(source_descriptor, min(COPY_CHUNK_BYTES, remaining))
            if not block:
                _fail("source_file_short_read")
            digest.update(block)
            _write_all(destination_descriptor, block)
            remaining -= len(block)
        if os.read(source_descriptor, 1):
            _fail("source_file_grew_during_copy")
    except OSError as exc:
        raise RecoveryCopyError("source_file_read_failed") from exc
    return digest.hexdigest()


def _hash_regular_descriptor(descriptor: int, expected: _Identity, *, field: str) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            block = os.read(descriptor, COPY_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
            total += len(block)
    except OSError as exc:
        raise RecoveryCopyError(f"{field}_readback_failed") from exc
    if total != expected.size_bytes or _Identity.from_stat(os.fstat(descriptor)) != expected:
        _fail(f"{field}_identity_changed_during_readback")
    return digest.hexdigest()


def _copy_regular_file(
    source_parent: int,
    destination_parent: int,
    entry: _Entry,
    *,
    pinned_source: int | None,
    expected_uid: int,
    expected_gid: int,
) -> str:
    name = entry.relative[-1]
    source_descriptor = (
        pinned_source
        if pinned_source is not None
        else _open_source_regular(source_parent, name, entry.identity, field="state_source_file")
    )
    close_source = pinned_source is None
    try:
        destination_descriptor: int | None = None
        try:
            named_before = os.stat(name, dir_fd=source_parent, follow_symlinks=False)
            if (
                _Identity.from_stat(named_before) != entry.identity
                or _Identity.from_stat(os.fstat(source_descriptor)) != entry.identity
            ):
                _fail("state_source_file_identity_changed")
            try:
                destination_descriptor = os.open(
                    name,
                    _write_flags(),
                    0o600,
                    dir_fd=destination_parent,
                )
            except OSError as exc:
                raise RecoveryCopyError("state_destination_file_create_failed") from exc
            digest = _copy_stream(source_descriptor, destination_descriptor, entry.identity.size_bytes)
            os.fchmod(destination_descriptor, stat.S_IMODE(entry.identity.mode))
            os.fsync(destination_descriptor)
            destination_identity = _Identity.from_stat(os.fstat(destination_descriptor))
            _require_owner(
                destination_identity,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                field="state_destination_file",
            )
            if (
                not stat.S_ISREG(destination_identity.mode)
                or destination_identity.link_count != 1
                or destination_identity.size_bytes != entry.identity.size_bytes
                or stat.S_IMODE(destination_identity.mode) != stat.S_IMODE(entry.identity.mode)
            ):
                _fail("state_destination_file_metadata_invalid")
        finally:
            if destination_descriptor is not None:
                os.close(destination_descriptor)

        named_after = os.stat(name, dir_fd=source_parent, follow_symlinks=False)
        if (
            _Identity.from_stat(named_after) != entry.identity
            or _Identity.from_stat(os.fstat(source_descriptor)) != entry.identity
        ):
            _fail("state_source_file_changed_during_copy")
        try:
            destination_read = os.open(name, _read_flags(), dir_fd=destination_parent)
        except OSError as exc:
            raise RecoveryCopyError("state_destination_file_readback_open_failed") from exc
        try:
            readback_identity = _Identity.from_stat(os.fstat(destination_read))
            named_destination = _Identity.from_stat(
                os.stat(name, dir_fd=destination_parent, follow_symlinks=False)
            )
            if readback_identity != named_destination:
                _fail("state_destination_file_identity_changed")
            readback_digest = _hash_regular_descriptor(
                destination_read,
                readback_identity,
                field="state_destination_file",
            )
            named_destination_after = _Identity.from_stat(
                os.stat(name, dir_fd=destination_parent, follow_symlinks=False)
            )
            if named_destination_after != readback_identity:
                _fail("state_destination_file_changed_during_readback")
        finally:
            os.close(destination_read)
        if readback_digest != digest:
            _fail("state_destination_file_hash_mismatch")
        return digest
    finally:
        if close_source:
            os.close(source_descriptor)


def _canonical_tree(
    entries: list[_Entry],
    file_hashes: dict[tuple[str, ...], str],
    *,
    excluded: frozenset[tuple[str, ...]],
) -> list[dict[str, object]]:
    canonical: list[dict[str, object]] = []
    for entry in entries:
        if entry.relative in excluded:
            continue
        record: dict[str, object] = {
            "gid": entry.identity.gid,
            "kind": entry.kind,
            "mode": stat.S_IMODE(entry.identity.mode),
            "path": _entry_path(entry.relative),
            "size_bytes": entry.identity.size_bytes if entry.kind == "file" else None,
            "uid": entry.identity.uid,
        }
        if entry.kind == "file":
            digest = file_hashes.get(entry.relative)
            if digest is None:
                _fail("destination_file_inventory_unexpected")
            record["sha256"] = digest
        canonical.append(record)
    return canonical


def _tree_digest(canonical: list[dict[str, object]]) -> str:
    raw = json.dumps(canonical, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def copy_state(
    source_raw: str | Path,
    destination_raw: str | Path,
    *,
    expected_uid: int,
    expected_gid: int,
    enforce_exact_incident: bool = True,
) -> dict[str, object]:
    """Copy one stopped incident state tree into one pristine destination."""

    source = _normalize_absolute(source_raw, field="source")
    destination = _normalize_absolute(destination_raw, field="destination")
    source_descriptor, source_root_identity = _open_absolute_directory(source, field="source")
    try:
        destination_descriptor, destination_root_identity = _open_absolute_directory(
            destination,
            field="destination",
        )
    except BaseException:
        os.close(source_descriptor)
        raise
    pinned: dict[tuple[str, ...], int] = {}
    try:
        _require_distinct_roots(
            source,
            source_root_identity,
            destination,
            destination_root_identity,
        )
        _require_state_root(
            source_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            destination=False,
        )
        _require_state_root(
            destination_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            destination=True,
        )
        if _list_names(destination_descriptor, field="destination"):
            _fail("destination_not_empty")

        source_entries = _scan_state_tree(
            source_descriptor,
            source_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allow_incident_shm=True,
        )
        _validate_incident_inventory(
            source_entries,
            source=True,
            enforce_exact_incident=enforce_exact_incident,
        )
        source_by_path = _entry_map(source_entries)
        if (destination_root_identity.device, destination_root_identity.inode) in {
            (entry.identity.device, entry.identity.inode) for entry in source_entries
        }:
            _fail("source_destination_inode_overlap")

        for relative in (MAIN_DATABASE, INCIDENT_WAL, INCIDENT_SHM):
            pinned[relative] = _open_source_regular(
                source_descriptor,
                relative[0],
                source_by_path[relative].identity,
                field="incident_sqlite_file",
            )
        if os.pread(pinned[MAIN_DATABASE], len(SQLITE_HEADER), 0) != SQLITE_HEADER:
            _fail("main_database_header_invalid")
        incident_shm_digest = _hash_regular_descriptor(
            pinned[INCIDENT_SHM],
            source_by_path[INCIDENT_SHM].identity,
            field="incident_shm",
        )

        os.fchmod(destination_descriptor, 0o700)
        os.fsync(destination_descriptor)
        sealed_destination_root = _Identity.from_stat(os.fstat(destination_descriptor))
        _require_state_root(
            sealed_destination_root,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            destination=False,
        )

        children: dict[tuple[str, ...], list[_Entry]] = {}
        for entry in source_entries[1:]:
            children.setdefault(entry.relative[:-1], []).append(entry)
        file_hashes: dict[tuple[str, ...], str] = {}

        def copy_directory(
            relative: tuple[str, ...],
            source_parent: int,
            destination_parent: int,
        ) -> None:
            directory_entry = source_by_path[relative]
            if _Identity.from_stat(os.fstat(source_parent)) != directory_entry.identity:
                _fail("state_source_directory_changed_during_copy")
            for entry in children.get(relative, []):
                name = entry.relative[-1]
                named = _Identity.from_stat(os.stat(name, dir_fd=source_parent, follow_symlinks=False))
                if named != entry.identity:
                    _fail("state_source_entry_changed_before_copy")
                if entry.relative == INCIDENT_SHM:
                    continue
                if entry.kind == "file":
                    file_hashes[entry.relative] = _copy_regular_file(
                        source_parent,
                        destination_parent,
                        entry,
                        pinned_source=pinned.get(entry.relative),
                        expected_uid=expected_uid,
                        expected_gid=expected_gid,
                    )
                    continue
                source_child, opened_identity = _open_child_directory(
                    source_parent,
                    name,
                    field="state_source",
                )
                destination_child: int | None = None
                try:
                    if opened_identity != entry.identity:
                        _fail("state_source_directory_identity_changed")
                    try:
                        os.mkdir(name, 0o700, dir_fd=destination_parent)
                        destination_child, _ = _open_child_directory(
                            destination_parent,
                            name,
                            field="state_destination",
                        )
                    except OSError as exc:
                        raise RecoveryCopyError("state_destination_directory_create_failed") from exc
                    copy_directory(entry.relative, source_child, destination_child)
                    os.fchmod(destination_child, stat.S_IMODE(entry.identity.mode))
                    os.fsync(destination_child)
                    destination_child_identity = _Identity.from_stat(os.fstat(destination_child))
                    _require_owner(
                        destination_child_identity,
                        expected_uid=expected_uid,
                        expected_gid=expected_gid,
                        field="state_destination_directory",
                    )
                    if destination_child_identity.device != sealed_destination_root.device or stat.S_IMODE(
                        destination_child_identity.mode
                    ) != stat.S_IMODE(entry.identity.mode):
                        _fail("state_destination_directory_metadata_invalid")
                finally:
                    if destination_child is not None:
                        os.close(destination_child)
                    os.close(source_child)
            os.fsync(destination_parent)
            if _Identity.from_stat(os.fstat(source_parent)) != directory_entry.identity:
                _fail("state_source_directory_changed_during_copy")

        copy_directory((), source_descriptor, destination_descriptor)
        os.fsync(destination_descriptor)
        if enforce_exact_incident:
            if file_hashes[MAIN_DATABASE] != INCIDENT_MAIN_DATABASE_SHA256:
                _fail("incident_main_database_hash_mismatch")
            if file_hashes[INCIDENT_WAL] != INCIDENT_WAL_SHA256:
                _fail("incident_wal_hash_mismatch")
            if incident_shm_digest != INCIDENT_SHM_SHA256:
                _fail("incident_shm_hash_mismatch")

        source_after = _scan_state_tree(
            source_descriptor,
            source_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allow_incident_shm=True,
        )
        if source_after != source_entries:
            _fail("source_tree_changed_during_copy")
        for pinned_relative, descriptor in pinned.items():
            if _Identity.from_stat(os.fstat(descriptor)) != source_by_path[pinned_relative].identity:
                _fail("incident_sqlite_file_changed_during_copy")
        _verify_absolute_directory(
            source,
            source_descriptor,
            source_root_identity,
            field="source",
        )

        destination_after_identity = _Identity.from_stat(os.fstat(destination_descriptor))
        destination_entries = _scan_state_tree(
            destination_descriptor,
            destination_after_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allow_incident_shm=False,
        )
        _validate_incident_inventory(
            destination_entries,
            source=False,
            enforce_exact_incident=False,
        )
        if {(entry.identity.device, entry.identity.inode) for entry in source_entries} & {
            (entry.identity.device, entry.identity.inode) for entry in destination_entries
        }:
            _fail("source_destination_inode_overlap")
        expected_canonical = _canonical_tree(
            source_entries,
            file_hashes,
            excluded=frozenset({INCIDENT_SHM}),
        )
        destination_canonical = _canonical_tree(
            destination_entries,
            file_hashes,
            excluded=frozenset(),
        )
        if destination_canonical != expected_canonical:
            _fail("destination_tree_mismatch")
        _verify_absolute_directory(
            destination,
            destination_descriptor,
            destination_after_identity,
            field="destination",
        )

        digest = _tree_digest(expected_canonical)
        file_count = len(file_hashes)
        directory_count = len(expected_canonical) - file_count
        return {
            "bytes_copied": sum(
                entry.identity.size_bytes
                for entry in source_entries
                if entry.kind == "file" and entry.relative != INCIDENT_SHM
            ),
            "content_tree_sha256": digest,
            "directory_count": directory_count,
            "excluded_entries": [INCIDENT_SHM[0]],
            "file_count": file_count,
            "incident_source_directory_entry_count": sum(
                entry.kind == "directory" and bool(entry.relative) for entry in source_entries
            ),
            "incident_source_file_count": sum(entry.kind == "file" for entry in source_entries),
            "incident_source_total_file_bytes": sum(
                entry.identity.size_bytes for entry in source_entries if entry.kind == "file"
            ),
            "main_database_sha256": file_hashes[MAIN_DATABASE],
            "main_database_size_bytes": source_by_path[MAIN_DATABASE].identity.size_bytes,
            "mode": "state",
            "schema_version": SCHEMA_VERSION,
            "shm_excluded_sha256": incident_shm_digest,
            "shm_excluded_size_bytes": source_by_path[INCIDENT_SHM].identity.size_bytes,
            "status": "passed",
            "wal_sha256": file_hashes[INCIDENT_WAL],
            "wal_size_bytes": source_by_path[INCIDENT_WAL].identity.size_bytes,
        }
    except OSError as exc:
        raise RecoveryCopyError("state_filesystem_operation_failed") from exc
    finally:
        for descriptor in pinned.values():
            os.close(descriptor)
        os.close(destination_descriptor)
        os.close(source_descriptor)


def _require_codex_root(
    identity: _Identity,
    *,
    expected_uid: int,
    expected_gid: int,
    destination: bool,
) -> None:
    field = "codex_destination_root" if destination else "codex_source_root"
    if not stat.S_ISDIR(identity.mode):
        _fail(f"{field}_not_directory")
    _require_owner(identity, expected_uid=expected_uid, expected_gid=expected_gid, field=field)
    allowed = STATE_DIRECTORY_MODES if destination else frozenset({0o700})
    if stat.S_IMODE(identity.mode) not in allowed:
        _fail(f"{field}_mode_invalid")


def copy_codex_auth(
    source_raw: str | Path,
    destination_raw: str | Path,
    *,
    expected_uid: int,
    expected_gid: int,
    maximum_auth_bytes: int = MAXIMUM_AUTH_BYTES,
) -> dict[str, object]:
    """Copy only bounded ``auth.json`` into one pristine Codex destination."""

    if maximum_auth_bytes <= 0:
        _fail("codex_auth_limit_invalid")
    source = _normalize_absolute(source_raw, field="source")
    destination = _normalize_absolute(destination_raw, field="destination")
    source_descriptor, source_root_identity = _open_absolute_directory(source, field="source")
    try:
        destination_descriptor, destination_root_identity = _open_absolute_directory(
            destination,
            field="destination",
        )
    except BaseException:
        os.close(source_descriptor)
        raise
    source_auth_descriptor: int | None = None
    destination_auth_descriptor: int | None = None
    destination_home_descriptor: int | None = None
    try:
        _require_distinct_roots(
            source,
            source_root_identity,
            destination,
            destination_root_identity,
        )
        _require_codex_root(
            source_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            destination=False,
        )
        _require_codex_root(
            destination_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            destination=True,
        )
        if _list_names(destination_descriptor, field="codex_destination") != ["home"]:
            _fail("codex_destination_inventory_invalid")
        destination_home_descriptor, home_identity = _open_child_directory(
            destination_descriptor,
            "home",
            field="codex_destination",
        )
        _require_owner(
            home_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            field="codex_destination_home",
        )
        if (
            home_identity.device != destination_root_identity.device
            or stat.S_IMODE(home_identity.mode) != 0o700
            or _list_names(destination_home_descriptor, field="codex_destination_home")
        ):
            _fail("codex_destination_home_invalid")

        try:
            source_auth_named = os.stat("auth.json", dir_fd=source_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise RecoveryCopyError("codex_source_auth_invalid") from exc
        source_auth_identity = _Identity.from_stat(source_auth_named)
        if (
            source_auth_identity.device != source_root_identity.device
            or not stat.S_ISREG(source_auth_identity.mode)
            or source_auth_identity.link_count != 1
            or stat.S_IMODE(source_auth_identity.mode) != 0o600
            or not 1 <= source_auth_identity.size_bytes <= maximum_auth_bytes
        ):
            _fail("codex_source_auth_invalid")
        _require_owner(
            source_auth_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            field="codex_source_auth",
        )
        source_auth_descriptor = _open_source_regular(
            source_descriptor,
            "auth.json",
            source_auth_identity,
            field="codex_source_auth",
        )

        os.fchmod(destination_descriptor, 0o700)
        os.fsync(destination_home_descriptor)
        os.fsync(destination_descriptor)
        try:
            destination_auth_descriptor = os.open(
                CODEX_TEMPORARY_NAME,
                _write_flags(),
                0o600,
                dir_fd=destination_descriptor,
            )
        except OSError as exc:
            raise RecoveryCopyError("codex_destination_temporary_create_failed") from exc
        digest = _copy_stream(
            source_auth_descriptor,
            destination_auth_descriptor,
            source_auth_identity.size_bytes,
        )
        os.fchmod(destination_auth_descriptor, 0o600)
        os.fsync(destination_auth_descriptor)
        temporary_identity = _Identity.from_stat(os.fstat(destination_auth_descriptor))
        _require_owner(
            temporary_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            field="codex_destination_auth",
        )
        if (
            temporary_identity.size_bytes != source_auth_identity.size_bytes
            or temporary_identity.link_count != 1
            or stat.S_IMODE(temporary_identity.mode) != 0o600
        ):
            _fail("codex_destination_auth_metadata_invalid")
        os.close(destination_auth_descriptor)
        destination_auth_descriptor = None

        named_source_after = _Identity.from_stat(
            os.stat("auth.json", dir_fd=source_descriptor, follow_symlinks=False)
        )
        if (
            named_source_after != source_auth_identity
            or _Identity.from_stat(os.fstat(source_auth_descriptor)) != source_auth_identity
        ):
            _fail("codex_source_auth_changed_during_copy")
        temporary_read = os.open(CODEX_TEMPORARY_NAME, _read_flags(), dir_fd=destination_descriptor)
        try:
            temporary_read_identity = _Identity.from_stat(os.fstat(temporary_read))
            if temporary_read_identity != temporary_identity:
                _fail("codex_destination_auth_identity_changed")
            if (
                _hash_regular_descriptor(
                    temporary_read,
                    temporary_read_identity,
                    field="codex_destination_auth",
                )
                != digest
            ):
                _fail("codex_destination_auth_content_mismatch")
            temporary_named_after = _Identity.from_stat(
                os.stat(
                    CODEX_TEMPORARY_NAME,
                    dir_fd=destination_descriptor,
                    follow_symlinks=False,
                )
            )
            if temporary_named_after != temporary_read_identity:
                _fail("codex_destination_auth_identity_changed")
        finally:
            os.close(temporary_read)

        try:
            os.link(
                CODEX_TEMPORARY_NAME,
                "auth.json",
                src_dir_fd=destination_descriptor,
                dst_dir_fd=destination_descriptor,
                follow_symlinks=False,
            )
            os.fsync(destination_descriptor)
            os.unlink(CODEX_TEMPORARY_NAME, dir_fd=destination_descriptor)
            os.fsync(destination_descriptor)
        except OSError as exc:
            raise RecoveryCopyError("codex_destination_auth_publish_failed") from exc

        if _list_names(destination_descriptor, field="codex_destination") != ["auth.json", "home"]:
            _fail("codex_destination_inventory_invalid")
        published = os.stat("auth.json", dir_fd=destination_descriptor, follow_symlinks=False)
        published_identity = _Identity.from_stat(published)
        if (
            not stat.S_ISREG(published.st_mode)
            or published_identity.device != destination_root_identity.device
            or published_identity.link_count != 1
            or published_identity.size_bytes != source_auth_identity.size_bytes
            or stat.S_IMODE(published.st_mode) != 0o600
        ):
            _fail("codex_destination_auth_metadata_invalid")
        _require_owner(
            published_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            field="codex_destination_auth",
        )
        published_descriptor = os.open("auth.json", _read_flags(), dir_fd=destination_descriptor)
        try:
            if (
                _hash_regular_descriptor(
                    published_descriptor,
                    published_identity,
                    field="codex_destination_auth",
                )
                != digest
            ):
                _fail("codex_destination_auth_content_mismatch")
            published_named_after = _Identity.from_stat(
                os.stat("auth.json", dir_fd=destination_descriptor, follow_symlinks=False)
            )
            if published_named_after != published_identity:
                _fail("codex_destination_auth_identity_changed")
        finally:
            os.close(published_descriptor)
        final_source_auth = _Identity.from_stat(
            os.stat("auth.json", dir_fd=source_descriptor, follow_symlinks=False)
        )
        if (
            final_source_auth != source_auth_identity
            or _Identity.from_stat(os.fstat(source_auth_descriptor)) != source_auth_identity
        ):
            _fail("codex_source_auth_changed_during_copy")
        if _Identity.from_stat(os.fstat(destination_home_descriptor)) != home_identity or _list_names(
            destination_home_descriptor, field="codex_destination_home"
        ):
            _fail("codex_destination_home_changed_during_copy")
        _verify_absolute_directory(
            source,
            source_descriptor,
            source_root_identity,
            field="source",
        )
        final_destination_identity = _Identity.from_stat(os.fstat(destination_descriptor))
        _verify_absolute_directory(
            destination,
            destination_descriptor,
            final_destination_identity,
            field="destination",
        )
        return {
            "auth_bytes_copied": source_auth_identity.size_bytes,
            "destination_entry_count": 2,
            "destination_home_empty": True,
            "mode": "codex",
            "schema_version": SCHEMA_VERSION,
            "status": "passed",
        }
    except OSError as exc:
        raise RecoveryCopyError("codex_filesystem_operation_failed") from exc
    finally:
        if destination_auth_descriptor is not None:
            os.close(destination_auth_descriptor)
        if source_auth_descriptor is not None:
            os.close(source_auth_descriptor)
        if destination_home_descriptor is not None:
            os.close(destination_home_descriptor)
        os.close(destination_descriptor)
        os.close(source_descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("state", "codex"):
        subparser = subparsers.add_parser(mode)
        subparser.add_argument("--source", required=True)
        subparser.add_argument("--destination", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "state":
            receipt = copy_state(
                args.source,
                args.destination,
                expected_uid=PRODUCTION_UID,
                expected_gid=PRODUCTION_GID,
            )
        else:
            receipt = copy_codex_auth(
                args.source,
                args.destination,
                expected_uid=PRODUCTION_UID,
                expected_gid=PRODUCTION_GID,
            )
    except RecoveryCopyError as exc:
        print(f"cardrag_v113_recovery_copy_failed:{exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
