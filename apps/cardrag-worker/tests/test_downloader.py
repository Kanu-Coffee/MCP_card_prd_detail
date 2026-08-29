from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from helpers import pdf_bytes

from cardrag_worker.contracts import DownloadRequest
from cardrag_worker.downloader import (
    DownloadPolicy,
    DownloadSecurityError,
    PDFNotModified,
    PDFValidationError,
    ProtectedDocumentError,
    SecurePDFDownloader,
    validate_pdf,
    validate_url,
)


def public_ip(_host: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


@pytest.mark.asyncio
async def test_downloader_follows_allowed_redirect_and_converts_post_to_get(tmp_path: Path) -> None:
    payload = pdf_bytes()
    requests: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), request.content))
        if request.url.path == "/prepare":
            return httpx.Response(302, headers={"location": "/current.pdf"}, request=request)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/pdf",
                "content-length": str(len(payload)),
                "etag": '"revision-7"',
                "last-modified": "Fri, 28 Aug 2026 01:02:03 GMT",
            },
            content=payload,
            request=request,
        )

    policy = DownloadPolicy(allowed_hosts=frozenset({"cards.example"}))
    downloader = SecurePDFDownloader(policy, resolver=public_ip)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await downloader.download(
            client,
            DownloadRequest(url="https://cards.example/prepare", method="POST", form={"id": "7"}),
            tmp_path / "result.pdf",
        )
    assert result.page_count == 1
    assert result.final_url == "https://cards.example/current.pdf"
    assert result.etag == '"revision-7"'
    assert result.last_modified == "Fri, 28 Aug 2026 01:02:03 GMT"
    assert [(method, url) for method, url, _ in requests] == [
        ("POST", "https://cards.example/prepare"),
        ("GET", "https://cards.example/current.pdf"),
    ]
    assert requests[0][2] == b"id=7"
    assert requests[1][2] == b""


@pytest.mark.asyncio
async def test_downloader_surfaces_conditional_not_modified_without_creating_a_file(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"revision-7"'
        return httpx.Response(
            304,
            headers={
                "etag": '"revision-7"',
                "last-modified": "Fri, 28 Aug 2026 01:02:03 GMT",
            },
            request=request,
        )

    destination = tmp_path / "result.pdf"
    downloader = SecurePDFDownloader(
        DownloadPolicy(allowed_hosts=frozenset({"cards.example"})),
        resolver=public_ip,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFNotModified) as captured:
            await downloader.download(
                client,
                DownloadRequest(
                    url="https://cards.example/current.pdf",
                    headers={"If-None-Match": '"revision-7"'},
                ),
                destination,
            )

    assert captured.value.final_url == "https://cards.example/current.pdf"
    assert captured.value.etag == '"revision-7"'
    assert captured.value.last_modified == "Fri, 28 Aug 2026 01:02:03 GMT"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_downloader_rejects_unexpected_unconditional_not_modified(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, request=request)

    downloader = SecurePDFDownloader(
        DownloadPolicy(allowed_hosts=frozenset({"cards.example"})),
        resolver=public_ip,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match="without a conditional request"):
            await downloader.download(
                client,
                DownloadRequest(url="https://cards.example/current.pdf"),
                tmp_path / "result.pdf",
            )


@pytest.mark.asyncio
async def test_downloader_revalidates_redirect_target_before_connecting(tmp_path: Path) -> None:
    contacted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append(request.url.host)
        return httpx.Response(
            302,
            headers={"location": "https://127.0.0.1/secret.pdf"},
            request=request,
        )

    downloader = SecurePDFDownloader(
        DownloadPolicy(allowed_hosts=frozenset({"cards.example"})),
        resolver=public_ip,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadSecurityError, match="allowlisted"):
            await downloader.download(
                client,
                DownloadRequest(url="https://cards.example/start"),
                tmp_path / "result.pdf",
            )
    assert contacted == ["cards.example"]


@pytest.mark.asyncio
async def test_downloader_rejects_service_error_html_without_publishing_a_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=EUC-KR"},
            content=b"<!DOCTYPE html><title>service error</title>",
            request=request,
        )

    destination = tmp_path / "result.pdf"
    downloader = SecurePDFDownloader(
        DownloadPolicy(allowed_hosts=frozenset({"cards.example"})),
        resolver=public_ip,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match="MIME type is not PDF-compatible"):
            await downloader.download(
                client,
                DownloadRequest(url="https://cards.example/download", method="POST"),
                destination,
            )
    assert not destination.exists()


