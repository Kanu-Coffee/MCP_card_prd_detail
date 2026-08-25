"""Immutable, read-back-verified WebDAV publication helpers."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import canonical_json_bytes, sha256_bytes, sha256_file
from .domain import ArtifactRef, VerifiedArtifact
from .paths import STABLE_POINTER_PATH, object_path, validate_relative_path
from .webdav import WebDAVClient, WebDAVHTTPError

_UPLOAD_CHUNK_SIZE = 1024 * 1024


class ImmutablePublisher:
    """Publish to a create-once path through temp PUT, readback, and MOVE."""

    def __init__(self, client: WebDAVClient) -> None:
        self._client = client

    def _verify_remote(self, path: PurePosixPath, *, digest: str, size_bytes: int) -> VerifiedArtifact:
        with tempfile.TemporaryDirectory(prefix="cardrag-webdav-verify-") as directory:
            target = Path(directory).resolve() / "artifact"
            return self._client.download(
                path,
                target,
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
        finally:
            self._client.delete(temporary, missing_ok=True)
        return ArtifactRef(
            sha256=digest,
            size_bytes=size_bytes,
            media_type=media_type,
            path=destination.as_posix(),
        )

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
    ) -> ArtifactRef:
        relative = validate_relative_path(destination)
        source_path = Path(source)
        digest, size_bytes = sha256_file(source_path)

        def chunks() -> Iterable[bytes]:
            with source_path.open("rb") as handle:
                while chunk := handle.read(_UPLOAD_CHUNK_SIZE):
                    yield chunk

        return self._publish(
            relative,
            digest=digest,
            size_bytes=size_bytes,
            media_type=media_type,
            content_factory=chunks,
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
    ) -> ArtifactRef:
        digest, _ = sha256_file(source)
        return self._publisher.publish_file(object_path(digest), source, media_type=media_type)


class StablePointerPublisher:
    """Atomically replace only ``v1/channels/stable.json`` after readback."""

    def __init__(self, client: WebDAVClient) -> None:
        self._client = client
        self._verifier = ImmutablePublisher(client)

    def atomic_replace_bytes(
        self,
        payload: bytes,
        *,
        media_type: str = "application/json",
    ) -> ArtifactRef:
        digest = sha256_bytes(payload)
        size_bytes = len(payload)
        destination = STABLE_POINTER_PATH
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
        finally:
            self._client.delete(temporary, missing_ok=True)
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
