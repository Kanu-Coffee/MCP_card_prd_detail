from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from cardrag_core import (
    AdoptedOCRArtifactManifest,
    ArtifactRef,
    LegacyAdoptionReceipt,
    LegacyAdoptionReceiptV2,
    LegacyAdoptionValidation,
    LegacyAdoptionValidationV2,
    NativeOCRContract,
    OCRArtifactManifest,
    OCRInput,
    OCRReady,
    adopted_ocr_reuse_key,
    canonical_json_bytes,
    canonical_sha256,
    native_ocr_reuse_key,
    object_path,
    sha256_bytes,
    verify_ocr_bytes,
)
from helpers import pdf_bytes

from cardrag_worker.ocr import (
    OCR_OUTPUT_POLICY,
    FailoverOCRResolver,
    OCRResolver,
    OCRValidationError,
    render_pdf,
)
from cardrag_worker.pipeline import _canonical_ocr_body
from cardrag_worker.providers import (
    DEFAULT_OCR_PROMPT,
    OCR_BLANK_PAGE_SENTINEL,
    OCR_SPARSE_PAGE_PREFIX,
    ProviderError,
)
from cardrag_worker.state import WorkerState

NOW = datetime(2026, 8, 25, tzinfo=UTC)
PDF_SHA = sha256_bytes(b"pdf")
OCR_BODY = "## Page 1\n\n이 페이지에는 카드 혜택 조건과 제외 사항이 자세하게 적혀 있습니다.\n".encode()
OCR_BODY_SINGLE_MARKER_NEWLINE = (
    "## Page 1\n이 페이지에는 카드 혜택 조건과 제외 사항이 자세하게 적혀 있습니다.\n\n"
    "## Page 2\n두 번째 페이지는 이전 페이지의 상품 설명을 연속해서 충분히 설명합니다.\n"
).encode()


