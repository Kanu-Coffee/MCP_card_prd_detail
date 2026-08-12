from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import cardrag.pdf as pdf_module
from cardrag.pdf import PDF_RENDERER_ID, PDFSecurityError, PDFStructureError, open_pdf
from cardrag.pipeline.ocr import render_pdf
from tests.support_pdf import synthetic_text_pdf_bytes


def test_pdfium_adapter_renders_opaque_rgb_and_records_renderer_contract(tmp_path: Path) -> None:
    source = tmp_path / "two-pages.pdf"
    source.write_bytes(
        synthetic_text_pdf_bytes(
            ("CardRAG fixture page one", "CardRAG fixture page two"),
            width=240,
            height=160,
        )
    )

    with open_pdf(source) as document:
        assert document.page_count == 2
        document.validate_all_pages()
        png = document.render_page_png(0, scale=2)

    with Image.open(BytesIO(png)) as image:
        assert image.mode == "RGB"
        assert image.size == (480, 320)

    rendered = render_pdf(source, tmp_path / "rendered", scale=1)
    contract = json.loads((tmp_path / "rendered/render-input.json").read_text())
    assert len(rendered.page_images) == 2
    assert contract == {
        "schema_version": "cardrag-render.v2",
        "renderer": PDF_RENDERER_ID,
        "pdf_sha256": rendered.pdf_sha256,
        "scale": 1,
    }


@pytest.mark.parametrize(
    "error_code",
    [
        pdf_module.pdfium_c.FPDF_ERR_PASSWORD,
        pdf_module.pdfium_c.FPDF_ERR_SECURITY,
    ],
)
def test_open_pdf_maps_password_and_security_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    def fail_open(_: Path) -> Any:
        raise pdf_module.pdfium.PdfiumError("injected security failure", err_code=error_code)

    monkeypatch.setattr(pdf_module.pdfium, "PdfDocument", fail_open)

    with pytest.raises(PDFSecurityError), open_pdf(Path("unused.pdf")):
        pass


def test_open_pdf_maps_other_pdfium_errors_to_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(_: Path) -> Any:
        raise pdf_module.pdfium.PdfiumError("injected format failure", err_code=3)

    monkeypatch.setattr(pdf_module.pdfium, "PdfDocument", fail_open)

    with pytest.raises(PDFStructureError), open_pdf(Path("unused.pdf")):
        pass


def test_open_pdf_rejects_encryption_with_empty_user_password(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    source = tmp_path / "empty-user-password.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=320, height=320)
    writer.encrypt(
        user_password="",
        owner_password="synthetic-owner",  # noqa: S106 - synthetic test fixture
        algorithm="AES-256",
    )
    with source.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(PDFSecurityError), open_pdf(source):
        pass


def test_render_page_closes_page_bitmap_and_image() -> None:
    closed: list[str] = []
    observed_options: dict[str, object] = {}

    class FakeImage:
        mode = "RGB"

        def save(self, output: BytesIO, *, format: str) -> None:
            assert format == "PNG"
            output.write(b"\x89PNG\r\n\x1a\nfixture")

        def close(self) -> None:
            closed.append("image")

    class FakeBitmap:
        def to_pil(self) -> FakeImage:
            return FakeImage()

        def close(self) -> None:
            closed.append("bitmap")

    class FakePage:
        def render(self, **options: object) -> FakeBitmap:
            observed_options.update(options)
            return FakeBitmap()

        def close(self) -> None:
            closed.append("page")

    class FakeDocument:
        def __getitem__(self, _: int) -> FakePage:
            return FakePage()

    document = pdf_module.PDFDocument(FakeDocument())
    png = document.render_page_png(0, scale=3)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert observed_options == {
        "scale": 3,
        "maybe_alpha": False,
        "rev_byteorder": True,
    }
    assert closed == ["image", "bitmap", "page"]


def test_open_pdf_serializes_all_pdfium_work_in_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_lock = threading.Lock()
    concurrent = 0
    peak = 0

    class FakeDocument:
        raw = object()

        def close(self) -> None:
            return None

    monkeypatch.setattr(pdf_module.pdfium, "PdfDocument", lambda _: FakeDocument())
    monkeypatch.setattr(
        pdf_module.pdfium_c,
        "FPDF_GetSecurityHandlerRevision",
        lambda _: -1,
    )

    def use_document() -> None:
        nonlocal concurrent, peak
        with open_pdf(Path("unused.pdf")):
            with state_lock:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.01)
            with state_lock:
                concurrent -= 1

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: use_document(), range(8)))

    assert peak == 1
