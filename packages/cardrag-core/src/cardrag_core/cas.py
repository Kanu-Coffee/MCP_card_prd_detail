"""Immutable, read-back-verified WebDAV publication helpers."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import canonical_json_bytes, sha256_bytes
from .domain import ArtifactRef, VerifiedArtifact
from .paths import channel_pointer_path, object_path, validate_relative_path, validate_sha256
from .webdav import WebDAVClient, WebDAVHTTPError, WebDAVIntegrityError

_UPLOAD_CHUNK_SIZE = 1024 * 1024
LOGGER = logging.getLogger(__name__)

_FileIdentity = tuple[int, int, int, int, int]


def _file_identity(metadata: os.stat_result) -> _FileIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_expected_size(value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("expected publication size must be a non-negative integer")
    return value


class ImmutablePublisher:
    """Publish to a create-once path through temp PUT, readback, and MOVE."""

    def __init__(self, client: WebDAVClient) -> None:
        self._client = client

    def _verify_remote(self, path: PurePosixPath, *, digest: str, size_bytes: int) -> VerifiedArtifact:
        return self._client.verify(
            path,
            expected_sha256=digest,
            expected_size_bytes=size_bytes,
        )

    def _publish(
        self,
        destination: PurePosixPath,
        *,
        digest: str,
        size_bytes: int,
        media_type: str,
        content_factory: Callable[[], bytes | Iterable[bytes]],
        pre_commit: Callable[[], None] | None = None,
    ) -> ArtifactRef:
        destination = validate_relative_path(destination)
        if self._client.exists(destination):
            self._verify_remote(destination, digest=digest, size_bytes=size_bytes)
            return ArtifactRef(
                sha256=digest,
                size_bytes=size_bytes,
                media_type=media_type,
                path=destination.as_posix(),
            )

        self._client.ensure_collection(destination.parent)
        temporary = PurePosixPath("v1", ".incoming", "publish", f"{uuid.uuid4().hex}.tmp")
        self._client.ensure_collection(temporary.parent)
        try:
            self._client.put(
                temporary,
                content_factory(),
                content_type=media_type,
                if_none_match=True,
            )
            self._verify_remote(temporary, digest=digest, size_bytes=size_bytes)
            if pre_commit is not None:
                pre_commit()
            try:
                self._client.move(temporary, destination, overwrite=False)
            except WebDAVHTTPError as exc:
                # RFC 4918 specifies 412 for an Overwrite:F destination
                # collision, while some otherwise-compatible servers report
                # 409.  Treat either as a race only after proving that the
                # immutable destination now exists; its bytes are verified
                # immediately below.
                if exc.status_code not in {409, 412} or not self._client.exists(destination):
                    raise
            self._verify_remote(destination, digest=digest, size_bytes=size_bytes)
        except BaseException:
            # Cleanup is best effort after a publication failure.  A second
            # WebDAV error while deleting the temporary object must not erase
            # the phase/status of the error that actually stopped publication.
            with suppress(Exception):
                self._client.delete(temporary, missing_ok=True)
            raise
        else:
            try:
                self._client.delete(temporary, missing_ok=True)
            except Exception:
                # MOVE plus immutable destination readback is the commit point.
                # Temp cleanup failure is observable but cannot negate it.
                LOGGER.warning("Immutable publication temporary cleanup failed after verified commit")
        return ArtifactRef(
            sha256=digest,
            size_bytes=size_bytes,
            media_type=media_type,
            path=destination.as_posix(),
        )

    def _publish_file(
        self,
        source: str | Path,
        *,
        destination_factory: Callable[[str], str | PurePosixPath],
        media_type: str,
        expected_sha256: str | None,
        expected_size_bytes: int | None,
    ) -> ArtifactRef:
        """Hash and upload one regular file through the same no-follow descriptor."""

        trusted_digest = validate_sha256(expected_sha256) if expected_sha256 is not None else None
        trusted_size = _validate_expected_size(expected_size_bytes)
        source_path = Path(os.path.abspath(os.fspath(source)))
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise WebDAVIntegrityError("secure publication source opening is unavailable")
        flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(source_path, flags)
        except OSError:
            raise WebDAVIntegrityError("publication source is missing or unsafe") from None

        try:
            try:
                initial_metadata = os.fstat(descriptor)
            except OSError:
                raise WebDAVIntegrityError("publication source identity could not be verified") from None
            if not stat.S_ISREG(initial_metadata.st_mode):
                raise WebDAVIntegrityError("publication source must be a regular file")
            initial_identity = _file_identity(initial_metadata)

            digest_builder = hashlib.sha256()
            hashed_size = 0
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                while chunk := os.read(descriptor, _UPLOAD_CHUNK_SIZE):
                    digest_builder.update(chunk)
                    hashed_size += len(chunk)
                os.lseek(descriptor, 0, os.SEEK_SET)
            except OSError:
                raise WebDAVIntegrityError("publication source could not be read safely") from None
            digest = digest_builder.hexdigest()

            def verify_source_identity() -> None:
                try:
                    descriptor_metadata = os.fstat(descriptor)
                    path_metadata = os.stat(source_path, follow_symlinks=False)
                except OSError:
                    raise WebDAVIntegrityError("publication source identity changed") from None
                if (
                    not stat.S_ISREG(descriptor_metadata.st_mode)
                    or not stat.S_ISREG(path_metadata.st_mode)
                    or _file_identity(descriptor_metadata) != initial_identity
                    or _file_identity(path_metadata) != initial_identity
                ):
                    raise WebDAVIntegrityError("publication source identity changed")

            verify_source_identity()
            if hashed_size != initial_metadata.st_size:
                raise WebDAVIntegrityError("publication source size changed while hashing")
            if trusted_digest is not None and digest != trusted_digest:
                raise WebDAVIntegrityError("publication source SHA-256 differs from its sealed identity")
            if trusted_size is not None and hashed_size != trusted_size:
                raise WebDAVIntegrityError("publication source size differs from its sealed identity")

            destination = validate_relative_path(destination_factory(digest))

            def chunks() -> Iterable[bytes]:
                verify_source_identity()
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    while chunk := os.read(descriptor, _UPLOAD_CHUNK_SIZE):
                        yield chunk
                except OSError:
                    raise WebDAVIntegrityError("publication source could not be read safely") from None

            reference = self._publish(
                destination,
                digest=digest,
                size_bytes=hashed_size,
                media_type=media_type,
                content_factory=chunks,
                pre_commit=verify_source_identity,
            )
            verify_source_identity()
            return reference
        finally:
            os.close(descriptor)

    def publish_bytes(
        self,
        destination: str | PurePosixPath,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        relative = validate_relative_path(destination)
        digest = sha256_bytes(payload)
        return self._publish(
            relative,
            digest=digest,
            size_bytes=len(payload),
            media_type=media_type,
            content_factory=lambda: payload,
        )

    def publish_file(
        self,
        destination: str | PurePosixPath,
        source: str | Path,
        *,
        media_type: str = "application/octet-stream",
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
    ) -> ArtifactRef:
        relative = validate_relative_path(destination)
        return self._publish_file(
            source,
            destination_factory=lambda _digest: relative,
            media_type=media_type,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )


class CASPublisher:
    """Publish bytes under their fixed ``v1/objects/sha256`` identity."""

    def __init__(self, client: WebDAVClient) -> None:
        self._publisher = ImmutablePublisher(client)

    def publish_bytes(
        self,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        digest = sha256_bytes(payload)
        return self._publisher.publish_bytes(object_path(digest), payload, media_type=media_type)

    def publish_file(
        self,
        source: str | Path,
        *,
        media_type: str = "application/octet-stream",
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
    ) -> ArtifactRef:
        return self._publisher._publish_file(
            source,
            destination_factory=object_path,
            media_type=media_type,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )


class StablePointerPublisher:
    """Atomically replace one validated channel pointer after readback."""

    def __init__(self, client: WebDAVClient, *, channel: str = "stable") -> None:
        self._client = client
        self._verifier = ImmutablePublisher(client)
        self._destination = channel_pointer_path(channel)

    @property
    def destination(self) -> PurePosixPath:
        return self._destination

    def atomic_replace_bytes(
        self,
        payload: bytes,
        *,
        media_type: str = "application/json",
    ) -> ArtifactRef:
        digest = sha256_bytes(payload)
        size_bytes = len(payload)
        destination = self._destination
        temporary = PurePosixPath("v1", ".incoming", "channels", f"{uuid.uuid4().hex}.tmp")
        self._client.ensure_collection(destination.parent)
        self._client.ensure_collection(temporary.parent)
        try:
            self._client.put(
                temporary,
                payload,
                content_type=media_type,
                if_none_match=True,
            )
            self._verifier._verify_remote(temporary, digest=digest, size_bytes=size_bytes)
            self._client.move(temporary, destination, overwrite=True)
            self._verifier._verify_remote(destination, digest=digest, size_bytes=size_bytes)
        except BaseException:
            with suppress(Exception):
                self._client.delete(temporary, missing_ok=True)
            raise
        else:
            try:
                self._client.delete(temporary, missing_ok=True)
            except Exception:
                # MOVE plus destination readback is the atomic commit point.
                # A failed best-effort temp cleanup may leave an orphan for GC,
                # but must not report the already verified pointer as failed.
                LOGGER.warning("Stable pointer temporary cleanup failed after verified replacement")
        return ArtifactRef(
            sha256=digest,
            size_bytes=size_bytes,
            media_type=media_type,
            path=destination.as_posix(),
        )

    def atomic_replace_json(self, value: Any) -> ArtifactRef:
        return self.atomic_replace_bytes(canonical_json_bytes(value), media_type="application/json")


def atomic_replace_bytes(
    client: WebDAVClient,
    payload: bytes,
    *,
    media_type: str = "application/json",
) -> ArtifactRef:
    """Convenience wrapper for the stable pointer's atomic byte replacement."""

    return StablePointerPublisher(client).atomic_replace_bytes(payload, media_type=media_type)


def atomic_replace_json(client: WebDAVClient, value: Any) -> ArtifactRef:
    """Canonicalize JSON and atomically replace the stable pointer."""

    return StablePointerPublisher(client).atomic_replace_json(value)