class FakeProvider:
    provider = "codex-exec"
    model = "gpt-5.6-sol"
    reasoning_effort = "high"

    def __init__(self, *, fail_calls: set[int] | None = None) -> None:
        self.calls: list[int] = []
        self.requests: list[dict[str, object]] = []
        self.fail_calls = fail_calls or set()

    async def recognize(
        self,
        images: tuple[Path, ...],
        *,
        page_numbers: tuple[int, ...],
        target_page_numbers: tuple[int, ...],
        total_pages: int,
        prompt: str,
    ) -> str:
        self.calls.append(target_page_numbers[0])
        self.requests.append(
            {
                "image_names": tuple(path.name for path in images),
                "page_numbers": page_numbers,
                "target_page_numbers": target_page_numbers,
                "total_pages": total_pages,
                "prompt": prompt,
            }
        )
        if len(self.calls) in self.fail_calls:
            raise ProviderError("temporary failure")
        return (
            "\n\n".join(
                f"## Page {page_number}\n\n페이지 {page_number} 카드 혜택 조건과 제외 사항 본문입니다."
                for page_number in target_page_numbers
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


def test_render_pdf_overwrites_artifact_from_an_older_scale(tmp_path: Path) -> None:
    pdf = tmp_path / "blank.pdf"
    pdf.write_bytes(pdf_bytes())
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    page = rendered / "page-0001.png"
    page.write_bytes(b"stale-scale-three-pixels")

    first = render_pdf(pdf, rendered, scale=6.0)
    first_body = first[0].read_bytes()
    assert first_body != b"stale-scale-three-pixels"
    assert first_body.startswith(b"\x89PNG")

    page.write_bytes(b"another-stale-artifact")
    second = render_pdf(pdf, rendered, scale=6.0)
    assert second[0].read_bytes() == first_body


def test_default_prompt_has_one_unambiguous_long_blank_page_sentinel() -> None:
    assert OCR_BLANK_PAGE_SENTINEL in DEFAULT_OCR_PROMPT
    assert len(f"## Page 1\n\n{OCR_BLANK_PAGE_SENTINEL}") >= 20
    assert OCR_SPARSE_PAGE_PREFIX in DEFAULT_OCR_PROMPT
    assert len(f"## Page 1\n\n{OCR_SPARSE_PAGE_PREFIX}\n우리카드") >= 20
    assert sha256_bytes(DEFAULT_OCR_PROMPT.encode()) == (
        "1448d7e530d4f8412102c67cd44dc9c9cdab9e3aa165eddb88b3a980a245b946"
    )


@pytest.mark.asyncio
async def test_blank_and_sparse_page_contract_seals_successfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)

    class SparseProvider(FakeProvider):
        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            del images, page_numbers, target_page_numbers, total_pages, prompt
            self.calls.append(1)
            return (
                f"## Page 1\n\n{OCR_BLANK_PAGE_SENTINEL}\n\n"
                f"## Page 2\n\n{OCR_SPARSE_PAGE_PREFIX}\n\nROVL Mileage\n"
            )

    provider = SparseProvider()
    resolver, state = make_resolver(tmp_path, provider, None)
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("2", encoding="utf-8")
        result = await resolver.resolve(
            run_id=run_id,
            document_id="doc_blank_sparse",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=2,
            output_dir=tmp_path / "ocr",
        )

        assert provider.calls == [1]
        assert result.pages == (
            OCR_BLANK_PAGE_SENTINEL,
            f"{OCR_SPARSE_PAGE_PREFIX}\nROVL Mileage",
        )
        assert verify_ocr_bytes(result.ocr_bytes, expected_page_count=2).sha256 == result.ocr_sha256
        checkpoint = state.checkpoint(run_id, "doc_blank_sparse", "ocr", 0)
        assert checkpoint is not None
        assert Path(checkpoint["artifact_path"]).read_text(encoding="utf-8") == (
            f"## Page 1\n\n{OCR_BLANK_PAGE_SENTINEL}\n\n## Page 2\n\n{OCR_SPARSE_PAGE_PREFIX}\nROVL Mileage\n"
        )
    finally:
        state.close()


@pytest.mark.asyncio
async def test_sparse_page_wrapper_rejects_more_than_twelve_visible_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)

    class DenseWrapperProvider(FakeProvider):
        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            del images, page_numbers, target_page_numbers, total_pages, prompt
            return f"## Page 1\n\n{OCR_SPARSE_PAGE_PREFIX}\n123456\n\n789012\n3\n"

    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(provider=DenseWrapperProvider(), state=state, webdav=None)  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        with pytest.raises(OCRValidationError, match="sparse-page wrapper is invalid"):
            await resolver.resolve(
                run_id=run_id,
                document_id="doc_dense_wrapper",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=1,
                output_dir=tmp_path / "ocr",
            )
        assert state.checkpoint(run_id, "doc_dense_wrapper", "ocr", 0) is None
    finally:
        state.close()


@pytest.mark.asyncio
async def test_sparse_page_wrapper_accepts_multiple_short_visible_lines_and_normalizes_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)

    class MultilineSparseProvider(FakeProvider):
        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            del images, page_numbers, target_page_numbers, total_pages, prompt
            self.calls.append(1)
            return (
                f"## Page 1\n\n{OCR_SPARSE_PAGE_PREFIX}\n\n TANTUM  \n\n < \n\n"
                f"## Page 2\n\n{OCR_SPARSE_PAGE_PREFIX}\n ROVL \n\n Mileage \n"
            )

    provider = MultilineSparseProvider()
    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(provider=provider, state=state, webdav=None)  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("2", encoding="utf-8")
        result = await resolver.resolve(
            run_id=run_id,
            document_id="doc_multiline_sparse",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=2,
            output_dir=tmp_path / "ocr",
        )

        expected_page_1 = f"{OCR_SPARSE_PAGE_PREFIX}\nTANTUM\n<"
        expected_page_2 = f"{OCR_SPARSE_PAGE_PREFIX}\nROVL\nMileage"
        assert provider.calls == [1]
        assert result.pages == (expected_page_1, expected_page_2)
        checkpoint = state.checkpoint(run_id, "doc_multiline_sparse", "ocr", 0)
        assert checkpoint is not None
        assert Path(checkpoint["artifact_path"]).read_text(encoding="utf-8") == (
            f"## Page 1\n\n{expected_page_1}\n\n## Page 2\n\n{expected_page_2}\n"
        )
    finally:
        state.close()


