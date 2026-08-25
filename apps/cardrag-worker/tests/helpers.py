from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter


def pdf_bytes(*, pages: int = 1, width: float = 612) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
