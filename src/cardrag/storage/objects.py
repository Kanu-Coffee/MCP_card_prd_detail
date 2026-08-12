"""Content-addressed immutable object storage on an external filesystem."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from cardrag.domain import ArtifactManifest

from .paths import (
    UnsafePathError,
    atomic_write_within_root,
    portable_relative_path,
    resolve_within_root,
)

_SHA256_LENGTH = 64
_COPY_CHUNK_SIZE = 1024 * 1024


class ObjectIntegrityError(RuntimeError):
    """Raised when stored bytes do not match their content-addressed name."""


def _validate_sha256(digest: str) -> str:
    if len(digest) != _SHA256_LENGTH or digest.lower() != digest:
        raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters")
    try:
        bytes.fromhex(digest)
    except ValueError as exc:
        raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters") from exc
    return digest


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class StoredObject:
    sha256: str
    size_bytes: int
    relative_path: PurePosixPath


class ContentAddressedObjectStore:
    """An immutable ``sha256/<prefix>/<digest>`` object store."""

    def __init__(self, root: str | Path) -> None:
        root_path = Path(root)
        if not root_path.is_absolute():
            raise UnsafePathError("object store root must be an explicit absolute path")
        root_path.mkdir(parents=True, exist_ok=True)
        self._root = root_path.resolve(strict=True)
        self._incoming = resolve_within_root(self._root, ".incoming")
        self._incoming.mkdir(parents=True, exist_ok=True)
        if self._incoming.is_symlink():
            raise UnsafePathError("object store incoming directory must not be a symlink")

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def relative_path_for(digest: str) -> PurePosixPath:
        valid_digest = _validate_sha256(digest)
        return portable_relative_path(f"sha256/{valid_digest[:2]}/{valid_digest}")

    def path_for(self, digest: str) -> Path:
        path = resolve_within_root(self._root, self.relative_path_for(digest))
        if path.is_symlink():
            raise ObjectIntegrityError("content-addressed objects must not be symlinks")
        return path

    def put_bytes(self, payload: bytes | bytearray | memoryview) -> StoredObject:
        return self.put_stream((bytes(payload),))

    def put_file(self, source: str | Path) -> StoredObject:
        source_path = Path(source)
        if source_path.is_symlink() or not source_path.is_file():
            raise ValueError("object source must be a regular, non-symlink file")
        with source_path.open("rb") as handle:
            return self.put_stream(iter(lambda: handle.read(_COPY_CHUNK_SIZE), b""))

    def put_stream(self, chunks: Iterable[bytes]) -> StoredObject:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._incoming,
            prefix=".object.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("object stream chunks must be bytes")
                    handle.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o444)

            hex_digest = digest.hexdigest()
            relative = self.relative_path_for(hex_digest)
            target = resolve_within_root(self._root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target = resolve_within_root(self._root, relative)
            if target.is_symlink():
                raise ObjectIntegrityError("content-addressed objects must not be symlinks")
            try:
                os.link(temporary, target)
                _fsync_directory(target.parent)
            except FileExistsError:
                self._verify_path(target, hex_digest, size_bytes)
            finally:
                temporary.unlink(missing_ok=True)
            return StoredObject(hex_digest, size_bytes, relative)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify_path(self, path: Path, digest: str, expected_size: int | None = None) -> int:
        if path.is_symlink():
            raise ObjectIntegrityError("content-addressed objects must not be symlinks")
        try:
            metadata = path.stat()
        except FileNotFoundError as exc:
            raise ObjectIntegrityError(f"object is missing: {digest}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ObjectIntegrityError("content-addressed object must be a regular file")
        if expected_size is not None and metadata.st_size != expected_size:
            raise ObjectIntegrityError("existing object has an unexpected size")

        actual = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_COPY_CHUNK_SIZE), b""):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            raise ObjectIntegrityError("existing object content does not match its address")
        return metadata.st_size

    def verify(self, digest: str) -> StoredObject:
        valid_digest = _validate_sha256(digest)
        relative = self.relative_path_for(valid_digest)
        path = self.path_for(valid_digest)
        size_bytes = self._verify_path(path, valid_digest)
        return StoredObject(valid_digest, size_bytes, relative)

    def read_bytes(self, digest: str) -> bytes:
        verified = self.verify(digest)
        return resolve_within_root(self._root, verified.relative_path).read_bytes()


def write_artifact_manifest(
    root: str | Path,
    relative: str | PurePosixPath,
    manifest: ArtifactManifest,
    *,
    overwrite: bool = False,
) -> Path:
    """Write the canonical manifest bytes without exposing arbitrary paths."""

    return atomic_write_within_root(
        root,
        relative,
        manifest.canonical_bytes(),
        overwrite=overwrite,
        mode=0o444,
    )