@pytest.mark.asyncio
async def test_sparse_page_wrapper_accepts_twelve_visible_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)

    class TwelveLineSparseProvider(FakeProvider):
        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            del images, page_numbers, target_page_numbers, total_pages, prompt
            return f"## Page 1\n\n{OCR_SPARSE_PAGE_PREFIX}\n" + "\n".join("x" for _ in range(12))

    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(provider=TwelveLineSparseProvider(), state=state, webdav=None)  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        result = await resolver.resolve(
            run_id=run_id,
            document_id="doc_twelve_line_sparse",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "ocr",
        )
        assert result.pages == (f"{OCR_SPARSE_PAGE_PREFIX}\n" + "\n".join("x" for _ in range(12)),)
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        OCR_SPARSE_PAGE_PREFIX,
        f"{OCR_SPARSE_PAGE_PREFIX} trailing\nTANTUM",
        f"ordinary\n{OCR_SPARSE_PAGE_PREFIX}\nTANTUM",
        f"{OCR_SPARSE_PAGE_PREFIX}\n  ## Page 9",
        f"{OCR_SPARSE_PAGE_PREFIX}\n{OCR_BLANK_PAGE_SENTINEL}",
        f"{OCR_SPARSE_PAGE_PREFIX}\n{OCR_SPARSE_PAGE_PREFIX}",
    ],
    ids=[
        "empty",
        "text-on-wrapper-line",
        "wrapper-after-ordinary-body",
        "nested-page-marker",
        "nested-blank-sentinel",
        "nested-sparse-wrapper",
    ],
)
async def test_sparse_page_wrapper_rejects_malformed_or_nested_control_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)

    class MalformedSparseProvider(FakeProvider):
        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            del images, page_numbers, target_page_numbers, total_pages, prompt
            return f"## Page 1\n\n{body}\n"

    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(provider=MalformedSparseProvider(), state=state, webdav=None)  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        with pytest.raises(OCRValidationError):
            await resolver.resolve(
                run_id=run_id,
                document_id="doc_malformed_sparse",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=1,
                output_dir=tmp_path / "ocr",
            )
        assert state.checkpoint(run_id, "doc_malformed_sparse", "ocr", 0) is None
    finally:
        state.close()


def make_resolver(
    tmp_path: Path, provider: FakeProvider, webdav: FakeWebDAV | None
) -> tuple[OCRResolver, WorkerState]:
    state = WorkerState(tmp_path / "state.sqlite3")
    return OCRResolver(provider=provider, state=state, webdav=webdav, chunk_pages=1), state  # type: ignore[arg-type]


