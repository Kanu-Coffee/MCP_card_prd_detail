"""Project-authored PDF fixtures without a production PDF-writer dependency.

The application only needs to read and rasterize issuer PDFs.  Tests therefore
build the small subset of PDF syntax they need directly, and reserve ``pypdf``
for the one case that genuinely needs a writer feature: encryption.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from pathlib import Path

import pypdfium2 as pdfium


def _pdf_string(value: str) -> bytes:
    """Encode fixture text as a safely escaped ASCII PDF literal string."""

    encoded = value.encode("ascii", errors="replace")
    return encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _stream(dictionary: bytes, body: bytes) -> bytes:
    separator = b" " if dictionary else b""
    return (
        b"<<"
        + separator
        + dictionary
        + b" /Length "
        + str(len(body)).encode()
        + b">>\nstream\n"
        + body
        + b"\nendstream"
    )


def _serialize(objects: Sequence[bytes], *, version: str = "1.4") -> bytes:
    output = bytearray(f"%PDF-{version}\n".encode())
    output.extend(b"%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def synthetic_text_pdf_bytes(
    page_texts: Sequence[str],
    *,
    width: int = 320,
    height: int = 320,
) -> bytes:
    """Return a valid PDF whose pages contain extractable fixture text."""

    if not page_texts:
        raise ValueError("at least one fixture page is required")
    font_object = 3
    objects: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    page_objects: list[int] = []
    for text in page_texts:
        page_number = len(objects) + 1
        content_number = page_number + 1
        page_objects.append(page_number)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] ".encode()
            + f"/Resources << /Font << /F1 {font_object} 0 R >> >> ".encode()
            + f"/Contents {content_number} 0 R >>".encode()
        )
        content = b"BT /F1 12 Tf 24 " + str(height - 48).encode() + b" Td (" + _pdf_string(text) + b") Tj ET"
        objects.append(_stream(b"", content))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(f"{number} 0 R".encode() for number in page_objects)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_objects)).encode() + b" >>"
    return _serialize(objects)


def synthetic_image_pdf_bytes(
    *,
    page_count: int = 1,
    width: int = 320,
    height: int = 320,
) -> bytes:
    """Return a PDF with raster-only pages and no extractable text objects."""

    if page_count < 1:
        raise ValueError("at least one fixture page is required")
    image_width = 32
    image_height = 32
    objects: list[bytes] = [b"", b""]
    page_objects: list[int] = []
    for page_index in range(page_count):
        page_number = len(objects) + 1
        content_number = page_number + 1
        image_number = page_number + 2
        page_objects.append(page_number)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] ".encode()
            + f"/Resources << /XObject << /Im1 {image_number} 0 R >> >> ".encode()
            + f"/Contents {content_number} 0 R >>".encode()
        )
        content = f"q {width} 0 0 {height} 0 0 cm /Im1 Do Q".encode()
        objects.append(_stream(b"", content))
        pixels = bytearray()
        for row in range(image_height):
            for column in range(image_width):
                pixels.extend(
                    (
                        (column * 8 + page_index * 29) % 256,
                        (row * 8 + page_index * 53) % 256,
                        ((column + row) * 4 + 64) % 256,
                    )
                )
        image_dictionary = (
            f"/Type /XObject /Subtype /Image /Width {image_width} /Height {image_height} ".encode()
            + b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode"
        )
        objects.append(_stream(image_dictionary, zlib.compress(bytes(pixels))))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(f"{number} 0 R".encode() for number in page_objects)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(page_count).encode() + b" >>"
    return _serialize(objects)


def write_synthetic_pdf(
    path: Path,
    page_texts: Sequence[str],
    *,
    image_only: bool = False,
    width: int = 320,
    height: int = 320,
) -> None:
    body = (
        synthetic_image_pdf_bytes(
            page_count=len(page_texts),
            width=width,
            height=height,
        )
        if image_only
        else synthetic_text_pdf_bytes(page_texts, width=width, height=height)
    )
    path.write_bytes(body)


def pdf_page_count(path: Path) -> int:
    document = pdfium.PdfDocument(path)
    try:
        return len(document)
    finally:
        document.close()


def pdf_page_text(path: Path, page_index: int = 0) -> str:
    document = pdfium.PdfDocument(path)
    try:
        page = document[page_index]
        try:
            text_page = page.get_textpage()
            try:
                return text_page.get_text_range()
            finally:
                text_page.close()
        finally:
            page.close()
    finally:
        document.close()


def write_encrypted_pdf(path: Path) -> None:
    """Create a password-protected PDF through the dev-only ``pypdf`` writer."""

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=320, height=320)
    writer.encrypt(
        user_password="synthetic-user",  # noqa: S106 - project-authored encrypted fixture
        owner_password="synthetic-owner",  # noqa: S106 - project-authored encrypted fixture
        algorithm="AES-256",
    )
    with path.open("wb") as stream:
        writer.write(stream)
