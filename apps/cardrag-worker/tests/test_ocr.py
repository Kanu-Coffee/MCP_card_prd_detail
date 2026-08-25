from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from cardrag_core import (
    AdoptedOCRArtifactManifest,
    ArtifactRef,
    LegacyAdoptionReceipt,
    LegacyAdoptionValidation,
    OCRArtifactManifest,
    OCRInput,
    OCRReady,
    adopted_ocr_reuse_key,
    canonical_json_bytes,
    native_ocr_reuse_key,
    object_path,
    sha256_bytes,
    verify_ocr_bytes,
)

from cardrag_worker.ocr import OCRResolver
from cardrag_worker.providers import ProviderError
from cardrag_worker.state import WorkerState

NOW = datetime(2026, 8, 25, tzinfo=UTC)
PDF_SHA = sha256_bytes(b"pdf")
OCR_BODY = "## Page 1\n\n이 페이지에는 카드 혜택 조건과 제외 사항이 자세하게 적혀 있습니다.\n".encode()


class FakeProvider:
    provider = "codex-exec"
    model = "gpt-5.4"
    reasoning_effort = "high"

    def __init__(self, *, fail_calls: set[int] | None = None) -> None:
        self.calls: list[int] = []
        self.fail_calls = fail_calls or set()

    async def recognize(self, images: tuple[Path, ...], *, first_page: int, prompt: str) -> str:
        self.calls.append(first_page)
        if len(self.calls) in self.fail_calls:
            raise ProviderError("temporary failure")
        return (
            "\n\n".join(
                f"## Page {first_page + index}\n\n페이지 {first_page + index} 카드 혜택 조건과 제외 사항 본문입니다."
                for index in range(len(images))
            )
            + "\n"
        )


class FakeWebDAV:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_control = False

    async def get_bytes(self, path: str | PurePosixPath, *, max_bytes: int | None = None) -> bytes | None:
        body = self.objects.get(str(path))
        if body is not None and max_bytes is not None and len(body) > max_bytes:
            raise RuntimeError("cap exceeded")
        return body

    async def put_cas(self, body: bytes, *, media_type: str) -> tuple[str, str]:
        digest = sha256_bytes(body)
        path = object_path(digest).as_posix()
        self.objects[path] = body
        return digest, path

    async def put_json(
        self,
        path: str | PurePosixPath,
        payload: dict[str, Any],
        *,
        immutable: bool,
    ) -> bytes:
        if self.fail_control:
            raise RuntimeError("immutable conflict")
        body = canonical_json_bytes(payload)
        key = str(path)
        existing = self.objects.get(key)
        if existing is not None and existing != body:
            raise RuntimeError("immutable conflict")
        self.objects[key] = body
        return body


def fake_render(pdf_path: Path, output_dir: Path, *, scale: float) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    page_count = int(pdf_path.read_text(encoding="utf-8"))
    paths = []
    for page in range(1, page_count + 1):
        path = output_dir / f"page-{page:04d}.png"
        path.write_bytes(f"image-{page}-scale-{scale}".encode())
        paths.append(path)
    return tuple(paths)


def make_resolver(
    tmp_path: Path, provider: FakeProvider, webdav: FakeWebDAV | None
) -> tuple[OCRResolver, WorkerState]:
    state = WorkerState(tmp_path / "state.sqlite3")
    return OCRResolver(provider=provider, state=state, webdav=webdav, chunk_pages=1), state  # type: ignore[arg-type]


def cache_native(resolver: OCRResolver, webdav: FakeWebDAV) -> str:
    source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1)
    verified = verify_ocr_bytes(OCR_BODY, expected_page_count=1)
    key = native_ocr_reuse_key(resolver.contract, source)
    artifact = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    manifest = OCRArtifactManifest(
        reuse_key=key,
        source=source,
        contract=resolver.contract,
        output=artifact,
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=NOW,
    )
    ready = OCRReady(
        reuse_key=key,
        manifest_sha256=sha256_bytes(manifest.canonical_bytes()),
        ocr_sha256=verified.sha256,
    )
    webdav.objects[artifact.path] = OCR_BODY
    webdav.objects[f"v1/ocr-cache/native/{key[:2]}/{key}/manifest.json"] = manifest.canonical_bytes()
    webdav.objects[f"v1/ocr-cache/native/{key[:2]}/{key}/READY.json"] = ready.canonical_bytes()
    return key


@pytest.mark.asyncio
async def test_native_cache_hit_skips_provider_and_records_exact_reference(tmp_path: Path) -> None:
    provider = FakeProvider()
    webdav = FakeWebDAV()
    resolver, state = make_resolver(tmp_path, provider, webdav)
    try:
        key = cache_native(resolver, webdav)
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        result = await resolver.resolve(
            run_id="run",
            document_id="doc_native",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "ocr",
        )
        assert provider.calls == []
        assert (result.cache_kind, result.cache_reuse_key) == ("native", key)
    finally:
        state.close()