def cache_native(
    resolver: OCRResolver,
    webdav: FakeWebDAV,
    *,
    body: bytes = OCR_BODY,
    page_count: int = 1,
    contract: NativeOCRContract | None = None,
) -> str:
    source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=page_count)
    verified = verify_ocr_bytes(body, expected_page_count=page_count)
    selected_contract = contract or resolver.contract
    key = native_ocr_reuse_key(selected_contract, source)
    artifact = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    manifest = OCRArtifactManifest(
        reuse_key=key,
        source=source,
        contract=selected_contract,
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
    webdav.objects[artifact.path] = body
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
        assert result.ocr_bytes == OCR_BODY
        assert result.ocr_text == OCR_BODY.decode("utf-8")
        assert _canonical_ocr_body(result) == OCR_BODY
    finally:
        state.close()


@pytest.mark.asyncio
async def test_native_cache_preserves_single_newline_after_marker_exactly(tmp_path: Path) -> None:
    """Regression for legacy KB OCR such as 01913 with canonical single-newline markers."""

    provider = FakeProvider()
    webdav = FakeWebDAV()
    resolver, state = make_resolver(tmp_path, provider, webdav)
    try:
        key = cache_native(
            resolver,
            webdav,
            body=OCR_BODY_SINGLE_MARKER_NEWLINE,
            page_count=2,
        )
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("2", encoding="utf-8")
        result = await resolver.resolve(
            run_id="run",
            document_id="doc_kb_01913",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=2,
            output_dir=tmp_path / "ocr",
        )

        assert provider.calls == []
        assert (result.cache_kind, result.cache_reuse_key) == ("native", key)
        assert result.ocr_bytes == OCR_BODY_SINGLE_MARKER_NEWLINE
        assert result.ocr_text == OCR_BODY_SINGLE_MARKER_NEWLINE.decode("utf-8")
        assert result.ocr_sha256 == sha256_bytes(OCR_BODY_SINGLE_MARKER_NEWLINE)
        assert _canonical_ocr_body(result) == OCR_BODY_SINGLE_MARKER_NEWLINE
    finally:
        state.close()


@pytest.mark.asyncio
async def test_legacy_gpt54_native_cache_is_an_automatic_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    provider = FakeProvider()
    webdav = FakeWebDAV()
    resolver, state = make_resolver(tmp_path, provider, webdav)
    legacy_contract = NativeOCRContract(
        schema_version="cardrag.ocr-contract.v1",
        processor_version="cardrag-worker/1.0.0",
        prompt_version="cardrag-ocr.ko.v1",
        prompt_sha256=sha256_bytes(b"legacy-prompt"),
        renderer_id="pypdfium2/5.12.1",
        render_scale_milli=3000,
        provider="codex-exec",
        model="gpt-5.4",
        reasoning_effort="high",
        chunk_pages=2,
    )
    try:
        legacy_key = cache_native(resolver, webdav, contract=legacy_contract)
        current_key = native_ocr_reuse_key(
            resolver.contract,
            OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1),
        )
        assert legacy_key != current_key
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        result = await resolver.resolve(
            run_id=run_id,
            document_id="doc_old_native",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "ocr",
        )

        assert provider.calls == [1]
        assert result.reuse_key == current_key
        assert result.cache_reuse_key == current_key
        assert webdav.objects[f"v1/ocr-cache/native/{legacy_key[:2]}/{legacy_key}/READY.json"]
    finally:
        state.close()


