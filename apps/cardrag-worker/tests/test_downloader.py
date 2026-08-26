from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from helpers import pdf_bytes

from cardrag_worker.contracts import DownloadRequest
from cardrag_worker.downloader import (
    DownloadPolicy,
    DownloadSecurityError,
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
            headers={"content-type": "application/pdf", "content-length": str(len(payload))},
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
    assert [(method, url) for method, url, _ in requests] == [
        ("POST", "https://cards.example/prepare"),
        ("GET", "https://cards.example/current.pdf"),
    ]
    assert requests[0][2] == b"id=7"
    assert requests[1][2] == b""


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


@pytest.mark.parametrize("signature", [b"SCDSA002", b"SCDSA004"])
def test_pdf_validator_classifies_recognized_protected_containers(tmp_path: Path, signature: bytes) -> None:
    protected = tmp_path / "protected.pdf"
    protected.write_bytes(signature + b"\x00" * 64)
    with pytest.raises(ProtectedDocumentError, match="protected document"):
        validate_pdf(protected)
