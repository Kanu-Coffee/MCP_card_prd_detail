#!/usr/bin/env python3
"""Fail-closed verification for a quiesced CardRAG object/generation store."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

SHA256 = re.compile(r"^[0-9a-f]{64}$")
GENERATION_ID = re.compile(r"^gen-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


class VerificationError(RuntimeError):
    """The filesystem cannot be safely used as a CardRAG runtime store."""


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def reject_unsafe_entries(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise VerificationError(f"root must be a non-symlink directory: {root}")
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in (*directory_names, *file_names):
            entry = current / name
            metadata = entry.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise VerificationError(f"symlink is forbidden: {entry.relative_to(root)}")
            if name in directory_names and not stat.S_ISDIR(metadata.st_mode):
                raise VerificationError(f"non-directory in directory inventory: {entry}")
            if name in file_names and not stat.S_ISREG(metadata.st_mode):
                raise VerificationError(f"special file is forbidden: {entry.relative_to(root)}")


def canonical_json_hash(payload: dict[str, Any]) -> str:
    canonical = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def verify_objects(root: Path, *, allow_migration_markers: bool = False) -> dict[str, int | str]:
    reject_unsafe_entries(root)
    allowed = {".incoming", "sha256"}
    if allow_migration_markers:
        allowed |= {".cardrag-storage-migration-owner", ".cardrag-storage-migration-commit"}
    unexpected = sorted(path.name for path in root.iterdir() if path.name not in allowed)
    if unexpected:
        raise VerificationError(f"unexpected object-store root entry: {unexpected[0]}")
    incoming = root / ".incoming"
    if incoming.exists():
        if incoming.is_symlink() or not incoming.is_dir():
            raise VerificationError("object .incoming root is unsafe")
        if any(incoming.iterdir()):
            raise VerificationError("object .incoming directory is not empty")

    sha_root = root / "sha256"
    if not sha_root.exists():
        return {"count": 0, "bytes": 0, "inventory_sha256": hashlib.sha256(b"").hexdigest()}
    if sha_root.is_symlink() or not sha_root.is_dir():
        raise VerificationError("object sha256 root is unsafe")

    inventory = hashlib.sha256()
    count = 0
    total_bytes = 0
    for path in sorted(sha_root.rglob("*")):
        if path.is_dir():
            parts = path.relative_to(sha_root).parts
            if len(parts) != 1 or not re.fullmatch(r"[0-9a-f]{2}", parts[0]):
                raise VerificationError(
                    f"invalid content-addressed directory: {path.relative_to(root)}"
                )
            continue
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(sha_root).parts
        if len(parts) != 2 or len(parts[0]) != 2 or not SHA256.fullmatch(parts[1]):
            raise VerificationError(f"invalid content-addressed object path: {relative}")
        if parts[0] != parts[1][:2]:
            raise VerificationError(f"object prefix differs from digest: {relative}")
        actual, size = hash_file(path)
        if actual != parts[1]:
            raise VerificationError(f"object content differs from digest: {relative}")
        inventory.update(f"{relative}\0{actual}\0{size}\n".encode())
        count += 1
        total_bytes += size
    return {"count": count, "bytes": total_bytes, "inventory_sha256": inventory.hexdigest()}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VerificationError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"JSON object required: {path}")
    return value


def verify_generation(path: Path) -> tuple[str, str, int]:
    generation_id = path.name
    if not GENERATION_ID.fullmatch(generation_id):
        raise VerificationError(f"invalid generation directory: {generation_id}")
    manifest_path = path / "manifest.json"
    ready_path = path / "READY"
    if not manifest_path.is_file() or not ready_path.is_file():
        raise VerificationError(f"generation is not sealed: {generation_id}")
    manifest = load_json(manifest_path)
    if manifest.get("generation_id") != generation_id:
        raise VerificationError(f"generation ID mismatch: {generation_id}")
    manifest_sha256 = canonical_json_hash(manifest)
    ready = load_json(ready_path)
    if ready != {"generation_id": generation_id, "manifest_sha256": manifest_sha256}:
        raise VerificationError(f"READY seal differs from manifest: {generation_id}")

    files = manifest.get("files")
    if not isinstance(files, list):
        raise VerificationError(f"generation file inventory is invalid: {generation_id}")
    total_bytes = 0
    seen: set[str] = set()
    resolved_generation = path.resolve(strict=True)
    for entry in files:
        if not isinstance(entry, dict):
            raise VerificationError(f"generation file entry is invalid: {generation_id}")
        relative = entry.get("path")
        expected_sha = entry.get("sha256")
        expected_size = entry.get("size")
        if (
            not isinstance(relative, str)
            or not relative
            or relative in {"manifest.json", "READY"}
            or relative in seen
            or Path(relative).is_absolute()
            or Path(relative).as_posix() != relative
            or any(part in {"", ".", ".."} for part in Path(relative).parts)
        ):
            raise VerificationError(f"generation file path is invalid: {generation_id}")
        if not isinstance(expected_sha, str) or not SHA256.fullmatch(expected_sha):
            raise VerificationError(f"generation checksum is invalid: {relative}")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise VerificationError(f"generation size is invalid: {relative}")
        seen.add(relative)
        candidate = path / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(resolved_generation)
        except (OSError, ValueError) as exc:
            raise VerificationError(f"generation file escapes root: {relative}") from exc
        if not resolved.is_file() or resolved.is_symlink():
            raise VerificationError(f"generation file is unsafe: {relative}")
        actual_sha, actual_size = hash_file(resolved)
        if actual_sha != expected_sha or actual_size != expected_size:
            raise VerificationError(f"generation checksum mismatch: {relative}")
        total_bytes += actual_size
    actual_files = {
        candidate.relative_to(path).as_posix()
        for candidate in path.rglob("*")
        if candidate.is_file()
    }
    expected_files = seen | {"manifest.json", "READY"}
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        detail = unexpected[0] if unexpected else missing[0]
        raise VerificationError(f"generation file inventory differs: {detail}")
    expected_directories = {
        parent.as_posix()
        for relative in seen
        for parent in Path(relative).parents
        if parent.as_posix() != "."
    }
    actual_directories = {
        candidate.relative_to(path).as_posix()
        for candidate in path.rglob("*")
        if candidate.is_dir()
    }
    if actual_directories != expected_directories:
        unexpected = sorted(actual_directories - expected_directories)
        missing = sorted(expected_directories - actual_directories)
        detail = unexpected[0] if unexpected else missing[0]
        raise VerificationError(f"generation directory inventory differs: {detail}")
    return generation_id, manifest_sha256, total_bytes


def write_report_atomic(path: Path, encoded: str) -> None:
    """Publish a report without following or truncating a predictable path."""

    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise VerificationError(f"report parent must be a non-symlink directory: {parent}")
    parent_metadata = parent.stat()
    if parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) & 0o022:
        raise VerificationError(
            "report parent must be owned by the verifier UID and not writable by group/other"
        )
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise VerificationError(f"report output already exists: {path}")

    temporary = parent / f".{path.name}.tmp.{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o640)
        payload = encoded.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        # The reports directory is required to be root-owned and not writable
        # by group/other, so the prior non-existence check remains stable.
        os.replace(temporary, path)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise VerificationError(f"cannot publish report safely: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def verify_generations(root: Path, *, allow_migration_markers: bool = False) -> dict[str, Any]:
    reject_unsafe_entries(root)
    allowed = {"generations", "current.json", "publication-history.jsonl", ".publish.lock"}
    if allow_migration_markers:
        allowed |= {".cardrag-storage-migration-owner", ".cardrag-storage-migration-commit"}
    unexpected = sorted(path.name for path in root.iterdir() if path.name not in allowed)
    if unexpected:
        raise VerificationError(f"unexpected generation-store root entry: {unexpected[0]}")
    generations_root = root / "generations"
    if not generations_root.exists():
        generation_paths: list[Path] = []
    elif generations_root.is_symlink() or not generations_root.is_dir():
        raise VerificationError("generation collection root is unsafe")
    else:
        collection_entries = sorted(generations_root.iterdir())
        unsafe_entry = next((path for path in collection_entries if not path.is_dir()), None)
        if unsafe_entry is not None:
            raise VerificationError(
                f"non-directory in generation collection: {unsafe_entry.name}"
            )
        generation_paths = collection_entries

    manifests: dict[str, str] = {}
    for path in generation_paths:
        generation_id, manifest_sha, _ = verify_generation(path)
        manifests[generation_id] = manifest_sha

    current_path = root / "current.json"
    current_generation_id: str | None = None
    if current_path.exists():
        current = load_json(current_path)
        current_generation_id = current.get("generation_id")
        current_manifest_sha = current.get("manifest_sha256")
        if not isinstance(current_generation_id, str) or current_generation_id not in manifests:
            raise VerificationError("current pointer references a missing generation")
        if manifests[current_generation_id] != current_manifest_sha:
            raise VerificationError("current pointer manifest hash mismatch")
        previous_generation_id = current.get("previous_generation_id")
        if previous_generation_id is not None and (
            not isinstance(previous_generation_id, str)
            or previous_generation_id not in manifests
        ):
            raise VerificationError("current pointer references a missing previous generation")

    inventory = hashlib.sha256()
    for generation_id, manifest_sha in sorted(manifests.items()):
        inventory.update(f"{generation_id}\0{manifest_sha}\n".encode())
    root_inventory = hashlib.sha256()
    root_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_dir() or path.name in {
            ".publish.lock",
            ".cardrag-storage-migration-owner",
            ".cardrag-storage-migration-commit",
        }:
            continue
        relative = path.relative_to(root).as_posix()
        digest, size = hash_file(path)
        root_inventory.update(f"{relative}\0{digest}\0{size}\n".encode())
        root_bytes += size
    return {
        "count": len(manifests),
        "bytes": root_bytes,
        "inventory_sha256": inventory.hexdigest(),
        "root_inventory_sha256": root_inventory.hexdigest(),
        "current_generation_id": current_generation_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--objects-root", type=Path, required=True)
    parser.add_argument("--generations-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-migration-markers", action="store_true")
    args = parser.parse_args()

    try:
        report = {
            "schema_version": "cardrag-runtime-storage-verification.v1",
            "objects": verify_objects(
                args.objects_root, allow_migration_markers=args.allow_migration_markers
            ),
            "generations": verify_generations(
                args.generations_root, allow_migration_markers=args.allow_migration_markers
            ),
        }
    except VerificationError as exc:
        print(f"storage verification failed: {exc}", file=sys.stderr)
        return 1

    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        if args.output:
            write_report_atomic(args.output, encoded)
        else:
            sys.stdout.write(encoded)
    except VerificationError as exc:
        print(f"storage verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