@pytest.mark.asyncio
async def test_v1_adopted_cache_remains_a_safe_read_fallback(tmp_path: Path) -> None:
    provider = FakeProvider()
    webdav = FakeWebDAV()
    resolver, state = make_resolver(tmp_path, provider, webdav)
    try:
        document_id = "doc_" + "d" * 64
        source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1)
        verified = verify_ocr_bytes(OCR_BODY, expected_page_count=1)
        key = adopted_ocr_reuse_key(
            adoption_policy_version="cardrag.legacy-ocr-adoption.v1",
            source_document_id=document_id,
            pdf_sha256=PDF_SHA,
        )
        artifact = ArtifactRef.for_cas(
            sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            media_type="text/markdown; charset=utf-8",
        )
        receipt = LegacyAdoptionReceipt(
            adoption_policy_version="cardrag.legacy-ocr-adoption.v1",
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
        assert resolver.adoption_policy_version == "cardrag.legacy-ocr-adoption.v2"
        assert (result.cache_kind, result.cache_reuse_key) == ("adopted", key)
        assert result.ocr_bytes == OCR_BODY
        assert result.ocr_text == OCR_BODY.decode("utf-8")
        assert _canonical_ocr_body(result) == OCR_BODY
    finally:
        state.close()


@pytest.mark.asyncio
async def test_v2_adopted_cache_is_preferred_over_v1_fallback(tmp_path: Path) -> None:
    provider = FakeProvider()
    webdav = FakeWebDAV()
    resolver, state = make_resolver(tmp_path, provider, webdav)
    try:
        document_id = "doc_" + "e" * 64
        source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1)

        v1_verified = verify_ocr_bytes(OCR_BODY, expected_page_count=1)
        v1_key = adopted_ocr_reuse_key(
            adoption_policy_version="cardrag.legacy-ocr-adoption.v1",
            source_document_id=document_id,
            pdf_sha256=PDF_SHA,
        )
        v1_output = ArtifactRef.for_cas(
            sha256=v1_verified.sha256,
            size_bytes=v1_verified.size_bytes,
            media_type="text/markdown; charset=utf-8",
        )
        v1_receipt = LegacyAdoptionReceipt(
            adoption_policy_version="cardrag.legacy-ocr-adoption.v1",
            source_bundle_id="bundle-v1",
            source_bundle_sha256="1" * 64,
            source_database_id="legacy-db-v1",
            source_document_id=document_id,
            pdf_sha256=PDF_SHA,
            ocr_sha256=v1_verified.sha256,
            validation=LegacyAdoptionValidation(
                hash_verified=True,
                page_coverage_verified=True,
                utf8_verified=True,
                ledger_bound=True,
            ),
        )
        v1_manifest = AdoptedOCRArtifactManifest(
            reuse_key=v1_key,
            source=source,
            receipt=v1_receipt,
            output=v1_output,
            ocr_chars=v1_verified.char_count,
            page_output_sha256=v1_verified.page_sha256,
            created_at=NOW,
        )

        v2_body = "## Page 1\n\n현재 v2 이관 결과가 이전 캐시보다 먼저 선택되어야 합니다.\n".encode()
        v2_verified = verify_ocr_bytes(v2_body, expected_page_count=1)
        v2_key = adopted_ocr_reuse_key(
            adoption_policy_version="cardrag.legacy-ocr-adoption.v2",
            source_document_id=document_id,
            pdf_sha256=PDF_SHA,
        )
        v2_output = ArtifactRef.for_cas(
            sha256=v2_verified.sha256,
            size_bytes=v2_verified.size_bytes,
            media_type="text/markdown; charset=utf-8",
        )
        v2_receipt = LegacyAdoptionReceiptV2(
            source_bundle_id="bundle-v2",
            source_bundle_sha256="2" * 64,
            source_database_id="legacy-db-v2",
            source_document_id=document_id,
            pdf_sha256=PDF_SHA,
            source_ocr_sha256=v2_verified.sha256,
            source_ocr_size_bytes=v2_verified.size_bytes,
            normalized_ocr_sha256=v2_verified.sha256,
            normalized_ocr_size_bytes=v2_verified.size_bytes,
            normalization_profile="exact",
            prefix_sha256=None,
            removed_bytes=0,
            validation=LegacyAdoptionValidationV2(
                source_hash_verified=True,
                normalized_hash_verified=True,
                transformation_verified=True,
                page_coverage_verified=True,
                utf8_verified=True,
                ledger_bound=True,
            ),
        )
        v2_manifest = AdoptedOCRArtifactManifest(
            schema_version="cardrag.ocr-artifact.v2",
            validation_profile="cardrag.legacy-ocr-adoption.v2",
            reuse_key=v2_key,
            source=source,
            receipt=v2_receipt,
            output=v2_output,
            ocr_chars=v2_verified.char_count,
            page_output_sha256=v2_verified.page_sha256,
            created_at=NOW,
        )
        for key, manifest, verified, body in (
            (v1_key, v1_manifest, v1_verified, OCR_BODY),
            (v2_key, v2_manifest, v2_verified, v2_body),
        ):
            ready = OCRReady(
                reuse_key=key,
                manifest_sha256=sha256_bytes(manifest.canonical_bytes()),
                ocr_sha256=verified.sha256,
            )
            root = f"v1/ocr-cache/adopted/{key[:2]}/{key}"
            webdav.objects[manifest.output.path] = body
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
        assert result.cache_reuse_key == v2_key
        assert result.ocr_bytes == v2_body
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
        assert resumed.ocr_bytes == first.ocr_bytes
        assert resumed.ocr_text == first.ocr_text
        assert _canonical_ocr_body(resumed) == (tmp_path / "ocr" / "ocr.md").read_bytes()
    finally:
        state.close()


