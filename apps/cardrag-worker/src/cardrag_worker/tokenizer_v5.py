"""Pinned, byte-verified Qwen tokenizer used by the v5 no-truncation gate."""

from __future__ import annotations

import hashlib
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
from tokenizers import Tokenizer

from .async_utils import to_thread_fenced

QWEN_TOKENIZER_REVISION: Final = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
QWEN_TOKENIZER_SHA256: Final = "83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d"
QWEN_TOKENIZER_SIZE_BYTES: Final = 11_422_947
QWEN_TOKENIZER_URL: Final = (
    f"https://huggingface.co/Qwen/Qwen3-Embedding-8B/resolve/{QWEN_TOKENIZER_REVISION}/tokenizer.json"
)


class QwenTokenizerError(RuntimeError):
    """The pinned tokenizer asset is unavailable or differs from its seal."""


def _verify_asset(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise QwenTokenizerError("Qwen tokenizer asset is not a regular file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
            if size > QWEN_TOKENIZER_SIZE_BYTES:
                raise QwenTokenizerError("Qwen tokenizer asset exceeds its sealed size")
    if size != QWEN_TOKENIZER_SIZE_BYTES or digest.hexdigest() != QWEN_TOKENIZER_SHA256:
        raise QwenTokenizerError("Qwen tokenizer asset differs from its pinned SHA-256/size")


def _write_verified_asset(path: Path, body: bytes) -> None:
    if len(body) != QWEN_TOKENIZER_SIZE_BYTES:
        raise QwenTokenizerError("downloaded Qwen tokenizer has the wrong size")
    if hashlib.sha256(body).hexdigest() != QWEN_TOKENIZER_SHA256:
        raise QwenTokenizerError("downloaded Qwen tokenizer has the wrong SHA-256")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise QwenTokenizerError("Qwen tokenizer parent cannot be a symlink")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as target:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    _verify_asset(path)


@dataclass(frozen=True, slots=True)
class QwenTokenizerV5:
    """Exact token counter bound to one immutable Qwen tokenizer JSON."""

    tokenizer: Tokenizer
    asset_sha256: str = QWEN_TOKENIZER_SHA256
    revision: str = QWEN_TOKENIZER_REVISION

    def __call__(self, text: str) -> int:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Qwen tokenizer input must be non-empty text")
        count = len(self.tokenizer.encode(text, add_special_tokens=True).ids)
        if isinstance(count, bool) or count < 1 or not math.isfinite(float(count)):
            raise QwenTokenizerError("Qwen tokenizer returned an invalid count")
        return count

    @classmethod
    def from_file(cls, path: Path) -> QwenTokenizerV5:
        _verify_asset(path)
        try:
            tokenizer = Tokenizer.from_file(str(path))
        except Exception:
            raise QwenTokenizerError("Qwen tokenizer JSON could not be loaded") from None
        return cls(tokenizer=tokenizer)


async def ensure_qwen_tokenizer(
    path: Path,
    *,
    timeout_seconds: float = 120,
    transport: httpx.AsyncBaseTransport | None = None,
) -> QwenTokenizerV5:
    """Load a verified local asset, downloading only the pinned revision when absent."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("tokenizer timeout must be positive and finite")
    target = path.absolute()
    if path.exists():
        return await to_thread_fenced(QwenTokenizerV5.from_file, target)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout_seconds,
        transport=transport,
    ) as client:
        response = await client.get(QWEN_TOKENIZER_URL)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        raise QwenTokenizerError("Qwen tokenizer download failed") from None
    body = response.content
    await to_thread_fenced(_write_verified_asset, target, body)
    return await to_thread_fenced(QwenTokenizerV5.from_file, target)


__all__ = [
    "QWEN_TOKENIZER_REVISION",
    "QWEN_TOKENIZER_SHA256",
    "QWEN_TOKENIZER_SIZE_BYTES",
    "QWEN_TOKENIZER_URL",
    "QwenTokenizerError",
    "QwenTokenizerV5",
    "ensure_qwen_tokenizer",
]
