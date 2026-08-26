"""The only component allowed to turn issuer download requests into PDF bytes."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
import pypdfium2 as pdfium  # type: ignore[import-untyped]

from .contracts import DownloadRequest


class DownloadSecurityError(RuntimeError):
    pass


class PDFValidationError(RuntimeError):
    pass


class ProtectedDocumentError(PDFValidationError):
    """The issuer served a recognized DRM container instead of PDF bytes."""

    def __init__(
        self,
        *,
        magic: Literal["SCDSA002", "SCDSA004", "FASOO_DRMONE"],
        sha256: str,
        size_bytes: int,
    ) -> None:
        super().__init__("response is a recognized protected document container")
        self.magic = magic
        self.sha256 = sha256
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class DownloadPolicy:
    allowed_hosts: frozenset[str]
    maximum_bytes: int = 100 * 1024 * 1024
    timeout_seconds: float = 30.0
    maximum_redirects: int = 5
    allow_private_networks_for_tests: bool = False


@dataclass(frozen=True, slots=True)
class DownloadedPDF:
    path: Path
    sha256: str
    size_bytes: int
    page_count: int
    final_url: str


def _resolve(host: str) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DownloadSecurityError("issuer host DNS resolution failed") from exc
    return tuple(sorted({str(answer[4][0]) for answer in answers}))


def validate_url(
    value: str,
    policy: DownloadPolicy,
    *,
    resolver: Callable[[str], Iterable[str]] = _resolve,
) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise DownloadSecurityError("only credential-free issuer HTTPS URLs on port 443 are allowed")
    if parsed.hostname.casefold() not in policy.allowed_hosts:
        raise DownloadSecurityError("download host is not allowlisted")
    if policy.allow_private_networks_for_tests:
        return
    addresses = tuple(resolver(parsed.hostname))
    if not addresses:
        raise DownloadSecurityError("issuer host did not resolve")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise DownloadSecurityError("issuer DNS resolver returned an invalid address") from exc
        if not ip.is_global:
            raise DownloadSecurityError("issuer host resolved to a non-public address")


def validate_pdf(path: Path, *, expected_sha256: str | None = None) -> tuple[str, int, int]:
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as source:
        signature = source.read(8)
        source.seek(0)
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    sha256 = digest.hexdigest()
    protected_signatures: dict[
        bytes,
        Literal["SCDSA002", "SCDSA004", "FASOO_DRMONE"],
    ] = {
        b"SCDSA002": "SCDSA002",
        b"SCDSA004": "SCDSA004",
        b"\x9b DRMONE": "FASOO_DRMONE",
    }
    protected_magic = protected_signatures.get(signature)
    if protected_magic is not None:
        raise ProtectedDocumentError(
            magic=protected_magic,
            sha256=sha256,
            size_bytes=size,
        )
    if not signature.startswith(b"%PDF-"):
        raise PDFValidationError("response does not start with a PDF signature")
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise PDFValidationError("downloaded PDF hash differs from the expected hash")
    try:
        document = pdfium.PdfDocument(str(path))
        try:
            count = len(document)
            if count <= 0:
                raise PDFValidationError("PDF contains no pages")
            # Load every page to reject a file whose trailer opens but page tree is corrupt.
            for index in range(count):
                page = document[index]
                page.close()
        finally:
            document.close()
    except PDFValidationError:
        raise
    except Exception as exc:
        raise PDFValidationError("PDF structure cannot be opened completely") from exc
    return sha256, size, count


class SecurePDFDownloader:
    def __init__(
        self,
        policy: DownloadPolicy,
        *,
        resolver: Callable[[str], Iterable[str]] = _resolve,
    ) -> None:
        self.policy = policy
        self.resolver = resolver

    async def download(
        self,
        client: httpx.AsyncClient,
        request: DownloadRequest,
        destination: Path,
        *,
        expected_sha256: str | None = None,
    ) -> DownloadedPDF:
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise DownloadSecurityError("download destination must be a regular non-symlink file")
        if destination.parent.is_symlink() or (
            destination.parent.exists() and not destination.parent.is_dir()
        ):
            raise DownloadSecurityError("download destination parent must be a non-symlink directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        current_url = request.url
        current_method = request.method
        current_form = dict(request.form) if request.form is not None else None
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.casefold() not in {"authorization", "cookie", "host", "proxy-authorization"}
        }
        temporary: Path | None = None
        final_url: str | None = None
        try:
            with NamedTemporaryFile(
                mode="wb", prefix=".partial-", suffix=".pdf", dir=destination.parent, delete=False
            ) as output:
                temporary = Path(output.name)
                for redirect_index in range(self.policy.maximum_redirects + 1):
                    # This check happens before every network connection, including redirects.
                    validate_url(current_url, self.policy, resolver=self.resolver)
                    async with client.stream(
                        current_method,
                        current_url,
                        data=current_form,
                        headers=headers,
                        follow_redirects=False,
                        timeout=self.policy.timeout_seconds,
                    ) as response:
                        if response.is_redirect:
                            if redirect_index >= self.policy.maximum_redirects:
                                raise DownloadSecurityError("download exceeded the redirect limit")
                            location = response.headers.get("location")
                            if not location:
                                raise DownloadSecurityError("redirect response has no location")
                            current_url = urljoin(str(response.url), location)
                            if response.status_code == 303 or (
                                response.status_code in {301, 302} and current_method == "POST"
                            ):
                                current_method = "GET"
                                current_form = None
                            continue
                        response.raise_for_status()
                        media_type = (
                            response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                        )
                        if media_type and media_type not in {"application/pdf", "application/octet-stream"}:
                            raise PDFValidationError("response MIME type is not PDF-compatible")
                        raw_length = response.headers.get("content-length")
                        try:
                            length = int(raw_length) if raw_length is not None else None
                        except ValueError as exc:
                            raise PDFValidationError("response has an invalid content length") from exc
                        if length is not None and (length < 0 or length > self.policy.maximum_bytes):
                            raise PDFValidationError("PDF exceeds configured maximum size")
                        written = 0
                        async for block in response.aiter_bytes():
                            written += len(block)
                            if written > self.policy.maximum_bytes:
                                raise PDFValidationError("streamed PDF exceeds configured maximum size")
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
                        final_url = str(response.url)
                        break
            if final_url is None or temporary is None:
                raise DownloadSecurityError("download did not reach a final response")
            digest, size, pages = validate_pdf(temporary, expected_sha256=expected_sha256)
            if destination.exists():
                current_digest, _, _ = validate_pdf(destination)
                if current_digest != digest:
                    raise FileExistsError("immutable destination contains different PDF bytes")
                temporary.unlink()
            else:
                temporary.replace(destination)
                descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            temporary = None
            return DownloadedPDF(destination, digest, size, pages, final_url)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
