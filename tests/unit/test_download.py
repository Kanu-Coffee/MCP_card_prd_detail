from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from cardrag.acquisition.download import (
    DownloadPolicy,
    DownloadSecurityError,
    PDFValidationError,
    SecurePDFDownloader,
)
from cardrag.domain import Issuer
from cardrag.issuers.base import SourceRecord
from tests.support_pdf import synthetic_text_pdf_bytes, write_encrypted_pdf


@pytest.fixture
def tiny_pdf_bytes() -> bytes:
    """Create a one-page, project-authored PDF without external fixture content."""

    return synthetic_text_pdf_bytes(
        ["CardRAG synthetic fixture 2026"],
        width=240,
        height=160,
    )


def _source(url: str = "https://issuer.test/fixture.pdf") -> SourceRecord:
    return SourceRecord(
        issuer=Issuer.WOORI,
        product_code="FIXTURE-001",
        product_name="Synthetic fixture card",
        effective_date=date(2026, 8, 12),
        source_version="1",
        source_url=url,
        source_post_id="fixture-001",
        file_name="fixture.pdf",
        category="personal",
        is_current=True,
        discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
    )


def _policy(*, maximum_bytes: int = 1024 * 1024) -> DownloadPolicy:
    return DownloadPolicy(
        allowed_hosts=frozenset({"issuer.test"}),
        maximum_bytes=maximum_bytes,
        allow_private_networks_for_tests=True,
    )


def _assert_no_partial_files(directory: Path) -> None:
    assert not list(directory.glob(".partial-*.pdf"))


@pytest.mark.asyncio
async def test_download_streams_and_validates_a_generated_pdf(
    tmp_path: Path,
    tiny_pdf_bytes: bytes,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=tiny_pdf_bytes)

    destination = tmp_path / "objects" / "fixture.pdf"
    downloader = SecurePDFDownloader(_policy())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await downloader.download(
            client,
            _source(),
            destination,
            expected_hash=hashlib.sha256(tiny_pdf_bytes).hexdigest(),
        )

    assert result.path == destination
    assert result.sha256 == hashlib.sha256(tiny_pdf_bytes).hexdigest()
    assert result.size == len(tiny_pdf_bytes)
    assert result.page_count == 1
    assert destination.read_bytes() == tiny_pdf_bytes
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_malicious_redirect_is_rejected_and_temporary_file_is_removed(
    tmp_path: Path,
    tiny_pdf_bytes: bytes,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "issuer.test":
            return httpx.Response(302, headers={"location": "https://evil.test/stolen.pdf"})
        return httpx.Response(200, content=tiny_pdf_bytes)

    destination = tmp_path / "redirect" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadSecurityError, match="allowlisted"):
            await SecurePDFDownloader(_policy()).download(client, _source(), destination)

    assert not destination.exists()
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_redirect_destination_is_rejected_before_it_is_contacted(
    tmp_path: Path,
    tiny_pdf_bytes: bytes,
) -> None:
    contacted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append(str(request.url))
        if request.url.host == "issuer.test":
            return httpx.Response(302, headers={"location": "https://evil.test/stolen.pdf"})
        return httpx.Response(200, content=tiny_pdf_bytes)

    destination = tmp_path / "preflight-redirect" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadSecurityError, match="allowlisted"):
            await SecurePDFDownloader(_policy()).download(client, _source(), destination)

    assert contacted == ["https://issuer.test/fixture.pdf"]
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_html_mime_is_rejected_even_if_the_body_starts_with_pdf_magic(
    tmp_path: Path,
    tiny_pdf_bytes: bytes,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=tiny_pdf_bytes)

    destination = tmp_path / "mime" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match="MIME"):
            await SecurePDFDownloader(_policy()).download(client, _source(), destination)

    assert not destination.exists()
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"<html><body>synthetic error</body></html>", "PDF signature"),
        (b"%PDF-this-is-not-a-valid-document", "cannot be opened"),
    ],
)
async def test_html_and_corrupt_pdf_are_rejected_with_cleanup(
    tmp_path: Path,
    payload: bytes,
    match: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    destination = tmp_path / "invalid" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match=match):
            await SecurePDFDownloader(_policy()).download(client, _source(), destination)

    assert not destination.exists()
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_content_length_oversize_is_rejected_before_publish(
    tmp_path: Path,
    tiny_pdf_bytes: bytes,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(len(tiny_pdf_bytes))},
            content=tiny_pdf_bytes,
        )

    destination = tmp_path / "oversize" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match="maximum size"):
            await SecurePDFDownloader(_policy(maximum_bytes=len(tiny_pdf_bytes) - 1)).download(
                client,
                _source(),
                destination,
            )

    assert not destination.exists()
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_hash_mismatch_does_not_publish_or_leave_partial_file(
    tmp_path: Path,
    tiny_pdf_bytes: bytes,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tiny_pdf_bytes)

    destination = tmp_path / "hash-mismatch" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match="hash differs"):
            await SecurePDFDownloader(_policy()).download(
                client,
                _source(),
                destination,
                expected_hash="0" * 64,
            )

    assert not destination.exists()
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_existing_different_immutable_destination_is_not_replaced(
    tmp_path: Path,
    tiny_pdf_bytes: bytes,
) -> None:
    existing = synthetic_text_pdf_bytes(["different immutable fixture"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tiny_pdf_bytes)

    destination = tmp_path / "existing" / "fixture.pdf"
    destination.parent.mkdir()
    destination.write_bytes(existing)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FileExistsError, match="different bytes"):
            await SecurePDFDownloader(_policy()).download(client, _source(), destination)

    assert destination.read_bytes() == existing
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_chunked_oversize_without_content_length_is_rejected(
    tmp_path: Path,
    tiny_pdf_bytes: bytes,
) -> None:
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            midpoint = len(tiny_pdf_bytes) // 2
            yield tiny_pdf_bytes[:midpoint]
            yield tiny_pdf_bytes[midpoint:]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, stream=Chunks())

    destination = tmp_path / "chunked-oversize" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match="streamed PDF exceeds"):
            await SecurePDFDownloader(_policy(maximum_bytes=len(tiny_pdf_bytes) - 1)).download(
                client,
                _source(),
                destination,
            )

    assert not destination.exists()
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_transport_timeout_removes_partial_file(tmp_path: Path) -> None:
    class TimeoutStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"%PDF-"
            raise httpx.ReadTimeout("synthetic timeout")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=TimeoutStream())

    destination = tmp_path / "timeout" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await SecurePDFDownloader(_policy()).download(client, _source(), destination)

    assert not destination.exists()
    _assert_no_partial_files(destination.parent)


@pytest.mark.asyncio
async def test_cancellation_removes_partial_file(tmp_path: Path) -> None:
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"%PDF-"
            started.set()
            await release.wait()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BlockingStream())

    destination = tmp_path / "cancelled" / "fixture.pdf"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(
            SecurePDFDownloader(_policy()).download(client, _source(), destination)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not destination.exists()
    _assert_no_partial_files(destination.parent)


def test_encrypted_pdf_is_rejected(tmp_path: Path) -> None:
    from cardrag.acquisition.download import validate_pdf

    path = tmp_path / "encrypted.pdf"
    write_encrypted_pdf(path)

    with pytest.raises(PDFValidationError, match="encrypted"):
        validate_pdf(path)