@pytest.mark.asyncio
async def test_three_page_product_uses_one_whole_document_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    provider = FakeProvider()
    resolver, state = make_resolver(tmp_path, provider, None)
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("3", encoding="utf-8")
        result = await resolver.resolve(
            run_id=run_id,
            document_id="doc_three_pages",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=3,
            output_dir=tmp_path / "ocr",
        )

        assert provider.calls == [1]
        assert provider.requests[0]["page_numbers"] == (1, 2, 3)
        assert provider.requests[0]["target_page_numbers"] == (1, 2, 3)
        assert provider.requests[0]["total_pages"] == 3
        assert len(result.pages) == 3
        assert resolver.contract.render_scale_milli == 6000
        assert resolver.contract.processor_version == "cardrag-worker/1.0.4"
        assert resolver.contract.prompt_version == "cardrag-ocr.ko.v2"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_five_page_product_uses_overlapping_visual_context_but_outputs_targets_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    provider = FakeProvider()
    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(provider=provider, state=state, webdav=None, chunk_pages=2)  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("5", encoding="utf-8")
        result = await resolver.resolve(
            run_id=run_id,
            document_id="doc_five_pages",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=5,
            output_dir=tmp_path / "ocr",
        )

        assert [request["page_numbers"] for request in provider.requests] == [
            (1, 2, 3),
            (2, 3, 4, 5),
            (4, 5),
        ]
        assert [request["target_page_numbers"] for request in provider.requests] == [
            (1, 2),
            (3, 4),
            (5,),
        ]
        assert result.pages == tuple(
            f"페이지 {page} 카드 혜택 조건과 제외 사항 본문입니다." for page in range(1, 6)
        )
        assert result.ocr_text.count("## Page ") == 5
    finally:
        state.close()


@pytest.mark.asyncio
async def test_context_page_output_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)

    class ContextLeakingProvider(FakeProvider):
        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            del images, page_numbers, total_pages, prompt
            return "\n\n".join(
                f"## Page {page}\n\n페이지 {page}의 충분히 긴 OCR 본문입니다."
                for page in (*target_page_numbers, 3)
            )

    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(
        provider=ContextLeakingProvider(),
        state=state,
        webdav=None,
        chunk_pages=2,
    )  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("5", encoding="utf-8")
        with pytest.raises(OCRValidationError, match=r"page markers \[1, 2, 3\] do not match \[1, 2\]"):
            await resolver.resolve(
                run_id=run_id,
                document_id="doc_context_leak",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=5,
                output_dir=tmp_path / "ocr",
            )
    finally:
        state.close()


@pytest.mark.parametrize("prefix", ["OCR 결과:\n", "```markdown\n"])
@pytest.mark.asyncio
async def test_provider_preamble_before_first_page_marker_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)

    class PrefixedProvider(FakeProvider):
        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            body = await super().recognize(
                images,
                page_numbers=page_numbers,
                target_page_numbers=target_page_numbers,
                total_pages=total_pages,
                prompt=prompt,
            )
            return prefix + body

    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(provider=PrefixedProvider(), state=state, webdav=None)  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        with pytest.raises(OCRValidationError, match="must begin with the first Page marker"):
            await resolver.resolve(
                run_id=run_id,
                document_id="doc_prefixed_output",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=1,
                output_dir=tmp_path / "ocr",
            )
        assert state.checkpoint(run_id, "doc_prefixed_output", "ocr", 0) is None
        assert not (tmp_path / "ocr" / "ocr.md").exists()
    finally:
        state.close()


@pytest.mark.asyncio
async def test_chunk_checkpoint_resumes_after_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    provider = FakeProvider(fail_calls={2})
    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(
        provider=provider,
        state=state,
        webdav=None,
        chunk_pages=1,
        whole_document_max_pages=1,
    )  # type: ignore[arg-type]
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
        assert result.ocr_bytes == (tmp_path / "ocr" / "ocr.md").read_bytes()
        assert result.ocr_text == result.ocr_bytes.decode("utf-8")
        assert _canonical_ocr_body(result) == result.ocr_bytes
        assert b"## Page 1\n\n" in result.ocr_bytes
        assert b"## Page 2\n\n" in result.ocr_bytes
    finally:
        state.close()