@pytest.mark.asyncio
async def test_adopted_cache_hit_uses_future_v1_document_identity(tmp_path: Path) -> None:
    provider = FakeProvider()
    webdav = FakeWebDAV()
    resolver, state = make_resolver(tmp_path, provider, webdav)
    try:
        document_id = "doc_" + "d" * 64
        source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1)
        verified = verify_ocr_bytes(OCR_BODY, expected_page_count=1)
        key = adopted_ocr_reuse_key(
            adoption_policy_version=resolver.adoption_policy_version,
            source_document_id=document_id,
            pdf_sha256=PDF_SHA,
        )
        artifact = ArtifactRef.for_cas(
            sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            media_type="text/markdown; charset=utf-8",
        )
        receipt = LegacyAdoptionReceipt(
            adoption_policy_version=resolver.adoption_policy_version,
            source_bundle_id="bundle-abc123",
            source_bundle_sha256="b" * 64,
            source_database_id="legacy-db",
            source_document_id=document_id,
            pdf_sha256=PDF_SHA,
            ocr_sha256=verified.sha256,
            validation=LegacyAdoptionValidation(
                hash_verified=True,
                page_coverage_verified=True,
                utf8_verified=True,
                ledger_bound=True,
            ),
        )
        manifest = AdoptedOCRArtifactManifest(
            reuse_key=key,
            source=source,
            receipt=receipt,
            output=artifact,
            ocr_chars=verified.char_count,
            page_output_sha256=verified.page_sha256,
            created_at=NOW,
        )
        ready = OCRReady(
            reuse_key=key,
            manifest_sha256=sha256_bytes(manifest.canonical_bytes()),
            ocr_sha256=verified.sha256,
        )
        root = f"v1/ocr-cache/adopted/{key[:2]}/{key}"
        webdav.objects[artifact.path] = OCR_BODY
        webdav.objects[root + "/manifest.json"] = manifest.canonical_bytes()
        webdav.objects[root + "/READY.json"] = ready.canonical_bytes()
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        result = await resolver.resolve(
            run_id="run",
            document_id=document_id,
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "ocr",
        )
        assert provider.calls == []
        assert (result.cache_kind, result.cache_reuse_key) == ("adopted", key)
    finally:
        state.close()


@pytest.mark.asyncio
async def test_corrupt_native_control_is_cache_miss_and_local_seal_avoids_repeat_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    provider = FakeProvider()
    webdav = FakeWebDAV()
    resolver, state = make_resolver(tmp_path, provider, webdav)
    try:
        state.start_run(run_id="run")
        source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1)
        key = native_ocr_reuse_key(resolver.contract, source)
        webdav.objects[f"v1/ocr-cache/native/{key[:2]}/{key}/READY.json"] = b"{}"
        webdav.fail_control = True
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        arguments = dict(
            run_id="run",
            document_id="doc_native",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "ocr",
        )
        with pytest.warns(RuntimeWarning) as first_warnings:
            first = await resolver.resolve(**arguments)
        assert any("invalid native OCR cache entry" in str(item.message) for item in first_warnings)
        assert any("publishing generation-only OCR" in str(item.message) for item in first_warnings)
        assert provider.calls == [1]
        assert (first.cache_kind, first.cache_reuse_key) == (None, None)
        assert (tmp_path / "ocr" / "native-manifest.json").is_file()
        with pytest.warns(RuntimeWarning) as resumed_warnings:
            resumed = await resolver.resolve(**arguments)
        assert any("invalid native OCR cache entry" in str(item.message) for item in resumed_warnings)
        assert any("publishing generation-only OCR" in str(item.message) for item in resumed_warnings)
        assert provider.calls == [1]
        assert (resumed.cache_kind, resumed.cache_reuse_key) == (None, None)
    finally:
        state.close()


@pytest.mark.asyncio
async def test_chunk_checkpoint_resumes_after_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    provider = FakeProvider(fail_calls={2})
    resolver, state = make_resolver(tmp_path, provider, None)
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("2", encoding="utf-8")
        arguments = dict(
            run_id=run_id,
            document_id="doc_resume",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=2,
            output_dir=tmp_path / "ocr",
        )
        with pytest.raises(ProviderError):
            await resolver.resolve(**arguments)
        result = await resolver.resolve(**arguments)
        assert provider.calls == [1, 2, 2]
        assert len(result.pages) == 2
        assert result.cache_kind is None
    finally:
        state.close()
