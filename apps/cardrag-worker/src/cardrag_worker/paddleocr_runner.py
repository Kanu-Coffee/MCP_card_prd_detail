"""Isolated CPU runner for the PaddleOCR-VL full document pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\([^\n)]*\)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_PADDLE_INPUT_SUFFIXES = frozenset(
    {
        ".bmp",
        ".dib",
        ".jpeg",
        ".jpg",
        ".pdf",
        ".pbm",
        ".pgm",
        ".png",
        ".pnm",
        ".ppm",
        ".ras",
        ".sr",
        ".tif",
        ".tiff",
        ".webp",
    }
)


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _markdown_text(result: Any) -> str:
    value = result.markdown
    if isinstance(value, dict) and isinstance(value.get("res"), dict):
        value = value["res"]
    if not isinstance(value, dict):
        raise ValueError("PaddleOCR markdown result is not a mapping")
    text = value.get("markdown_texts")
    if isinstance(text, list):
        if any(not isinstance(item, str) for item in text):
            raise ValueError("PaddleOCR markdown page contains a non-string part")
        text = "\n\n".join(text)
    if not isinstance(text, str):
        raise ValueError("PaddleOCR markdown result has no text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # The CardRAG OCR artifact is intentionally text-only. Preserve image alt
    # text but never emit links to temporary PaddleX image assets.
    text = _MARKDOWN_IMAGE.sub(lambda match: match.group(1), text)
    text = _HTML_IMAGE.sub("", text)
    return text.strip()


@contextmanager
def _paddle_input_path(input_path: Path, *, output_path: Path) -> Iterator[Path]:
    """Give extensionless CAS objects a private suffix PaddleX can classify."""

    if input_path.suffix.casefold() in _PADDLE_INPUT_SUFFIXES:
        yield input_path
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    link = output_path.parent / f".paddle-input-{os.getpid()}.pdf"
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(input_path.resolve(strict=True))
        yield link
    finally:
        link.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--pipeline-version", choices=("v1", "v1.5", "v1.6"), default="v1.6")
    parser.add_argument("--cpu-threads", type=int, default=8)
    parser.add_argument("--prefetch", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.cpu_threads <= 64:
        return 70
    if not args.prefetch and (
        args.input is None
        or args.expected_pages is None
        or not 1 <= args.expected_pages <= 100
        or not args.input.is_file()
    ):
        return 65
    try:
        from paddleocr import PaddleOCRVL  # type: ignore[import-not-found]

        pipeline = PaddleOCRVL(
            pipeline_version=args.pipeline_version,
            device="cpu",
            cpu_threads=args.cpu_threads,
            use_doc_orientation_classify=True,
            use_doc_unwarping=False,
            use_layout_detection=True,
            use_chart_recognition=False,
            use_seal_recognition=False,
            use_ocr_for_image_block=True,
            format_block_content=True,
            merge_layout_blocks=True,
            markdown_ignore_labels=[],
        )
    except Exception:
        print("paddleocr_runner_configuration_failed", file=sys.stderr)
        return 70
    if args.prefetch:
        _atomic_write(
            args.output,
            (json.dumps({"pipeline_version": args.pipeline_version}, sort_keys=True) + "\n").encode(),
        )
        return 0
    try:
        assert args.input is not None
        with _paddle_input_path(args.input, output_path=args.output) as input_path:
            pages = pipeline.predict(
                input=str(input_path),
                use_layout_detection=True,
                use_ocr_for_image_block=True,
                format_block_content=True,
                merge_layout_blocks=True,
                markdown_ignore_labels=[],
            )
            if len(pages) != args.expected_pages:
                raise ValueError("PaddleOCR page count differs")
            restructured = pipeline.restructure_pages(
                pages,
                merge_tables=True,
                relevel_titles=True,
                concatenate_pages=False,
            )
            if len(restructured) != args.expected_pages:
                raise ValueError("PaddleOCR restructured page count differs")
            texts = [_markdown_text(page) for page in restructured]
            _atomic_write(
                args.output,
                (json.dumps({"pages": texts}, ensure_ascii=False, sort_keys=True) + "\n").encode(),
            )
    except Exception:
        print("paddleocr_runner_document_failed", file=sys.stderr)
        return 65
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