@pytest.mark.asyncio
async def test_implausibly_short_existing_checkpoint_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    provider = FakeProvider()
    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(
        provider=provider,
        state=state,
        webdav=None,
        chunk_pages=1,
        whole_document_max_pages=1,
    )  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        rendered = fake_render(pdf, tmp_path / "ocr" / "rendered", scale=6.0)
        input_sha = canonical_sha256(
            {
                "contract_sha256": resolver.contract.contract_sha256,
                "input_images": [{"page_number": 1, "sha256": sha256_bytes(rendered[0].read_bytes())}],
                "model": provider.model,
                "output_policy": OCR_OUTPUT_POLICY,
                "provider": provider.provider,
                "schema_version": "cardrag.ocr-checkpoint-input.v2",
                "target_page_numbers": (1,),
                "total_pages": 1,
            }
        )
        checkpoint = tmp_path / "poisoned-short-checkpoint.md"
        checkpoint.write_text("## Page 1\n\n빈 페이지\n", encoding="utf-8")
        state.save_checkpoint(
            run_id=run_id,
            document_id="doc_short_checkpoint",
            stage_name="ocr",
            chunk_index=0,
            input_sha256=input_sha,
            output_sha256=sha256_bytes(checkpoint.read_bytes()),
            artifact_path=checkpoint,
        )

        result = await resolver.resolve(
            run_id=run_id,
            document_id="doc_short_checkpoint",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "ocr",
        )

        assert provider.calls == [1]
        assert result.pages == ("페이지 1 카드 혜택 조건과 제외 사항 본문입니다.",)
        replaced = state.checkpoint(run_id, "doc_short_checkpoint", "ocr", 0)
        assert replaced is not None
        assert replaced["output_sha256"] != sha256_bytes("## Page 1\n\n빈 페이지\n".encode())
    finally:
        state.close()


@pytest.mark.asyncio
async def test_checkpoint_input_binds_overlapping_context_image_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = {3: "a"}

    def changing_render(pdf_path: Path, output_dir: Path, *, scale: float) -> tuple[Path, ...]:
        output_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for page in range(1, int(pdf_path.read_text(encoding="utf-8")) + 1):
            path = output_dir / f"page-{page:04d}.png"
            path.write_bytes(f"image-{page}-{revision.get(page, 'stable')}-scale-{scale}".encode())
            paths.append(path)
        return tuple(paths)

    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", changing_render)
    provider = FakeProvider(fail_calls={2})
    state = WorkerState(tmp_path / "state.sqlite3")
    resolver = OCRResolver(provider=provider, state=state, webdav=None, chunk_pages=2)  # type: ignore[arg-type]
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("5", encoding="utf-8")
        arguments = dict(
            run_id=run_id,
            document_id="doc_context_checkpoint",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=5,
            output_dir=tmp_path / "ocr",
        )
        with pytest.raises(ProviderError):
            await resolver.resolve(**arguments)
        assert provider.calls == [1, 3]

        # Page 3 was context-after for target pages 1-2. Changing only that
        # visual context must invalidate and rerun the first checkpoint.
        revision[3] = "b"
        result = await resolver.resolve(**arguments)
        assert provider.calls == [1, 3, 1, 3, 5]
        assert len(result.pages) == 5
    finally:
        state.close()


@pytest.mark.asyncio
async def test_failover_preserves_fallback_verified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    primary_provider = FakeProvider(fail_calls={1})
    fallback_provider = FakeProvider()
    state = WorkerState(tmp_path / "state.sqlite3")
    primary = OCRResolver(provider=primary_provider, state=state, webdav=None, chunk_pages=1)  # type: ignore[arg-type]
    fallback = OCRResolver(provider=fallback_provider, state=state, webdav=None, chunk_pages=1)  # type: ignore[arg-type]
    resolver = FailoverOCRResolver(primary, fallback)
    try:
        run_id = state.start_run(run_id="run")
        pdf = tmp_path / "pdf-pages.txt"
        pdf.write_text("1", encoding="utf-8")
        with pytest.warns(RuntimeWarning, match="primary OCR failed"):
            result = await resolver.resolve(
                run_id=run_id,
                document_id="doc_failover",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=1,
                output_dir=tmp_path / "ocr",
            )

        assert primary_provider.calls == [1]
        assert fallback_provider.calls == [1]
        assert result.ocr_bytes == (tmp_path / "ocr" / "fallback" / "ocr.md").read_bytes()
        assert result.ocr_text == result.ocr_bytes.decode("utf-8")
        assert _canonical_ocr_body(result) == result.ocr_bytes
    finally:
        state.close()
