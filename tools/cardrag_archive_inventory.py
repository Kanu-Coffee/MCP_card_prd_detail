#!/usr/bin/env python3
"""Create a fail-closed, read-only inventory of explicitly allowed local trees.

The scanner never follows symlinks and has no move, delete, Docker, or WebDAV
capability.  Regular files are opened relative to pinned directory descriptors;
their identity is checked before and after SHA-256 hashing.  Supplying
``--output`` is the only opt-in filesystem mutation: it creates one new,
mode-0600 manifest and refuses to overwrite an existing path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

SCHEMA_VERSION: Final = "cardrag.archive-inventory.v1"
OUTPUT_RECEIPT_SCHEMA: Final = "cardrag.archive-inventory-output.v1"
DEFAULT_MAX_ENTRIES: Final = 1_000_000
MAXIMUM_DEPTH: Final = 128
HASH_CHUNK_BYTES: Final = 1024 * 1024


class ArchiveInventoryError(RuntimeError):
    """A requested root or observed node cannot be inventoried safely."""


@dataclass(frozen=True, slots=True)
class _Identity:
    device: int
    inode: int
    mode: int
    link_count: int
    uid: int
    gid: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> _Identity:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            link_count=metadata.st_nlink,
            uid=metadata.st_uid,
            gid=metadata.st_gid,
            size_bytes=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )

    def as_json(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "mode": stat.S_IMODE(self.mode),
            "link_count": self.link_count,
            "uid": self.uid,
            "gid": self.gid,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }


@dataclass(slots=True)
class _EntryBudget:
    maximum: int
    used: int = 0

    def consume(self) -> None:
        self.used += 1
        if self.used > self.maximum:
            raise ArchiveInventoryError("inventory entry limit exceeded")


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _normalize_absolute(value: str | Path, *, field: str) -> Path:
    path = Path(value)
    rendered = str(path)
    if (
        not path.is_absolute()
        or path == Path("/")
        or "\x00" in rendered
        or ".." in path.parts
        or os.path.normpath(rendered) != rendered
    ):
        raise ArchiveInventoryError(f"{field} must be an absolute normalized path below /")
    return path


def _node_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    field: str,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        raise ArchiveInventoryError(f"{field} could not be pinned without following symlinks") from exc
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or _Identity.from_stat(opened) != _Identity.from_stat(named)
        ):
            raise ArchiveInventoryError(f"{field} is not one stable directory")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _open_absolute_directory(path: Path, *, field: str) -> tuple[int, os.stat_result]:
    descriptor = os.open(os.sep, _directory_flags())
    opened = os.fstat(descriptor)
    try:
        for component in path.parts[1:]:
            child, opened = _open_child_directory(descriptor, component, field=field)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _verify_absolute_directory(path: Path, descriptor: int, *, field: str) -> None:
    try:
        current, _metadata = _open_absolute_directory(path, field=field)
    except ArchiveInventoryError as exc:
        raise ArchiveInventoryError(f"{field} changed after it was pinned") from exc
    try:
        if _Identity.from_stat(os.fstat(current)) != _Identity.from_stat(os.fstat(descriptor)):
            raise ArchiveInventoryError(f"{field} changed after it was pinned")
    finally:
        os.close(current)


def _validate_text(value: str, *, field: str) -> bytes:
    raw = os.fsencode(value)
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ArchiveInventoryError(f"{field} is not valid UTF-8") from exc
    if decoded != value or not value or "\x00" in value or "/" in value:
        raise ArchiveInventoryError(f"{field} is not a safe single path component")
    return raw


def _relative_path(parts: tuple[str, ...]) -> str:
    return "." if not parts else "/".join(parts)


def _metadata_record(relative_path: str, kind: str, identity: _Identity) -> dict[str, object]:
    return {"path": relative_path, "kind": kind, **identity.as_json()}


def _hash_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, HASH_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _scan_regular_file(
    parent_descriptor: int,
    name: str,
    relative_path: str,
    named_before: os.stat_result,
) -> dict[str, object]:
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        raise ArchiveInventoryError(f"regular file changed or became unsafe: {relative_path}") from exc
    try:
        opened = os.fstat(descriptor)
        expected = _Identity.from_stat(named_before)
        if not stat.S_ISREG(opened.st_mode) or _Identity.from_stat(opened) != expected:
            raise ArchiveInventoryError(f"regular file identity changed before hashing: {relative_path}")
        sha256, bytes_read = _hash_descriptor(descriptor)
        opened_after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            bytes_read != expected.size_bytes
            or _Identity.from_stat(opened_after) != expected
            or _Identity.from_stat(named_after) != expected
        ):
            raise ArchiveInventoryError(f"regular file identity changed while hashing: {relative_path}")
    finally:
        os.close(descriptor)
    return {**_metadata_record(relative_path, "file", expected), "sha256": sha256}


def _scan_symlink(
    parent_descriptor: int,
    name: str,
    relative_path: str,
    named_before: os.stat_result,
) -> dict[str, object]:
    expected = _Identity.from_stat(named_before)
    try:
        target = os.readlink(name, dir_fd=parent_descriptor)
        target_bytes = os.fsencode(target)
        target_bytes.decode("utf-8", errors="strict")
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except (OSError, UnicodeDecodeError) as exc:
        raise ArchiveInventoryError(f"symlink changed or has a non-UTF-8 target: {relative_path}") from exc
    if _Identity.from_stat(named_after) != expected:
        raise ArchiveInventoryError(f"symlink identity changed while reading: {relative_path}")
    return {
        **_metadata_record(relative_path, "symlink", expected),
        "sha256": hashlib.sha256(target_bytes).hexdigest(),
        "target_size_bytes": len(target_bytes),
        "target_is_absolute": os.path.isabs(target),
    }


def _walk_directory(
    descriptor: int,
    directory_identity: _Identity,
    relative_parts: tuple[str, ...],
    *,
    root_device: int,
    budget: _EntryBudget,
    entries: list[dict[str, object]],
    hardlinks: dict[tuple[int, int], list[str]],
) -> None:
    if len(relative_parts) > MAXIMUM_DEPTH:
        raise ArchiveInventoryError("maximum directory depth exceeded")
    try:
        with os.scandir(descriptor) as iterator:
            names = [entry.name for entry in iterator]
    except OSError as exc:
        raise ArchiveInventoryError(
            f"directory could not be listed: {_relative_path(relative_parts)}"
        ) from exc
    for name in sorted(names, key=os.fsencode):
        _validate_text(name, field="inventory entry name")
        budget.consume()
        child_parts = (*relative_parts, name)
        relative_path = _relative_path(child_parts)
        try:
            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ArchiveInventoryError(f"entry disappeared during inventory: {relative_path}") from exc
        if named.st_dev != root_device:
            raise ArchiveInventoryError(
                f"cross-filesystem entry requires a separate explicit root: {relative_path}"
            )
        if stat.S_ISDIR(named.st_mode):
            child_descriptor, opened = _open_child_directory(
                descriptor,
                name,
                field=f"directory {relative_path}",
            )
            opened_identity = _Identity.from_stat(opened)
            try:
                _walk_directory(
                    child_descriptor,
                    opened_identity,
                    child_parts,
                    root_device=root_device,
                    budget=budget,
                    entries=entries,
                    hardlinks=hardlinks,
                )
                current = _Identity.from_stat(os.fstat(child_descriptor))
                renamed = _Identity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
                if current != opened_identity or renamed != opened_identity:
                    raise ArchiveInventoryError(
                        f"directory identity changed during inventory: {relative_path}"
                    )
            finally:
                os.close(child_descriptor)
            entries.append(_metadata_record(relative_path, "directory", opened_identity))
        elif stat.S_ISREG(named.st_mode):
            record = _scan_regular_file(descriptor, name, relative_path, named)
            entries.append(record)
            identity = _Identity.from_stat(named)
            if identity.link_count > 1:
                hardlinks.setdefault((identity.device, identity.inode), []).append(relative_path)
        elif stat.S_ISLNK(named.st_mode):
            entries.append(_scan_symlink(descriptor, name, relative_path, named))
        else:
            raise ArchiveInventoryError(f"special filesystem node is not permitted: {relative_path}")
    if _Identity.from_stat(os.fstat(descriptor)) != directory_identity:
        raise ArchiveInventoryError(
            f"directory identity changed while listing: {_relative_path(relative_parts)}"
        )


def _content_projection(record: dict[str, object]) -> dict[str, object]:
    projected = {key: record[key] for key in ("path", "kind", "mode", "uid", "gid")}
    if record["kind"] == "file":
        projected["size_bytes"] = record["size_bytes"]
        projected["sha256"] = record["sha256"]
    elif record["kind"] == "symlink":
        projected["target_size_bytes"] = record["target_size_bytes"]
        projected["sha256"] = record["sha256"]
    return projected


def _scan_root(path: Path, budget: _EntryBudget) -> dict[str, object]:
    descriptor, root_metadata = _open_absolute_directory(path, field="inventory root")
    root_identity = _Identity.from_stat(root_metadata)
    budget.consume()
    entries: list[dict[str, object]] = []
    hardlinks: dict[tuple[int, int], list[str]] = {}
    try:
        _walk_directory(
            descriptor,
            root_identity,
            (),
            root_device=root_identity.device,
            budget=budget,
            entries=entries,
            hardlinks=hardlinks,
        )
        _verify_absolute_directory(path, descriptor, field="inventory root")
    finally:
        os.close(descriptor)
    entries.append(_metadata_record(".", "directory", root_identity))
    entries.sort(key=lambda record: os.fsencode(str(record["path"])))
    regular_files = [record for record in entries if record["kind"] == "file"]
    symlinks = [record for record in entries if record["kind"] == "symlink"]
    directories = [record for record in entries if record["kind"] == "directory"]
    hardlink_groups = [
        {
            "device": device,
            "inode": inode,
            "paths": sorted(paths, key=os.fsencode),
        }
        for (device, inode), paths in sorted(hardlinks.items())
        if len(paths) > 1
    ]
    return {
        "path": str(path),
        "root_identity": root_identity.as_json(),
        "entries": entries,
        "summary": {
            "entry_count": len(entries),
            "file_count": len(regular_files),
            "directory_count": len(directories),
            "symlink_count": len(symlinks),
            "total_file_bytes": sum(int(record["size_bytes"]) for record in regular_files),
            "hardlink_group_count": len(hardlink_groups),
            "hardlink_groups": hardlink_groups,
            "content_tree_sha256": _sha256_json([_content_projection(record) for record in entries]),
            "identity_tree_sha256": _sha256_json(entries),
        },
    }


def _validate_roots(
    roots: list[str | Path], allowed_roots: list[str | Path]
) -> tuple[list[Path], list[Path]]:
    if not roots:
        raise ArchiveInventoryError("at least one inventory root is required")
    if not allowed_roots:
        raise ArchiveInventoryError("at least one allow-root is required")
    normalized_roots = [_normalize_absolute(path, field="inventory root") for path in roots]
    normalized_allowed = [_normalize_absolute(path, field="allow-root") for path in allowed_roots]
    if len(set(normalized_roots)) != len(normalized_roots):
        raise ArchiveInventoryError("inventory roots must be unique")
    for index, root in enumerate(normalized_roots):
        if not any(root == allowed or allowed in root.parents for allowed in normalized_allowed):
            raise ArchiveInventoryError(f"inventory root is outside the explicit allowlist: {root}")
        for other in normalized_roots[index + 1 :]:
            if root in other.parents or other in root.parents:
                raise ArchiveInventoryError("inventory roots must not overlap")
    for allowed in normalized_allowed:
        descriptor, _metadata = _open_absolute_directory(allowed, field="allow-root")
        os.close(descriptor)
    return normalized_roots, normalized_allowed


def build_inventory(
    roots: list[str | Path],
    allowed_roots: list[str | Path],
    *,
    maximum_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict[str, object]:
    """Inventory *roots* after proving they are below pinned allowed roots."""

    if maximum_entries <= 0:
        raise ArchiveInventoryError("maximum_entries must be positive")
    normalized_roots, normalized_allowed = _validate_roots(roots, allowed_roots)
    budget = _EntryBudget(maximum=maximum_entries)
    root_manifests = [_scan_root(root, budget) for root in sorted(normalized_roots)]
    summaries = [manifest["summary"] for manifest in root_manifests]
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode": "read-only-local-filesystem-inventory",
        "consistency": "stable-per-root; multiple roots are not an atomic snapshot",
        "allowed_roots": [str(path) for path in sorted(normalized_allowed)],
        "roots": root_manifests,
        "summary": {
            "root_count": len(root_manifests),
            "entry_count": budget.used,
            "file_count": sum(int(summary["file_count"]) for summary in summaries),
            "directory_count": sum(int(summary["directory_count"]) for summary in summaries),
            "symlink_count": sum(int(summary["symlink_count"]) for summary in summaries),
            "total_file_bytes": sum(int(summary["total_file_bytes"]) for summary in summaries),
        },
    }
    payload["inventory_sha256"] = _sha256_json(payload)
    return payload


def _write_new_manifest(path: Path, raw: bytes, inventory_roots: list[Path]) -> None:
    output = _normalize_absolute(path, field="output")
    if any(output == root or root in output.parents for root in inventory_roots):
        raise ArchiveInventoryError("output must be outside every inventoried root")
    parent_descriptor, parent_metadata = _open_absolute_directory(output.parent, field="output parent")
    name = output.name
    _validate_text(name, field="output filename")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
        except OSError as exc:
            raise ArchiveInventoryError("output must be a new regular file") from exc
        try:
            offset = 0
            while offset < len(raw):
                written_bytes = os.write(descriptor, raw[offset:])
                if written_bytes <= 0:
                    raise ArchiveInventoryError("output write made no progress")
                offset += written_bytes
            os.fsync(descriptor)
            written = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            written_identity = _Identity.from_stat(written)
            named_identity = _Identity.from_stat(named)
            expected_raw_sha256 = hashlib.sha256(raw).hexdigest()
            os.lseek(descriptor, 0, os.SEEK_SET)
            readback_sha256, readback_size = _hash_descriptor(descriptor)
            readback_identity = _Identity.from_stat(os.fstat(descriptor))
            if (
                not stat.S_ISREG(written.st_mode)
                or written.st_nlink != 1
                or written.st_size != len(raw)
                or written_identity != named_identity
                or readback_identity != written_identity
                or readback_size != len(raw)
                or readback_sha256 != expected_raw_sha256
            ):
                raise ArchiveInventoryError("output identity or readback changed while writing")
            current_parent, _metadata = _open_absolute_directory(output.parent, field="output parent")
            try:
                if _node_identity(os.fstat(current_parent)) != _node_identity(parent_metadata):
                    raise ArchiveInventoryError("output parent changed while writing")
                final_named = os.stat(name, dir_fd=current_parent, follow_symlinks=False)
                final_named_identity = _Identity.from_stat(final_named)
                final_pinned_identity = _Identity.from_stat(os.fstat(descriptor))
                if (
                    not stat.S_ISREG(final_named.st_mode)
                    or final_named_identity != readback_identity
                    or final_pinned_identity != readback_identity
                ):
                    raise ArchiveInventoryError("output path changed after readback")
            finally:
                os.close(current_parent)
            os.fsync(parent_descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-root",
        action="append",
        required=True,
        help="absolute directory allowed to contain --root (repeatable)",
    )
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        help="absolute directory to inventory without following symlinks (repeatable)",
    )
    parser.add_argument(
        "--maximum-entries",
        type=_positive_integer,
        default=DEFAULT_MAX_ENTRIES,
        help=f"fail after this many entries (default: {DEFAULT_MAX_ENTRIES})",
    )
    parser.add_argument(
        "--output",
        help="optional absolute new file; existing files are never overwritten",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        inventory = build_inventory(
            arguments.root,
            arguments.allow_root,
            maximum_entries=arguments.maximum_entries,
        )
        if arguments.pretty:
            raw = (json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        else:
            raw = _canonical_json_bytes(inventory)
        if arguments.output is None:
            sys.stdout.buffer.write(raw)
        else:
            roots = [_normalize_absolute(path, field="inventory root") for path in arguments.root]
            output = _normalize_absolute(arguments.output, field="output")
            _write_new_manifest(output, raw, roots)
            receipt = {
                "schema_version": OUTPUT_RECEIPT_SCHEMA,
                "output": str(output),
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            sys.stdout.buffer.write(_canonical_json_bytes(receipt))
        return 0
    except (ArchiveInventoryError, OSError, ValueError) as exc:
        print(f"cardrag-archive-inventory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
