"""Bounded, allowlisted and structurally validated PDF acquisition."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin, urlparse

import httpx

from cardrag.issuers.base import SourceRecord
from cardrag.pdf import PDFSecurityError, PDFStructureError, open_pdf


class PDFValidationError(RuntimeError):
    pass


class DownloadSecurityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadPolicy:
    allowed_hosts: frozenset[str]
    maximum_bytes: int = 100 * 1024 * 1024
    timeout_seconds: float = 30.0
    allow_private_networks_for_tests: bool = False


@dataclass(frozen=True, slots=True)
class DownloadedPDF:
    path: Path
    sha256: str
    size: int
    page_count: int
    media_type: str
    final_url: str


def _validate_url(value: str, policy: DownloadPolicy) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise DownloadSecurityError("only credential-free issuer HTTPS URLs are allowed")
    if parsed.hostname not in policy.allowed_hosts:
        raise DownloadSecurityError("download host is not allowlisted")


def _validate_public_dns(host: str, policy: DownloadPolicy) -> None:
    if policy.allow_private_networks_for_tests:
        return
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DownloadSecurityError("issuer host DNS resolution failed") from exc
    if not addresses:
        raise DownloadSecurityError("issuer host did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise DownloadSecurityError("issuer host resolved to a non-public address")


def validate_pdf(path: Path, *, expected_hash: str | None = None) -> tuple[str, int, int]:
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        magic = stream.read(5)
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    hexdigest = digest.hexdigest()
    if magic != b"%PDF-":
        raise PDFValidationError("response does not start with the PDF signature")
    if expected_hash and hexdigest != expected_hash:
        raise PDFValidationError("downloaded PDF hash differs from the catalog")
    try:
        with open_pdf(path) as document:
            pages = document.page_count
            if pages <= 0:
                raise PDFValidationError("PDF contains no pages")
            document.validate_all_pages()
    except PDFSecurityError as exc:
        raise PDFValidationError("encrypted PDFs are not accepted") from exc
    except PDFStructureError as exc:
        raise PDFValidationError("PDF structure cannot be opened completely") from exc
    except PDFValidationError:
        raise
    except Exception as exc:
        raise PDFValidationError("PDF structure cannot be opened completely") from exc
    return hexdigest, size, pages


class SecurePDFDownloader:
    def __init__(self, policy: DownloadPolicy) -> None:
        self.policy = policy

    async def download(
        self,
        client: httpx.AsyncClient,
        source: SourceRecord,
        destination: Path,
        *,
        method: str = "GET",
        form: dict[str, str] | None = None,
        expected_hash: str | None = None,
    ) -> DownloadedPDF:
        url = str(source.source_url)
        _validate_url(url, self.policy)
        _validate_public_dns(urlparse(url).hostname or "", self.policy)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                prefix=".partial-",
                suffix=".pdf",
                dir=destination.parent,
                delete=False,
            ) as output:
                temp_path = Path(output.name)
                current_url = url
                current_method = method.upper()
                current_form = form
                final_url: str | None = None
                for redirect_count in range(6):
                    # Validate the destination *before* opening a connection.  Checking
                    # only response.url after automatic redirects is too late for SSRF.
                    _validate_url(current_url, self.policy)
                    _validate_public_dns(urlparse(current_url).hostname or "", self.policy)
                    async with client.stream(
                        current_method,
                        current_url,
                        data=current_form,
                        follow_redirects=False,
                        timeout=self.policy.timeout_seconds,
                    ) as response:
                        if response.is_redirect:
                            if redirect_count >= 5:
                                raise DownloadSecurityError("download exceeded the redirect limit")
                            location = response.headers.get("location")
                            if not location:
                                raise DownloadSecurityError("redirect response has no location")
                            current_url = urljoin(str(response.url), location)
                            # Match ordinary user-agent semantics without forwarding a
                            # POST body after a 301/302/303 redirect.
                            if response.status_code == 303 or (
                                response.status_code in {301, 302} and current_method == "POST"
                            ):
                                current_method = "GET"
                                current_form = None
                            continue
                        response.raise_for_status()
                        content_type = (
                            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        )
                        if content_type and content_type not in {
                            "application/pdf",
                            "application/octet-stream",
                        }:
                            raise PDFValidationError("response MIME type is not PDF-compatible")
                        length = response.headers.get("content-length")
                        try:
                            declared_length = int(length) if length is not None else None
                        except ValueError as exc:
                            raise PDFValidationError("response has an invalid content length") from exc
                        if declared_length is not None and declared_length > self.policy.maximum_bytes:
                            raise PDFValidationError("PDF exceeds configured maximum size")
                        written = 0
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > self.policy.maximum_bytes:
                                raise PDFValidationError("streamed PDF exceeds configured maximum size")
                            output.write(chunk)
                        output.flush()
                        final_url = str(response.url)
                        break
                if final_url is None:
                    raise DownloadSecurityError("download did not reach a final response")
            digest, size, pages = validate_pdf(temp_path, expected_hash=expected_hash)
            # Object path callers are responsible for selecting a content-addressed destination.
            if destination.exists():  # noqa: ASYNC240 - bounded local artifact operation
                existing_digest, _, _ = validate_pdf(destination)
                if existing_digest != digest:
                    raise FileExistsError("immutable destination already contains different bytes")
                temp_path.unlink()
            else:
                temp_path.replace(destination)
            temp_path = None
            return DownloadedPDF(
                path=destination,
                sha256=digest,
                size=size,
                page_count=pages,
                media_type="application/pdf",
                final_url=final_url,
            )
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()