@pytest.mark.asyncio
async def test_downloader_accepts_official_shinhan_mime_typo_after_pdf_validation(
    tmp_path: Path,
) -> None:
    payload = pdf_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octect-stream"},
            content=payload,
            request=request,
        )

    downloader = SecurePDFDownloader(
        DownloadPolicy(allowed_hosts=frozenset({"cards.example"})),
        resolver=public_ip,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await downloader.download(
            client,
            DownloadRequest(url="https://cards.example/download"),
            tmp_path / "result.pdf",
        )
    assert result.page_count == 1


@pytest.mark.asyncio
async def test_downloader_still_rejects_non_pdf_with_official_shinhan_mime_typo(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octect-stream"},
            content=b"not a PDF",
            request=request,
        )

    destination = tmp_path / "result.pdf"
    downloader = SecurePDFDownloader(
        DownloadPolicy(allowed_hosts=frozenset({"cards.example"})),
        resolver=public_ip,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match="PDF signature"):
            await downloader.download(
                client,
                DownloadRequest(url="https://cards.example/download"),
                destination,
            )
    assert not destination.exists()


@pytest.mark.asyncio
async def test_downloader_enforces_stream_cap_and_refuses_symlink(tmp_path: Path) -> None:
    payload = pdf_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, request=request)

    downloader = SecurePDFDownloader(
        DownloadPolicy(allowed_hosts=frozenset({"cards.example"}), maximum_bytes=32),
        resolver=public_ip,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PDFValidationError, match="exceeds"):
            await downloader.download(
                client,
                DownloadRequest(url="https://cards.example/file.pdf"),
                tmp_path / "large.pdf",
            )

    target = tmp_path / "target.pdf"
    target.write_bytes(payload)
    link = tmp_path / "link.pdf"
    link.symlink_to(target)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadSecurityError, match="non-symlink"):
            await SecurePDFDownloader(
                DownloadPolicy(allowed_hosts=frozenset({"cards.example"})), resolver=public_ip
            ).download(client, DownloadRequest(url="https://cards.example/file.pdf"), link)


def test_url_validation_rejects_private_dns_and_credentials() -> None:
    policy = DownloadPolicy(allowed_hosts=frozenset({"cards.example"}))
    with pytest.raises(DownloadSecurityError, match="non-public"):
        validate_url("https://cards.example/a.pdf", policy, resolver=lambda _host: ("10.0.0.4",))
    with pytest.raises(DownloadSecurityError, match="credential-free"):
        validate_url("https://user:pass@cards.example/a.pdf", policy, resolver=public_ip)


@pytest.mark.parametrize(
    ("signature", "magic"),
    [
        (b"SCDSA002", "SCDSA002"),
        (b"SCDSA004", "SCDSA004"),
        (b"\x9b DRMONE", "FASOO_DRMONE"),
    ],
)
def test_pdf_validator_classifies_recognized_protected_containers(
    tmp_path: Path,
    signature: bytes,
    magic: str,
) -> None:
    protected = tmp_path / "protected.pdf"
    protected.write_bytes(signature + b"\x00" * 64)
    with pytest.raises(ProtectedDocumentError, match="protected document") as captured:
        validate_pdf(protected)
    assert captured.value.magic == magic


def test_pdf_validator_requires_exact_fasoo_magic(tmp_path: Path) -> None:
    protected = tmp_path / "near-miss.pdf"
    protected.write_bytes(b"\x9a DRMONE" + b"\x00" * 64)
    with pytest.raises(PDFValidationError, match="PDF signature"):
        validate_pdf(protected)
