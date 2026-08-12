"""Process-wide serialized PDF inspection and RGB page rendering.

PDFium is not thread-safe, even when separate documents are used.  Keep every
PDFium call behind one process-global lock and expose only operations that close
their native page, bitmap and image resources deterministically.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any, NoReturn

import pypdfium2 as pdfium  # type: ignore
import pypdfium2.raw as pdfium_c  # type: ignore

PDF_RENDERER_ID = (
    f"pypdfium2-{version('pypdfium2')}+pillow-{version('Pillow')}-rgb-v1"
)


class PDFEngineError(RuntimeError):
    """Base error for a PDF that PDFium cannot safely process."""


class PDFSecurityError(PDFEngineError):
    """The PDF requires a password or uses an unsupported security scheme."""


class PDFStructureError(PDFEngineError):
    """The PDF cannot be opened, traversed or rendered completely."""


_PDFIUM_LOCK = Lock()
_SECURITY_ERROR_CODES = frozenset(
    {pdfium_c.FPDF_ERR_PASSWORD, pdfium_c.FPDF_ERR_SECURITY}
)


def _raise_engine_error(error: Exception) -> NoReturn:
    if getattr(error, "err_code", None) in _SECURITY_ERROR_CODES:
        raise PDFSecurityError(
            "PDF requires a password or uses an unsupported security scheme"
        ) from error
    raise PDFStructureError("PDF structure cannot be processed completely") from error


class PDFDocument:
    """A PDFium document usable only inside :func:`open_pdf`."""

    def __init__(self, document: Any) -> None:
        self._document = document

    @property
    def page_count(self) -> int:
        return int(len(self._document))

    def validate_all_pages(self) -> None:
        """Load every page so broken page trees fail before publication."""
        for index in range(self.page_count):
            page = self._document[index]
            try:
                # Loading the page handle is the validation performed by the
                # former PyMuPDF implementation.  Accessing its size also
                # forces PDFium to resolve the page boxes.
                page.get_size()
            finally:
                page.close()

    def render_page_png(self, page_index: int, *, scale: float) -> bytes:
        """Render a zero-based page as an opaque RGB PNG."""
        page = self._document[page_index]
        bitmap: Any | None = None
        image: Any | None = None
        try:
            bitmap = page.render(
                scale=scale,
                maybe_alpha=False,
                rev_byteorder=True,
            )
            image = bitmap.to_pil()
            if image.mode != "RGB":
                converted = image.convert("RGB")
                original = image
                image = converted
                original.close()
            with BytesIO() as output:
                image.save(output, format="PNG")
                return output.getvalue()
        finally:
            if image is not None:
                image.close()
            if bitmap is not None:
                bitmap.close()
            page.close()


@contextmanager
def open_pdf(path: Path) -> Iterator[PDFDocument]:
    """Open a path while holding PDFium's process-global serialization lock."""
    with _PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as error:
            _raise_engine_error(error)
        try:
            try:
                # PDFium accepts an encrypted document when its user password is
                # empty.  This service deliberately rejects every encrypted PDF,
                # including that otherwise transparent case, so security policy
                # does not depend on whether opening happened to raise error 4/5.
                if pdfium_c.FPDF_GetSecurityHandlerRevision(document.raw) != -1:
                    raise PDFSecurityError(
                        "PDF requires a password or uses an unsupported security scheme"
                    )
                yield PDFDocument(document)
            except pdfium.PdfiumError as error:
                _raise_engine_error(error)
        finally:
            try:
                document.close()
            except pdfium.PdfiumError as error:
                _raise_engine_error(error)
