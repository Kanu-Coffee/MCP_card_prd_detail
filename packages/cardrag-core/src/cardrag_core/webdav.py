"""Minimal synchronous RFC 4918 client with explicit Basic authentication."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .domain import VerifiedArtifact
from .paths import validate_relative_path
from .settings import WebDAVSettings

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_PROPFIND_BODY = b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'


class WebDAVError(RuntimeError):
    """Base error for a WebDAV operation."""


class WebDAVHTTPError(WebDAVError):
    """A server returned a status outside the operation's RFC contract."""

    def __init__(self, method: str, path: PurePosixPath, status_code: int) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        super().__init__(f"WebDAV {method} {path.as_posix()} returned HTTP {status_code}")


class WebDAVIntegrityError(WebDAVError):
    """Downloaded bytes differ from their trusted expected identity."""


@dataclass(frozen=True, slots=True)
class WebDAVResponse:
    path: PurePosixPath
    status_code: int
    headers: Mapping[str, str]
    content: bytes


@dataclass(frozen=True, slots=True)
class WebDAVObjectStat:
    path: PurePosixPath
    status_code: int
    size_bytes: int | None
    etag: str | None
    last_modified: str | None


class WebDAVClient:
    """Small RFC 4918 client. It follows no redirects and logs no credentials."""

    def __init__(
        self,
        settings: WebDAVSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(
            auth=httpx.BasicAuth(settings.username, settings.password.get_secret_value()),
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.transfer_timeout_seconds,
                write=settings.transfer_timeout_seconds,
                pool=settings.transfer_timeout_seconds,
            ),
            verify=settings.httpx_verify,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "cardrag-core/1.0"},
        )

    def __enter__(self) -> WebDAVClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def read_only(self) -> ReadOnlyWebDAVClient:
        return ReadOnlyWebDAVClient(self)

    def _url(self, path: str | PurePosixPath) -> str:
        relative = validate_relative_path(path)
        base = urlsplit(self.settings.base_url)
        encoded_relative = "/".join(quote(segment, safe="-._~") for segment in relative.parts)
        combined_path = f"{base.path.rstrip('/')}/{encoded_relative}"
        return urlunsplit((base.scheme, base.netloc, combined_path, "", ""))

    def _request(
        self,
        method: str,
        path: str | PurePosixPath,
        *,
        expected_statuses: set[int],
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | None = None,
    ) -> httpx.Response:
        relative = validate_relative_path(path)
        try:
            response = self._client.request(
                method,
                self._url(relative),
                headers=dict(headers or {}),
                content=content,
            )
        except httpx.HTTPError as exc:
            raise WebDAVError(f"WebDAV {method} {relative.as_posix()} failed") from exc
        if response.status_code not in expected_statuses:
            response.close()
            raise WebDAVHTTPError(method, relative, response.status_code)
        return response

    @staticmethod
    def _headers(response: httpx.Response) -> Mapping[str, str]:
        return MappingProxyType(dict(response.headers.items()))

    def head(self, path: str | PurePosixPath) -> WebDAVObjectStat:
        relative = validate_relative_path(path)
        response = self._request("HEAD", relative, expected_statuses={200, 204})
        raw_size = response.headers.get("content-length")
        try:
            size = int(raw_size) if raw_size is not None else None
        except ValueError:
            size = None
        return WebDAVObjectStat(
            path=relative,
            status_code=response.status_code,
            size_bytes=size,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    def exists(self, path: str | PurePosixPath) -> bool:
        try:
            self.head(path)
        except WebDAVHTTPError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def get(self, path: str | PurePosixPath, *, max_bytes: int | None = None) -> WebDAVResponse:
        relative = validate_relative_path(path)
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes cannot be negative")
        try:
            with self._client.stream("GET", self._url(relative)) as response:
                if response.status_code != 200:
                    raise WebDAVHTTPError("GET", relative, response.status_code)
                raw_length = response.headers.get("content-length")
                if (
                    max_bytes is not None
                    and raw_length is not None
                    and raw_length.isdigit()
                    and int(raw_length) > max_bytes
                ):
                    raise WebDAVIntegrityError(f"WebDAV object {relative.as_posix()} exceeds the read limit")
                body = bytearray()
                for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_SIZE):
                    body.extend(chunk)
                    if max_bytes is not None and len(body) > max_bytes:
                        raise WebDAVIntegrityError(
                            f"WebDAV object {relative.as_posix()} exceeds the read limit"
                        )
                return WebDAVResponse(
                    relative,
                    response.status_code,
                    self._headers(response),
                    bytes(body),
                )
        except httpx.HTTPError as exc:
            raise WebDAVError(f"WebDAV GET {relative.as_posix()} failed") from exc

    def propfind(
        self,
        path: str | PurePosixPath,
        *,
        depth: Literal["0", "1", "infinity"] = "0",
        body: bytes = _PROPFIND_BODY,
    ) -> WebDAVResponse:
        relative = validate_relative_path(path)
        response = self._request(
            "PROPFIND",
            relative,
            expected_statuses={207},
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            content=body,
        )
        return WebDAVResponse(relative, response.status_code, self._headers(response), response.content)

    def mkcol(self, path: str | PurePosixPath, *, exist_ok: bool = False) -> WebDAVObjectStat:
        relative = validate_relative_path(path)
        expected = {201, 405} if exist_ok else {201}
        response = self._request("MKCOL", relative, expected_statuses=expected)
        return WebDAVObjectStat(relative, response.status_code, 0, None, None)

    def ensure_collection(self, path: str | PurePosixPath) -> None:
        relative = validate_relative_path(path)
        current = PurePosixPath()
        for segment in relative.parts:
            current /= segment
            self.mkcol(current, exist_ok=True)

    def put(
        self,
        path: str | PurePosixPath,
        content: bytes | Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
        if_none_match: bool = False,
    ) -> WebDAVObjectStat:
        relative = validate_relative_path(path)
        headers = {"Content-Type": content_type}
        if if_none_match:
            headers["If-None-Match"] = "*"
        response = self._request(
            "PUT",
            relative,
            expected_statuses={200, 201, 204},
            headers=headers,
            content=content,
        )
        raw_size = response.headers.get("content-length")
        size = int(raw_size) if raw_size and raw_size.isdigit() else None
        return WebDAVObjectStat(
            relative,
            response.status_code,
            size,
            response.headers.get("etag"),
            response.headers.get("last-modified"),
        )

    def move(
        self,
        source: str | PurePosixPath,
        destination: str | PurePosixPath,
        *,
        overwrite: bool = False,
    ) -> WebDAVObjectStat:
        source_path = validate_relative_path(source)
        destination_path = validate_relative_path(destination)
        response = self._request(
            "MOVE",
            source_path,
            expected_statuses={201, 204},
            headers={
                "Destination": self._url(destination_path),
                "Overwrite": "T" if overwrite else "F",
            },
        )
        return WebDAVObjectStat(destination_path, response.status_code, None, None, None)

    def delete(self, path: str | PurePosixPath, *, missing_ok: bool = False) -> None:
        relative = validate_relative_path(path)
        expected = {200, 202, 204, 404} if missing_ok else {200, 202, 204}
        self._request("DELETE", relative, expected_statuses=expected).close()

    def download(
        self,
        path: str | PurePosixPath,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        max_bytes: int | None = None,
    ) -> VerifiedArtifact:
        """Stream into an atomic local replacement and verify trusted identity."""

        relative = validate_relative_path(path)
        if expected_size_bytes is not None and expected_size_bytes < 0:
            raise ValueError("expected_size_bytes cannot be negative")
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes cannot be negative")
        caps = [value for value in (expected_size_bytes, max_bytes) if value is not None]
        hard_cap = min(caps) if caps else None
        target = Path(destination)
        if not target.is_absolute():
            raise ValueError("download destination must be an absolute path")
        if target.is_symlink():
            raise ValueError("download destination must not be a symlink")
        parent = target.parent.resolve(strict=True)
        if parent.is_symlink():
            raise ValueError("download destination parent must not be a symlink")
        descriptor, temporary_name = tempfile.mkstemp(dir=parent, prefix=f".{target.name}.", suffix=".tmp")
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            try:
                with self._client.stream("GET", self._url(relative)) as response:
                    if response.status_code != 200:
                        raise WebDAVHTTPError("GET", relative, response.status_code)
                    raw_length = response.headers.get("content-length")
                    if (
                        hard_cap is not None
                        and raw_length is not None
                        and raw_length.isdigit()
                        and int(raw_length) > hard_cap
                    ):
                        raise WebDAVIntegrityError("downloaded object exceeds the allowed byte size")
                    with os.fdopen(descriptor, "wb") as output:
                        descriptor = -1
                        for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_SIZE):
                            size_bytes += len(chunk)
                            if hard_cap is not None and size_bytes > hard_cap:
                                raise WebDAVIntegrityError("downloaded object exceeds the allowed byte size")
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
            except httpx.HTTPError as exc:
                raise WebDAVError(f"WebDAV GET {relative.as_posix()} failed") from exc
            actual_sha256 = digest.hexdigest()
            if expected_size_bytes is not None and size_bytes != expected_size_bytes:
                raise WebDAVIntegrityError("downloaded byte size does not match the manifest")
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise WebDAVIntegrityError("downloaded SHA-256 does not match the manifest")
            os.chmod(temporary, 0o440)
            os.replace(temporary, target)
            return VerifiedArtifact(relative, actual_sha256, size_bytes)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


class ReadOnlyWebDAVClient:
    """Capability-limited wrapper: no PUT, MOVE, MKCOL, or DELETE methods."""

    __slots__ = ("__client",)

    def __init__(self, client: WebDAVClient) -> None:
        self.__client = client

    def __enter__(self) -> ReadOnlyWebDAVClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.__client.close()

    def head(self, path: str | PurePosixPath) -> WebDAVObjectStat:
        return self.__client.head(path)

    def exists(self, path: str | PurePosixPath) -> bool:
        return self.__client.exists(path)

    def get(self, path: str | PurePosixPath, *, max_bytes: int | None = None) -> WebDAVResponse:
        return self.__client.get(path, max_bytes=max_bytes)

    def propfind(
        self,
        path: str | PurePosixPath,
        *,
        depth: Literal["0", "1", "infinity"] = "0",
        body: bytes = _PROPFIND_BODY,
    ) -> WebDAVResponse:
        return self.__client.propfind(path, depth=depth, body=body)

    def download(
        self,
        path: str | PurePosixPath,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        max_bytes: int | None = None,
    ) -> VerifiedArtifact:
        return self.__client.download(
            path,
            destination,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
            max_bytes=max_bytes,
        )
